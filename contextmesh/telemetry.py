"""Thin, deterministic, read-only telemetry projection layer.

Exposes what ContextGraph, Walker, and BuildReport already authoritatively
record without mutating engine state or altering execution behavior.

No duplicate semantic algorithms; projection performs read-only aggregation
over existing authoritative engine state and methods.

Immutability here is deep, not shallow. ``frozen=True`` stops an attribute
being rebound, but it leaves a nested ``dict`` -- or a ``dict`` sitting
inside a tuple -- fully writable, so a caller could edit a projection in
place and watch the edit surface again in :meth:`to_dict`. Every container
is therefore coerced to an immutable form in ``__post_init__`` rather than
at the point of construction: tuples for sequences, read-only views for
mappings. Doing it in the dataclass makes the guarantee structural, so it
holds for anything that builds one of these by hand and does not depend on
:meth:`TelemetryProjection.project` remaining the only way in.

:meth:`to_dict` is the mutable serialization boundary, and stays one. It
copies each read-only view back out into a plain ``dict`` or ``list``, so
callers get something they may freely edit, JSON-encode, or hand onward
without reaching back into the projection it came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple

from .graph import ContextGraph
from .metrics import DEAD_END_LABELS, LEDGER_EDGES, _hop_histogram, _traversal_grid
from .pipeline import BuildReport
from .traverse import DeadEnd, Walker


def _frozen_mapping(mapping: Mapping[Any, Any]) -> Mapping[Any, Any]:
    """A read-only view over a *private copy* of ``mapping``.

    The copy earns its keep as much as the view does: wrapping the caller's
    own dict would leave them holding a writable handle on the projection's
    insides, which is the same defect one indirection further out.
    """
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class StageMetrics:
    """Read-only metrics for a single build pipeline stage."""

    name: str
    caption: str
    admitted: int
    dropped: int
    notes: Tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "notes", tuple(self.notes))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "caption": self.caption,
            "admitted": self.admitted,
            "dropped": self.dropped,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class BuildMetrics:
    """Read-only projection of BuildReport pipeline metrics."""

    number: int
    spans_in: int
    committed_walkable: int
    dropped_at_resolve: int
    collapsed_aliases: int
    pruned_nodes: int
    stages: Tuple[StageMetrics, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "stages", tuple(self.stages))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "spans_in": self.spans_in,
            "committed_walkable": self.committed_walkable,
            "dropped_at_resolve": self.dropped_at_resolve,
            "collapsed_aliases": self.collapsed_aliases,
            "pruned_nodes": self.pruned_nodes,
            "stages": [s.to_dict() for s in self.stages],
        }


@dataclass(frozen=True)
class GraphTypeMetrics:
    """Read-only counts of live nodes and typed edges across the graph."""

    nodes_total: int
    nodes_live: int
    edges_total: int
    untyped_edges: int
    type_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "type_counts", _frozen_mapping(self.type_counts))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes_total": self.nodes_total,
            "nodes_live": self.nodes_live,
            "edges_total": self.edges_total,
            "untyped_edges": self.untyped_edges,
            "type_counts": dict(self.type_counts),
        }


@dataclass(frozen=True)
class WalkSummary:
    """Read-only summary of walk outcomes."""

    walk_count: int
    resolved_count: int
    dead_end_count: int
    resolved_rate: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "walk_count": self.walk_count,
            "resolved_count": self.resolved_count,
            "dead_end_count": self.dead_end_count,
            "resolved_rate": self.resolved_rate,
        }


@dataclass(frozen=True)
class HopMetrics:
    """Hop budget metrics.

    Preserves Walk.hops under its existing name ('hops'); does not define or rename
    to 'answer_depth' while that semantic question remains OPEN.
    """

    median_hops: int
    hops_histogram: Mapping[int, int]
    bins: Tuple[Mapping[str, int], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "hops_histogram", _frozen_mapping(self.hops_histogram))
        object.__setattr__(self, "bins", tuple(_frozen_mapping(b) for b in self.bins))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "median_hops": self.median_hops,
            "hops_histogram": dict(self.hops_histogram),
            "bins": [dict(b) for b in self.bins],
        }


@dataclass(frozen=True)
class EdgeLedgerRow:
    """Read-only edge count and traversal telemetry for one typed edge."""

    type: str
    label: str
    count: int
    traversals: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "label": self.label,
            "count": self.count,
            "traversals": self.traversals,
        }


@dataclass(frozen=True)
class EdgeTraversals:
    """Read-only edge ledger breaking out typed edge counts and traversal traffic."""

    total: int
    untyped: int
    rows: Tuple[EdgeLedgerRow, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "untyped": self.untyped,
            "rows": [r.to_dict() for r in self.rows],
        }


@dataclass(frozen=True)
class DeadEndRow:
    """Read-only failure count for one of the four DeadEnd failure modes."""

    reason: str
    label: str
    count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reason": self.reason,
            "label": self.label,
            "count": self.count,
        }


@dataclass(frozen=True)
class DeadEndLedger:
    """Read-only dead-end ledger breaking out terminal walk failures."""

    total: int
    resolved_rate: float
    rows: Tuple[DeadEndRow, ...]
    counts: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))
        object.__setattr__(self, "counts", _frozen_mapping(self.counts))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "resolved_rate": self.resolved_rate,
            "rows": [r.to_dict() for r in self.rows],
            "counts": dict(self.counts),
        }


@dataclass(frozen=True)
class TokenSeriesPoint:
    """Token comparison for one recent walk against flat top-k baseline."""

    flat: int
    walk: int

    def to_dict(self) -> Dict[str, int]:
        return {"flat": self.flat, "walk": self.walk}


@dataclass(frozen=True)
class TokenSavings:
    """Read-only token savings metrics comparing walked paths to flat chunks."""

    saving: float
    tokens_flat_total: int
    tokens_walked_total: int
    flat_peak: int
    walk_typical: int
    series: Tuple[TokenSeriesPoint, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "series", tuple(self.series))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "saving": self.saving,
            "tokens_flat_total": self.tokens_flat_total,
            "tokens_walked_total": self.tokens_walked_total,
            "flat_peak": self.flat_peak,
            "walk_typical": self.walk_typical,
            "series": [p.to_dict() for p in self.series],
        }


@dataclass(frozen=True)
class TraversalGrid:
    """Read-only ringbuffer of recent walk outcomes (1 = resolved, 0 = dead end)."""

    cells: Tuple[int, ...]
    capacity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "cells", tuple(self.cells))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cells": list(self.cells),
            "capacity": self.capacity,
        }


@dataclass(frozen=True)
class TelemetryProjection:
    """Thin, deterministic, read-only projection over Context Mesh engine state."""

    build: BuildMetrics
    graph: GraphTypeMetrics
    walk_summary: WalkSummary
    hop_metrics: HopMetrics
    edge_traversals: EdgeTraversals
    dead_ends: DeadEndLedger
    token_savings: TokenSavings
    traversal_grid: TraversalGrid

    @classmethod
    def project(
        cls,
        *,
        graph: ContextGraph,
        walker: Walker,
        build: BuildReport,
        grid_cells: int = 1008,
        token_series_limit: int = 60,
        hop_span: int = 8,
    ) -> "TelemetryProjection":
        """Project engine state without mutating graph, walker, or pipeline."""
        # 1. Build metrics
        stage_projections = tuple(
            StageMetrics(
                name=s.name,
                caption=s.caption,
                admitted=s.admitted,
                dropped=s.dropped,
                notes=tuple(s.notes),
            )
            for s in build.stages
        )
        build_metrics = BuildMetrics(
            number=build.build,
            spans_in=build.spans_in,
            committed_walkable=build.committed_walkable,
            dropped_at_resolve=build.dropped_at_resolve,
            collapsed_aliases=build.collapsed_aliases,
            pruned_nodes=build.pruned_nodes,
            stages=stage_projections,
        )

        # 2. Graph & type metrics
        live_nodes_count = sum(1 for n in graph.nodes.values() if n.live)
        edge_counts = graph.edge_counts()
        graph_metrics = GraphTypeMetrics(
            nodes_total=len(graph.nodes),
            nodes_live=live_nodes_count,
            edges_total=len(graph.edges),
            untyped_edges=graph.untyped_edges,
            type_counts=graph.type_counts(),
        )

        # 3. Walk summary
        resolved_walks = [w for w in walker.walks if w.resolved]
        dead_walks_count = sum(1 for w in walker.walks if w.dead_end)
        walk_summary = WalkSummary(
            walk_count=len(walker.walks),
            resolved_count=len(resolved_walks),
            dead_end_count=dead_walks_count,
            resolved_rate=round(walker.resolved_rate, 4),
        )

        # 4. Hop metrics (preserves Walk.hops under its existing name)
        hop_metrics = HopMetrics(
            median_hops=walker.median_hops(),
            hops_histogram=walker.hop_histogram(),
            bins=_hop_histogram(walker, span=hop_span),
        )

        # 5. Edge traversals
        edge_rows = tuple(
            EdgeLedgerRow(
                type=e.value,
                label=e.value.replace("_", " ").upper(),
                count=edge_counts.get(e.value, 0),
                traversals=sum(
                    edge.traversals for edge in graph.edges.values() if edge.type is e
                ),
            )
            for e in LEDGER_EDGES
        )
        edge_traversals = EdgeTraversals(
            total=sum(edge_counts.values()),
            untyped=graph.untyped_edges,
            rows=edge_rows,
        )

        # 6. Dead-end ledger
        dead_counts = walker.dead_end_ledger()
        dead_rows = tuple(
            DeadEndRow(
                reason=reason.value,
                label=DEAD_END_LABELS[reason],
                count=dead_counts.get(reason.value, 0),
            )
            for reason in DeadEnd
        )
        dead_ends = DeadEndLedger(
            total=dead_walks_count,
            resolved_rate=round(walker.resolved_rate, 4),
            rows=dead_rows,
            counts=dead_counts,
        )

        # 7. Token savings
        flat_total = sum(w.tokens_flat for w in walker.walks) or 1
        walked_total = sum(w.tokens_walked for w in walker.walks)
        series_points = tuple(
            TokenSeriesPoint(flat=w.tokens_flat, walk=w.tokens_walked)
            for w in walker.walks[-token_series_limit:]
        )
        flat_peak = max((w.tokens_flat for w in walker.walks), default=0)
        walk_typical = (
            sorted(w.tokens_walked for w in resolved_walks)[len(resolved_walks) // 2]
            if resolved_walks
            else 0
        )
        token_savings = TokenSavings(
            saving=round(walker.token_saving(), 4),
            tokens_flat_total=flat_total,
            tokens_walked_total=walked_total,
            flat_peak=flat_peak,
            walk_typical=walk_typical,
            series=series_points,
        )

        # 8. Traversal grid
        grid_cells_data = tuple(_traversal_grid(walker, cells=grid_cells))
        traversal_grid = TraversalGrid(
            cells=grid_cells_data,
            capacity=grid_cells,
        )

        return cls(
            build=build_metrics,
            graph=graph_metrics,
            walk_summary=walk_summary,
            hop_metrics=hop_metrics,
            edge_traversals=edge_traversals,
            dead_ends=dead_ends,
            token_savings=token_savings,
            traversal_grid=traversal_grid,
        )

    def to_dict(self) -> Dict[str, Any]:
        """The mutable serialization boundary, in plain dicts and lists.

        The projection itself is deeply immutable; what comes back from here
        is a fresh, freely editable copy. Callers may mutate it, encode it,
        or pass it on without any of that reaching the projection.
        """
        return {
            "build": self.build.to_dict(),
            "graph": self.graph.to_dict(),
            "walk_summary": self.walk_summary.to_dict(),
            "hop_metrics": self.hop_metrics.to_dict(),
            "edge_traversals": self.edge_traversals.to_dict(),
            "dead_ends": self.dead_ends.to_dict(),
            "token_savings": self.token_savings.to_dict(),
            "traversal_grid": self.traversal_grid.to_dict(),
        }


def project_telemetry(
    *,
    graph: ContextGraph,
    walker: Walker,
    build: BuildReport,
    grid_cells: int = 1008,
    token_series_limit: int = 60,
    hop_span: int = 8,
) -> TelemetryProjection:
    """Convenience functional interface for TelemetryProjection.project()."""
    return TelemetryProjection.project(
        graph=graph,
        walker=walker,
        build=build,
        grid_cells=grid_cells,
        token_series_limit=token_series_limit,
        hop_span=hop_span,
    )
