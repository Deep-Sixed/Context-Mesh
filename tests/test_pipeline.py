"""CHUNK → EXTRACT → RESOLVE → LINK → EMBED → PRUNE, stage by stage."""

import unittest

from contextmesh.corpus import documents
from contextmesh.model import EdgeType, NodeType
from contextmesh.pipeline import STAGES, Document, Pipeline


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


if __name__ == "__main__":
    unittest.main()
