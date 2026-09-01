"""Temporal context reconstruction: what was known, and when it became known.

Context Mesh already keeps history rather than deleting it — rejected
assumptions, superseded decisions, invalidated artefacts and the evidence paths
that felled them all stay walkable. What it could not do was *stand at a past
moment and look forward from there*. Asking "why did we choose B over A" would
answer with today's graph, in which the evidence that changed our minds is
already present, so the answer silently credits the decision-maker with
knowledge they did not have.

This module supplies the missing primitive: a deterministic reading of when
each node's information became available.

Two clocks, deliberately not merged
-----------------------------------

**Source time** is when the information entered the world we can see. It comes
from the ``source`` node's ``retrieved_at`` attribute — data the corpus
supplies, not something this module invents. Every ``claim``, ``entity``,
``decision`` and ``evidence`` node reaches a source through provenance or a
typed edge, so source time is inherited rather than stored twice.

**Processing time** is when Context Mesh recorded it: the monotonic build
counter already carried by ``Node.build``, ``Provenance.recorded_at_build``,
``Assumption.created_at_build`` and ``DecisionRecord.at_build``.

They are not interchangeable. A source dated January can be ingested in build
40; an assumption has no source date at all, because an assumption is not
lifted from a document — it is *taken as true so work could proceed*, and its
only honest timestamps are the builds in which it was created and rejected.
Collapsing the two would let a late ingest of an old document look like late
knowledge, or an assumption look like a dated observation.

There is no wall clock anywhere in this module. ``datetime.now()`` would make a
reconstruction non-reproducible across runs and would put a value in the graph
that no source vouches for; the build counter exists precisely so that ordering
never depends on when the process happened to run.

Fail closed on time as on everything else
-----------------------------------------

A source whose ``retrieved_at`` is not an ISO-8601 date is ``UNDATED``, not
guessed at and not quietly dropped. ``contextmesh/execute.py`` mints sources
with ``retrieved_at="at plan time"``, so this is a real case, not a defensive
hypothetical. A reconstruction reports what it could not date instead of
pretending the horizon is cleaner than the data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Dict, Optional

from .graph import ContextGraph
from .model import AssumptionStatus, EdgeType, Node, NodeType

#: The source attribute GRAPH.md already requires a ``source`` to carry.
SOURCE_TIME_ATTR = "retrieved_at"

_ISO_DATE = re.compile(r"\A(\d{4})-(\d{2})-(\d{2})\Z")

#: How a non-source node reaches the source whose date it inherits, in order.
#: Provenance first because rule 4 of GRAPH.md makes it the authoritative link
#: for a claim or decision; the typed edges cover nodes that carry none.
_ANCHOR_EDGES = (EdgeType.DERIVED_FROM, EdgeType.CITES)


class TemporalError(ValueError):
    """A temporal input this build will not reason from."""


class Horizon(str, Enum):
    """Where a node sits relative to the moment being reconstructed."""

    #: Datable, and dated on or before the horizon: available at the time.
    THEN = "then"
    #: Datable, and dated after the horizon: hindsight, not contemporary.
    LATER = "later"
    #: No source date resolves. Neither claimed as known nor assumed absent.
    UNDATED = "undated"


def parse_date(value: object, *, where: str = "date") -> date:
    """Read a strict ISO-8601 calendar date. No locale, no clock, no guessing.

    Accepting anything looser would let ``"at plan time"`` or ``"March"`` sort
    against real dates and silently decide what a decision-maker knew.
    """
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TemporalError(f"{where} must be an ISO-8601 date string, got {type(value).__name__}")
    match = _ISO_DATE.fullmatch(value.strip())
    if match is None:
        raise TemporalError(f"{where} must be an ISO-8601 date (YYYY-MM-DD), got {value!r}")
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise TemporalError(f"{where} is not a real calendar date: {value!r} ({exc})") from None


def source_date(node: Node) -> Optional[date]:
    """The date a ``source`` node carries, or None when it carries none."""
    if node.type is not NodeType.SOURCE:
        return None
    try:
        return parse_date(node.attrs.get(SOURCE_TIME_ATTR), where=f"{node.id}.{SOURCE_TIME_ATTR}")
    except TemporalError:
        return None


@dataclass(frozen=True)
class Anchor:
    """Why a node has the date it has — the answer has to be inspectable."""

    node_id: str
    source_id: Optional[str]
    #: "self", "provenance", or the edge type that reached the source.
    via: str
    when: Optional[date]

    @property
    def dated(self) -> bool:
        return self.when is not None


class Timeline:
    """Every node's source time, resolved once against one graph.

    History is the subject, so this reads the graph with ``live_only=False``
    throughout: a node invalidated last month was still known last year, and a
    reconstruction that skipped it would answer the wrong question.
    """

    def __init__(self, graph: ContextGraph) -> None:
        self.graph = graph
        self._dates: Dict[str, Optional[date]] = {
            node.id: source_date(node)
            for node in graph.nodes.values()
            if node.type is NodeType.SOURCE
        }
        self._anchors: Dict[str, Anchor] = {}
        for node in graph.nodes.values():
            self._anchors[node.id] = self._resolve(node)

    def _resolve(self, node: Node) -> Anchor:
        if node.type is NodeType.SOURCE:
            return Anchor(node.id, node.id, "self", self._dates.get(node.id))
        provenance = node.provenance
        if provenance is not None and provenance.source_id in self._dates:
            return Anchor(
                node.id, provenance.source_id, "provenance", self._dates[provenance.source_id]
            )
        for edge_type in _ANCHOR_EDGES:
            for edge in self.graph.out_edges(node.id, [edge_type], live_only=False):
                if edge.dst in self._dates:
                    return Anchor(node.id, edge.dst, edge_type.value, self._dates[edge.dst])
        # An assumption lands here by design rather than by omission: it is not
        # lifted from a document, so it has no source date to inherit. Its
        # lifecycle builds are its honest clock, and callers read those instead.
        return Anchor(node.id, None, "none", None)

    def anchor(self, node_id: str) -> Anchor:
        try:
            return self._anchors[node_id]
        except KeyError:
            raise TemporalError(f"{node_id!r} is not in this graph") from None

    def when(self, node_id: str) -> Optional[date]:
        return self.anchor(node_id).when

    def horizon(self, node_id: str, as_of: date) -> Horizon:
        """Classify one node against a moment. Three outcomes, never two."""
        when = self.anchor(node_id).when
        if when is None:
            return Horizon.UNDATED
        return Horizon.THEN if when <= as_of else Horizon.LATER

    def span(self) -> Optional[tuple]:
        """The dated extent of this graph, or None when nothing is datable."""
        dated = [d for d in self._dates.values() if d is not None]
        return (min(dated), max(dated)) if dated else None


def _source_time_of(graph: ContextGraph, node: Node) -> Optional[date]:
    """One node's source date, resolved the way :class:`Timeline` resolves it."""
    if node.type is NodeType.SOURCE:
        return source_date(node)
    provenance = node.provenance
    if provenance is not None:
        source = graph.nodes.get(provenance.source_id)
        if source is not None:
            return source_date(source)
    for edge_type in _ANCHOR_EDGES:
        for edge in graph.out_edges(node.id, [edge_type], live_only=False):
            source = graph.nodes.get(edge.dst)
            if source is not None and source.type is NodeType.SOURCE:
                return source_date(source)
    return None


def _fell_at(graph: ContextGraph, assumption_id: str) -> Optional[date]:
    """When an assumption fell, on the source clock.

    Rule 7 of GRAPH.md: an assumption is only ever rejected by ``evidence``
    that ``contradicts`` it. So the moment it stopped standing is the moment
    that evidence became available — a source date, comparable to a horizon,
    with no clock crossed. The earliest such witness wins; a second one
    arriving later does not un-fell it.
    """
    fell: Optional[date] = None
    for edge in graph.in_edges(assumption_id, [EdgeType.CONTRADICTS], live_only=False):
        witness = graph.nodes.get(edge.src)
        if witness is None or witness.type is not NodeType.EVIDENCE:
            continue
        when = _source_time_of(graph, witness)
        if when is not None and (fell is None or when < fell):
            fell = when
    return fell


def _standing_assumptions(
    graph: ContextGraph, kept: Dict[str, Node], as_of: date
) -> Dict[str, Node]:
    """Assumptions the surviving work was standing on, still uncrossed clocks.

    An assumption carries no source date of its own — it is not lifted from a
    document — so it cannot be compared to the horizon directly. It gets its
    position from two things the model already supplies, both on the source
    clock:

    it entered with the work that depends on it, which is in ``kept`` only
    because its own source date cleared the horizon; and it fell when the
    evidence contradicting it arrived, which is a source date too.

    The build counter is kept as a second gate rather than the first. It is
    real for work the Runner executes across rounds, and nearly flat for a
    corpus ingested in one build — in the bundled demo a February decision and
    the July evidence that felled its ground both sit at build 1, so build
    comparison alone would drop an assumption out of the very decision it was
    holding up. Source dates carry the history there; builds carry it in
    execution. Neither is asked to do the other's job.
    """
    standing: Dict[str, Node] = {}
    for node in kept.values():
        for edge in graph.out_edges(node.id, [EdgeType.DEPENDS_ON], live_only=False):
            assumption = graph.assumptions.get(edge.dst)
            if assumption is None:
                continue
            if assumption.created_at_build > node.build:
                continue
            fell = _fell_at(graph, edge.dst)
            if fell is not None and fell <= as_of:
                continue
            ground = graph.nodes.get(edge.dst)
            if ground is not None:
                standing[ground.id] = ground
    return standing


def as_of_graph(graph: ContextGraph, as_of: date) -> ContextGraph:
    """The graph as it stood: a real graph, not today's with nodes greyed out.

    A reconstruction has to be walkable by the ordinary walker, scored by the
    ordinary policy and able to dead-end the ordinary way. Filtering a result
    after the fact would still let the walk cross a claim that did not exist
    yet, and the path — which is the answer in this system — would be one
    nobody could have taken. So the projection is built first and walked
    second.

    Nodes survive when their source time is ``THEN``; assumptions survive when
    the surviving work was standing on them. An edge survives only when both
    of its endpoints do, so no edge points out of the past.
    """
    timeline = Timeline(graph)
    kept: Dict[str, Node] = {
        node.id: node
        for node in graph.nodes.values()
        if timeline.horizon(node.id, as_of) is Horizon.THEN
    }
    kept.update(_standing_assumptions(graph, kept, as_of))

    payload = graph.to_dict()
    payload["nodes"] = [n for n in payload["nodes"] if n["id"] in kept]
    payload["edges"] = [
        e for e in payload["edges"] if e["src"] in kept and e["dst"] in kept
    ]
    payload["assumptions"] = [
        _rewind_assumption(a, kept, as_of, graph)
        for a in payload["assumptions"]
        if a["id"] in kept
    ]
    _mirror_status(payload)
    return ContextGraph.from_dict(payload)


def _rewind_assumption(
    record: Dict[str, Any], kept: Dict[str, Node], as_of: date, graph: ContextGraph
) -> Dict[str, Any]:
    """State an assumption as it stood, rather than as it ended up.

    Carrying today's record into a past projection would make the projection
    lie in the one place it most needs to be honest: an assumption rejected in
    July was *active* in February, and a February reconstruction that reports
    it as rejected has quietly imported the finding it exists to exclude.

    So each lifecycle field is wound back to the horizon:

    ``status`` and ``rejected_at_build`` clear unless the contradicting
    evidence had already arrived; ``evidence_ids`` keeps only witnesses that
    had; ``supersedes`` and ``superseded_by`` clear when the other half of the
    relationship is not in the projection, because a successor that did not
    exist yet cannot already have replaced anything.

    That last one is also what ``ContextGraph.from_dict`` demands — it refuses
    a snapshot whose supersession is one-sided or dangling. The projection has
    to be a graph the ordinary loader would accept, not a special case, or it
    is not really the graph as it stood.
    """
    out = dict(record)
    fell = _fell_at(graph, record["id"])
    if fell is None or fell > as_of:
        out["status"] = AssumptionStatus.ACTIVE.value
        out["rejected_at_build"] = None
    out["evidence_ids"] = [
        eid
        for eid in record.get("evidence_ids", [])
        if eid in kept and _witness_arrived(graph, eid, as_of)
    ]
    for field_name in ("supersedes", "superseded_by"):
        other = out.get(field_name)
        if other is not None and other not in kept:
            out[field_name] = None
    return out


def _witness_arrived(graph: ContextGraph, evidence_id: str, as_of: date) -> bool:
    node = graph.nodes.get(evidence_id)
    if node is None:
        return False
    when = _source_time_of(graph, node)
    return when is not None and when <= as_of


def _mirror_status(payload: Dict[str, Any]) -> None:
    """Keep each assumption node's mirrored lifecycle equal to its record.

    ``sync_assumption`` is the single writer of that mirror in a live graph and
    ``from_dict`` refuses a snapshot where the two disagree. Winding a record
    back without winding its node back would trip exactly that check — the
    graph declining to hold two answers to one question, which is the rule
    working rather than an obstacle to route around.
    """
    rewound = {a["id"]: a for a in payload["assumptions"]}
    for node in payload["nodes"]:
        record = rewound.get(node["id"])
        if record is not None:
            node["attrs"] = dict(node["attrs"])
            node["attrs"]["status"] = record["status"]
            node["attrs"]["version"] = record["version"]


__all__ = [
    "Anchor",
    "Horizon",
    "SOURCE_TIME_ATTR",
    "TemporalError",
    "Timeline",
    "as_of_graph",
    "parse_date",
    "source_date",
]
