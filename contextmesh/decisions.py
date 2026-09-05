"""Decision history: append-only, with the reasoning attached.

A decision node records why it was made, what it stood on, and what it replaced.
Superseding never deletes — the old decision stays walkable so "why did we
change our mind" is answerable by walking, not by reading a changelog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .graph import ContextGraph
from .model import EdgeType, Node, NodeType, Provenance, slug
from .ontology import OntologyError


@dataclass
class DecisionRecord:
    decision_id: str
    title: str
    rationale: str
    at_build: int
    supersedes: Optional[str] = None
    assumptions: List[str] = field(default_factory=list)
    supported_by: List[str] = field(default_factory=list)
    cites: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "title": self.title,
            "rationale": self.rationale,
            "at_build": self.at_build,
            "supersedes": self.supersedes,
            "assumptions": list(self.assumptions),
            "supported_by": list(self.supported_by),
            "cites": list(self.cites),
        }


class DecisionLog:
    """Append-only log mirrored into the graph as `decision` nodes."""

    def __init__(self, graph: ContextGraph) -> None:
        self.graph = graph
        self.records: List[DecisionRecord] = []
        # Keyed by an explicit `id=` a caller has already used once, so a
        # retry with that id can be told apart from a genuine collision --
        # see `decide`'s docstring.
        self._payloads: Dict[str, Dict[str, Any]] = {}

    def decide(
        self,
        title: str,
        rationale: str,
        *,
        source_id: str,
        id: Optional[str] = None,
        supported_by: Iterable[str] = (),
        cites: Iterable[str] = (),
        assumptions: Iterable[str] = (),
        produces: Iterable[str] = (),
        supersedes: Optional[str] = None,
    ) -> Node:
        """Record a decision. Every call without an explicit id mints a new,
        never-reused decision, even if the content is byte-identical to a
        prior one -- a decision is an immutable event, not content-addressed,
        so "the same title again" is a second decision, not an update. Use
        `supersedes` to link it to the one it replaces.

        An explicit `id` is purely an idempotency key, not a content
        address: calling again with the same `id` and the exact same
        immutable payload (title, rationale, source_id, supported_by, cites,
        assumptions, produces, supersedes) is a true no-op that returns the
        existing node untouched; calling again with the same `id` and any
        different payload is refused with `OntologyError` before anything is
        written, rather than silently rewriting what that id already means.

        The write is atomic: if any referenced id is invalid partway through
        (a bad source, claim, assumption, entity, or supersedes target), the
        decision and edges this call would have created are rolled back
        rather than left standing incomplete.
        """
        supported_by = tuple(supported_by)
        cites = tuple(cites)
        assumptions = tuple(assumptions)
        produces = tuple(produces)
        payload: Dict[str, Any] = {
            "title": title,
            "rationale": rationale,
            "source_id": source_id,
            "supported_by": supported_by,
            "cites": cites,
            "assumptions": assumptions,
            "produces": produces,
            "supersedes": supersedes,
        }

        if id is not None:
            existing_payload = self._payloads.get(id)
            if existing_payload is not None:
                if existing_payload != payload:
                    raise OntologyError(
                        f"decision id {id!r} is already recorded with "
                        "different content; refusing to treat this call as "
                        "the same decision"
                    )
                return self.graph.node(id)

        node_id = id if id is not None else slug(
            f"{title}|{len(self.records) + 1}", "decision"
        )
        node_already_existed = node_id in self.graph.nodes
        node = self.graph.add_node(
            NodeType.DECISION,
            title,
            id=node_id,
            attrs={"rationale": rationale},
            provenance=Provenance(
                source_id=source_id,
                extractor="decision-log",
                checks=["rationale-present", "provenance-present"],
                recorded_at_build=self.graph.build,
            ),
        )

        created_edge_ids: List[str] = []

        def _tracked_edge(src: str, etype: EdgeType, dst: str) -> None:
            key = (src, etype.value, dst)
            already = key in self.graph._edge_key
            edge = self.graph.add_edge(src, etype, dst)
            if not already:
                created_edge_ids.append(edge.id)

        try:
            _tracked_edge(node.id, EdgeType.CITES, source_id)
            for claim_id in supported_by:
                _tracked_edge(claim_id, EdgeType.SUPPORTS, node.id)
                _tracked_edge(node.id, EdgeType.DERIVED_FROM, claim_id)
            for src in cites:
                _tracked_edge(node.id, EdgeType.CITES, src)
            for assumption_id in assumptions:
                _tracked_edge(node.id, EdgeType.DEPENDS_ON, assumption_id)
            for entity_id in produces:
                _tracked_edge(node.id, EdgeType.PRODUCES, entity_id)
            if supersedes:
                _tracked_edge(node.id, EdgeType.SUPERSEDES, supersedes)
                self.graph.node(supersedes).attrs["superseded_by"] = node.id
        except Exception:
            for edge_id in created_edge_ids:
                self.graph._discard_edge(edge_id)
            if not node_already_existed:
                self.graph._discard_node(node.id)
            raise

        self.records.append(
            DecisionRecord(
                decision_id=node.id,
                title=title,
                rationale=rationale,
                at_build=self.graph.build,
                supersedes=supersedes,
                assumptions=list(assumptions),
                supported_by=list(supported_by),
                cites=list(cites),
            )
        )
        if id is not None:
            self._payloads[id] = payload
        return node

    def history_of(self, decision_id: str) -> List[DecisionRecord]:
        """Every version of a decision, oldest first."""
        by_id = {r.decision_id: r for r in self.records}
        chain: List[DecisionRecord] = []
        current: Optional[str] = decision_id
        while current and current in by_id:
            record = by_id[current]
            chain.append(record)
            current = record.supersedes
        return list(reversed(chain))

    def current(self) -> List[DecisionRecord]:
        """Decisions nothing has superseded yet."""
        replaced = {r.supersedes for r in self.records if r.supersedes}
        return [r for r in self.records if r.decision_id not in replaced]

    def to_dict(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self.records]
