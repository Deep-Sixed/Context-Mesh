"""Historical reconstruction: stand at a past moment and look forward from there.

``Walker.ask`` answers from the graph as it is now. That is the right answer to
"what do we believe", and the wrong one to "why did we decide this in June",
because June's answer would be assembled out of August's evidence. The
decision-maker gets credited with knowledge they did not have, and the record
quietly stops being a record.

:func:`explain_as_of` answers the second question. It walks the graph *as it
stood* — :func:`contextmesh.temporal.as_of_graph` builds that projection, and
the ordinary walker walks it with the ordinary policy — so the path it returns
is one somebody could actually have taken. Filtering a present-day walk
afterwards would leave a path that steps through claims which did not exist
yet, and in this system the path is the answer.

Four buckets, and none of them collapse
---------------------------------------

``then``      what was legitimately available at the horizon
``decision``  the decisions and the assumptions they were standing on
``later``     evidence, contradictions and supersessions that arrived after
``undated``   material that cannot honestly be placed on the source-time axis

``undated`` is not a rounding error to be tidied into one of its neighbours. A
source whose ``retrieved_at`` will not parse is uncertainty the reconstruction
observed, and hiding it would make the horizon look sharper than the data
supports.

Both clocks travel with every item
----------------------------------

Each :class:`Placed` carries the ``Anchor`` that put it where it is — which
source, reached by which edge, on what date — alongside the build in which
Context Mesh recorded it. A claim can be contemporary by source time while its
provenance was recorded much later; an assumption can have been standing at the
horizon while having no source date at all. One timestamp column would erase
the difference, so there isn't one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from .graph import ContextGraph
from .model import AssumptionStatus, EdgeType, Node, NodeType
from .resolve import Resolver
from .temporal import Anchor, Horizon, Timeline, as_of_graph, parse_date
from .traverse import DEFAULT_POLICY, Walk, Walker
from .traverse import _question_mentions as question_mentions

#: Edges along which later material bears on what was decided earlier.
_HINDSIGHT_EDGES = (EdgeType.CONTRADICTS, EdgeType.SUPERSEDES, EdgeType.SUPPORTS)


@dataclass(frozen=True)
class Placed:
    """One node, placed against the horizon, with the reason it landed there."""

    node_id: str
    type: str
    label: str
    horizon: Horizon
    anchor: Anchor
    at_build: int
    #: Assumption lifecycle, when the node is one. The processing clock is all
    #: an assumption has, so it travels with it rather than being looked up.
    status: Optional[str] = None
    version: Optional[int] = None
    created_at_build: Optional[int] = None
    rejected_at_build: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "type": self.type,
            "label": self.label,
            "horizon": self.horizon.value,
            "source_time": self.anchor.when.isoformat() if self.anchor.when else None,
            "anchored_via": self.anchor.via,
            "anchored_on": self.anchor.source_id,
            "at_build": self.at_build,
            "status": self.status,
            "version": self.version,
            "created_at_build": self.created_at_build,
            "rejected_at_build": self.rejected_at_build,
        }


@dataclass(frozen=True)
class Reconstruction:
    """What was known then, what was decided, and what arrived afterwards."""

    question: str
    as_of: date
    walk: Optional[Walk]
    then: List[Placed] = field(default_factory=list)
    decision: List[Placed] = field(default_factory=list)
    later: List[Placed] = field(default_factory=list)
    undated: List[Placed] = field(default_factory=list)
    #: Entities the question names that the graph holds *now* but did not hold
    #: at the horizon. The walk dead-ends either way, and the walker is right
    #: to call that ``entity_unresolved`` — at its level the entity is simply
    #: not there. One level up the difference matters: "there is no such thing"
    #: and "that was not known yet" are opposite answers to "why did we decide
    #: this in June", and a reconstruction that could not tell them apart would
    #: report a gap in the corpus where the honest answer is a gap in time.
    not_yet_known: List[Placed] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "as_of": self.as_of.isoformat(),
            "walk": self.walk.to_dict() if self.walk else None,
            "then": [p.to_dict() for p in self.then],
            "decision": [p.to_dict() for p in self.decision],
            "later": [p.to_dict() for p in self.later],
            "undated": [p.to_dict() for p in self.undated],
            "not_yet_known": [p.to_dict() for p in self.not_yet_known],
        }


def _place(node: Node, graph: ContextGraph, timeline: Timeline, as_of: date) -> Placed:
    assumption = graph.assumptions.get(node.id)
    return Placed(
        node_id=node.id,
        type=node.type.value,
        label=node.label,
        horizon=timeline.horizon(node.id, as_of),
        anchor=timeline.anchor(node.id),
        at_build=node.build,
        status=assumption.status.value if assumption else None,
        version=assumption.version if assumption else None,
        created_at_build=assumption.created_at_build if assumption else None,
        rejected_at_build=assumption.rejected_at_build if assumption else None,
    )


def explain_as_of(
    graph: ContextGraph,
    resolver: Resolver,
    question: str,
    as_of: Any,
    *,
    hop_budget: int = 6,
    policy: Sequence[EdgeType] = DEFAULT_POLICY,
) -> Reconstruction:
    """Answer ``question`` from the graph as it stood on ``as_of``.

    The walk runs over the projection, so its path, its cost and its dead-end
    classification are all the past's rather than today's. Hindsight is then
    gathered from the full graph and reported separately — never merged into
    the contemporary answer, and never silently dropped either.
    """
    when = parse_date(as_of, where="as_of")
    timeline = Timeline(graph)
    past = as_of_graph(graph, when)

    # Seeded exactly as the walker seeds, so this names the entities the walk
    # would have started from rather than a second, drifting notion of them.
    not_yet_known: List[Placed] = []
    seeded: set = set()
    for mention in question_mentions(question):
        record = resolver.resolve(mention)
        canonical = record.canonical_id
        if not record.resolved or canonical in seeded:
            continue
        if canonical in graph.nodes and canonical not in past.nodes:
            seeded.add(canonical)
            not_yet_known.append(_place(graph.nodes[canonical], graph, timeline, when))

    walk: Optional[Walk] = None
    visited: List[str] = []
    if past.nodes:
        walk = Walker(past, resolver, hop_budget=hop_budget, policy=policy).ask(question)
        visited = [step.node_id for step in walk.steps]

    seen = set(visited)
    then: List[Placed] = []
    decisions: List[Placed] = []
    for node_id in visited:
        node = graph.nodes.get(node_id)
        if node is None:  # pragma: no cover - the projection is built from graph
            continue
        placed = _place(node, graph, timeline, when)
        if node.type in (NodeType.DECISION, NodeType.ASSUMPTION):
            decisions.append(placed)
        else:
            then.append(placed)

    # The ground a walked decision stood on, even where the walk's budget ran
    # out before reaching it: "what was this decided on" must not depend on how
    # far a hop budget happened to stretch.
    for placed in list(decisions):
        for edge in graph.out_edges(placed.node_id, [EdgeType.DEPENDS_ON], live_only=False):
            ground = past.nodes.get(edge.dst)
            if ground is not None and ground.id not in seen:
                seen.add(ground.id)
                decisions.append(_place(ground, graph, timeline, when))

    # Hindsight: what bears on any of it, and arrived after the horizon.
    later: List[Placed] = []
    undated: List[Placed] = []
    for node_id in list(seen):
        for edge in graph.in_edges(node_id, _HINDSIGHT_EDGES, live_only=False):
            other = graph.nodes.get(edge.src)
            if other is None or other.id in seen:
                continue
            placed = _place(other, graph, timeline, when)
            if placed.horizon is Horizon.LATER:
                seen.add(other.id)
                later.append(placed)
            elif placed.horizon is Horizon.UNDATED:
                seen.add(other.id)
                undated.append(placed)

    # A rejected assumption the walk stood on is the clearest hindsight there
    # is: the ground moved. Report the evidence that moved it, whenever it came.
    for placed in decisions:
        if placed.status != AssumptionStatus.REJECTED.value:
            continue
        for edge in graph.in_edges(placed.node_id, [EdgeType.CONTRADICTS], live_only=False):
            witness = graph.nodes.get(edge.src)
            if witness is None or witness.id in seen:
                continue
            seen.add(witness.id)
            found = _place(witness, graph, timeline, when)
            (later if found.horizon is not Horizon.UNDATED else undated).append(found)

    return Reconstruction(
        question=question,
        as_of=when,
        walk=walk,
        then=then,
        decision=decisions,
        later=later,
        undated=undated,
        not_yet_known=not_yet_known,
    )


__all__ = ["Placed", "Reconstruction", "explain_as_of"]
