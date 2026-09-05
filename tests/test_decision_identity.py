"""Decision identity: an immutable event, not content-addressed.

GRAPH.md rule 9 / "What a decision's identity is": two calls to `decide()`
with byte-identical title and rationale are two decisions, not one being
re-observed. An explicit `id` is an idempotency key for one call, not a way
to name a decision by its content -- same id + same payload is a no-op,
same id + different payload fails closed, and a write that fails partway
through leaves nothing behind.
"""

import unittest

from contextmesh.assumptions import AssumptionLedger
from contextmesh.decisions import DecisionLog
from contextmesh.graph import ContextGraph
from contextmesh.model import EdgeType, NodeType, Provenance
from contextmesh.ontology import OntologyError


def _graph_with_source():
    graph = ContextGraph()
    graph.build = 1
    source = graph.add_node(
        NodeType.SOURCE,
        "Fixture source",
        attrs={"origin": "fixture", "retrieved_at": "fixture"},
    )
    return graph, source


class AutoIdentityIsAlwaysFreshTest(unittest.TestCase):
    def test_repeated_title_and_rationale_mint_two_distinct_decisions(self):
        graph, source = _graph_with_source()
        decisions = DecisionLog(graph)

        first = decisions.decide(
            "Use PostgreSQL", "It fits our access patterns.", source_id=source.id
        )
        second = decisions.decide(
            "Use PostgreSQL", "It fits our access patterns.", source_id=source.id
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(decisions.records), 2)
        self.assertEqual(graph.type_counts(live_only=False)["decision"], 2)

    def test_third_repeat_also_gets_its_own_id(self):
        graph, source = _graph_with_source()
        decisions = DecisionLog(graph)
        ids = {
            decisions.decide("Same title", "Same rationale.", source_id=source.id).id
            for _ in range(3)
        }
        self.assertEqual(len(ids), 3)


class ExplicitIdIsAnIdempotencyKeyTest(unittest.TestCase):
    def test_same_id_same_payload_is_a_true_no_op(self):
        graph, source = _graph_with_source()
        decisions = DecisionLog(graph)

        first = decisions.decide(
            "Rebuild the index",
            "Bounded memory beats build time.",
            source_id=source.id,
            id="decision:step-1",
        )
        before_edges = len(graph.edges)
        before_records = len(decisions.records)

        second = decisions.decide(
            "Rebuild the index",
            "Bounded memory beats build time.",
            source_id=source.id,
            id="decision:step-1",
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(len(graph.edges), before_edges)
        self.assertEqual(len(decisions.records), before_records)

    def test_same_id_different_payload_fails_closed(self):
        graph, source = _graph_with_source()
        decisions = DecisionLog(graph)
        decisions.decide(
            "Rebuild the index",
            "Bounded memory beats build time.",
            source_id=source.id,
            id="decision:step-1",
        )

        with self.assertRaises(OntologyError):
            decisions.decide(
                "Rebuild the index",
                "Different rationale entirely.",
                source_id=source.id,
                id="decision:step-1",
            )

    def test_rejected_retry_leaves_no_partial_state(self):
        graph, source = _graph_with_source()
        decisions = DecisionLog(graph)
        decisions.decide(
            "Rebuild the index",
            "Bounded memory beats build time.",
            source_id=source.id,
            id="decision:step-1",
        )
        node_count = len(graph.nodes)
        edge_count = len(graph.edges)
        record_count = len(decisions.records)

        with self.assertRaises(OntologyError):
            decisions.decide(
                "Rebuild the index",
                "Different rationale entirely.",
                source_id=source.id,
                id="decision:step-1",
            )

        self.assertEqual(len(graph.nodes), node_count)
        self.assertEqual(len(graph.edges), edge_count)
        self.assertEqual(len(decisions.records), record_count)

    def test_retry_after_attempt_bump_gets_its_own_identity(self):
        """Mirrors Runner._execute_task's real usage: id includes the attempt
        number, so a genuine retry with new content is simply a new id, not
        a collision."""
        graph, source = _graph_with_source()
        decisions = DecisionLog(graph)
        v1 = decisions.decide(
            "Fetch shard manifest",
            "First attempt.",
            source_id=source.id,
            id="plan|fetch-shard-manifest|v1",
        )
        v2 = decisions.decide(
            "Fetch shard manifest",
            "Retried with backoff.",
            source_id=source.id,
            id="plan|fetch-shard-manifest|v2",
        )
        self.assertNotEqual(v1.id, v2.id)


class AtomicRollbackTest(unittest.TestCase):
    def test_bad_supported_by_claim_leaves_nothing_behind(self):
        graph, source = _graph_with_source()
        decisions = DecisionLog(graph)
        node_count = len(graph.nodes)
        edge_count = len(graph.edges)

        with self.assertRaises(OntologyError):
            decisions.decide(
                "Rebuild the index",
                "Bounded memory beats build time.",
                source_id=source.id,
                supported_by=["claim:does-not-exist"],
            )

        self.assertEqual(len(graph.nodes), node_count)
        self.assertEqual(len(graph.edges), edge_count)
        self.assertEqual(graph.type_counts(live_only=False)["decision"], 0)

    def test_bad_produces_entity_leaves_nothing_behind(self):
        graph, source = _graph_with_source()
        decisions = DecisionLog(graph)
        node_count = len(graph.nodes)
        edge_count = len(graph.edges)

        with self.assertRaises(OntologyError):
            decisions.decide(
                "Rebuild the index",
                "Bounded memory beats build time.",
                source_id=source.id,
                produces=["entity:does-not-exist"],
            )

        self.assertEqual(len(graph.nodes), node_count)
        self.assertEqual(len(graph.edges), edge_count)

    def test_bad_supersedes_target_leaves_nothing_behind(self):
        graph, source = _graph_with_source()
        decisions = DecisionLog(graph)
        node_count = len(graph.nodes)
        edge_count = len(graph.edges)

        with self.assertRaises(OntologyError):
            decisions.decide(
                "Rebuild the index",
                "Bounded memory beats build time.",
                source_id=source.id,
                supersedes="decision:does-not-exist",
            )

        self.assertEqual(len(graph.nodes), node_count)
        self.assertEqual(len(graph.edges), edge_count)

    def test_bad_assumption_leaves_nothing_behind(self):
        graph, source = _graph_with_source()
        decisions = DecisionLog(graph)
        node_count = len(graph.nodes)
        edge_count = len(graph.edges)

        with self.assertRaises(OntologyError):
            decisions.decide(
                "Rebuild the index",
                "Bounded memory beats build time.",
                source_id=source.id,
                assumptions=["assumption:does-not-exist"],
            )

        self.assertEqual(len(graph.nodes), node_count)
        self.assertEqual(len(graph.edges), edge_count)

    def test_partial_edges_from_a_multi_item_list_are_rolled_back(self):
        """First supported_by claim is real, second is not: the edges from
        the first must not survive the failure on the second."""
        graph, source = _graph_with_source()
        good_claim = graph.add_node(
            NodeType.CLAIM,
            "A real claim",
            provenance=Provenance(source_id=source.id),
        )
        graph.add_edge(good_claim.id, EdgeType.DERIVED_FROM, source.id)
        decisions = DecisionLog(graph)
        node_count = len(graph.nodes)
        edge_count = len(graph.edges)

        with self.assertRaises(OntologyError):
            decisions.decide(
                "Rebuild the index",
                "Bounded memory beats build time.",
                source_id=source.id,
                supported_by=[good_claim.id, "claim:does-not-exist"],
            )

        self.assertEqual(len(graph.nodes), node_count)
        self.assertEqual(len(graph.edges), edge_count)
        self.assertFalse(
            [e for e in graph.edges.values() if e.type is EdgeType.SUPPORTS]
        )

    def test_supersede_wiring_is_rolled_back_together_with_the_edge(self):
        graph, source = _graph_with_source()
        decisions = DecisionLog(graph)
        old = decisions.decide(
            "Original plan", "Because reasons.", source_id=source.id
        )

        with self.assertRaises(OntologyError):
            decisions.decide(
                "Replacement plan",
                "Better reasons.",
                source_id=source.id,
                supersedes=old.id,
                produces=["entity:does-not-exist"],
            )

        self.assertNotIn("superseded_by", old.attrs)
        self.assertEqual(graph.type_counts(live_only=False)["decision"], 1)


class PreexistingEdgesSurviveRollbackTest(unittest.TestCase):
    """A rollback must only discard what this call itself created -- an
    edge or node another write already put in place is left alone, mirroring
    AssumptionLedger.reject's edge_already_existed guard."""

    def test_unrelated_assumption_survives_a_failing_decide_call(self):
        graph, source = _graph_with_source()
        ledger = AssumptionLedger(graph)
        assumption = ledger.assume("Some ground truth")
        decisions = DecisionLog(graph)

        with self.assertRaises(OntologyError):
            decisions.decide(
                "Faulty decision",
                "Rationale.",
                source_id=source.id,
                assumptions=["assumption:does-not-exist"],
            )
        self.assertEqual(graph.type_counts(live_only=False)["decision"], 0)
        self.assertIs(graph.assumptions[assumption.id], assumption)


if __name__ == "__main__":
    unittest.main()
