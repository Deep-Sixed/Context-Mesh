"""Session v2 manifest: exact fields, three-way companions, unique JSON keys."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contextmesh_mcp.session import Session, SessionError
from test_session_v2 import deployment, session_with_a_plan


def _save_plan() -> Path:
    target = Path(tempfile.mkdtemp()) / "session"
    session_with_a_plan().save(target)
    return target


def _save_graph_only() -> Path:
    target = Path(tempfile.mkdtemp()) / "session"
    Session.build(rounds=1).save(target)
    return target


def _rewrite(directory: Path, **updates):
    path = directory / "session.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(updates)
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")


class ExactManifestTest(unittest.TestCase):
    def test_unknown_v2_field_is_refused(self):
        directory = _save_graph_only()
        _rewrite(directory, approved_by="alice")
        with self.assertRaises(SessionError) as caught:
            Session.load(directory)
        self.assertIn("approved_by", str(caught.exception))
        self.assertIn("does not define", str(caught.exception))


class ThreeWayInvariantTest(unittest.TestCase):
    def test_null_head_with_an_execution_is_refused(self):
        directory = _save_plan()
        data = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(data["execution"])
        self.assertIsNotNone(data["ledger_head"])
        _rewrite(directory, ledger_head=None)
        with self.assertRaises(SessionError) as caught:
            Session.load(directory, registry=deployment())
        message = str(caught.exception)
        self.assertIn("ledger_head", message)
        self.assertIn("not null", message)

    def test_head_without_execution_is_refused(self):
        directory = _save_graph_only()
        data = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        self.assertIsNone(data["execution"])
        _rewrite(directory, ledger_head="a" * 64)
        with self.assertRaises(SessionError) as caught:
            Session.load(directory)
        self.assertIn("three fields travel together", str(caught.exception))

    def test_malformed_head_with_an_execution_is_refused(self):
        directory = _save_plan()
        for bad in ("", "abc", "A" * 64, "g" * 64, "0" * 63, "0" * 65, 12345):
            with self.subTest(bad=bad):
                _rewrite(directory, ledger_head=bad)
                with self.assertRaises(SessionError) as caught:
                    Session.load(directory, registry=deployment())
                self.assertIn("ledger_head", str(caught.exception))


class DuplicateManifestKeyTest(unittest.TestCase):
    def test_duplicate_ledger_head_is_refused(self):
        directory = _save_plan()
        path = directory / "session.json"
        text = path.read_text(encoding="utf-8")
        self.assertIn('"ledger_head":', text)
        text = text.replace('"ledger_head":', '"ledger_head": null,\n  "ledger_head":', 1)
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(SessionError) as caught:
            Session.load(directory, registry=deployment())
        self.assertIn("duplicate JSON key", str(caught.exception))
        self.assertIn("ledger_head", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
