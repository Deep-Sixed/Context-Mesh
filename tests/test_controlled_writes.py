"""PR #8B-8D controlled-write integration over the native durable session."""

import tempfile
import unittest
from pathlib import Path

from contextmesh.execute import Runner, TaskRegistry, TaskState
from contextmesh.model import AssumptionStatus, EdgeType, NodeType
from contextmesh_mcp.session import Checkpointer, Session, writer_lock
from contextmesh_mcp.writes import (
    ControlledWriteError,
    mesh_recheck,
    mesh_repair,
    mesh_resume,
    mesh_submit_evidence,
)


def deployment() -> TaskRegistry:
    registry = TaskRegistry()
    registry.register_worker("auth.schema.v1", lambda ctx: {"schema": "v1"})
    registry.register_worker("auth.hash.argon2.v1", lambda ctx: {"impl": "argon2"})
    registry.register_worker("auth.hash.bcrypt.v1", lambda ctx: {"impl": "bcrypt"})
    registry.register_worker(
        "auth.routes.v1",
        lambda ctx: {"hasher": ctx.inputs["hashing"]["impl"]},
    )
    registry.register_worker("auth.tokens.v1", lambda ctx: {"algorithm": "EdDSA"})

    def audit_hash(ctx):
        for node in ctx.graph.nodes.values():
            if node.type is not NodeType.EVIDENCE:
                continue
            record = node.attrs.get("evidence_intake", {})
            metadata = record.get("metadata", {})
            if metadata.get("package") == "argon2" and metadata.get("severity") == "critical":
                return ctx.disproved(
                    "CVE-2026-9999 affects the active Argon2 implementation",
                    evidence_id=node.id,
                )
        return ctx.ok("no critical advisory applies")

    registry.register_auditor("auth.hash.audit.v1", audit_hash)
    return registry


def saved_auth_session(root: Path) -> Session:
    base = Session.build(rounds=2)
    base.graph.add_node(
        NodeType.SOURCE,
        "Vendor advisory feed",
        id="source:vendor-advisory",
    )
    runner = Runner("auth", graph=base.graph, registry=deployment())
    runner.task(
        "schema",
        worker_key="auth.schema.v1",
        assumes="the schema store is available",
        produces=("Auth Schema",),
    )
    runner.task(
        "hashing",
        worker_key="auth.hash.argon2.v1",
        auditor_key="auth.hash.audit.v1",
        assumes="Argon2 has no open advisory",
        needs=("schema",),
        produces=("Password Hasher",),
    )
    runner.task(
        "routes",
        worker_key="auth.routes.v1",
        assumes="the hasher interface is stable",
        needs=("hashing",),
        produces=("Auth Routes",),
    )
    runner.task(
        "tokens",
        worker_key="auth.tokens.v1",
        assumes="the signing key is provisioned",
        needs=("schema",),
        produces=("Tokens",),
    )
    first = runner.run()
    assert first.complete
    base.runner = runner
    base.save(root)
    return Session.load(root, registry=deployment())


class ControlledWriteIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "session"
        self.session = saved_auth_session(self.root)
        self.checkpointer = Checkpointer(self.session)

    def tearDown(self):
        self.temp.cleanup()

    def submit_cve(self):
        result = mesh_submit_evidence(
            self.session,
            self.checkpointer,
            text="CVE-2026-9999 affects Argon2",
            source_id="source:vendor-advisory",
            external_id="CVE-2026-9999",
            metadata={"package": "argon2", "severity": "critical"},
        )
        self.session = result.session
        return result

    def test_recheck_is_a_registered_auditor_decision_not_a_client_verdict(self):
        evidence = self.submit_cve().payload["evidence_id"]
        result = mesh_recheck(self.session, self.checkpointer)
        self.session = result.session

        runner = self.session.runner
        assert runner is not None
        hashing = runner["hashing"]
        routes = runner["routes"]
        schema = runner["schema"]
        tokens = runner["tokens"]
        assumption = runner.graph.assumptions[hashing.assumption_id]

        self.assertIs(assumption.status, AssumptionStatus.REJECTED)
        self.assertIs(hashing.state, TaskState.STALE)
        self.assertIs(routes.state, TaskState.STALE)
        self.assertIs(schema.state, TaskState.DONE)
        self.assertIs(tokens.state, TaskState.DONE)
        self.assertEqual(result.payload["audited"], 1)
        self.assertEqual(len(result.payload["invalidations"]), 1)

        contradictions = [
            edge
            for edge in runner.graph.edges.values()
            if edge.type is EdgeType.CONTRADICTS
        ]
        self.assertEqual(len(contradictions), 1)
        self.assertEqual(contradictions[0].src, evidence)
        self.assertEqual(contradictions[0].dst, assumption.id)
        self.assertEqual(
            sum(
                1
                for node in runner.graph.nodes.values()
                if node.type is NodeType.EVIDENCE and node.id == evidence
            ),
            1,
        )

    def test_recheck_checkpoint_contention_leaves_the_live_session_unchanged(self):
        self.submit_cve()
        runner = self.session.runner
        assert runner is not None
        hashing = runner["hashing"]
        assumption = runner.graph.assumptions[hashing.assumption_id]
        generation = self.session.generation
        head = runner.ledger.head

        assert self.session.path is not None
        with writer_lock(self.session.path):
            with self.assertRaises(ControlledWriteError):
                mesh_recheck(self.session, self.checkpointer)

        self.assertEqual(self.session.generation, generation)
        self.assertEqual(runner.ledger.head, head)
        self.assertIs(hashing.state, TaskState.DONE)
        self.assertIs(assumption.status, AssumptionStatus.ACTIVE)
        self.assertFalse(
            any(
                edge.type is EdgeType.CONTRADICTS
                for edge in runner.graph.edges.values()
            )
        )

    def test_unknown_repair_key_fails_before_the_committed_session_changes(self):
        self.submit_cve()
        rechecked = mesh_recheck(self.session, self.checkpointer)
        self.session = rechecked.session
        runner = self.session.runner
        assert runner is not None
        generation = self.session.generation
        binding = dict(runner["hashing"].binding())
        head = runner.ledger.head

        with self.assertRaisesRegex(Exception, "not registered"):
            mesh_repair(
                self.session,
                self.checkpointer,
                task="hashing",
                worker_key="auth.hash.unknown.v9",
                assumes="replacement ground",
            )

        self.assertEqual(self.session.generation, generation)
        self.assertEqual(runner.ledger.head, head)
        self.assertEqual(runner["hashing"].binding(), binding)
        self.assertIs(runner["hashing"].state, TaskState.STALE)

    def test_repair_and_resume_rerun_only_the_stale_closure(self):
        self.submit_cve()
        self.session = mesh_recheck(self.session, self.checkpointer).session

        repaired = mesh_repair(
            self.session,
            self.checkpointer,
            task="hashing",
            worker_key="auth.hash.bcrypt.v1",
            assumes="bcrypt has no open advisory",
            produces=["Password Hasher"],
        )
        self.session = repaired.session
        runner = self.session.runner
        assert runner is not None
        self.assertEqual(runner["hashing"].worker_key, "auth.hash.bcrypt.v1")
        self.assertIs(runner["hashing"].state, TaskState.STALE)

        resumed = mesh_resume(self.session, self.checkpointer)
        self.session = resumed.session
        final = self.session.runner
        assert final is not None

        self.assertEqual(final["hashing"].output, {"impl": "bcrypt"})
        self.assertEqual(final["routes"].output, {"hasher": "bcrypt"})
        self.assertEqual(final["hashing"].attempt, 2)
        self.assertEqual(final["routes"].attempt, 2)
        self.assertEqual(final["schema"].attempt, 1)
        self.assertEqual(final["tokens"].attempt, 1)
        self.assertEqual(set(resumed.payload["executed"]), {"hashing", "routes"})
        self.assertEqual(set(resumed.payload["cached"]), {"schema", "tokens"})
        self.assertTrue(final.ledger.verify())

    def test_the_complete_flow_survives_fresh_session_objects(self):
        self.submit_cve()

        # Process-boundary equivalent #1: throw away every live object and bind
        # the plan from durable keys in a fresh deployment registry.
        fresh_b = Session.load(self.root, registry=deployment())
        cp_b = Checkpointer(fresh_b)
        after_recheck = mesh_recheck(fresh_b, cp_b).session

        # Process-boundary equivalent #2: another fresh registry sees the stale
        # closure and changes executable identity before any rerun occurs.
        fresh_c = Session.load(self.root, registry=deployment())
        cp_c = Checkpointer(fresh_c)
        repaired = mesh_repair(
            fresh_c,
            cp_c,
            task="hashing",
            worker_key="auth.hash.bcrypt.v1",
            assumes="bcrypt has no open advisory",
            produces=["Password Hasher"],
        ).session
        self.assertEqual(repaired.runner["hashing"].worker_key, "auth.hash.bcrypt.v1")
        self.assertEqual(repaired.runner["hashing"].attempt, 1)

        # Process-boundary equivalent #3: resume from disk, not from the object
        # that performed the repair.
        fresh_d = Session.load(self.root, registry=deployment())
        cp_d = Checkpointer(fresh_d)
        final_write = mesh_resume(fresh_d, cp_d)

        # And read it once more as a verifier that ran none of the mutations.
        verified = Session.load(self.root, registry=deployment())
        runner = verified.runner
        assert runner is not None
        self.assertEqual(runner["hashing"].worker_key, "auth.hash.bcrypt.v1")
        self.assertEqual(runner["hashing"].output, {"impl": "bcrypt"})
        self.assertEqual(runner["routes"].output, {"hasher": "bcrypt"})
        self.assertEqual(
            {task.name: task.attempt for task in runner.tasks},
            {"schema": 1, "hashing": 2, "routes": 2, "tokens": 1},
        )
        self.assertTrue(runner.ledger.verify())
        self.assertEqual(runner.ledger.head, final_write.payload["ledger_head"])
        self.assertGreater(after_recheck.generation, fresh_b.generation - 1)


if __name__ == "__main__":
    unittest.main()
