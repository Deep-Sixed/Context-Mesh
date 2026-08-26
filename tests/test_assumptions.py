"""Selective invalidation: exactly the dependent work falls, and nothing else."""

import unittest

from contextmesh.assumptions import AssumptionLedger
from contextmesh.decisions import DecisionLog
from contextmesh.graph import ContextGraph
from contextmesh.model import AssumptionStatus, EdgeType, NodeType, Provenance


def scenario():
    """Two independent branches, one of which stands on an assumption."""
    graph = ContextGraph()
    graph.build = 1
    ledger = AssumptionLedger(graph)
    decisions = DecisionLog(graph)

    source = graph.add_node(NodeType.SOURCE, "Capacity model")
    other_source = graph.add_node(NodeType.SOURCE, "Reranker evaluation")
    claim = graph.add_node(
        NodeType.CLAIM,
        "Shards stay under four gigabytes during rebuild",
        provenance=Provenance(source_id=source.id),
    )
    graph.add_edge(claim.id, EdgeType.DERIVED_FROM, source.id)
    unrelated_claim = graph.add_node(
        NodeType.CLAIM,
        "Reranking adds ninety milliseconds at p95",
        provenance=Provenance(source_id=other_source.id),
    )
    graph.add_edge(unrelated_claim.id, EdgeType.DERIVED_FROM, other_source.id)

    artefact = graph.add_node(NodeType.ENTITY, "Partitioned Rebuild")
    assumption = ledger.assume("Shard count grows linearly with corpus size")

    dependent = decisions.decide(
        "Rebuild the index in partitions",
        "Bounded memory beats build time.",
        source_id=source.id,
        supported_by=[claim.id],
        assumptions=[assumption.id],
        produces=[artefact.id],
    )
    unrelated = decisions.decide(
        "Rerank only the top fifty candidates",
        "Keeps p95 inside budget.",
        source_id=other_source.id,
        supported_by=[unrelated_claim.id],
    )
    return graph, ledger, decisions, assumption, dependent, unrelated, artefact, unrelated_claim


class BlastRadiusTest(unittest.TestCase):
    def setUp(self):
        (
            self.graph,
            self.ledger,
            self.decisions,
            self.assumption,
            self.dependent,
            self.unrelated,
            self.artefact,
            self.unrelated_claim,
        ) = scenario()

    def test_radius_follows_depends_on_backwards(self):
        radius = self.ledger.blast_radius(self.assumption.id)
        self.assertIn(self.dependent.id, radius)

    def test_radius_follows_produces_forwards(self):
        radius = self.ledger.blast_radius(self.assumption.id)
        self.assertIn(self.artefact.id, radius)

    def test_radius_excludes_the_unrelated_branch(self):
        radius = self.ledger.blast_radius(self.assumption.id)
        self.assertNotIn(self.unrelated.id, radius)
        self.assertNotIn(self.unrelated_claim.id, radius)

    def test_radius_explains_itself(self):
        radius = self.ledger.blast_radius(self.assumption.id)
        chain = radius[self.artefact.id]
        self.assertIn("depends_on", " ".join(chain))
        self.assertIn("produces", " ".join(chain))


class RejectionTest(unittest.TestCase):
    def setUp(self):
        (
            self.graph,
            self.ledger,
            self.decisions,
            self.assumption,
            self.dependent,
            self.unrelated,
            self.artefact,
            self.unrelated_claim,
        ) = scenario()
        self.evidence = self.graph.add_node(
            NodeType.EVIDENCE, "One tenant held 31% of chunks in a single shard"
        )
        self.report = self.ledger.reject(
            self.assumption.id,
            evidence_id=self.evidence.id,
            replacement="Shards are keyed on tenant as well as corpus position",
        )

    def test_the_assumption_is_rejected_not_deleted(self):
        self.assertIs(
            self.graph.assumptions[self.assumption.id].status, AssumptionStatus.REJECTED
        )
        self.assertIn(self.assumption.id, self.graph.nodes)

    def test_dependent_work_is_invalidated(self):
        self.assertTrue(self.graph.node(self.dependent.id).invalidated)
        self.assertTrue(self.graph.node(self.artefact.id).invalidated)

    def test_unrelated_work_is_preserved(self):
        self.assertFalse(self.graph.node(self.unrelated.id).invalidated)
        self.assertFalse(self.graph.node(self.unrelated_claim.id).invalidated)
        self.assertIn(self.unrelated.id, self.report.preserved)

    def test_the_report_proves_both_halves(self):
        self.assertEqual(self.report.blast_radius, 2)
        self.assertGreater(len(self.report.preserved), self.report.blast_radius)
        self.assertIn("depends_on", self.report.why(self.dependent.id))

    def test_evidence_is_wired_as_a_contradiction(self):
        edges = self.graph.in_edges(
            self.assumption.id, (EdgeType.CONTRADICTS,), live_only=False
        )
        self.assertEqual([e.src for e in edges], [self.evidence.id])

    def test_replacement_supersedes_the_rejected_assumption(self):
        replacement = self.graph.assumptions[self.report.replacement_id]
        self.assertEqual(replacement.supersedes, self.assumption.id)
        self.assertEqual(replacement.version, self.assumption.version + 1)

    def test_lineage_reads_oldest_first(self):
        lineage = self.ledger.lineage(self.report.replacement_id)
        self.assertEqual([a.id for a in lineage], [self.assumption.id, self.report.replacement_id])


class SupersedeTest(unittest.TestCase):
    def test_superseding_keeps_the_old_assumption(self):
        graph = ContextGraph()
        ledger = AssumptionLedger(graph)
        first = ledger.assume("Latency budget is 200ms")
        second = ledger.supersede(first.id, "Latency budget is 150ms")
        self.assertIs(graph.assumptions[first.id].status, AssumptionStatus.SUPERSEDED)
        self.assertEqual(graph.assumptions[first.id].superseded_by, second.id)
        self.assertEqual(second.version, 2)
        self.assertIn(first.id, graph.nodes)
        self.assertEqual(
            [e.dst for e in graph.out_edges(second.id, (EdgeType.SUPERSEDES,))],
            [first.id],
        )

    def test_only_active_assumptions_are_listed_as_active(self):
        graph = ContextGraph()
        ledger = AssumptionLedger(graph)
        first = ledger.assume("A")
        second = ledger.supersede(first.id, "B")
        self.assertEqual([a.id for a in ledger.active()], [second.id])


class DecisionHistoryTest(unittest.TestCase):
    def test_history_is_append_only_and_ordered(self):
        graph, ledger, decisions, *_ = scenario()
        latest = decisions.records[1]
        third = decisions.decide(
            "Rebuild the index in tenant-keyed partitions",
            "Tenant skew broke the sizing rule.",
            source_id="source:capacity" if "source:capacity" in graph.nodes else
            next(n.id for n in graph.by_type(NodeType.SOURCE)),
            supersedes=latest.decision_id,
        )
        chain = decisions.history_of(third.id)
        self.assertEqual([r.decision_id for r in chain][-1], third.id)
        self.assertIn(latest.decision_id, [r.decision_id for r in chain])
        # nothing was removed
        self.assertIn(latest.decision_id, graph.nodes)

    def test_current_excludes_superseded_decisions(self):
        graph, ledger, decisions, _, dependent, unrelated, *_ = scenario()
        source = next(n.id for n in graph.by_type(NodeType.SOURCE))
        replacement = decisions.decide(
            "Rebuild in tenant-keyed partitions",
            "Tenant skew.",
            source_id=source,
            supersedes=dependent.id,
        )
        current = {r.decision_id for r in decisions.current()}
        self.assertIn(replacement.id, current)
        self.assertNotIn(dependent.id, current)


if __name__ == "__main__":
    unittest.main()
