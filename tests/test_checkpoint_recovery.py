"""A failed controlled checkpoint is recovered only when its commit is live."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contextmesh.model import NodeType
from contextmesh_mcp.session import Checkpointer, Session
from contextmesh_mcp.writes import ControlledWriteError, commit_mutation


class TransactionOwnedRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "session"
        Session.build(rounds=1).save(self.root)
        self.session = Session.load(self.root)
        self.checkpointer = Checkpointer(self.session)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _add_source(label: str):
        def mutate(staged: Session):
            node = staged.graph.add_node(
                NodeType.SOURCE,
                label,
                attrs={"origin": "fixture", "retrieved_at": "fixture"},
            )
            return {"node_id": node.id}, True

        return mutate

    def test_another_writers_next_generation_is_not_mistaken_for_ours(self) -> None:
        before = self.session.generation
        other = Session.load(self.root)
        mine = []
        theirs = []

        def mutate(staged: Session):
            node = staged.graph.add_node(
                NodeType.SOURCE,
                "mine from failed checkpoint",
                attrs={"origin": "fixture", "retrieved_at": "fixture"},
            )
            mine.append(node.id)
            return {"node_id": node.id}, True

        def fail_after_other_writer(_staged: Session) -> None:
            node = other.graph.add_node(
                NodeType.SOURCE,
                "the other writers committed source",
                attrs={"origin": "fixture", "retrieved_at": "fixture"},
            )
            theirs.append(node.id)
            other.save(self.root)
            raise RuntimeError("synthetic checkpoint failure")

        with patch.object(
            Session,
            "checkpoint",
            autospec=True,
            side_effect=fail_after_other_writer,
        ):
            with self.assertRaisesRegex(
                ControlledWriteError, "session checkpoint failed"
            ):
                commit_mutation(self.session, self.checkpointer, mutate)

        self.assertIs(self.checkpointer.session, self.session)
        self.assertEqual(self.checkpointer.commits, 0)
        self.assertEqual(self.session.generation, before)

        committed = Session.load(self.root)
        self.assertEqual(committed.generation, before + 1)
        self.assertIn(theirs[0], committed.graph.nodes)
        self.assertNotIn(mine[0], committed.graph.nodes)

    def test_recovery_reads_pinned_target_not_retargeted_public_alias(self) -> None:
        alias = self.root.parent / "recovery-alias"
        other = self.root.parent / "other-session"
        Session.build(rounds=1).save(other)
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink creation unavailable: {exc}")

        loaded = Session.load(alias)
        cp = Checkpointer(loaded)
        mine = []

        def mutate(staged: Session):
            node = staged.graph.add_node(
                NodeType.SOURCE,
                "staged only in redirected recovery",
                attrs={"origin": "fixture", "retrieved_at": "fixture"},
            )
            mine.append(node.id)
            return {"node_id": node.id}, True

        def fail_after_publishing_elsewhere(staged: Session) -> None:
            staged.save(other)
            alias.unlink()
            alias.symlink_to(other, target_is_directory=True)
            raise RuntimeError("synthetic checkpoint failure after alias retarget")

        try:
            with patch.object(
                Session, "checkpoint", autospec=True, side_effect=fail_after_publishing_elsewhere
            ):
                with self.assertRaisesRegex(
                    ControlledWriteError, "session checkpoint failed"
                ):
                    commit_mutation(loaded, cp, mutate)
        finally:
            try:
                alias.unlink()
            except OSError:
                pass

        self.assertEqual(cp.commits, 0)
        original = Session.load(self.root)
        redirected = Session.load(other)
        self.assertEqual(original.generation, 1)
        self.assertNotIn(mine[0], original.graph.nodes)
        self.assertEqual(redirected.generation, 2)
        self.assertIn(mine[0], redirected.graph.nodes)

    def test_failure_after_our_manifest_swap_recovers_the_committed_clone(self) -> None:
        before = self.session.generation

        with patch.object(
            Session,
            "_sweep",
            side_effect=RuntimeError("synthetic cleanup failure"),
        ):
            result = commit_mutation(
                self.session,
                self.checkpointer,
                self._add_source("source committed before cleanup failed"),
            )

        self.assertTrue(result.changed)
        self.assertIs(self.checkpointer.session, result.session)
        self.assertEqual(self.checkpointer.commits, 1)
        self.assertEqual(result.session.generation, before + 1)

        committed = Session.load(self.root)
        self.assertEqual(committed.generation, before + 1)
        self.assertIn(result.payload["node_id"], committed.graph.nodes)


if __name__ == "__main__":
    unittest.main()
