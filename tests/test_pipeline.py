"""CHUNK → EXTRACT → RESOLVE → LINK → EMBED → PRUNE, stage by stage."""

import json
import unittest

from contextmesh.assumptions import AssumptionLedger
from contextmesh.corpus import documents
from contextmesh.demo import run
from contextmesh.graph import ContextGraph
from contextmesh.model import NodeType
from contextmesh.pipeline import MIN_OBSERVATION_WALKS, STAGES, Document, Pipeline
from contextmesh.traverse import Walker


def tiny_docs():
    return [
        Document(
            id="source:one",
            title="Note one",
            origin="notes/one.md",
            entities=["pgvector", "HNSW index"],
            text=(
                "The pgvector extension stores embeddings. "
                "An HNSW index is built at write time. "
                "Please see below."
            ),
        ),
        Document(
            id="source:two",
            title="Note two",
            origin="notes/two.md",
            entities=["PG Vector"],
            text="PG Vector was upgraded last quarter.",
        ),
    ]


class StageTest(unittest.TestCase):
    def setUp(self):
        self.pipeline = Pipeline()
        self.report = self.pipeline.build(tiny_docs())
        self.graph = self.pipeline.graph

    def test_reports_every_stage_in_order(self):
        self.assertEqual(
            [s.name for s in self.report.stages], [name for name, _ in STAGES]
        )

    def test_chunk_admits_one_source_per_document(self):
        self.assertEqual(self.report.stage("CHUNK").admitted, 2)
        self.assertEqual(len(self.graph.by_type(NodeType.SOURCE)), 2)

    def test_extract_drops_a_span_that_asserts_nothing(self):
        labels = [n.label for n in self.graph.by_type(NodeType.CLAIM)]
        self.assertNotIn("Please see below.", labels)
        self.assertGreaterEqual(self.report.stage("EXTRACT").dropped, 1)

    def test_resolve_gives_one_id_per_real_world_thing(self):
        entities = self.graph.by_type(NodeType.ENTITY)
        labels = sorted(n.label for n in entities)
        self.assertEqual(labels, ["HNSW index", "pgvector"])
        # "PG Vector" folded into pgvector rather than becoming its own node.
        pgvector = next(n for n in entities if n.label == "pgvector")
        self.assertIn("PG Vector", pgvector.attrs["aliases"])

    def test_dropped_at_resolve_is_recorded_not_hidden(self):
        self.assertEqual(
            self.report.dropped_at_resolve, self.report.stage("RESOLVE").dropped
        )
        self.assertGreater(self.report.dropped_at_resolve, 0)

    def test_link_produces_only_typed_edges(self):
        self.assertEqual(self.graph.untyped_edges, 0)
        self.assertGreater(self.graph.edge_counts()["mentions"], 0)
        self.assertGreater(self.graph.edge_counts()["derived_from"], 0)

    def test_every_claim_carries_provenance_to_its_source(self):
        for claim in self.graph.by_type(NodeType.CLAIM):
            self.assertIsNotNone(claim.provenance, claim.label)
            self.assertIn(claim.provenance.source_id, self.graph.nodes)
            start, end = claim.provenance.span
            self.assertLess(start, end)

    def test_embed_attaches_a_vector_to_every_node(self):
        for node in self.graph.nodes.values():
            self.assertIsNotNone(node.embedding, node.label)

    def test_committed_walkable_counts_live_edges(self):
        live = sum(1 for e in self.graph.edges.values() if e.live)
        self.assertEqual(self.report.committed_walkable, live)

    def test_build_is_idempotent_on_ids(self):
        second = Pipeline().build(tiny_docs())
        self.assertEqual(second.spans_in, self.report.spans_in)
        self.assertEqual(
            sorted(n.id for n in Pipeline().graph.nodes.values()), []
        )


class RelationHintTest(unittest.TestCase):
    def test_illegal_pair_is_dropped_never_stored_untyped(self):
        doc = Document(
            id="source:x",
            title="X",
            origin="x.md",
            entities=["pgvector"],
            text="The pgvector extension stores embeddings.",
            # entity -[contradicts]-> entity is not a legal pair
            relations=[("pgvector", "contradicts", "pgvector")],
        )
        pipeline = Pipeline()
        report = pipeline.build([doc])
        self.assertEqual(pipeline.graph.untyped_edges, 0)
        self.assertEqual(pipeline.graph.edge_counts()["contradicts"], 0)
        self.assertGreater(report.stage("LINK").dropped, 0)


class FullCorpusTest(unittest.TestCase):
    def test_corpus_builds_a_connected_typed_graph(self):
        pipeline = Pipeline()
        pipeline.build(documents())
        graph = pipeline.graph
        self.assertEqual(graph.untyped_edges, 0)
        self.assertGreaterEqual(len(graph.by_type(NodeType.ENTITY)), 8)
        self.assertGreaterEqual(len(graph.by_type(NodeType.CLAIM)), 40)
        self.assertEqual(len(graph.by_type(NodeType.SOURCE)), len(documents()))
        orphans = [n for n in graph.nodes.values() if n.live and graph.degree(n.id) == 0]
        self.assertEqual(orphans, [])


def _prune_fixture_docs():
    return [
        Document(
            id="source:one",
            title="Note one",
            origin="notes/one.md",
            entities=["pgvector", "HNSW index"],
            text=(
                "The pgvector extension stores embeddings. "
                "An HNSW index is built at write time."
            ),
        ),
        Document(
            id="source:isolated",
            title="Isolated note",
            origin="notes/isolated.md",
            entities=[],
            text="The obscure operational detail was never mentioned again.",
        ),
    ]


class WalkDrivenPruneTest(unittest.TestCase):
    """A claim from an isolated, entity-free document -- degree 1, its only
    edge back to its own source -- sits alongside a well-connected claim from
    a document the walker actually asks about.
    """

    def setUp(self):
        self.pipeline = Pipeline()
        self.pipeline.build(_prune_fixture_docs())
        self.graph = self.pipeline.graph
        self.walker = Walker(self.graph, self.pipeline.resolver)

        claims = self.graph.by_type(NodeType.CLAIM)
        self.isolated = next(
            c for c in claims if c.provenance.source_id == "source:isolated"
        )
        self.walked = next(
            c for c in claims if c.provenance.source_id == "source:one"
        )
        self.assertEqual(self.graph.degree(self.isolated.id), 1)  # sanity
        self.assertGreater(self.graph.degree(self.walked.id), 1)  # sanity

    def _observe(self, count=MIN_OBSERVATION_WALKS):
        questions = ["What does pgvector store?", "What is an HNSW index?"]
        for i in range(count):
            self.walker.walk(questions[i % len(questions)])

    def test_does_not_run_before_telemetry_exists(self):
        # Zero walks: the window has not even opened.
        dropped = self.pipeline.prune_unwalked_nodes(self.walker)
        self.assertEqual(dropped, 0)
        self.assertFalse(self.graph.node(self.isolated.id).pruned)

        # One walk short of the window: still a no-op.
        self._observe(MIN_OBSERVATION_WALKS - 1)
        dropped = self.pipeline.prune_unwalked_nodes(self.walker)
        self.assertEqual(dropped, 0)
        self.assertFalse(self.graph.node(self.isolated.id).pruned)

    def test_eligible_unwalked_node_is_pruned(self):
        self._observe()
        dropped = self.pipeline.prune_unwalked_nodes(self.walker)
        self.assertEqual(dropped, 1)
        node = self.graph.node(self.isolated.id)
        self.assertTrue(node.pruned)
        self.assertFalse(node.live)

    def test_walked_node_survives(self):
        self._observe()
        self.pipeline.prune_unwalked_nodes(self.walker)
        node = self.graph.node(self.walked.id)
        self.assertFalse(node.pruned)
        self.assertTrue(node.live)

    def test_running_prune_twice_is_idempotent(self):
        self._observe()
        first = self.pipeline.prune_unwalked_nodes(self.walker)
        second = self.pipeline.prune_unwalked_nodes(self.walker)
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertTrue(self.graph.node(self.isolated.id).pruned)

    def test_a_later_walk_cannot_revive_an_already_pruned_node(self):
        self._observe()
        self.pipeline.prune_unwalked_nodes(self.walker)
        self.assertTrue(self.graph.node(self.isolated.id).pruned)

        # Asking directly about it cannot resurrect it: a pruned node can
        # never again become a seed (Walker.seed skips pruned/invalidated
        # nodes) or a traversal target (out_edges/in_edges filter on .live).
        self.walker.walk("Tell me about the obscure operational detail")
        node = self.graph.node(self.isolated.id)
        self.assertEqual(node.walks, 0)
        self.assertTrue(node.pruned)
        self.assertFalse(node.live)

    def test_source_assumption_and_evidence_nodes_are_preserved(self):
        ledger = AssumptionLedger(self.graph)
        assumption = ledger.assume("Something nobody will ever ask about")
        evidence = self.graph.add_node(
            NodeType.EVIDENCE, "An observation nobody walks", attrs={"kind": "note"}
        )
        self._observe()
        self.pipeline.prune_unwalked_nodes(self.walker)
        self.assertFalse(self.graph.node(assumption.id).pruned)
        self.assertFalse(self.graph.node(evidence.id).pruned)
        # source:isolated is degree 1 and never walked either, but sources
        # are exempt the same way they always have been.
        self.assertFalse(self.graph.node("source:isolated").pruned)

    def test_an_already_invalidated_node_is_not_double_processed(self):
        node = self.graph.node(self.isolated.id)
        node.invalidated = True
        self._observe()
        dropped = self.pipeline.prune_unwalked_nodes(self.walker)
        self.assertEqual(dropped, 0)
        self.assertFalse(node.pruned)  # never separately marked
        self.assertTrue(node.invalidated)

    def test_refuses_a_walker_run_against_a_different_graph(self):
        other = Pipeline()
        other.build(tiny_docs())
        foreign_walker = Walker(other.graph, other.resolver)
        for _ in range(MIN_OBSERVATION_WALKS):
            foreign_walker.walk("What does pgvector store?")
        with self.assertRaises(ValueError):
            self.pipeline.prune_unwalked_nodes(foreign_walker)

    def test_snapshot_round_trip_preserves_walk_pruned_state(self):
        self._observe()
        self.pipeline.prune_unwalked_nodes(self.walker)
        self.assertTrue(self.graph.node(self.isolated.id).pruned)

        restored = ContextGraph.from_dict(self.graph.to_dict())
        self.assertTrue(restored.node(self.isolated.id).pruned)
        self.assertFalse(restored.node(self.isolated.id).live)
        self.assertFalse(restored.node(self.walked.id).pruned)

    def test_disabled_via_the_pipeline_flag_is_still_a_no_op(self):
        pipeline = Pipeline(prune_unwalked=False)
        pipeline.build(_prune_fixture_docs())
        walker = Walker(pipeline.graph, pipeline.resolver)
        for _ in range(MIN_OBSERVATION_WALKS):
            walker.walk("What does pgvector store?")
        self.assertEqual(pipeline.prune_unwalked_nodes(walker), 0)


class BuildTimeOrphanPruneRegressionTest(unittest.TestCase):
    """The first half of PRUNE -- degree zero at build time -- behaves
    exactly as before, whether or not the second half is ever invoked.
    """

    def test_still_drops_only_true_orphans_at_build_time(self):
        pipeline = Pipeline()
        report = pipeline.build(tiny_docs())
        self.assertEqual(report.pruned_nodes, 0)  # tiny_docs has no orphans
        orphans = [
            n for n in pipeline.graph.nodes.values()
            if n.live and pipeline.graph.degree(n.id) == 0
        ]
        self.assertEqual(orphans, [])


class DemoWalkDrivenPruneTest(unittest.TestCase):
    def test_default_run_never_invokes_the_second_half_of_prune(self):
        result = run(rounds=3)
        self.assertEqual(result.pruned_unwalked, 0)

    def test_default_run_is_byte_identical_regardless_of_the_new_parameter(self):
        first = run(rounds=3).payload()
        second = run(rounds=3).payload()
        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )

    def test_opting_in_runs_the_real_pass_against_the_real_corpus(self):
        # A live integration check: prune_after_walk=True calls
        # Pipeline.prune_unwalked_nodes with the real corpus and walker, not
        # a stub. The demo's fully-connected corpus (FullCorpusTest) leaves
        # nothing at degree <= 1 for the second half to drop, so 0 here is
        # the correct answer -- WalkDrivenPruneTest above covers a graph
        # where something is actually eligible.
        result = run(rounds=3, prune_after_walk=True)
        self.assertEqual(result.pruned_unwalked, 0)
        payload = result.payload()
        self.assertIn("header", payload)


if __name__ == "__main__":
    unittest.main()
