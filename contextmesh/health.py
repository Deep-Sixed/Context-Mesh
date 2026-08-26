"""Graph health — the conditions that quietly make a graph useless.

None of these are errors. They are the states a long-running graph drifts into:
entities that never resolved, claims with no source, hubs nothing walks, edges
standing on assumptions nobody has revisited in fifty builds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .graph import ContextGraph
from .model import AssumptionStatus, EdgeType, NodeType
from .resolve import Resolver
from .traverse import Walker


@dataclass
class Signal:
    kind: str
    severity: str  # "info" | "warn" | "error"
    count: int
    detail: str
    remedy: str
    items: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "count": self.count,
            "detail": self.detail,
            "remedy": self.remedy,
            "items": list(self.items[:12]),
        }


def _sample(ids: List[str], n: int = 12) -> List[str]:
    return ids[:n]


def check(
    graph: ContextGraph,
    resolver: Optional[Resolver] = None,
    walker: Optional[Walker] = None,
    *,
    stale_after_builds: int = 25,
) -> List[Signal]:
    """Run every health check and return the signals that fired."""
    signals: List[Signal] = []

    # 1. Untyped edges. Structurally impossible, checked anyway.
    untyped = graph.untyped_edges
    signals.append(
        Signal(
            kind="untyped_edges",
            severity="error" if untyped else "info",
            count=untyped,
            detail="edges stored without a type from GRAPH.md",
            remedy="none needed; add_edge has no untyped code path",
        )
    )

    # 2. Orphans — live nodes nothing connects to.
    orphans = [
        n.id for n in graph.nodes.values() if n.live and graph.degree(n.id) == 0
    ]
    if orphans:
        signals.append(
            Signal(
                kind="orphans",
                severity="warn",
                count=len(orphans),
                detail="live nodes with no edge in either direction",
                remedy="link them at the next LINK stage or let PRUNE drop them",
                items=_sample(orphans),
            )
        )

    # 3. Provenance gaps — a claim or decision with no path to a source.
    gaps: List[str] = []
    for node in graph.nodes.values():
        if not node.live or node.type not in (NodeType.CLAIM, NodeType.DECISION):
            continue
        has_source = any(
            graph.node(e.dst).type is NodeType.SOURCE
            for e in graph.out_edges(node.id, (EdgeType.DERIVED_FROM, EdgeType.CITES))
        )
        if not (has_source or node.provenance):
            gaps.append(node.id)
    if gaps:
        signals.append(
            Signal(
                kind="provenance_gap",
                severity="error",
                count=len(gaps),
                detail="claims or decisions that cannot name where they came from",
                remedy="re-extract from the source span or drop the node",
                items=_sample(gaps),
            )
        )

    # 4. Unresolved entities — mentions that never got a canonical id.
    if resolver is not None:
        borderline = resolver.borderline()
        if borderline:
            signals.append(
                Signal(
                    kind="unresolved_entities",
                    severity="warn",
                    count=len(borderline),
                    detail=(
                        "near-miss mentions dropped at RESOLVE "
                        f"({len(resolver.unresolved())} unresolved in total, "
                        "most of which are not entities at all)"
                    ),
                    remedy="extend the alias table or lower the resolver threshold",
                    items=_sample(
                        [f"{r.mention!r} — {r.reason}" for r in sorted(
                            borderline, key=lambda r: -r.score
                        )]
                    ),
                )
            )

    # 5. Missing relationships — entities named by a source but never linked
    #    to a claim, so nothing can be said about them.
    mute: List[str] = []
    for node in graph.by_type(NodeType.ENTITY):
        claims = [
            e for e in graph.in_edges(node.id, (EdgeType.MENTIONS,))
            if graph.node(e.src).type is NodeType.CLAIM
        ]
        if not claims:
            mute.append(node.id)
    if mute:
        signals.append(
            Signal(
                kind="missing_relationships",
                severity="warn",
                count=len(mute),
                detail="resolved entities no claim ever mentions",
                remedy="the entity is a name with no content; extract more or prune",
                items=_sample(mute),
            )
        )

    # 6. Stale evidence — assumptions still ACTIVE long after they were made.
    stale = [
        a.id
        for a in graph.assumptions.values()
        if a.status is AssumptionStatus.ACTIVE
        and graph.build - a.created_at_build > stale_after_builds
    ]
    if stale:
        signals.append(
            Signal(
                kind="stale_assumptions",
                severity="warn",
                count=len(stale),
                detail=f"assumptions unrevisited for more than {stale_after_builds} builds",
                remedy="re-check against current evidence, then supersede or confirm",
                items=_sample(stale),
            )
        )

    # 7. Contradiction pressure — claims with a live contradicts edge and no
    #    decision resolving them.
    contested: List[str] = []
    for edge in graph.edges.values():
        if edge.type is EdgeType.CONTRADICTS and edge.live:
            contested.append(edge.src)
    if contested:
        signals.append(
            Signal(
                kind="open_contradictions",
                severity="warn",
                count=len(set(contested)),
                detail="claims contradicted by another claim with no decision on top",
                remedy="record a decision citing both, or supersede the loser",
                items=_sample(sorted(set(contested))),
            )
        )

    # 8. Dead ends by reason, if any walking has happened.
    if walker is not None and walker.walks:
        ledger = walker.dead_end_ledger()
        total = sum(ledger.values())
        if total:
            signals.append(
                Signal(
                    kind="dead_ends",
                    severity="warn",
                    count=total,
                    detail="; ".join(f"{k}={v}" for k, v in ledger.items() if v),
                    remedy="each reason has a different fix — see docs/ARCHITECTURE.md",
                    items=[
                        w.question for w in walker.walks if w.dead_end
                    ][:12],
                )
            )

    # 9. Invalidated work still sitting in the graph.
    invalid = [n.id for n in graph.nodes.values() if n.invalidated]
    if invalid:
        signals.append(
            Signal(
                kind="invalidated_nodes",
                severity="info",
                count=len(invalid),
                detail="work invalidated by a rejected assumption, kept for audit",
                remedy="re-execute against the replacement assumption",
                items=_sample(invalid),
            )
        )

    return signals


def report(
    graph: ContextGraph,
    resolver: Optional[Resolver] = None,
    walker: Optional[Walker] = None,
) -> Dict[str, Any]:
    signals = check(graph, resolver, walker)
    worst = "ok"
    for signal in signals:
        if signal.severity == "error" and signal.count:
            worst = "error"
            break
        if signal.severity == "warn" and signal.count:
            worst = "warn"
    return {
        "status": worst,
        "nodes_live": sum(1 for n in graph.nodes.values() if n.live),
        "nodes_total": len(graph.nodes),
        "edges_live": sum(1 for e in graph.edges.values() if e.live),
        "edge_types_used": sum(1 for v in graph.edge_counts().values() if v),
        "signals": [s.to_dict() for s in signals],
    }
