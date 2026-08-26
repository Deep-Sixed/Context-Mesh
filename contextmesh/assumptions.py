"""Assumptions as first-class, versioned graph objects — and what breaks when
one of them turns out to be false.

Selective invalidation is the load-bearing idea: rejecting an assumption must
invalidate *exactly* the work that stood on it, and must leave everything else
in place. The report proves both halves, because "we invalidated something" is
not a useful answer without "and here is what we deliberately kept".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .graph import ContextGraph
from .model import (
    Assumption,
    AssumptionStatus,
    EdgeType,
    NodeType,
    slug,
)

# Failure travels along these edges and no others (GRAPH.md rule 2), and the
# direction matters:
#
#   X -[depends_on]->  Y   Y failing invalidates X   → follow backwards
#   X -[derived_from]->Y   Y failing invalidates X   → follow backwards
#   X -[produces]->    Y   X failing invalidates Y   → follow forwards
#
# Nothing else propagates. `mentions`, `cites`, `supports` and `supersedes`
# survive an invalidation, which is what makes it selective rather than a purge.
BACKWARD: Tuple[EdgeType, ...] = (EdgeType.DEPENDS_ON, EdgeType.DERIVED_FROM)
FORWARD: Tuple[EdgeType, ...] = (EdgeType.PRODUCES,)
PROPAGATING: Tuple[EdgeType, ...] = BACKWARD + FORWARD


@dataclass
class InvalidationReport:
    assumption_id: str
    statement: str
    #: node id -> the readable chain that made it depend on the assumption
    invalidated: Dict[str, List[str]] = field(default_factory=dict)
    invalidated_edges: List[str] = field(default_factory=list)
    preserved: List[str] = field(default_factory=list)
    replacement_id: Optional[str] = None

    @property
    def blast_radius(self) -> int:
        return len(self.invalidated)

    def why(self, node_id: str) -> str:
        chain = self.invalidated.get(node_id)
        if not chain:
            return f"{node_id} was not invalidated"
        return " → ".join(chain)

    def to_dict(self) -> Dict[str, object]:
        return {
            "assumption_id": self.assumption_id,
            "statement": self.statement,
            "blast_radius": self.blast_radius,
            "invalidated": {k: list(v) for k, v in self.invalidated.items()},
            "invalidated_edges": list(self.invalidated_edges),
            "preserved_count": len(self.preserved),
            "preserved": list(self.preserved),
            "replacement_id": self.replacement_id,
        }


class AssumptionLedger:
    """Creates, supersedes and rejects assumptions against a ContextGraph."""

    def __init__(self, graph: ContextGraph) -> None:
        self.graph = graph
        self.history: List[Dict[str, object]] = []

    # ── lifecycle ────────────────────────────────────────────────────────
    def assume(
        self,
        statement: str,
        *,
        created_by: str = "agent",
        evidence_ids: Iterable[str] = (),
    ) -> Assumption:
        assumption = Assumption(
            id=slug(statement, "assumption"),
            statement=statement,
            created_by=created_by,
            created_at_build=self.graph.build,
            evidence_ids=list(evidence_ids),
        )
        self.graph.add_assumption(assumption)
        for ev in assumption.evidence_ids:
            self.graph.add_edge(assumption.id, EdgeType.JUSTIFIED_BY, ev)
        self._record("assumed", assumption.id, statement)
        return assumption

    def supersede(self, old_id: str, statement: str, *, created_by: str = "agent") -> Assumption:
        """Replace an assumption without deleting it. History is append-only."""
        old = self.graph.assumptions[old_id]
        new = Assumption(
            id=slug(f"{statement}|v{old.version + 1}", "assumption"),
            statement=statement,
            version=old.version + 1,
            created_by=created_by,
            created_at_build=self.graph.build,
            supersedes=old_id,
        )
        self.graph.add_assumption(new)
        old.status = AssumptionStatus.SUPERSEDED
        old.superseded_by = new.id
        self.graph.node(old.id).attrs["status"] = old.status.value
        self.graph.add_edge(new.id, EdgeType.SUPERSEDES, old_id)
        self._record("superseded", new.id, statement, replaces=old_id)
        return new

    def justifies(self, assumption_id: str, edge_id: str) -> None:
        """Mark an existing edge as standing on an assumption."""
        self.graph.edges[edge_id].assumption_id = assumption_id

    def depends(self, node_id: str, assumption_id: str) -> None:
        """Wire a decision or claim to the assumption it rests on."""
        self.graph.add_edge(node_id, EdgeType.DEPENDS_ON, assumption_id)

    # ── the interesting part ─────────────────────────────────────────────
    def blast_radius(self, assumption_id: str) -> Dict[str, List[str]]:
        """Nodes whose validity depends on this assumption, with the chain why.

        Walks *backwards* along the propagating edge types from the assumption:
        a decision `depends_on` an assumption, a claim is `derived_from` that
        decision, an entity is `produces`d by it, and so on down the line.
        """
        graph = self.graph
        assumption = graph.assumptions[assumption_id]
        start_label = f"assumption({assumption.statement[:48]})"

        reached: Dict[str, List[str]] = {}
        frontier: List[Tuple[str, List[str]]] = [(assumption_id, [start_label])]

        seeds: List[Tuple[str, List[str]]] = []
        # Edges explicitly justified by the assumption drag their target in too.
        for edge in graph.edges.values():
            if edge.assumption_id == assumption_id and edge.live:
                seeds.append(
                    (
                        edge.dst,
                        [
                            start_label,
                            f"justifies {edge.type.value} edge",
                            graph.node(edge.dst).label,
                        ],
                    )
                )
        frontier.extend(seeds)

        while frontier:
            node_id, chain = frontier.pop(0)
            if node_id != assumption_id:
                if node_id in reached and len(reached[node_id]) <= len(chain):
                    continue
                reached[node_id] = chain
            # Whoever depends on this node, or derives from it, falls with it.
            for edge in graph.in_edges(node_id, BACKWARD):
                parent = graph.node(edge.src)
                if parent.type is NodeType.ASSUMPTION:
                    continue
                frontier.append(
                    (parent.id, [*chain, f"<-{edge.type.value}-", parent.label])
                )
            # Whatever this node produced falls with it too.
            if node_id != assumption_id:
                for edge in graph.out_edges(node_id, FORWARD):
                    child = graph.node(edge.dst)
                    frontier.append(
                        (child.id, [*chain, f"-{edge.type.value}->", child.label])
                    )
        return reached

    def reject(
        self,
        assumption_id: str,
        *,
        evidence_id: Optional[str] = None,
        replacement: Optional[str] = None,
    ) -> InvalidationReport:
        """Disprove an assumption and invalidate exactly what stood on it."""
        graph = self.graph
        assumption = graph.assumptions[assumption_id]
        radius = self.blast_radius(assumption_id)

        assumption.status = AssumptionStatus.REJECTED
        assumption.rejected_at_build = graph.build
        node = graph.node(assumption_id)
        node.attrs["status"] = assumption.status.value
        node.invalidated = True

        if evidence_id:
            assumption.evidence_ids.append(evidence_id)
            graph.add_edge(evidence_id, EdgeType.CONTRADICTS, assumption_id)

        invalidated_edges: List[str] = []
        for edge in graph.edges.values():
            if edge.assumption_id == assumption_id or (
                edge.src in radius and edge.dst in radius
            ):
                edge.invalidated = True
                invalidated_edges.append(edge.id)
        for node_id in radius:
            graph.node(node_id).invalidated = True

        preserved = sorted(
            n.id
            for n in graph.nodes.values()
            if n.id not in radius and n.id != assumption_id and n.live
        )

        report = InvalidationReport(
            assumption_id=assumption_id,
            statement=assumption.statement,
            invalidated=radius,
            invalidated_edges=invalidated_edges,
            preserved=preserved,
        )

        if replacement:
            new = self.assume(replacement, created_by=assumption.created_by)
            new.version = assumption.version + 1
            new.supersedes = assumption_id
            assumption.superseded_by = new.id
            graph.add_edge(new.id, EdgeType.SUPERSEDES, assumption_id)
            report.replacement_id = new.id

        self._record(
            "rejected",
            assumption_id,
            assumption.statement,
            blast_radius=report.blast_radius,
            preserved=len(preserved),
        )
        return report

    def active(self) -> List[Assumption]:
        return [
            a
            for a in self.graph.assumptions.values()
            if a.status is AssumptionStatus.ACTIVE
        ]

    def lineage(self, assumption_id: str) -> List[Assumption]:
        """Oldest first, following `supersedes` back to the original."""
        chain: List[Assumption] = []
        current: Optional[str] = assumption_id
        seen: Set[str] = set()
        while current and current not in seen:
            seen.add(current)
            assumption = self.graph.assumptions[current]
            chain.append(assumption)
            current = assumption.supersedes
        return list(reversed(chain))

    def _record(self, action: str, assumption_id: str, statement: str, **extra) -> None:
        self.history.append(
            {
                "build": self.graph.build,
                "action": action,
                "assumption_id": assumption_id,
                "statement": statement,
                **extra,
            }
        )
