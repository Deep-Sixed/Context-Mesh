"""Session stale-writer checks follow directory identity, not path spelling."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from contextmesh.model import NodeType
from contextmesh_mcp.session import Checkpointer, Session, SessionError
from contextmesh_mcp.writes import mesh_submit_evidence


class SessionPathIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self.directory = self.root / "session"
        Session.build(rounds=1).save(self.directory)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _readers(self) -> tuple[Session, Session]:
        return Session.load(self.directory), Session.load(self.directory)

    def _advance(self, writer: Session) -> str:
        node = writer.graph.add_node(
            NodeType.SOURCE,
            "newer observation source",
            attrs={"origin": "fixture", "retrieved_at": "fixture"},
        )
        writer.checkpoint()
        self.assertEqual(Session.load(self.directory).generation, 2)
        return node.id

    def _json_state(self) -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in self.directory.iterdir()
            if path.suffix == ".json"
        }

    def _assert_alias_refuses_stale_writer(self, alias: Path) -> None:
        current, stale = self._readers()
        newer_id = self._advance(current)
        before = self._json_state()

        with self.assertRaisesRegex(SessionError, "another writer has committed"):
            stale.save(alias)

        self.assertEqual(self._json_state(), before)
        restored = Session.load(self.directory)
        self.assertEqual(restored.generation, 2)
        self.assertIn(newer_id, restored.graph.nodes)

    def test_same_literal_path_refuses_a_stale_writer(self) -> None:
        self._assert_alias_refuses_stale_writer(self.directory)

    def test_dotdot_alias_refuses_a_stale_writer(self) -> None:
        alias = self.directory / ".." / self.directory.name
        self.assertNotEqual(alias, self.directory)
        self._assert_alias_refuses_stale_writer(alias)

    def test_relative_alias_refuses_a_stale_writer(self) -> None:
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            self._assert_alias_refuses_stale_writer(Path("session"))
        finally:
            os.chdir(previous)

    def test_relative_load_keeps_stale_writer_identity_after_chdir(self) -> None:
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            stale = Session.load("session")
        finally:
            os.chdir(previous)

        current = Session.load(self.directory)
        newer_id = self._advance(current)
        before = self._json_state()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        try:
            os.chdir(elsewhere)
            with self.assertRaisesRegex(SessionError, "another writer has committed"):
                stale.save(self.directory)
        finally:
            os.chdir(previous)

        self.assertEqual(self._json_state(), before)
        restored = Session.load(self.directory)
        self.assertEqual(restored.generation, 2)
        self.assertIn(newer_id, restored.graph.nodes)

    def test_relative_load_checkpoint_stays_bound_after_chdir(self) -> None:
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            restored = Session.load("session")
        finally:
            os.chdir(previous)

        node = restored.graph.add_node(
            NodeType.SOURCE,
            "checkpoint after chdir",
            attrs={"origin": "fixture", "retrieved_at": "fixture"},
        )
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        try:
            os.chdir(elsewhere)
            restored.checkpoint()
        finally:
            os.chdir(previous)

        self.assertFalse((elsewhere / "session").exists())
        loaded = Session.load(self.directory)
        self.assertEqual(loaded.generation, 2)
        self.assertIn(node.id, loaded.graph.nodes)

    def test_symlink_alias_refuses_a_stale_writer_where_supported(self) -> None:
        alias = self.root / "session-link"
        try:
            alias.symlink_to(self.directory, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink creation unavailable: {exc}")
        self._assert_alias_refuses_stale_writer(alias)

    if os.name == "nt":

        def test_junction_alias_refuses_a_stale_writer_on_windows(self) -> None:
            alias = self.root / "session-junction"
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(alias), str(self.directory)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"directory junction creation failed: {completed.stderr}",
            )
            try:
                self._assert_alias_refuses_stale_writer(alias)
            finally:
                try:
                    alias.rmdir()
                except OSError:
                    pass

    def _make_retargetable_alias(self, alias: Path, target: Path) -> None:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(alias), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"directory junction creation failed: {completed.stderr}",
            )
        else:
            alias.symlink_to(target, target_is_directory=True)

    def _remove_retargetable_alias(self, alias: Path) -> None:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()

    def test_checkpoint_stays_on_original_target_after_alias_is_retargeted(self) -> None:
        first = self.directory
        second = self.root / "second"
        Session.build(rounds=1).save(second)
        alias = self.root / "retargetable-session"
        self._make_retargetable_alias(alias, first)
        loaded = Session.load(alias)
        self.assertEqual(loaded.path, alias.absolute())

        self._remove_retargetable_alias(alias)
        self._make_retargetable_alias(alias, second)
        node = loaded.graph.add_node(
            NodeType.SOURCE,
            "pinned target observation",
            attrs={"origin": "fixture", "retrieved_at": "fixture"},
        )
        try:
            loaded.checkpoint()
        finally:
            self._remove_retargetable_alias(alias)

        original = Session.load(first)
        redirected = Session.load(second)
        self.assertEqual(original.generation, 2)
        self.assertIn(node.id, original.graph.nodes)
        self.assertEqual(redirected.generation, 1)
        self.assertNotIn(node.id, redirected.graph.nodes)

    def test_retargeting_alias_cannot_disable_stale_writer_cas(self) -> None:
        first = self.directory
        second = self.root / "second"
        Session.build(rounds=1).save(second)
        alias = self.root / "retargetable-session"
        self._make_retargetable_alias(alias, first)
        stale = Session.load(alias)
        current = Session.load(first)
        newer_id = self._advance(current)

        self._remove_retargetable_alias(alias)
        self._make_retargetable_alias(alias, second)
        try:
            with self.assertRaisesRegex(SessionError, "another writer has committed"):
                stale.save(first)
        finally:
            self._remove_retargetable_alias(alias)

        original = Session.load(first)
        self.assertEqual(original.generation, 2)
        self.assertIn(newer_id, original.graph.nodes)
        self.assertEqual(Session.load(second).generation, 1)

    def test_controlled_write_clone_keeps_the_pinned_target(self) -> None:
        first = self.directory
        second = self.root / "second"
        Session.build(rounds=1).save(second)
        alias = self.root / "retargetable-session"
        self._make_retargetable_alias(alias, first)
        loaded = Session.load(alias)
        source = next(
            node for node in loaded.graph.nodes.values() if node.type is NodeType.SOURCE
        )
        checkpointer = Checkpointer(loaded)

        self._remove_retargetable_alias(alias)
        self._make_retargetable_alias(alias, second)
        try:
            result = mesh_submit_evidence(
                loaded,
                checkpointer,
                text="controlled write stays on original target",
                source_id=source.id,
                external_id="pinned-target-probe",
            )
        finally:
            self._remove_retargetable_alias(alias)

        evidence_id = result.payload["evidence_id"]
        original = Session.load(first)
        redirected = Session.load(second)
        self.assertEqual(original.generation, 2)
        self.assertIn(evidence_id, original.graph.nodes)
        self.assertEqual(redirected.generation, 1)
        self.assertNotIn(evidence_id, redirected.graph.nodes)

    def test_a_genuinely_different_directory_remains_a_save_as(self) -> None:
        current, stale = self._readers()
        newer_id = self._advance(current)
        other = self.root / "copy"

        written = stale.save(other)

        self.assertTrue(os.path.samefile(written, other))
        copied = Session.load(other)
        self.assertEqual(copied.generation, 1)
        self.assertNotIn(newer_id, copied.graph.nodes)
        original = Session.load(self.directory)
        self.assertEqual(original.generation, 2)
        self.assertIn(newer_id, original.graph.nodes)


if __name__ == "__main__":
    unittest.main()
