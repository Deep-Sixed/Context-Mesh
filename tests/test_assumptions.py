"""Selective invalidation: exactly the dependent work falls, and nothing else."""

import unittest
from datetime import date

from contextmesh.assumptions import AssumptionLedger
from contextmesh.decisions import DecisionLog
from contextmesh.graph import ContextGraph
from contextmesh.model import AssumptionStatus, EdgeType, NodeType, Provenance
from contextmesh.ontology import OntologyError
from contextmesh.temporal import _fell_at


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
        # Rule 7 wants the witness to be datable, so it gets the source it
        # always implied: the postmortem the observation was lifted from.
        self.postmortem = self.graph.add_node(
            NodeType.SOURCE,
            "Postmortem 233",
            attrs={"origin": "incident-review", "retrieved_at": "2026-07-08"},
        )
        self.evidence = self.graph.add_node(
            NodeType.EVIDENCE,
            "One tenant held 31% of chunks in a single shard",
            attrs={"kind": "postmortem"},
            provenance=Provenance(
                source_id=self.postmortem.id, recorded_at_build=self.graph.build
            ),
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


class Rule7WriteBoundaryTest(unittest.TestCase):
    """GRAPH.md rule 7 at the moment of the write, not after it.

    An assumption is only ever rejected by evidence that contradicts it,
    "because 'why did this fall over' has to have an answer inside the graph".
    A rejection is also a *historical* transition: ``temporal._fell_at`` reads
    the fall date off that evidence and nothing else, so a witness with no
    source date leaves the assumption reported ACTIVE at every horizon,
    including horizons long after it fell.

    Each case below checks two things — that the write is refused, and that
    the graph is exactly as it was. Validation used to run after the mutation,
    so a bad witness left a rejected assumption with no edge saying why.
    """

    def setUp(self):
        self.graph = ContextGraph()
        self.graph.build = 1
        self.ledger = AssumptionLedger(self.graph)
        self.assumption = self.ledger.assume("shards grow linearly")

    def untouched(self):
        """Every field a rejection would have moved, still where it started."""
        record = self.graph.assumptions[self.assumption.id]
        node = self.graph.node(self.assumption.id)
        return {
            "status": record.status,
            "rejected_at_build": record.rejected_at_build,
            "evidence_ids": list(record.evidence_ids),
            "invalidated": node.invalidated,
            "mirrored_status": node.attrs.get("status"),
            "contradictions": len(
                self.graph.in_edges(
                    self.assumption.id, [EdgeType.CONTRADICTS], live_only=False
                )
            ),
        }

    def refuses(self, evidence_id, expected):
        before = self.untouched()
        with self.assertRaisesRegex(OntologyError, expected):
            self.ledger.reject(self.assumption.id, evidence_id=evidence_id)
        self.assertEqual(self.untouched(), before)

    def dated_witness(self, retrieved_at="2026-07-08"):
        source = self.graph.add_node(
            NodeType.SOURCE,
            "Postmortem 233",
            attrs={"origin": "incident-review", "retrieved_at": retrieved_at},
        )
        return self.graph.add_node(
            NodeType.EVIDENCE,
            "one tenant held 31% of chunks in a single shard",
            attrs={"kind": "postmortem"},
            provenance=Provenance(source_id=source.id, recorded_at_build=1),
        )

    # ── the four conditions ──────────────────────────────────────────────
    def test_a_rejection_with_no_witness_at_all_does_not_typecheck(self):
        """The signature is the first refusal: evidence_id has no default."""
        with self.assertRaises(TypeError):
            self.ledger.reject(self.assumption.id)
        self.assertIs(
            self.graph.assumptions[self.assumption.id].status, AssumptionStatus.ACTIVE
        )

    def test_a_witness_that_is_not_in_the_graph_is_refused(self):
        self.refuses("evidence:never-ingested", "is not in this graph")

    def test_a_witness_of_the_wrong_type_is_refused(self):
        claim = self.graph.add_node(NodeType.CLAIM, "not an observation")
        self.refuses(claim.id, "is a claim, not evidence")

    def test_an_invalidated_witness_is_refused(self):
        witness = self.dated_witness()
        witness.invalidated = True
        self.refuses(witness.id, "is not live")

    def test_a_witness_with_no_provenance_is_refused(self):
        bare = self.graph.add_node(
            NodeType.EVIDENCE, "someone said so", attrs={"kind": "hearsay"}
        )
        self.refuses(bare.id, "has no source provenance")

    def test_a_witness_whose_source_carries_no_date_is_refused(self):
        """The case the old signature could not see.

        Evidence, contradicting, properly provenanced — and still unable to
        say *when*, because the source it came from is undated. Accepting it
        would record a fall no reconstruction could ever place.
        """
        self.refuses(
            self.dated_witness(retrieved_at="at plan time").id, "no usable retrieved_at"
        )

    # ── and the case that must still work ────────────────────────────────
    def test_a_dated_witness_rejects_and_the_fall_is_reconstructible(self):
        witness = self.dated_witness()
        report = self.ledger.reject(self.assumption.id, evidence_id=witness.id)

        record = self.graph.assumptions[self.assumption.id]
        self.assertIs(record.status, AssumptionStatus.REJECTED)
        self.assertEqual(record.evidence_ids, [witness.id])
        self.assertEqual(report.assumption_id, self.assumption.id)
        self.assertEqual(
            len(
                self.graph.in_edges(
                    self.assumption.id, [EdgeType.CONTRADICTS], live_only=False
                )
            ),
            1,
        )
        # The point of the whole guard: the moment is recoverable, on the
        # source clock, without the build counter or a wall clock.
        self.assertEqual(_fell_at(self.graph, self.assumption.id), date(2026, 7, 8))

    def test_the_witness_edge_is_created_once_even_if_the_caller_added_it(self):
        """Callers used to add the contradicts edge themselves and double it."""
        witness = self.dated_witness()
        self.graph.add_edge(witness.id, EdgeType.CONTRADICTS, self.assumption.id)
        self.ledger.reject(self.assumption.id, evidence_id=witness.id)
        edges = self.graph.in_edges(
            self.assumption.id, [EdgeType.CONTRADICTS], live_only=False
        )
        self.assertEqual(len(edges), 1)


if __name__ == "__main__":
    unittest.main()
