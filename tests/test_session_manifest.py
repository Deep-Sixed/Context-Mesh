"""Session v2 manifest: exact fields, three-way companions, unique JSON keys."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contextmesh.execute import Runner, TaskRegistry
from contextmesh_mcp.session import Session, SessionError


class Advisory:
    def __init__(self) -> None:
        self.published = False

    def __call__(self, ctx):
        if self.published and ctx.output.get("impl") == "argon2":
            return ctx.disproved("CVE-2026-9999 published for argon2id")
        return True


def deployment(advisory=None):
    registry = TaskRegistry()
    registry.register_worker("auth.schema.v1", lambda ctx: {"ok": True})
    registry.register_worker("auth.hash.argon2.v1", lambda ctx: {"impl": "argon2"})
    registry.register_worker("auth.hash.bcrypt.v1", lambda ctx: {"impl": "bcrypt"})
    registry.register_worker("auth.tokens.v1", lambda ctx: {"ok": True})
    registry.register_auditor("auth.hash.audit.v1", advisory or Advisory())
    return registry


def session_with_a_plan():
    base = Session.build(rounds=2)
    advisory = Advisory()
    runner = Runner("auth", graph=base.graph, registry=deployment(advisory))
    runner.task(
        "schema",
        worker_key="auth.schema.v1",
        assumes="sqlite is fine",
        produces=("Schema",),
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
        "tokens",
        worker_key="auth.tokens.v1",
        assumes="JWT is fine",
        needs=("schema",),
        produces=("Tokens",),
    )
    runner.run()
    advisory.published = True
    runner.recheck()
    runner.repair(
        "hashing",
        assumes="bcrypt has no open advisory",
        worker_key="auth.hash.bcrypt.v1",
    )
    base.runner = runner
    return base


def _save_plan() -> Path:
    target = Path(tempfile.mkdtemp()) / "session"
    session_with_a_plan().save(target)
    return target


def _save_graph_only() -> Path:
    target = Path(tempfile.mkdtemp()) / "session"
    Session.build(rounds=1).save(target)
    return target


def _rewrite(directory: Path, **updates) -> None:
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
        text = text.replace(
            '"ledger_head":',
            '"ledger_head": null,\n  "ledger_head":',
            1,
        )
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(SessionError) as caught:
            Session.load(directory, registry=deployment())
        self.assertIn("duplicate JSON key", str(caught.exception))
        self.assertIn("ledger_head", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
