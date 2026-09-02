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

    # ── field types: a snapshot fails closed, it does not coerce ─────────
    def test_a_string_flag_does_not_become_true(self):
        """The sharpest case: bool("false") is True.

        Coercing here would let a malformed snapshot turn a live node into an
        invalidated one, silently, with no other sign that anything was wrong.
        """
        for field in ("invalidated", "pruned"):
            with self.subTest(field=field):
                p = self.payload()
                p["nodes"][0][field] = "false"
                self.refuses(p, ValueError)

    def test_a_boolean_is_not_a_count(self):
        # bool is a subclass of int, so True would arrive as 1.
        for field in ("walks", "build"):
            with self.subTest(field=field):
                p = self.payload()
                p["nodes"][0][field] = True
                self.refuses(p, ValueError)

    def test_a_negative_counter_is_refused(self):
        p = self.payload()
        p["nodes"][0]["walks"] = -1
        self.refuses(p, ValueError)

    def test_a_span_must_be_a_pair(self):
        for span in ([1, 2, 3], [1], "0-4", 4):
            with self.subTest(span=span):
                p = self.payload()
                node = next(n for n in p["nodes"] if n["provenance"])
                node["provenance"]["span"] = span
                self.refuses(p, ValueError)

    def test_a_string_does_not_become_a_vector(self):
        # list("abc") is ["a", "b", "c"], which would restore as an embedding.
        p = self.payload()
        p["nodes"][0]["embedding"] = "abc"
        p["nodes"][0]["embedded"] = True
        self.refuses(p, ValueError)

    def test_a_vector_of_non_numbers_is_refused(self):
        p = self.payload()
        p["nodes"][0]["embedding"] = ["not-a-number"]
        p["nodes"][0]["embedded"] = True
        self.refuses(p, ValueError)

    def test_the_embedded_flag_must_agree_with_the_vector(self):
        p = self.payload()
        p["nodes"][0]["embedding"] = None
        p["nodes"][0]["embedded"] = True
        self.refuses(p, ValueError)
        p = self.payload()
        p["nodes"][0]["embedding"] = [0.5]
        p["nodes"][0]["embedded"] = False
        self.refuses(p, ValueError)

    def test_a_non_finite_weight_is_refused(self):
        p = self.payload()
        p["edges"][0]["weight"] = "not-a-number"
        self.refuses(p, ValueError)

    def test_evidence_ids_must_be_strings(self):
        p = self.payload()
        p["edges"][0]["evidence_ids"] = [1, 2]
        self.refuses(p, ValueError)

    def test_attrs_must_be_an_object(self):
        p = self.payload()
        p["nodes"][0]["attrs"] = ["not", "an", "object"]
        self.refuses(p, ValueError)

    def test_an_assumption_version_below_one_is_refused(self):
        p = self.payload()
        p["assumptions"][0]["version"] = 0
        self.refuses(p, ValueError)

    # ── missing fields: absence is corruption, not a default ─────────────
    def test_a_deleted_flag_does_not_revive_a_dead_node(self):
        """The sharpest of these: dropping a field is quieter than a bad type.

        ``invalidated`` defaulting to ``False`` restores a node the graph had
        deliberately killed, with nothing at all to show that it happened.
        """
        p = self.payload()
        node = p["nodes"][0]
        node["invalidated"] = True
        ContextGraph.from_dict(p)          # the flag is honoured when present
        del node["invalidated"]
        self.refuses(p, ValueError)

    def test_every_field_the_writer_emits_is_required_on_a_node(self):
        emitted = list(self.good["nodes"][0])
        self.assertGreaterEqual(len(emitted), 10)
        for field in emitted:
            with self.subTest(field=field):
                p = self.payload()
                del p["nodes"][0][field]
                self.refuses(p, ValueError)

    def test_every_field_the_writer_emits_is_required_on_an_edge(self):
        for field in list(self.good["edges"][0]):
            with self.subTest(field=field):
                p = self.payload()
                del p["edges"][0][field]
                self.refuses(p, ValueError)

    def test_every_field_the_writer_emits_is_required_on_an_assumption(self):
        for field in list(self.good["assumptions"][0]):
            with self.subTest(field=field):
                p = self.payload()
                del p["assumptions"][0][field]
                self.refuses(p, ValueError)

    def test_every_field_the_writer_emits_is_required_on_provenance(self):
        node = next(n for n in self.good["nodes"] if n["provenance"])
        for field in list(node["provenance"]):
            with self.subTest(field=field):
                p = self.payload()
                del next(n for n in p["nodes"] if n["provenance"])["provenance"][field]
                self.refuses(p, ValueError)

    def test_every_container_field_is_required(self):
        for field in ("schema", "version", "build", "nodes", "edges", "assumptions"):
            with self.subTest(field=field):
                p = self.payload()
                del p[field]
                self.refuses(p)

    def test_a_dropped_embedding_is_refused(self):
        p = self.payload()
        node = p["nodes"][0]
        del node["embedding"]
        del node["embedded"]
        self.refuses(p, ValueError)

    def test_a_boolean_version_does_not_pass_as_version_one(self):
        # True == 1, so an equality check alone would let this through.
        p = self.payload()
        p["version"] = True
        self.refuses(p)

    def test_a_boolean_build_is_refused(self):
        p = self.payload()
        p["build"] = True
        self.refuses(p)

    def test_a_record_array_that_is_not_a_list(self):
        for field in ("nodes", "edges", "assumptions"):
            with self.subTest(field=field):
                p = self.payload()
                p[field] = {"not": "a list"}
                self.refuses(p)

    def test_null_does_not_normalise_to_empty(self):
        """A null in a non-nullable field is corruption, not an empty value.

        The sharpest case is evidence: an assumption whose ``evidence_ids``
        arrived as null would restore with nothing recorded as having
        disproved it, and the file would load without complaint.
        """
        cases = (
            ("node.attrs", lambda p: p["nodes"][0].__setitem__("attrs", None)),
            (
                "provenance.checks",
                lambda p: next(n for n in p["nodes"] if n["provenance"])[
                    "provenance"
                ].__setitem__("checks", None),
            ),
            ("edge.evidence_ids", lambda p: p["edges"][0].__setitem__("evidence_ids", None)),
            (
                "assumption.evidence_ids",
                lambda p: p["assumptions"][0].__setitem__("evidence_ids", None),
            ),
        )
        for label, corrupt in cases:
            with self.subTest(field=label):
                p = self.payload()
                corrupt(p)
                self.refuses(p, ValueError)

    def test_evidence_is_not_silently_dropped(self):
        """The consequence the case above exists to prevent."""
        graph = ContextGraph()
        graph.build = 1
        evidence = graph.add_node(NodeType.EVIDENCE, "CVE report", attrs={"kind": "disproof"})
        ledger = AssumptionLedger(graph)
        assumption = ledger.assume("ground", evidence_ids=[evidence.id])
        self.assertEqual(assumption.evidence_ids, [evidence.id])
        restored = ContextGraph.from_dict(graph.to_dict())
        self.assertEqual(restored.assumptions[assumption.id].evidence_ids, [evidence.id])

    def test_the_fields_the_schema_does_write_as_null_still_load(self):
        """The fix must not close doors the format legitimately uses."""
        nullable = (
            ("node.provenance", lambda p: p["nodes"][0].__setitem__("provenance", None)),
            (
                "provenance.span",
                lambda p: next(n for n in p["nodes"] if n["provenance"])[
                    "provenance"
                ].__setitem__("span", None),
            ),
            (
                "node.embedding",
                lambda p: (
                    p["nodes"][0].__setitem__("embedding", None),
                    p["nodes"][0].__setitem__("embedded", False),
                ),
            ),
            ("edge.assumption_id", lambda p: p["edges"][0].__setitem__("assumption_id", None)),
            (
                "assumption.supersedes",
                lambda p: p["assumptions"][0].__setitem__("supersedes", None),
            ),
            (
                "assumption.rejected_at_build",
                lambda p: p["assumptions"][0].__setitem__("rejected_at_build", None),
            ),
        )
        for label, mutate in nullable:
            with self.subTest(field=label):
                p = self.payload()
                mutate(p)
                ContextGraph.from_dict(p)

    # ── references between records ───────────────────────────────────────
    def test_a_dangling_supersedes_is_refused(self):
        p = self.payload()
        p["assumptions"][0]["supersedes"] = "assumption:does-not-exist"
        self.refuses(p)

    def test_a_dangling_superseded_by_is_refused(self):
        p = self.payload()
        p["assumptions"][0]["superseded_by"] = "assumption:does-not-exist"
        self.refuses(p)

    def test_a_dangling_edge_assumption_id_is_refused(self):
        p = self.payload()
        p["edges"][0]["assumption_id"] = "assumption:ghost"
        self.refuses(p)

    def test_evidence_that_is_not_a_node_is_refused(self):
        p = self.payload()
        p["assumptions"][0]["evidence_ids"] = ["evidence:ghost"]
        self.refuses(p)
        p = self.payload()
        p["edges"][0]["evidence_ids"] = ["evidence:ghost"]
        self.refuses(p)

    def test_one_sided_supersession_is_refused(self):
        """Half a two-sided relationship is not a weaker version of it."""
        graph = ContextGraph()
        graph.build = 1
        ledger = AssumptionLedger(graph)
        first = ledger.assume("v1")
        ledger.supersede(first.id, "v2")
        good = json.loads(json.dumps(graph.to_dict()))
        ContextGraph.from_dict(good)

        for field in ("supersedes", "superseded_by"):
            with self.subTest(field=field):
                p = json.loads(json.dumps(good))
                for row in p["assumptions"]:
                    if row[field]:
                        row[field] = None
                with self.assertRaises(SnapshotError):
                    ContextGraph.from_dict(p)

    def test_a_lineage_walk_cannot_be_broken_by_a_loaded_snapshot(self):
        # What the dangling checks above are protecting: lineage() indexes
        # graph.assumptions directly, so a dangling id is a KeyError later.
        graph = ContextGraph()
        graph.build = 1
        ledger = AssumptionLedger(graph)
        first = ledger.assume("v1")
        latest = ledger.supersede(first.id, "v2")
        restored = ContextGraph.from_dict(graph.to_dict())
        chain = AssumptionLedger(restored).lineage(latest.id)
        self.assertEqual([a.statement for a in chain], ["v1", "v2"])

    # ── the assumption mirror ────────────────────────────────────────────
    def test_a_status_the_node_and_the_record_disagree_on(self):
        p = self.payload()
        node = next(n for n in p["nodes"] if n["id"] == self.assumption_id)
        node["attrs"]["status"] = "rejected"
        with self.assertRaises(SnapshotError) as caught:
            ContextGraph.from_dict(p)
        self.assertIn("status", str(caught.exception))

    def test_a_version_the_node_and_the_record_disagree_on(self):
        p = self.payload()
        node = next(n for n in p["nodes"] if n["id"] == self.assumption_id)
        node["attrs"]["version"] = 99
        with self.assertRaises(SnapshotError) as caught:
            ContextGraph.from_dict(p)
        self.assertIn("version", str(caught.exception))

    def test_a_missing_mirror_is_refused(self):
        p = self.payload()
        node = next(n for n in p["nodes"] if n["id"] == self.assumption_id)
        node["attrs"] = {}
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


class AssumptionMirrorTest(unittest.TestCase):
    """status and version live on the record and on the node. They must agree.

    The loader refusing disagreement is only safe if the live code keeps them
    in step. It did not: two paths bumped ``version`` on the record alone, and
    both demos carried a mismatch. These pin the fix.
    """

    def mismatches(self, graph):
        return [
            (aid, field)
            for aid, assumption in graph.assumptions.items()
            for field, recorded in (
                ("status", assumption.status.value),
                ("version", assumption.version),
            )
            if graph.nodes[aid].attrs.get(field) != recorded
        ]

    def test_the_corpus_demo_keeps_them_in_step(self):
        self.assertEqual(self.mismatches(demo_graph().graph), [])

    def test_rejecting_with_a_replacement_keeps_them_in_step(self):
        from contextmesh.assumptions import AssumptionLedger

        graph = ContextGraph()
        graph.build = 1
        ledger = AssumptionLedger(graph)
        original = ledger.assume("shards grow linearly")
        evidence = graph.add_node(
            NodeType.EVIDENCE,
            "One tenant held 31% of chunks in a single shard",
            attrs={"kind": "disproof"},
        )
        report = ledger.reject(
            original.id,
            evidence_id=evidence.id,
            replacement="shards grow with tenant skew",
        )
        self.assertIsNotNone(report.replacement_id)
        replacement = graph.assumptions[report.replacement_id]
        self.assertEqual(replacement.version, original.version + 1)
        self.assertEqual(self.mismatches(graph), [])

    def test_a_runner_repair_keeps_them_in_step(self):
        from contextmesh.execute import demo as execute_demo

        self.assertEqual(self.mismatches(execute_demo().runner.graph), [])

    def test_superseding_keeps_them_in_step(self):
        from contextmesh.assumptions import AssumptionLedger

        graph = ContextGraph()
        graph.build = 1
        ledger = AssumptionLedger(graph)
        first = ledger.assume("v1")
        ledger.supersede(first.id, "v2")
        self.assertEqual(self.mismatches(graph), [])

    def test_a_graph_that_has_been_repaired_still_round_trips(self):
        from contextmesh.execute import demo as execute_demo

        graph = execute_demo().runner.graph
        self.assertEqual(ContextGraph.from_dict(graph.to_dict()).to_dict(), graph.to_dict())


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
