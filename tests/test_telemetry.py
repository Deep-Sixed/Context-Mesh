"""Tests for the Telemetry Projection Layer.

Verifies:
1. Projection completeness: exposes build, graph, walk_summary, hop_metrics,
   edge_traversals, dead_ends, token_savings, and traversal_grid.
2. Semantic authority: projections derive strictly from authoritative engine methods.
3. Determinism: repeated projections over unchanged state yield identical results.
4. Zero observer effect: taking a projection does not mutate graph, walker, or build state.
5. Invariant preservation: Walk.hops is preserved under 'hops' (no answer_depth),
   and no timing/live-event modifications are introduced.
"""

from __future__ import annotations

import dataclasses
import unittest

from contextmesh import (
    BuildReport,
    ContextGraph,
    Pipeline,
    Walker,
    documents,
    project_telemetry,
)


def _create_sample_engine() -> tuple[ContextGraph, Walker, BuildReport]:
    """Build a deterministic small engine instance for testing."""
    pipeline = Pipeline()
    docs = documents()[:3]
    build = pipeline.build(docs)

    walker = Walker(pipeline.graph, pipeline.resolver)
    # Run a few queries to populate walk and traversal history
    walker.ask("Why did the Index Builder run out of memory?")
    walker.ask("Who approved the shard count change?")
    walker.ask("An out-of-corpus question that must dead end")

    return pipeline.graph, walker, build


class TelemetryProjectionContractTest(unittest.TestCase):
    """Tests completeness and structure of the telemetry projection contract."""

    def setUp(self) -> None:
        self.graph, self.walker, self.build = _create_sample_engine()
        self.projection = project_telemetry(
            graph=self.graph,
            walker=self.walker,
            build=self.build,
        )

    def test_projection_contains_all_eight_contract_sections(self) -> None:
        self.assertIsNotNone(self.projection.build)
        self.assertIsNotNone(self.projection.graph)
        self.assertIsNotNone(self.projection.walk_summary)
        self.assertIsNotNone(self.projection.hop_metrics)
        self.assertIsNotNone(self.projection.edge_traversals)
        self.assertIsNotNone(self.projection.dead_ends)
        self.assertIsNotNone(self.projection.token_savings)
        self.assertIsNotNone(self.projection.traversal_grid)

    def test_build_metrics_derive_from_build_report(self) -> None:
        build_proj = self.projection.build
        self.assertEqual(build_proj.number, self.build.build)
        self.assertEqual(build_proj.spans_in, self.build.spans_in)
        self.assertEqual(build_proj.committed_walkable, self.build.committed_walkable)
        self.assertEqual(build_proj.dropped_at_resolve, self.build.dropped_at_resolve)
        self.assertEqual(len(build_proj.stages), len(self.build.stages))
        for proj_stage, engine_stage in zip(build_proj.stages, self.build.stages):
            self.assertEqual(proj_stage.name, engine_stage.name)
            self.assertEqual(proj_stage.admitted, engine_stage.admitted)
            self.assertEqual(proj_stage.dropped, engine_stage.dropped)

    def test_graph_metrics_derive_from_context_graph(self) -> None:
        graph_proj = self.projection.graph
        self.assertEqual(graph_proj.nodes_total, len(self.graph.nodes))
        self.assertEqual(graph_proj.nodes_live, sum(1 for n in self.graph.nodes.values() if n.live))
        self.assertEqual(graph_proj.edges_total, len(self.graph.edges))
        self.assertEqual(graph_proj.untyped_edges, 0)
        self.assertEqual(graph_proj.type_counts, self.graph.type_counts())

    def test_walk_summary_derives_from_walker(self) -> None:
        summary = self.projection.walk_summary
        self.assertEqual(summary.walk_count, len(self.walker.walks))
        resolved_count = sum(1 for w in self.walker.walks if w.resolved)
        dead_end_count = sum(1 for w in self.walker.walks if w.dead_end)
        self.assertEqual(summary.resolved_count, resolved_count)
        self.assertEqual(summary.dead_end_count, dead_end_count)
        self.assertAlmostEqual(summary.resolved_rate, self.walker.resolved_rate, places=4)

    def test_hop_metrics_preserves_hops_name_without_answer_depth(self) -> None:
        hops_proj = self.projection.hop_metrics
        self.assertEqual(hops_proj.median_hops, self.walker.median_hops())
        self.assertEqual(hops_proj.hops_histogram, self.walker.hop_histogram())
        # Assert 'hops' is preserved and 'answer_depth' is NOT introduced
        self.assertTrue(hasattr(hops_proj, "median_hops"))
        self.assertTrue(hasattr(hops_proj, "hops_histogram"))
        self.assertFalse(hasattr(hops_proj, "answer_depth"))
        self.assertFalse(hasattr(hops_proj, "median_answer_depth"))

    def test_edge_traversals_reflect_traffic_without_ledger_mutation(self) -> None:
        edge_proj = self.projection.edge_traversals
        edge_counts = self.graph.edge_counts()
        self.assertEqual(edge_proj.total, sum(edge_counts.values()))
        self.assertEqual(edge_proj.untyped, 0)

        for row in edge_proj.rows:
            expected_type = row.type
            expected_count = edge_counts.get(expected_type, 0)
            expected_traversals = sum(
                e.traversals for e in self.graph.edges.values() if e.type.value == expected_type
            )
            self.assertEqual(row.count, expected_count)
            self.assertEqual(row.traversals, expected_traversals)

    def test_dead_ends_match_terminal_failure_buckets(self) -> None:
        dead_proj = self.projection.dead_ends
        engine_dead_ends = self.walker.dead_end_ledger()
        self.assertEqual(dead_proj.counts, engine_dead_ends)
        self.assertEqual(dead_proj.total, sum(1 for w in self.walker.walks if w.dead_end))
        for row in dead_proj.rows:
            self.assertEqual(row.count, engine_dead_ends[row.reason])

    def test_token_savings_derives_from_walker_tokens(self) -> None:
        tokens_proj = self.projection.token_savings
        self.assertAlmostEqual(tokens_proj.saving, self.walker.token_saving(), places=4)
        flat_total = sum(w.tokens_flat for w in self.walker.walks) or 1
        walked_total = sum(w.tokens_walked for w in self.walker.walks)
        self.assertEqual(tokens_proj.tokens_flat_total, flat_total)
        self.assertEqual(tokens_proj.tokens_walked_total, walked_total)

    def test_traversal_grid_capacity_and_outcomes(self) -> None:
        grid_proj = self.projection.traversal_grid
        self.assertEqual(grid_proj.capacity, 1008)
        self.assertEqual(len(grid_proj.cells), len(self.walker.walks))
        for cell, walk in zip(grid_proj.cells, self.walker.walks):
            expected = 1 if walk.resolved else 0
            self.assertEqual(cell, expected)

    def test_to_dict_produces_serializable_structure(self) -> None:
        d = self.projection.to_dict()
        self.assertIsInstance(d, dict)
        self.assertIn("build", d)
        self.assertIn("graph", d)
        self.assertIn("walk_summary", d)
        self.assertIn("hop_metrics", d)
        self.assertIn("edge_traversals", d)
        self.assertIn("dead_ends", d)
        self.assertIn("token_savings", d)
        self.assertIn("traversal_grid", d)


class TelemetryProjectionDeterminismTest(unittest.TestCase):
    """Tests determinism across multiple projection invocations."""

    def test_repeated_projection_yields_identical_output(self) -> None:
        graph, walker, build = _create_sample_engine()
        proj1 = project_telemetry(graph=graph, walker=walker, build=build)
        proj2 = project_telemetry(graph=graph, walker=walker, build=build)

        self.assertEqual(proj1, proj2)
        self.assertEqual(proj1.to_dict(), proj2.to_dict())

    def test_projections_are_shallow_immutable(self) -> None:
        graph, walker, build = _create_sample_engine()
        proj = project_telemetry(graph=graph, walker=walker, build=build)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            proj.walk_summary = None  # type: ignore[misc]

        with self.assertRaises(dataclasses.FrozenInstanceError):
            proj.build.number = 999  # type: ignore[misc]

    def test_nested_collections_are_deeply_immutable(self) -> None:
        graph, walker, build = _create_sample_engine()
        proj = project_telemetry(graph=graph, walker=walker, build=build)

        # 1. GraphTypeMetrics.type_counts refuses item assignment and deletion
        with self.assertRaises(TypeError):
            proj.graph.type_counts["entity"] = 999  # type: ignore[index]
        with self.assertRaises(TypeError):
            del proj.graph.type_counts["entity"]  # type: ignore[attr-defined]

        # 2. HopMetrics.hops_histogram refuses item assignment and deletion
        with self.assertRaises(TypeError):
            proj.hop_metrics.hops_histogram[1] = 999  # type: ignore[index]
        with self.assertRaises(TypeError):
            del proj.hop_metrics.hops_histogram[1]  # type: ignore[attr-defined]

        # 3. HopMetrics.bins elements refuse item assignment
        if proj.hop_metrics.bins:
            with self.assertRaises(TypeError):
                proj.hop_metrics.bins[0]["count"] = 999  # type: ignore[index]

        # 4. DeadEndLedger.counts refuses item assignment and deletion
        with self.assertRaises(TypeError):
            proj.dead_ends.counts["no_typed_edge"] = 999  # type: ignore[index]
        with self.assertRaises(TypeError):
            del proj.dead_ends.counts["no_typed_edge"]  # type: ignore[attr-defined]

        # 5. Row structures are frozen dataclasses
        with self.assertRaises(dataclasses.FrozenInstanceError):
            proj.build.stages[0].admitted = 999  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            proj.edge_traversals.rows[0].traversals = 999  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            proj.dead_ends.rows[0].count = 999  # type: ignore[misc]
        if proj.token_savings.series:
            with self.assertRaises(dataclasses.FrozenInstanceError):
                proj.token_savings.series[0].walk = 999  # type: ignore[misc]

        # 6. Tuple collections refuse item assignment
        with self.assertRaises(TypeError):
            proj.traversal_grid.cells[0] = 999  # type: ignore[index]


class TelemetryZeroObserverEffectTest(unittest.TestCase):
    """Proves that taking a projection does NOT alter engine state."""

    def test_projecting_does_not_mutate_graph_or_walker_or_build(self) -> None:
        graph, walker, build = _create_sample_engine()

        # Record pre-projection snapshots
        graph_snapshot_before = graph.to_dict()
        node_walks_before = {n.id: n.walks for n in graph.nodes.values()}
        edge_traversals_before = {e.id: e.traversals for e in graph.edges.values()}
        walker_walks_count_before = len(walker.walks)
        build_dict_before = build.to_dict()

        # Perform multiple projections
        for _ in range(5):
            proj = project_telemetry(graph=graph, walker=walker, build=build)
            _ = proj.to_dict()

        # Verify post-projection state is completely identical
        self.assertEqual(graph.to_dict(), graph_snapshot_before)
        self.assertEqual({n.id: n.walks for n in graph.nodes.values()}, node_walks_before)
        self.assertEqual({e.id: e.traversals for e in graph.edges.values()}, edge_traversals_before)
        self.assertEqual(len(walker.walks), walker_walks_count_before)
        self.assertEqual(build.to_dict(), build_dict_before)


if __name__ == "__main__":
    unittest.main()
