"""Entity resolution: one id per real-world thing, and a recorded drop otherwise."""

import unittest

from contextmesh.resolve import Resolver, acronym, normalise, similarity


class NormaliseTest(unittest.TestCase):
    def test_strips_case_punctuation_and_articles(self):
        self.assertEqual(normalise("The  Retrieval-Layer!"), "retrieval layer")

    def test_strips_corporate_forms_but_keeps_versions(self):
        self.assertEqual(normalise("Acme Corp"), "acme")
        # A version is part of the identity, not noise to be tidied away.
        self.assertEqual(normalise("v2 encoder"), "v2 encoder")

    def test_acronym_needs_more_than_one_word(self):
        self.assertEqual(acronym("Hierarchical Navigable Small World"), "hnsw")
        self.assertEqual(acronym("pgvector"), "")


class SimilarityTest(unittest.TestCase):
    def test_generic_single_token_does_not_carry_containment(self):
        # "index" alone must not swallow "HNSW index".
        self.assertLess(similarity("index", "HNSW index"), 0.62)

    def test_distinctive_single_token_does_carry_containment(self):
        self.assertGreaterEqual(similarity("the pgvector extension", "pgvector"), 0.62)

    def test_versions_of_the_same_thing_stay_apart(self):
        self.assertLess(similarity("v2 encoder", "v3 encoder"), 0.62)

    def test_a_name_is_not_reduced_to_its_head_noun(self):
        self.assertLess(similarity("top-k retrieval", "Retrieval Service"), 0.62)

    def test_unrelated_strings_score_zero(self):
        self.assertEqual(similarity("payroll rotation", "pgvector"), 0.0)


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.resolver = Resolver()
        self.resolver.register("entity:pgv", "pgvector", ["pg vector", "Postgres vector extension"])
        self.resolver.register("entity:hnsw", "HNSW index")

    def test_alias_table_hit_is_exact(self):
        record = self.resolver.resolve("PG Vector")
        self.assertEqual(record.canonical_id, "entity:pgv")
        self.assertEqual(record.score, 1.0)
        self.assertEqual(record.reason, "alias table")

    def test_scored_match_is_learned_for_next_time(self):
        first = self.resolver.resolve("the pgvector extension")
        self.assertEqual(first.canonical_id, "entity:pgv")
        self.assertEqual(first.reason, "scored match")
        second = self.resolver.resolve("the pgvector extension")
        self.assertEqual(second.reason, "alias table")

    def test_unmatched_mention_is_dropped_and_recorded(self):
        record = self.resolver.resolve("quarterly margin target")
        self.assertFalse(record.resolved)
        self.assertIn(record, self.resolver.log)
        self.assertEqual(self.resolver.dropped_count, 1)

    def test_generic_word_does_not_collapse_into_an_entity(self):
        self.assertFalse(self.resolver.resolve("index").resolved)

    def test_unresolved_is_distinct_not_a_traffic_count(self):
        for _ in range(5):
            self.resolver.resolve("payroll")
        self.assertEqual(len(self.resolver.unresolved()), 1)
        self.assertEqual(self.resolver.dropped_count, 5)

    def test_collapsed_reports_surface_forms(self):
        self.resolver.resolve("pgvector")
        self.resolver.resolve("PG Vector")
        self.assertEqual(
            sorted(self.resolver.collapsed()["entity:pgv"]), ["PG Vector", "pgvector"]
        )


if __name__ == "__main__":
    unittest.main()
