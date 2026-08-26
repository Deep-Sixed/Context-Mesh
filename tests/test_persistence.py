"""A snapshot has to restore the same graph, not a graph that looks the same.

Three things this suite is built around:

1. **Insertion order is behaviour.** The walker's frontier is a heap whose
   tie-breaker is an insertion counter, and expansions arrive in adjacency
   order, so two equal-cost branches are decided by which edge was added first.
   `OrderingTest` builds that case deliberately and shows the answer flips —
   which is why `to_dict` emits records in insertion order and never sorts them.

2. **A snapshot is untrusted input.** Every edge is restored through
   `add_edge`, so `GRAPH.md` typechecks a file exactly as it would a live
   write. `IntegrityTest` corrupts a good snapshot one way at a time and
   requires each to be refused.

3. **Round-tripping is not the same as behaving identically.** `BehaviourTest`
   compares blast radius, the preserved complement, health, and 120 walks
   including their dead-end classifications.
"""

import json
import math
import tempfile
import unittest
from pathlib import Path

from contextmesh.assumptions import AssumptionLedger
from contextmesh.demo import questions, run
from contextmesh.graph import (
    SNAPSHOT_SCHEMA,
    SNAPSHOT_VERSION,
    ContextGraph,
    SnapshotError,
)
from contextmesh.health import report as health_report
from contextmesh.model import AssumptionStatus, EdgeType, NodeType, Provenance
from contextmesh.ontology import OntologyError
from contextmesh.resolve import Resolver
from contextmesh.traverse import Walker

_DEMO = None


def demo_graph(rounds=4):
    """Built once; every test that only reads it shares the same instance."""
    global _DEMO
    if _DEMO is None:
        _DEMO = run(rounds=rounds)
    return _DEMO


def resolver_for(graph):
    """Rebuild a resolver from the graph's entities.

    The resolver is not part of the graph snapshot — restoring it is PR #6's
    problem. Rebuilding one here lets the walk comparisons run against the
    reloaded graph without pretending that gap is closed.
    """
    resolver = Resolver()
    for node in graph.by_type(NodeType.ENTITY):
        resolver.register(node.id, node.label)
    return resolver


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.original = demo_graph().graph
        self.restored = ContextGraph.from_dict(self.original.to_dict())

    def test_the_snapshot_round_trips_exactly(self):
        self.assertEqual(self.restored.to_dict(), self.original.to_dict())

    def test_through_an_actual_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.json"
            self.original.save_json(path)
            reloaded = ContextGraph.load_json(path)
        self.assertEqual(reloaded.to_dict(), self.original.to_dict())

    def test_the_file_is_valid_json_a_strict_parser_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.json"
            self.original.save_json(path)
            text = path.read_text(encoding="utf-8")
        def refuse(constant):
            raise AssertionError(f"non-JSON constant in the file: {constant}")
        json.loads(text, parse_constant=refuse)
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)

    def test_it_is_schema_versioned(self):
        payload = self.original.to_dict()
        self.assertEqual(payload["schema"], SNAPSHOT_SCHEMA)
        self.assertEqual(payload["version"], SNAPSHOT_VERSION)

    def test_build_counter_survives(self):
        self.assertEqual(self.restored.build, self.original.build)

    def test_every_embedding_survives_exactly(self):
        checked = 0
        for node_id, before in self.original.nodes.items():
            after = self.restored.nodes[node_id]
            self.assertEqual(after.embedding, before.embedding, node_id)
            if before.embedding is not None:
                self.assertEqual(len(after.embedding), len(before.embedding))
                checked += 1
        self.assertGreater(checked, 0, "no embeddings in the graph to check")

    def test_provenance_survives_including_the_span_tuple(self):
        checked = 0
        for node_id, before in self.original.nodes.items():
            after = self.restored.nodes[node_id]
            if before.provenance is None:
                self.assertIsNone(after.provenance, node_id)
                continue
            self.assertEqual(after.provenance.to_dict(), before.provenance.to_dict())
            self.assertEqual(after.provenance.source_id, before.provenance.source_id)
            # A JSON list must come back as the tuple it was, or an otherwise
            # identical graph compares unequal.
            if before.provenance.span is not None:
                self.assertIsInstance(after.provenance.span, tuple)
            checked += 1
        self.assertGreater(checked, 0)

    def test_telemetry_survives(self):
        for node_id, before in self.original.nodes.items():
            self.assertEqual(self.restored.nodes[node_id].walks, before.walks, node_id)
        for edge_id, before in self.original.edges.items():
            self.assertEqual(
                self.restored.edges[edge_id].traversals, before.traversals, edge_id
            )

    def test_pruned_and_invalidated_state_survives(self):
        for node_id, before in self.original.nodes.items():
            after = self.restored.nodes[node_id]
            self.assertEqual((after.pruned, after.invalidated),
                             (before.pruned, before.invalidated), node_id)
        for edge_id, before in self.original.edges.items():
            self.assertEqual(
                self.restored.edges[edge_id].invalidated, before.invalidated, edge_id
            )
        self.assertTrue(
            any(n.invalidated for n in self.original.nodes.values()),
            "the demo should leave invalidated work to restore",
        )

    def test_assumption_lifecycle_survives(self):
        self.assertEqual(sorted(self.restored.assumptions), sorted(self.original.assumptions))
        for aid, before in self.original.assumptions.items():
            self.assertEqual(self.restored.assumptions[aid].to_dict(), before.to_dict())
        statuses = {a.status for a in self.restored.assumptions.values()}
        self.assertIn(AssumptionStatus.REJECTED, statuses)

    def test_the_adjacency_indexes_are_rebuilt_not_copied(self):
        payload = self.original.to_dict()
        self.assertNotIn("_out", payload)
        self.assertNotIn("_in", payload)
        self.assertNotIn("_edge_key", payload)
        for node_id in self.original.nodes:
            self.assertEqual(
                [e.id for e in self.restored.out_edges(node_id, live_only=False)],
                [e.id for e in self.original.out_edges(node_id, live_only=False)],
                node_id,
            )
            self.assertEqual(
                [e.id for e in self.restored.in_edges(node_id, live_only=False)],
                [e.id for e in self.original.in_edges(node_id, live_only=False)],
                node_id,
            )


class OrderingTest(unittest.TestCase):
    """Adjacency order decides equal-cost answers, so the snapshot preserves it."""

    @staticmethod
    def fixture(bravo_first=False):
        graph = ContextGraph()
        graph.build = 1
        source = graph.add_node(NodeType.SOURCE, "Capacity note")
        entity = graph.add_node(NodeType.ENTITY, "Index Builder")
        alpha = graph.add_node(
            NodeType.CLAIM, "Index Builder alpha outcome recorded",
            provenance=Provenance(source_id=source.id, span=(0, 4)),
        )
        bravo = graph.add_node(
            NodeType.CLAIM, "Index Builder bravo outcome recorded",
            provenance=Provenance(source_id=source.id, span=(5, 9)),
        )
        for claim in ((bravo, alpha) if bravo_first else (alpha, bravo)):
            graph.add_edge(claim.id, EdgeType.MENTIONS, entity.id)
            graph.add_edge(claim.id, EdgeType.DERIVED_FROM, source.id)
        return graph, alpha.id, bravo.id

    QUESTION = "What do we know about Index Builder?"

    def answer(self, graph):
        return Walker(graph, resolver_for(graph)).ask(self.QUESTION)

    def test_edge_order_really_does_decide_an_equal_cost_answer(self):
        # If this ever stops being true the snapshot may sort its arrays. Until
        # then, sorting would change answers without changing any fact.
        first, alpha, _ = self.fixture(bravo_first=False)
        second, _, bravo = self.fixture(bravo_first=True)
        one, two = self.answer(first), self.answer(second)
        self.assertAlmostEqual(one.score, two.score, places=9,
                               msg="fixture is not an equal-cost tie any more")
        self.assertNotEqual(one.answer_id, two.answer_id,
                            "fixture no longer demonstrates order sensitivity")
        self.assertEqual(one.answer_id, alpha)
        self.assertEqual(two.answer_id, bravo)

    def test_the_snapshot_does_not_sort_its_records(self):
        graph, _, _ = self.fixture(bravo_first=True)
        payload = graph.to_dict()
        for key in ("nodes", "edges", "assumptions"):
            ids = [row["id"] for row in payload[key]]
            if len(ids) > 1:
                self.assertNotEqual(ids, sorted(ids), f"{key} came out sorted")

    def test_a_round_trip_keeps_the_same_answer_and_the_same_path(self):
        for bravo_first in (False, True):
            with self.subTest(bravo_first=bravo_first):
                graph, _, _ = self.fixture(bravo_first)
                before = self.answer(graph)
                after = self.answer(ContextGraph.from_dict(graph.to_dict()))
                self.assertEqual(after.answer_id, before.answer_id)
                self.assertEqual(after.path(), before.path())
                self.assertAlmostEqual(after.score, before.score, places=9)

    def test_a_file_round_trip_keeps_it_too(self):
        graph, _, _ = self.fixture(bravo_first=True)
        before = self.answer(graph)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.json"
            graph.save_json(path)
            after = self.answer(ContextGraph.load_json(path))
        self.assertEqual(after.answer_id, before.answer_id)
        self.assertEqual(after.path(), before.path())


class IntegrityTest(unittest.TestCase):
    """One corruption at a time, against a snapshot that is otherwise good."""

    def setUp(self):
        graph = ContextGraph()
        graph.build = 3
        source = graph.add_node(NodeType.SOURCE, "Postmortem")
        entity = graph.add_node(NodeType.ENTITY, "Rebuild")
        claim = graph.add_node(NodeType.CLAIM, "Rebuild exhausted memory",
                               provenance=Provenance(source_id=source.id))
        graph.add_edge(claim.id, EdgeType.MENTIONS, entity.id)
        graph.add_edge(claim.id, EdgeType.DERIVED_FROM, source.id)
        ledger = AssumptionLedger(graph)
        assumption = ledger.assume("Shards stay under four gigabytes")
        decision = graph.add_node(NodeType.DECISION, "Rebuild in partitions",
                                  attrs={"rationale": "bounded memory"})
        graph.add_edge(decision.id, EdgeType.DEPENDS_ON, assumption.id)
        graph.add_edge(decision.id, EdgeType.PRODUCES, entity.id)
        self.graph = graph
        self.assumption_id = assumption.id
        self.good = graph.to_dict()

    def payload(self):
        return json.loads(json.dumps(self.good))

    def refuses(self, payload, expected=SnapshotError):
        with self.assertRaises(expected):
            ContextGraph.from_dict(payload)

    def test_the_untouched_snapshot_loads(self):
        self.assertEqual(ContextGraph.from_dict(self.good).to_dict(), self.good)

    # ── container ────────────────────────────────────────────────────────
    def test_wrong_schema(self):
        p = self.payload()
        p["schema"] = "something.else"
        self.refuses(p)

    def test_missing_schema(self):
        p = self.payload()
        del p["schema"]
        self.refuses(p)

    def test_future_version(self):
        p = self.payload()
        p["version"] = SNAPSHOT_VERSION + 1
        self.refuses(p)

    def test_not_an_object(self):
        self.refuses([], SnapshotError)

    # ── ontology ─────────────────────────────────────────────────────────
    def test_an_illegal_pair_is_rejected_by_graph_md_on_load(self):
        p = self.payload()
        edge = next(e for e in p["edges"] if e["type"] == "mentions")
        edge["type"] = "resolves_to"      # claim -[resolves_to]-> entity is illegal
        edge["id"] = "edge:hand-written"
        self.refuses(p, OntologyError)

    def test_unknown_edge_type(self):
        p = self.payload()
        p["edges"][0]["type"] = "teleports_to"
        self.refuses(p, ValueError)

    def test_unknown_node_type(self):
        p = self.payload()
        p["nodes"][0]["type"] = "wormhole"
        self.refuses(p, ValueError)

    def test_edge_pointing_at_a_missing_node(self):
        p = self.payload()
        p["edges"][0]["dst"] = "entity:not-here"
        self.refuses(p, OntologyError)

    def test_self_edge(self):
        p = self.payload()
        p["edges"][0]["dst"] = p["edges"][0]["src"]
        self.refuses(p, OntologyError)

    # ── identity ─────────────────────────────────────────────────────────
    def test_duplicate_node_id(self):
        p = self.payload()
        p["nodes"].append(dict(p["nodes"][0]))
        self.refuses(p)

    def test_duplicate_edge_id(self):
        p = self.payload()
        clone = dict(p["edges"][1])
        clone["id"] = p["edges"][0]["id"]
        p["edges"].append(clone)
        self.refuses(p)

    def test_duplicate_relationship_under_a_different_id(self):
        """Exercises the relationship guard specifically, not the id guard.

        Appending a row with the *same* id trips the duplicate-id check first,
        so it proves nothing about this guard. A second row carrying the same
        (src, type, dst) under a different id is the case that reaches
        ``add_edge`` — which would read it as another observation and add to
        the weight, quietly changing a restored graph.
        """
        p = self.payload()
        clone = dict(p["edges"][0])
        clone["id"] = "edge:a-different-id"
        p["edges"].append(clone)
        with self.assertRaises(SnapshotError) as caught:
            ContextGraph.from_dict(p)
        self.assertIn("duplicate relationship", str(caught.exception))

    def test_a_restored_edge_keeps_its_recorded_weight(self):
        # The consequence the guard above exists to prevent.
        edge = next(iter(self.graph.edges.values()))
        edge.weight = 3.5
        restored = ContextGraph.from_dict(self.graph.to_dict())
        self.assertEqual(restored.edges[edge.id].weight, 3.5)

    def test_an_edge_id_this_build_would_not_derive(self):
        p = self.payload()
        p["edges"][0]["id"] = "edge:invented"
        self.refuses(p)

    # ── assumptions ──────────────────────────────────────────────────────
    def test_assumption_record_without_a_node(self):
        p = self.payload()
        p["nodes"] = [n for n in p["nodes"] if n["id"] != self.assumption_id]
        p["edges"] = [e for e in p["edges"] if self.assumption_id not in (e["src"], e["dst"])]
        self.refuses(p)

    def test_assumption_node_without_a_record(self):
        p = self.payload()
        p["assumptions"] = []
        self.refuses(p)

    def test_assumption_statement_disagreeing_with_its_node_label(self):
        p = self.payload()
        for row in p["assumptions"]:
            row["statement"] = "something the node does not say"
        self.refuses(p)

    def test_an_assumption_record_pointing_at_a_non_assumption_node(self):
        p = self.payload()
        claim = next(n for n in p["nodes"] if n["type"] == "claim")
        p["assumptions"][0]["id"] = claim["id"]
        p["assumptions"][0]["statement"] = claim["label"]
        self.refuses(p)

    # ── non-JSON values ──────────────────────────────────────────────────
    def test_saving_a_nan_is_refused_rather_than_written(self):
        self.graph.node(next(iter(self.graph.nodes))).attrs["score"] = float("nan")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self.graph.save_json(Path(tmp) / "g.json")

    def test_loading_a_file_containing_nan_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.json"
            self.graph.save_json(path)
            text = path.read_text(encoding="utf-8")
            text = text.replace('"build": 3', '"build": NaN', 1)
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(SnapshotError):
                ContextGraph.load_json(path)

    def test_loading_a_file_containing_infinity_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.json"
            self.graph.save_json(path)
            text = path.read_text(encoding="utf-8").replace('"build": 3', '"build": Infinity', 1)
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(SnapshotError):
                ContextGraph.load_json(path)


class BehaviourTest(unittest.TestCase):
    """Restoring the records is necessary; behaving the same is the point."""

    def setUp(self):
        self.original = demo_graph().graph
        self.restored = ContextGraph.from_dict(self.original.to_dict())

    def test_the_same_blast_radius_and_the_same_preserved_complement(self):
        for assumption_id in sorted(self.original.assumptions):
            with self.subTest(assumption_id=assumption_id):
                a = AssumptionLedger(self.original).blast_radius(assumption_id)
                b = AssumptionLedger(self.restored).blast_radius(assumption_id)
                self.assertEqual(b, a)
                def preserved(graph, radius):
                    return sorted(
                        n.id for n in graph.nodes.values()
                        if n.id not in radius and n.id != assumption_id and n.live
                    )
                self.assertEqual(preserved(self.restored, b), preserved(self.original, a))

    def test_the_same_health_report(self):
        self.assertEqual(
            health_report(self.restored), health_report(self.original)
        )

    def test_the_same_answers_and_the_same_dead_end_reasons(self):
        qs = questions(self.original, 120)
        one = Walker(self.original, resolver_for(self.original))
        two = Walker(self.restored, resolver_for(self.restored))
        mismatches = []
        for q in qs:
            a, b = one.ask(q), two.ask(q)
            if (a.answer_id, a.hops, round(a.score, 9), a.path(),
                    a.dead_end) != (b.answer_id, b.hops, round(b.score, 9), b.path(),
                                    b.dead_end):
                mismatches.append(q)
        self.assertEqual(mismatches, [])

    def test_the_same_type_and_edge_counts(self):
        self.assertEqual(self.restored.type_counts(), self.original.type_counts())
        self.assertEqual(self.restored.edge_counts(), self.original.edge_counts())
        self.assertEqual(self.restored.untyped_edges, 0)

    def test_a_reloaded_graph_still_refuses_an_illegal_write(self):
        entity = self.restored.by_type(NodeType.ENTITY)[0]
        source = self.restored.by_type(NodeType.SOURCE)[0]
        with self.assertRaises(OntologyError):
            self.restored.add_edge(entity.id, EdgeType.MENTIONS, source.id)


class DeterminismTest(unittest.TestCase):
    def test_two_saves_of_the_same_graph_are_byte_identical(self):
        graph = demo_graph().graph
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.json", Path(tmp) / "b.json"
            graph.save_json(a)
            graph.save_json(b)
            self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_a_reload_saves_to_the_same_bytes(self):
        graph = demo_graph().graph
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "1.json", Path(tmp) / "2.json"
            graph.save_json(first)
            ContextGraph.load_json(first).save_json(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_the_saved_file_has_no_floats_json_cannot_represent(self):
        graph = demo_graph().graph
        for node in graph.nodes.values():
            for value in (node.embedding or []):
                self.assertFalse(math.isnan(value) or math.isinf(value))


if __name__ == "__main__":
    unittest.main()
