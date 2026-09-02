"""PR #8B: registered auditors interpret evidence; clients never supply verdicts."""

import unittest

from contextmesh.evidence import submit_evidence
from contextmesh.execute import (
    Event,
    ExecutionError,
    Runner,
    TaskRegistry,
    TaskState,
)
from contextmesh.graph import ContextGraph
from contextmesh.model import AssumptionStatus, EdgeType, NodeType
from contextmesh.recheck import EvidenceRecheckError, recheck


class EvidenceBoundRecheckTest(unittest.TestCase):
    def build(self, recheck_auditor):
        graph = ContextGraph()
        source = graph.add_node(
            NodeType.SOURCE,
            "Vendor advisory feed",
            id="source:vendor-advisory",
            # Dated: rule 7 takes the moment an assumption fell from the
            # contradicting evidence's source, so an undated feed could never
            # be the reason for a rejection.
            attrs={"origin": "vendor-feed", "retrieved_at": "2026-07-08"},
        )
        registry = TaskRegistry()
        registry.register_worker("auth.hash.argon2.v1", lambda ctx: {"impl": "argon2"})
        registry.register_auditor("auth.hash.audit.v1", lambda ctx: ctx.ok("holds"))
        runner = Runner("auth", graph=graph, registry=registry)
        task = runner.task(
            "hashing",
            worker_key="auth.hash.argon2.v1",
            auditor_key="auth.hash.audit.v1",
            assumes="Argon2 has no open advisory",
            produces=("Password Hasher",),
        )
        runner.run()
        self.assertIs(task.state, TaskState.DONE)

        # The refusal tests below are about the standing recheck boundary, not
        # initial execution. Install their auditor only after the task is known
        # to be DONE; otherwise a deliberately bad auditor fires inside
        # Runner.run() and the test measures the wrong path.
        task.audit = recheck_auditor
        return runner, source

    @staticmethod
    def cve_auditor(ctx):
        for node in ctx.graph.nodes.values():
            if node.type is not NodeType.EVIDENCE:
                continue
            intake = node.attrs.get("evidence_intake", {})
            metadata = intake.get("metadata", {})
            if (
                metadata.get("package") == "argon2"
                and metadata.get("severity") == "critical"
            ):
                return ctx.disproved(
                    "critical Argon2 advisory is active",
                    evidence_id=node.id,
                )
        return ctx.ok("no matching advisory")

    def with_evidence(self, runner, source):
        return submit_evidence(
            runner.graph,
            text="CVE-2026-9999 affects Argon2",
            source_id=source.id,
            external_id="CVE-2026-9999",
            metadata={"package": "argon2", "severity": "critical"},
        )

    def test_registered_auditor_links_the_pre_ingested_evidence(self):
        runner, source = self.build(self.cve_auditor)
        receipt = self.with_evidence(runner, source)
        evidence_before = len(
            runner.graph.by_type(NodeType.EVIDENCE, live_only=False)
        )

        reports = recheck(runner, require_evidence=True)

        self.assertEqual(len(reports), 1)
        task = runner["hashing"]
        assumption = runner.graph.assumptions[task.assumption_id]
        self.assertIs(task.state, TaskState.STALE)
        self.assertIs(assumption.status, AssumptionStatus.REJECTED)
        self.assertEqual(
            len(runner.graph.by_type(NodeType.EVIDENCE, live_only=False)),
            evidence_before,
            "8B must link the parked observation, not invent a second one",
        )
        edges = [
            edge
            for edge in runner.graph.edges.values()
            if edge.type is EdgeType.CONTRADICTS
        ]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].src, receipt.evidence_id)
        self.assertEqual(edges[0].dst, assumption.id)

        disproofs = [
            entry
            for entry in runner.ledger.entries
            if entry.event is Event.DISPROVED
        ]
        self.assertEqual(len(disproofs), 1)
        self.assertEqual(disproofs[0].data["evidence_id"], receipt.evidence_id)
        self.assertTrue(runner.ledger.verify())

    def test_missing_evidence_id_is_refused_before_belief_mutation(self):
        def bad(ctx):
            return ctx.disproved(
                "invented finding", evidence_id="evidence:missing"
            )

        runner, _ = self.build(bad)
        task = runner["hashing"]
        assumption = runner.graph.assumptions[task.assumption_id]
        head = runner.ledger.head
        contradicts = {
            edge.id
            for edge in runner.graph.edges.values()
            if edge.type is EdgeType.CONTRADICTS
        }

        with self.assertRaisesRegex(EvidenceRecheckError, "not in this graph"):
            recheck(runner, require_evidence=True)

        self.assertIs(task.state, TaskState.DONE)
        self.assertIs(assumption.status, AssumptionStatus.ACTIVE)
        self.assertEqual(runner.ledger.head, head)
        self.assertEqual(
            {
                edge.id
                for edge in runner.graph.edges.values()
                if edge.type is EdgeType.CONTRADICTS
            },
            contradicts,
        )

    def test_non_evidence_id_is_refused(self):
        def bad(ctx):
            return ctx.disproved("wrong node", evidence_id=ctx.task.node_id)

        runner, _ = self.build(bad)
        task = runner["hashing"]
        assumption = runner.graph.assumptions[task.assumption_id]

        with self.assertRaisesRegex(EvidenceRecheckError, "not evidence"):
            recheck(runner, require_evidence=True)

        self.assertIs(task.state, TaskState.DONE)
        self.assertIs(assumption.status, AssumptionStatus.ACTIVE)

    def test_controlled_recheck_requires_pre_ingested_evidence(self):
        runner, _ = self.build(lambda ctx: ctx.disproved("legacy disproof"))
        task = runner["hashing"]
        evidence_before = len(
            runner.graph.by_type(NodeType.EVIDENCE, live_only=False)
        )

        with self.assertRaisesRegex(EvidenceRecheckError, "without identifying"):
            recheck(runner, require_evidence=True)

        self.assertIs(task.state, TaskState.DONE)
        self.assertEqual(
            len(runner.graph.by_type(NodeType.EVIDENCE, live_only=False)),
            evidence_before,
        )

    def test_the_in_process_path_can_no_longer_manufacture_its_own_evidence(self):
        """The old in-process behaviour, now refused rather than accommodated.

        This used to mint an EVIDENCE node from the auditor's own reason and
        reject on it — the runner deciding the ground was false and then
        writing the proof that said so. Rule 7 wants the answer to "why did
        this fall over" to be in the graph, and evidence invented from the
        finding it supports is not an answer. It was also undatable, so every
        projection reported the assumption still standing.
        """
        runner, _ = self.build(lambda ctx: ctx.disproved("legacy disproof"))
        task = runner["hashing"]
        assumption = runner.graph.assumptions[task.assumption_id]
        before = len(runner.graph.by_type(NodeType.EVIDENCE, live_only=False))

        with self.assertRaisesRegex(ExecutionError, "bound no evidence"):
            recheck(runner)

        self.assertIs(assumption.status, AssumptionStatus.ACTIVE)
        self.assertEqual(
            len(runner.graph.by_type(NodeType.EVIDENCE, live_only=False)),
            before,
        )
        # The refusal costs nothing: no rejection, so no closure went stale.
        self.assertIs(runner["hashing"].state, TaskState.DONE)


if __name__ == "__main__":
    unittest.main()
