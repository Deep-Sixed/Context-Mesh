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

    def decide(
        self,
        title: str,
        rationale: str,
        *,
        source_id: str,
        supported_by: Iterable[str] = (),
        cites: Iterable[str] = (),
        assumptions: Iterable[str] = (),
        produces: Iterable[str] = (),
        supersedes: Optional[str] = None,
    ) -> Node:
        node = self.graph.add_node(
            NodeType.DECISION,
            title,
            attrs={"rationale": rationale},
            provenance=Provenance(
                source_id=source_id,
                extractor="decision-log",
                checks=["rationale-present", "provenance-present"],
                recorded_at_build=self.graph.build,
            ),
        )
        self.graph.add_edge(node.id, EdgeType.CITES, source_id)
        for claim_id in supported_by:
            self.graph.add_edge(claim_id, EdgeType.SUPPORTS, node.id)
            self.graph.add_edge(node.id, EdgeType.DERIVED_FROM, claim_id)
        for src in cites:
            self.graph.add_edge(node.id, EdgeType.CITES, src)
        for assumption_id in assumptions:
            self.graph.add_edge(node.id, EdgeType.DEPENDS_ON, assumption_id)
        for entity_id in produces:
            self.graph.add_edge(node.id, EdgeType.PRODUCES, entity_id)
        if supersedes:
            self.graph.add_edge(node.id, EdgeType.SUPERSEDES, supersedes)
            self.graph.node(supersedes).attrs["superseded_by"] = node.id

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
