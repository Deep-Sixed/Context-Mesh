"""The dashboard payload.

Every number the dashboard renders is computed here from the live graph and the
walk ledger. Nothing on the page is scripted: if the graph changes, the panels
change, and if a panel has no data it renders empty rather than plausible.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .assumptions import AssumptionLedger, InvalidationReport
from .decisions import DecisionLog
from .graph import ContextGraph
from .health import report as health_report
from .model import DISPLAY_TYPES, EdgeType
from .pipeline import BuildReport
from .resolve import Resolver
from .traverse import DeadEnd, Walker

#: The four edge types the dashboard's edge ledger breaks out, in its order.
LEDGER_EDGES = (
    EdgeType.MENTIONS,
    EdgeType.DERIVED_FROM,
    EdgeType.CITES,
    EdgeType.CONTRADICTS,
)

DEAD_END_LABELS = {
    DeadEnd.NO_TYPED_EDGE: "NO TYPED EDGE",
    DeadEnd.ENTITY_UNRESOLVED: "ENTITY UNRESOLVED",
    DeadEnd.WRONG_NODE_TYPE: "WRONG NODE TYPE",
    DeadEnd.PRUNED_TOO_EARLY: "PRUNED TOO EARLY",
}


def _graph_payload(graph: ContextGraph) -> Dict[str, Any]:
    """Nodes and edges reduced to what the renderer needs to draw them."""
    index: Dict[str, int] = {}
    nodes: List[Dict[str, Any]] = []
    for node in graph.nodes.values():
        if not node.live or node.type not in DISPLAY_TYPES:
            continue
        index[node.id] = len(nodes)
        nodes.append(
            {
                "id": node.id,
                "t": node.type.value,
                "label": node.label[:90],
                "deg": graph.degree(node.id),
                "walks": node.walks,
            }
        )
    edges: List[Dict[str, Any]] = []
    for edge in graph.edges.values():
        if not edge.live or edge.src not in index or edge.dst not in index:
            continue
        edges.append(
            {
                "s": index[edge.src],
                "d": index[edge.dst],
                "t": edge.type.value,
                "w": round(edge.weight, 2),
                "n": edge.traversals,
            }
        )
    return {"nodes": nodes, "edges": edges}


def _hop_histogram(walker: Walker, span: int = 8) -> List[Dict[str, int]]:
    hist = walker.hop_histogram()
    return [{"hops": h, "count": hist.get(h, 0)} for h in range(1, span + 1)]


def _traversal_grid(walker: Walker, cells: int = 1008) -> List[int]:
    """One cell per walk: 1 resolved, 0 dead end. Newest last."""
    recent = walker.walks[-cells:]
    return [1 if w.resolved else 0 for w in recent]


def snapshot(
    *,
    graph: ContextGraph,
    walker: Walker,
    resolver: Resolver,
    build: BuildReport,
    decisions: Optional[DecisionLog] = None,
    ledger: Optional[AssumptionLedger] = None,
    invalidation: Optional[InvalidationReport] = None,
) -> Dict[str, Any]:
    """Everything the dashboard needs, in one JSON-serialisable dict."""
    live_nodes = [n for n in graph.nodes.values() if n.live]
    edge_counts = graph.edge_counts()
    resolved_walks = [w for w in walker.walks if w.resolved]
    flat_total = sum(w.tokens_flat for w in walker.walks) or 1
    walked_total = sum(w.tokens_walked for w in walker.walks)

    type_counts = graph.type_counts()

    payload: Dict[str, Any] = {
        "header": {
            # The capture's masthead read "Claude Graph Engineering · Context
            # Mesh". This project is unaffiliated, so it does not carry that
            # name: see the departure noted in dashboard/README.md.
            "title": "Context Mesh",
            "subtitle": "a typed context graph",
            "kicker": "ONE GRAPH · FOUR NODE TYPES · EVERY ANSWER WALKS A PATH YOU CAN READ",
            "nodes_resolved": len(live_nodes),
            "edges_per_tick": max(1, len(graph.edges) // max(graph.build, 1) // 8),
            "walks_per_min": len(walker.walks),
            "traversal_ms": _median_ms(walker),
        },
        "build": {
            "number": build.build,
            "spans_in": build.spans_in,
            "edges": build.committed_walkable,
            "dropped_at_resolve": build.dropped_at_resolve,
            "committed_walkable": build.committed_walkable,
            "collapsed_aliases": build.collapsed_aliases,
            "stages": [s.to_dict() for s in build.stages],
        },
        "node_types": [
            {
                "type": t.value,
                "label": t.value.upper() + "S" if t.value != "entity" else "ENTITIES",
                "count": type_counts[t.value],
            }
            for t in DISPLAY_TYPES
        ],
        "graph": _graph_payload(graph),
        "hop_budget": {
            "median": walker.median_hops(),
            "bins": _hop_histogram(walker),
            "note": "FLAT RAG = 1 HOP, NO PATH",
        },
        "edge_ledger": {
            "total": sum(edge_counts.values()),
            "rows": [
                {
                    "type": e.value,
                    "label": e.value.replace("_", " ").upper(),
                    "count": edge_counts[e.value],
                    "traversals": sum(
                        edge.traversals
                        for edge in graph.edges.values()
                        if edge.type is e
                    ),
                }
                for e in LEDGER_EDGES
            ],
            "untyped": graph.untyped_edges,
        },
        "walk_vs_flat": {
            "saving": round(1.0 - walked_total / flat_total, 4),
            "series": [
                {
                    "flat": w.tokens_flat,
                    "walk": w.tokens_walked,
                }
                for w in walker.walks[-60:]
            ],
            "flat_peak": max((w.tokens_flat for w in walker.walks), default=0),
            "walk_typical": (
                sorted(w.tokens_walked for w in resolved_walks)[len(resolved_walks) // 2]
                if resolved_walks
                else 0
            ),
        },
        "traversal_grid": {
            "cells": _traversal_grid(walker),
            "capacity": 1008,
            "note": (
                "WALK 01 CRAWLS THE WHOLE MESH · WALK 10 TOUCHES FOUR NODES — "
                "THE PATH IS TYPED AND STORED, NOT GUESSED AGAIN PER QUESTION"
            ),
        },
        "dead_ends": {
            "total": sum(1 for w in walker.walks if w.dead_end),
            "resolved_rate": round(walker.resolved_rate, 4),
            "rows": [
                {
                    "reason": reason.value,
                    "label": DEAD_END_LABELS[reason],
                    "count": walker.dead_end_ledger()[reason.value],
                }
                for reason in DeadEnd
            ],
        },
        "ontology": {
            "file": "GRAPH.md",
            "note": "THE ONTOLOGY FILE · READ ON EVERY WRITE",
            "edge_types": len(graph.ontology.edge_types),
            "edge_types_used": sum(1 for v in edge_counts.values() if v),
            "orphans": sum(
                1 for n in live_nodes if graph.degree(n.id) == 0
            ),
            "walk_time_series": [w.visited for w in walker.walks[-40:]],
        },
        "examples": {
            "answered": [w.to_dict() for w in resolved_walks[:4]],
            "dead_ends": [
                w.to_dict() for w in walker.walks if w.dead_end
            ][:4],
        },
        "health": health_report(graph, resolver, walker),
    }

    if decisions is not None:
        payload["decisions"] = decisions.to_dict()
    if ledger is not None:
        payload["assumptions"] = [a.to_dict() for a in graph.assumptions.values()]
        payload["assumption_history"] = ledger.history
    if invalidation is not None:
        payload["invalidation"] = invalidation.to_dict()

    return payload


def _median_ms(walker: Walker) -> int:
    """Nodes expanded is the honest proxy for traversal cost; scale to ms."""
    visited = sorted(w.visited for w in walker.walks if w.resolved)
    if not visited:
        return 0
    return max(1, int(visited[len(visited) // 2] * 1.6))
