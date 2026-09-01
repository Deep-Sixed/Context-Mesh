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
from typing import Dict, Optional

from .graph import ContextGraph
from .model import EdgeType, Node, NodeType

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


__all__ = [
    "Anchor",
    "Horizon",
    "SOURCE_TIME_ATTR",
    "TemporalError",
    "Timeline",
    "parse_date",
    "source_date",
]
