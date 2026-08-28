"""PR #8A: observation enters as evidence, never as a client verdict."""

import inspect
import math
import tempfile
import unittest
from pathlib import Path

from contextmesh.evidence import (
    EvidenceConflictError,
    EvidenceIntake,
    EvidenceIntakeError,
    canonical_payload,
    submit_evidence,
)
from contextmesh.graph import ContextGraph
from contextmesh.model import (
    Assumption,
    AssumptionStatus,
    EdgeType,
    NodeType,
    Provenance,
    slug,
)
from contextmesh_mcp import writes
from contextmesh_mcp.session import Checkpointer, Session, writer_lock


class EvidenceIntakeTest(unittest.TestCase):
    def setUp(self):
        self.graph = ContextGraph()
        self.source = self.graph.add_node(
            NodeType.SOURCE,
            "NVD feed",
            id="source:nvd",
            attrs={"origin": "nvd", "retrieved_at": "fixture"},
            provenance=Provenance(
                source_id="fixture",
                span=None,
                extractor="test",
                checks=[],
                recorded_at_build=0,
            ),
        )
        self.decision = self.graph.add_node(
            NodeType.DECISION,
            "Use Argon2id",
            id="decision:argon",
            attrs={"rationale": "fixture", "at_build": 0},
        )

    def submit(self, **overrides):
        values = {
            "text": "CVE-2026-9999 affects argon2-cffi",
            "source_id": self.source.id,
            "external_id": "CVE-2026-9999",
            "metadata": {"package": "argon2-cffi", "severity": "critical"},
        }
        values.update(overrides)
        return submit_evidence(self.graph, **values)

    def test_valid_evidence_is_one_detached_evidence_node(self):
        before_edges = dict(self.graph.edges)
        receipt = self.submit()
        self.assertTrue(receipt.created)
        self.assertIs(receipt.node.type, NodeType.EVIDENCE)
        self.assertEqual(self.graph.edges, before_edges)
        self.assertEqual(receipt.node.provenance.source_id, self.source.id)

    def test_external_id_is_optional(self):
        receipt = self.submit(external_id=None)
        self.assertTrue(receipt.created)
        self.assertIsNone(receipt.node.attrs["evidence_intake"]["external_id"])

    def test_bad_text_is_refused_before_mutation(self):
        before = self.graph.to_dict()
        for value in ("", "   ", None, 3, True):
            with self.subTest(value=value), self.assertRaises(EvidenceIntakeError):
                self.submit(text=value)
            self.assertEqual(self.graph.to_dict(), before)

    def test_bad_source_is_refused_before_mutation(self):
        before = self.graph.to_dict()
        for value in ("", "source:missing", self.decision.id, 5):
            with self.subTest(value=value), self.assertRaises(EvidenceIntakeError):
                self.submit(source_id=value)
            self.assertEqual(self.graph.to_dict(), before)

    def test_bad_external_id_is_refused(self):
        for value in ("", "   ", 4, True, "bad\nvalue"):
            with self.subTest(value=value), self.assertRaises(EvidenceIntakeError):
                self.submit(external_id=value)

    def test_metadata_is_strict_recursively(self):
        bad = (
            "not-an-object",
            {1: "integer key"},
            {"nested": {2: "integer key"}},
            {"tuple": (1, 2)},
            {"set": {1, 2}},
            {"bytes": b"x"},
            {"nan": math.nan},
            {"inf": math.inf},
            {"callable": lambda: None},
        )
        for value in bad:
            with self.subTest(value=repr(value)), self.assertRaises(EvidenceIntakeError):
                self.submit(metadata=value)

    def test_metadata_is_detached_from_caller_memory(self):
        metadata = {"packages": ["argon2-cffi"], "nested": {"severity": "high"}}
        receipt = self.submit(metadata=metadata)
        digest = receipt.node.attrs["evidence_intake"]["payload_digest"]
        metadata["packages"][0] = "bcrypt"
        metadata["nested"]["severity"] = "low"
        self.assertEqual(
            receipt.node.attrs["evidence_intake"]["metadata"],
            {"packages": ["argon2-cffi"], "nested": {"severity": "high"}},
        )
        self.assertEqual(receipt.node.attrs["evidence_intake"]["payload_digest"], digest)

    def test_identical_replay_is_idempotent(self):
        first = self.submit()
        second = self.submit()
        self.assertFalse(second.created)
        self.assertEqual(second.evidence_id, first.evidence_id)
        self.assertEqual(len(self.graph.by_type(NodeType.EVIDENCE, live_only=False)), 1)

    def test_same_external_id_with_different_content_is_refused(self):
        self.submit()
        with self.assertRaises(EvidenceConflictError):
            self.submit(text="different report")

    def test_replay_recomputes_the_stored_payload_not_only_the_digest_field(self):
        receipt = self.submit()
        receipt.node.label = "forged label"
        with self.assertRaises(EvidenceConflictError):
            self.submit()

    def test_replay_refuses_a_corrupt_stored_digest(self):
        receipt = self.submit()
        receipt.node.attrs["evidence_intake"]["payload_digest"] = "0" * 64
        with self.assertRaises(EvidenceConflictError):
            self.submit()

    def test_deterministic_id_collision_with_a_non_evidence_node_is_refused(self):
        metadata = {"package": "argon2-cffi", "severity": "critical"}
        canonical = canonical_payload(
            text="CVE-2026-9999 affects argon2-cffi",
            source_id=self.source.id,
            external_id="CVE-2026-9999",
            metadata=metadata,
        )
        eid = slug(canonical, "evidence")
        self.graph.add_node(NodeType.CLAIM, "collision", id=eid)
        with self.assertRaises(EvidenceConflictError):
            self.submit()

    def test_duplicate_external_id_records_are_refused_as_corruption(self):
        first = self.submit()
        duplicate = self.graph.add_node(
            NodeType.EVIDENCE,
            "other",
            id="evidence:manual-duplicate",
            attrs={
                "kind": "observation",
                "evidence_intake": {
                    "version": 1,
                    "external_id": "CVE-2026-9999",
                    "payload_digest": first.node.attrs["evidence_intake"]["payload_digest"],
                    "metadata": {"package": "argon2-cffi", "severity": "critical"},
                },
            },
            provenance=first.node.provenance,
        )
        self.assertIsNotNone(duplicate)
        with self.assertRaises(EvidenceConflictError):
            self.submit(text="different report")

    def test_subtraction_invariant_freezes_belief_and_edges(self):
        assumption = Assumption(
            id="assumption:argon",
            statement="argon2 is safe",
            status=AssumptionStatus.ACTIVE,
        )
        self.graph.add_assumption(assumption)
        before_edges = self.graph.to_dict()["edges"]
        before_assumptions = self.graph.to_dict()["assumptions"]
        before_invalidated = {
            n.id: n.invalidated for n in self.graph.nodes.values()
        }
        before_nodes = len(self.graph.nodes)
        receipt = self.submit()
        self.assertEqual(len(self.graph.nodes), before_nodes + 1)
        self.assertIs(receipt.node.type, NodeType.EVIDENCE)
        self.assertEqual(self.graph.to_dict()["edges"], before_edges)
        self.assertEqual(self.graph.to_dict()["assumptions"], before_assumptions)
        for node_id, invalidated in before_invalidated.items():
            self.assertEqual(self.graph.node(node_id).invalidated, invalidated)
        self.assertFalse(
            any(
                e.type in (EdgeType.CONTRADICTS, EdgeType.SUPPORTS)
                for e in self.graph.edges.values()
            )
        )

    def test_intake_signature_has_no_verdict_or_edge_authority(self):
        params = set(inspect.signature(EvidenceIntake.submit).parameters)
        self.assertEqual(params, {"self", "text", "source_id", "external_id", "metadata"})
        for forbidden in (
            "edge",
            "edge_type",
            "target",
            "target_id",
            "assumption_id",
            "verdict",
            "reject",
            "invalidate",
            "status",
        ):
            self.assertNotIn(forbidden, params)

    def test_replay_and_conflict_survive_graph_round_trip(self):
        self.submit()
        restored = ContextGraph.from_dict(self.graph.to_dict())
        replay = submit_evidence(
            restored,
            text="CVE-2026-9999 affects argon2-cffi",
            source_id=self.source.id,
            external_id="CVE-2026-9999",
            metadata={"package": "argon2-cffi", "severity": "critical"},
        )
        self.assertFalse(replay.created)
        with self.assertRaises(EvidenceConflictError):
            submit_evidence(
                restored,
                text="different",
                source_id=self.source.id,
                external_id="CVE-2026-9999",
            )


class DurableEvidenceWriteTest(unittest.TestCase):
    def _persistent(self, root: Path):
        built = Session.build(rounds=1)
        built.save(root)
        loaded = Session.load(root)
        cp = Checkpointer(loaded, "every-ask")
        source = next(n for n in loaded.graph.nodes.values() if n.type is NodeType.SOURCE)
        return loaded, cp, source.id

    def test_success_commits_and_returns_a_new_live_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, cp, source_id = self._persistent(Path(tmp))
            before = session.generation
            result = writes.mesh_submit_evidence(
                session,
                cp,
                text="new observation",
                source_id=source_id,
                external_id="OBS-1",
            )
            self.assertTrue(result.changed)
            self.assertIsNot(result.session, session)
            self.assertEqual(result.session.generation, before + 1)
            self.assertIs(cp.session, result.session)
            restored = Session.load(Path(tmp))
            self.assertIn(result.payload["evidence_id"], restored.graph.nodes)

    def test_replay_does_not_create_an_extra_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, cp, source_id = self._persistent(Path(tmp))
            first = writes.mesh_submit_evidence(
                session, cp, text="same", source_id=source_id, external_id="OBS-2"
            )
            generation = first.session.generation
            second = writes.mesh_submit_evidence(
                first.session, cp, text="same", source_id=source_id, external_id="OBS-2"
            )
            self.assertFalse(second.changed)
            self.assertFalse(second.payload["created"])
            self.assertEqual(second.session.generation, generation)

    def test_contention_leaves_the_live_session_byte_equivalent(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, cp, source_id = self._persistent(Path(tmp))
            before = session.graph.to_dict()
            with writer_lock(Path(tmp)):
                with self.assertRaises(writes.ControlledWriteError):
                    writes.mesh_submit_evidence(
                        session, cp, text="contended", source_id=source_id
                    )
            self.assertEqual(session.graph.to_dict(), before)
            self.assertEqual(cp.pending, 0)
            self.assertEqual(cp.contended, 1)

    def test_never_policy_refuses_before_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, _, source_id = self._persistent(Path(tmp))
            cp = Checkpointer(session, "never")
            before = session.graph.to_dict()
            with self.assertRaises(writes.ControlledWriteError):
                writes.mesh_submit_evidence(
                    session, cp, text="not durable", source_id=source_id
                )
            self.assertEqual(session.graph.to_dict(), before)

    def test_ephemeral_demo_refuses_before_mutating(self):
        session = Session.build(rounds=1)
        cp = Checkpointer(session, "every-ask")
        source = next(n for n in session.graph.nodes.values() if n.type is NodeType.SOURCE)
        before = session.graph.to_dict()
        with self.assertRaises(writes.ControlledWriteError):
            writes.mesh_submit_evidence(
                session, cp, text="ephemeral", source_id=source.id
            )
        self.assertEqual(session.graph.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
