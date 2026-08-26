"""GRAPH.md is the schema, so the schema is what these tests check."""

import unittest

from contextmesh.graph import ContextGraph
from contextmesh.model import EdgeType, NodeType
from contextmesh.ontology import ONTOLOGY, OntologyError, load


class OntologyFileTest(unittest.TestCase):
    def test_parses_every_declared_node_type(self):
        self.assertEqual(
            ONTOLOGY.node_types,
            {t.value for t in NodeType},
            "GRAPH.md and NodeType have drifted apart",
        )

    def test_parses_every_declared_edge_type(self):
        self.assertEqual(
            ONTOLOGY.edge_types,
            {t.value for t in EdgeType},
            "GRAPH.md and EdgeType have drifted apart",
        )

    def test_every_edge_type_has_at_least_one_legal_pair(self):
        for edge_type, pairs in ONTOLOGY.signatures.items():
            self.assertTrue(pairs, f"{edge_type} declares no legal pairs")

    def test_reload_is_stable(self):
        self.assertEqual(load().signatures, ONTOLOGY.signatures)


class TypecheckTest(unittest.TestCase):
    def setUp(self):
        self.graph = ContextGraph()
        self.source = self.graph.add_node(NodeType.SOURCE, "RFC 014")
        self.entity = self.graph.add_node(NodeType.ENTITY, "pgvector")
        self.claim = self.graph.add_node(NodeType.CLAIM, "pgvector stores embeddings")

    def test_legal_edge_is_accepted(self):
        edge = self.graph.add_edge(self.claim.id, EdgeType.MENTIONS, self.entity.id)
        self.assertIs(edge.type, EdgeType.MENTIONS)

    def test_illegal_pair_is_refused(self):
        with self.assertRaises(OntologyError):
            self.graph.add_edge(self.entity.id, EdgeType.MENTIONS, self.source.id)

    def test_unknown_endpoint_is_refused(self):
        with self.assertRaises(OntologyError):
            self.graph.add_edge(self.claim.id, EdgeType.MENTIONS, "entity:missing")

    def test_self_edge_is_refused(self):
        with self.assertRaises(OntologyError):
            self.graph.add_edge(self.claim.id, EdgeType.CITES, self.claim.id)

    def test_untyped_edges_are_structurally_impossible(self):
        self.graph.add_edge(self.claim.id, EdgeType.MENTIONS, self.entity.id)
        self.graph.add_edge(self.claim.id, EdgeType.DERIVED_FROM, self.source.id)
        self.assertEqual(self.graph.untyped_edges, 0)

    def test_duplicate_edge_merges_rather_than_multiplies(self):
        first = self.graph.add_edge(self.claim.id, EdgeType.MENTIONS, self.entity.id)
        second = self.graph.add_edge(self.claim.id, EdgeType.MENTIONS, self.entity.id)
        self.assertIs(first, second)
        self.assertEqual(len(self.graph.edges), 1)
        self.assertEqual(second.weight, 2.0)

    def test_empty_graph_is_truthy(self):
        # ``graph or ContextGraph()`` must not swap in a fresh graph.
        self.assertTrue(ContextGraph())


if __name__ == "__main__":
    unittest.main()
