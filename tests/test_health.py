"""Graph health and the dashboard payload."""

import json
import unittest

from contextmesh.demo import run
from contextmesh.graph import ContextGraph
from contextmesh.health import check, report
from contextmesh.metrics import LEDGER_EDGES, snapshot
from contextmesh.model import EdgeType, NodeType, Provenance
from contextmesh.pipeline import Pipeline
from contextmesh.corpus import documents
from contextmesh.resolve import Resolver
from contextmesh.traverse import DeadEnd, Walker


class SignalTest(unittest.TestCase):
    def test_untyped_edges_is_always_reported_and_always_zero(self):
        graph = ContextGraph()
        signal = next(s for s in check(graph) if s.kind == "untyped_edges")
        self.assertEqual(signal.count, 0)
        self.assertEqual(signal.severity, "info")

    def test_orphan_is_reported(self):
        graph = ContextGraph()
        graph.add_node(NodeType.ENTITY, "Nobody links to me")
        kinds = {s.kind for s in check(graph)}
        self.assertIn("orphans", kinds)

    def test_claim_without_provenance_is_an_error(self):
        graph = ContextGraph()
        graph.add_node(NodeType.SOURCE, "Doc")
        entity = graph.add_node(NodeType.ENTITY, "Thing")
        claim = graph.add_node(NodeType.CLAIM, "Thing is fine")
        graph.add_edge(claim.id, EdgeType.MENTIONS, entity.id)
        signals = {s.kind: s for s in check(graph)}
        self.assertIn("provenance_gap", signals)
        self.assertEqual(signals["provenance_gap"].severity, "error")

    def test_prose_that_matched_nothing_is_not_a_health_signal(self):
        resolver = Resolver()
        resolver.register("entity:a", "pgvector")
        resolver.resolve("quarterly margin target")
        signals = {s.kind: s for s in check(ContextGraph(), resolver)}
        self.assertNotIn("unresolved_entities", signals)

    def test_near_miss_mentions_are_surfaced(self):
        resolver = Resolver()
        resolver.register("entity:a", "HNSW index")
        resolver.resolve("index")  # scores against HNSW index, below threshold
        signals = {s.kind: s for s in check(ContextGraph(), resolver)}
        self.assertIn("unresolved_entities", signals)
        self.assertEqual(signals["unresolved_entities"].count, 1)
        self.assertIn("HNSW index", signals["unresolved_entities"].items[0])

    def test_dead_ends_are_broken_out_by_reason(self):
        pipeline = Pipeline()
        pipeline.build(documents())
        walker = Walker(pipeline.graph, pipeline.resolver)
        walker.ask("What is the refund policy for annual plans?")
        signals = {s.kind: s for s in check(pipeline.graph, pipeline.resolver, walker)}
        self.assertIn("dead_ends", signals)
        self.assertIn(DeadEnd.ENTITY_UNRESOLVED.value, signals["dead_ends"].detail)

    def test_report_summarises_worst_severity(self):
        graph = ContextGraph()
        graph.add_node(NodeType.ENTITY, "Orphan")
        summary = report(graph)
        self.assertEqual(summary["status"], "warn")
        self.assertEqual(summary["nodes_live"], 1)


class SnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run(rounds=3)
        cls.payload = cls.result.payload()

    def test_payload_is_json_serialisable(self):
        json.dumps(self.payload)

    def test_header_counts_live_nodes_only(self):
        live = sum(1 for n in self.result.graph.nodes.values() if n.live)
        self.assertEqual(self.payload["header"]["nodes_resolved"], live)

    def test_four_display_types_in_video_order(self):
        self.assertEqual(
            [row["type"] for row in self.payload["node_types"]],
            ["entity", "claim", "source", "decision"],
        )

    def test_graph_payload_edges_point_at_real_node_indices(self):
        nodes = self.payload["graph"]["nodes"]
        for edge in self.payload["graph"]["edges"]:
            self.assertLess(edge["s"], len(nodes))
            self.assertLess(edge["d"], len(nodes))

    def test_graph_payload_excludes_invalidated_nodes(self):
        ids = {n["id"] for n in self.payload["graph"]["nodes"]}
        for node_id in self.result.invalidation.invalidated:
            self.assertNotIn(node_id, ids)

    def test_edge_ledger_rows_match_the_video(self):
        self.assertEqual(
            [row["type"] for row in self.payload["edge_ledger"]["rows"]],
            [e.value for e in LEDGER_EDGES],
        )
        self.assertEqual(self.payload["edge_ledger"]["untyped"], 0)

    def test_hop_budget_bins_cover_one_to_eight(self):
        bins = self.payload["hop_budget"]["bins"]
        self.assertEqual([b["hops"] for b in bins], list(range(1, 9)))
        self.assertEqual(
            sum(b["count"] for b in bins),
            sum(1 for w in self.result.walker.walks if w.resolved),
        )

    def test_dead_end_rows_cover_every_reason(self):
        self.assertEqual(
            [row["reason"] for row in self.payload["dead_ends"]["rows"]],
            [r.value for r in DeadEnd],
        )

    def test_traversal_grid_is_one_cell_per_walk(self):
        grid = self.payload["traversal_grid"]
        self.assertEqual(len(grid["cells"]), min(len(self.result.walker.walks), 1008))
        self.assertTrue(set(grid["cells"]) <= {0, 1})

    def test_walk_vs_flat_saving_is_a_fraction(self):
        saving = self.payload["walk_vs_flat"]["saving"]
        self.assertGreater(saving, 0.5)
        self.assertLess(saving, 1.0)

    def test_invalidation_report_is_included(self):
        self.assertIn("invalidation", self.payload)
        self.assertGreater(self.payload["invalidation"]["preserved_count"], 0)

    def test_ontology_panel_reads_the_real_schema(self):
        self.assertEqual(self.payload["ontology"]["file"], "GRAPH.md")
        self.assertEqual(
            self.payload["ontology"]["edge_types"], len(self.result.graph.ontology.edge_types)
        )


if __name__ == "__main__":
    unittest.main()
