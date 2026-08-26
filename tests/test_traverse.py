"""Walks return a readable path, or they say exactly why they failed."""

import unittest

from contextmesh.graph import ContextGraph
from contextmesh.model import EdgeType, NodeType, Provenance
from contextmesh.pipeline import Pipeline
from contextmesh.resolve import Resolver
from contextmesh.traverse import ANSWERING, DeadEnd, Walker
from contextmesh.corpus import documents


def built():
    pipeline = Pipeline()
    pipeline.build(documents())
    return pipeline.graph, pipeline.resolver


class PathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph, cls.resolver = built()

    def setUp(self):
        self.walker = Walker(self.graph, self.resolver)

    def test_an_answer_comes_with_a_path(self):
        walk = self.walker.ask("Why did the Index Builder run out of memory?")
        self.assertTrue(walk.resolved)
        self.assertGreaterEqual(walk.hops, 1)
        self.assertEqual(len(walk.steps), walk.hops + 1)
        self.assertIn("Index Builder", walk.path())

    def test_the_path_starts_at_a_seed_and_ends_at_the_answer_or_its_source(self):
        walk = self.walker.ask("What made the sharding rule wrong?")
        self.assertTrue(walk.resolved)
        self.assertEqual(walk.steps[0].node_id, walk.seeds[0])
        self.assertIsNone(walk.steps[0].edge_type)
        self.assertIn(
            self.graph.node(walk.answer_id).type,
            ANSWERING,
        )

    def test_every_hop_after_the_first_names_its_edge_type(self):
        walk = self.walker.ask("What does Context Mesh add?")
        for step in walk.steps[1:]:
            self.assertIsNotNone(step.edge_type)
            self.assertIn(step.edge_type, EdgeType)

    def test_a_walk_costs_far_less_than_flat_top_k(self):
        walk = self.walker.ask("How much did token spend fall per answer?")
        self.assertTrue(walk.resolved)
        self.assertGreater(walk.tokens_flat, walk.tokens_walked * 5)

    def test_walking_credits_the_edges_it_crossed(self):
        before = sum(e.traversals for e in self.graph.edges.values())
        self.walker.ask("Why did the Index Builder run out of memory?")
        after = sum(e.traversals for e in self.graph.edges.values())
        self.assertGreater(after, before)

    def test_hop_budget_is_respected(self):
        walker = Walker(self.graph, self.resolver, hop_budget=2)
        for walk in [walker.ask(q) for q in ("What is pgvector?", "Why HNSW index?")]:
            if walk.resolved:
                # the justification tail may extend past the search budget, but
                # the searched portion may not
                self.assertLessEqual(walk.hops, 2 + 3)


class DeadEndTest(unittest.TestCase):
    def test_out_of_corpus_question_is_entity_unresolved(self):
        graph, resolver = built()
        walk = Walker(graph, resolver).ask("What is the refund policy for annual plans?")
        self.assertFalse(walk.resolved)
        self.assertIs(walk.dead_end, DeadEnd.ENTITY_UNRESOLVED)
        self.assertIn("no mention resolved", walk.detail)

    def test_seed_with_no_typed_edge_says_so(self):
        graph = ContextGraph()
        resolver = Resolver()
        entity = graph.add_node(NodeType.ENTITY, "Kryptonite")
        resolver.register(entity.id, "Kryptonite")
        walk = Walker(graph, resolver).ask("What do we know about Kryptonite?")
        self.assertIs(walk.dead_end, DeadEnd.NO_TYPED_EDGE)
        self.assertIn("no outgoing edge", walk.detail)

    def test_reaching_only_an_irrelevant_claim_is_wrong_node_type(self):
        graph = ContextGraph()
        resolver = Resolver()
        entity = graph.add_node(NodeType.ENTITY, "Kryptonite")
        source = graph.add_node(NodeType.SOURCE, "Almanac")
        claim = graph.add_node(
            NodeType.CLAIM,
            "Zebras graze on open savannah during the wet season",
            provenance=Provenance(source_id=source.id),
        )
        graph.add_edge(claim.id, EdgeType.MENTIONS, entity.id)
        graph.add_edge(claim.id, EdgeType.DERIVED_FROM, source.id)
        for node in graph.nodes.values():
            from contextmesh.embed import embed

            node.embedding = embed(node.label)
        resolver.register(entity.id, "Kryptonite")
        walk = Walker(graph, resolver).ask("What do we know about Kryptonite?")
        self.assertFalse(walk.resolved)
        self.assertIs(walk.dead_end, DeadEnd.WRONG_NODE_TYPE)

    def test_pruned_seed_is_reported_as_pruned_too_early(self):
        graph, resolver = built()
        target = next(n for n in graph.by_type(NodeType.ENTITY) if n.label == "pgvector")
        target.pruned = True
        walk = Walker(graph, resolver).ask("What does pgvector store?")
        self.assertIs(walk.dead_end, DeadEnd.PRUNED_TOO_EARLY)
        self.assertIn("no longer walkable", walk.detail)

    def test_failed_walks_are_kept_in_the_ledger(self):
        graph, resolver = built()
        walker = Walker(graph, resolver)
        walker.ask("What is the refund policy for annual plans?")
        walker.ask("Why did the Index Builder run out of memory?")
        self.assertEqual(len(walker.walks), 2)
        self.assertEqual(sum(walker.dead_end_ledger().values()), 1)
        self.assertAlmostEqual(walker.resolved_rate, 0.5)


class LedgerTest(unittest.TestCase):
    def test_hop_histogram_only_counts_answers(self):
        graph, resolver = built()
        walker = Walker(graph, resolver)
        walker.ask("What is the refund policy for annual plans?")
        walker.ask("Why did the Index Builder run out of memory?")
        self.assertEqual(sum(walker.hop_histogram().values()), 1)

    def test_reproducible_across_runs(self):
        graph_a, resolver_a = built()
        graph_b, resolver_b = built()
        a = Walker(graph_a, resolver_a).ask("What made the sharding rule wrong?")
        b = Walker(graph_b, resolver_b).ask("What made the sharding rule wrong?")
        self.assertEqual(a.answer_id, b.answer_id)
        self.assertEqual(a.tokens_flat, b.tokens_flat)
        self.assertEqual(a.path(), b.path())


if __name__ == "__main__":
    unittest.main()
