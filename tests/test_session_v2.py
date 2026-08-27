"""One session, four companions, one commit.

Session v1 held a graph and the resolver that reads it. v2 adds the execution
plan and its run ledger, so a restart brings back not just what is known but
what was being done about it::

    session/
      session.json          the manifest, and the only thing that commits
      graph-000003.json     what is known
      resolver-000003.json  how a question finds it
      execution-000003.json the plan, mid-repair
      runledger-000003.json the record of it getting there

The interesting failures are between the files rather than inside them. Each
companion already refuses its own corruption — the ledger's chain, the plan's
references, the graph's ontology — and every one of those checks can pass while
the four describe different runs. So this suite is mostly about the seams: a
ledger naming a task the plan does not hold, a plan without its history, a
session that needs a registry nobody brought.

One thing a directory can never carry is the registry. A key means something
only because a running process was configured to say so, which is the whole
point of 7A — so a session holding an execution is restorable only by a
deployment that brings one.
"""

import json
import tempfile
import unittest
from pathlib import Path

from contextmesh.execute import Runner, TaskRegistry
from contextmesh_mcp.session import (
    READABLE_VERSIONS,
    SESSION_FILE,
    SESSION_VERSION,
    Session,
    SessionError,
)


class Advisory:
    def __init__(self):
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
    """A session whose plan is mid-repair: argon2 disproved, bcrypt not yet run."""
    base = Session.build(rounds=2)
    advisory = Advisory()
    runner = Runner("auth", graph=base.graph, registry=deployment(advisory))
    runner.task("schema", worker_key="auth.schema.v1", assumes="sqlite is fine",
                produces=("Schema",))
    runner.task("hashing", worker_key="auth.hash.argon2.v1",
                auditor_key="auth.hash.audit.v1",
                assumes="Argon2 has no open advisory",
                needs=("schema",), produces=("Password Hasher",))
    runner.task("tokens", worker_key="auth.tokens.v1", assumes="JWT is fine",
                needs=("schema",), produces=("Tokens",))
    runner.run()
    advisory.published = True
    runner.recheck()
    runner.repair("hashing", assumes="bcrypt has no open advisory",
                  worker_key="auth.hash.bcrypt.v1")
    base.runner = runner
    return base


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "session"

    def manifest(self):
        return json.loads((self.dir / SESSION_FILE).read_text(encoding="utf-8"))

    def test_a_session_with_a_plan_names_all_four_companions(self):
        session_with_a_plan().save(self.dir)
        manifest = self.manifest()
        self.assertEqual(manifest["version"], SESSION_VERSION)
        for field in ("graph", "resolver", "execution", "ledger"):
            self.assertIsNotNone(manifest[field], field)
            self.assertTrue((self.dir / manifest[field]).is_file(), field)

    def test_a_session_without_a_plan_names_them_null_rather_than_absent(self):
        """Present-and-null distinguishes "no execution" from "field missing"."""
        Session.build(rounds=2).save(self.dir)
        manifest = self.manifest()
        self.assertIn("execution", manifest)
        self.assertIn("ledger", manifest)
        self.assertIsNone(manifest["execution"])
        self.assertIsNone(manifest["ledger"])

    def test_every_companion_is_named_for_its_generation(self):
        session = session_with_a_plan()
        for expected in (1, 2, 3):
            session.save(self.dir)
            manifest = self.manifest()
            self.assertEqual(manifest["generation"], expected)
            for field in ("graph", "resolver", "execution", "ledger"):
                self.assertIn(f"-{expected:06d}.json", manifest[field], field)

    def test_superseded_generations_of_every_companion_are_swept(self):
        session = session_with_a_plan()
        session.save(self.dir)
        first = self.manifest()
        session.save(self.dir)
        for field in ("graph", "resolver", "execution", "ledger"):
            self.assertFalse((self.dir / first[field]).exists(), field)

    def test_a_save_never_writes_a_file_the_live_manifest_names(self):
        """The generation rule, extended to the two new companions."""
        session = session_with_a_plan()
        session.save(self.dir)
        live = self.manifest()
        names = {live[f] for f in ("graph", "resolver", "execution", "ledger")}
        session.save(self.dir)
        after = self.manifest()
        self.assertFalse(
            names & {after[f] for f in ("graph", "resolver", "execution", "ledger")}
        )


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "session"

    def test_the_plan_comes_back_mid_repair(self):
        before = session_with_a_plan()
        before.save(self.dir)
        ready = before.runner._ready()

        after = Session.load(self.dir, registry=deployment())
        self.assertIsNotNone(after.runner)
        self.assertEqual(after.runner.plan, "auth")
        self.assertEqual(after.runner.round, before.runner.round)
        self.assertEqual(after.runner._ready(), ready)
        self.assertEqual(
            after.runner["hashing"].worker_key, "auth.hash.bcrypt.v1"
        )
        self.assertEqual(after.runner["hashing"].attempt, 1)

    def test_the_ledger_comes_back_at_the_head_it_was_saved_at(self):
        before = session_with_a_plan()
        before.save(self.dir)
        after = Session.load(self.dir, registry=deployment())
        self.assertEqual(after.runner.ledger.head, before.runner.ledger.head)
        self.assertTrue(after.runner.ledger.verify())

    def test_the_restored_plan_reruns_only_the_stale_closure(self):
        before = session_with_a_plan()
        before.save(self.dir)

        published = Advisory()
        published.published = True
        after = Session.load(self.dir, registry=deployment(published))
        after.runner.run()

        self.assertEqual(after.runner["hashing"].output, {"impl": "bcrypt"})
        self.assertEqual(after.runner["hashing"].attempt, 2)
        for untouched in ("schema", "tokens"):
            self.assertEqual(after.runner[untouched].attempt, 1, untouched)

    def test_re_saving_a_restored_session_reproduces_every_companion(self):
        session_with_a_plan().save(self.dir)
        first = Session.load(self.dir, registry=deployment())
        text = {
            name: (self.dir / name).read_text(encoding="utf-8")
            for name in ("execution", "ledger")
            for name in [json.loads((self.dir / SESSION_FILE).read_text())[name]]
        }
        first.save(self.dir)
        manifest = json.loads((self.dir / SESSION_FILE).read_text())
        for old_name, body in text.items():
            stem = old_name.rsplit("-", 1)[0]
            new_name = next(
                manifest[f] for f in ("execution", "ledger")
                if manifest[f].startswith(stem + "-")
            )
            self.assertEqual((self.dir / new_name).read_text(encoding="utf-8"), body)

    def test_a_session_with_no_plan_round_trips_without_one(self):
        Session.build(rounds=2).save(self.dir)
        restored = Session.load(self.dir)
        self.assertIsNone(restored.runner)


class RegistryTest(unittest.TestCase):
    """A directory cannot carry what a key means."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "session"
        session_with_a_plan().save(self.dir)

    def test_a_session_with_an_execution_needs_a_registry(self):
        with self.assertRaises(SessionError) as caught:
            Session.load(self.dir)
        self.assertIn("no TaskRegistry was given", str(caught.exception))

    def test_a_deployment_missing_a_key_refuses_the_session(self):
        partial = TaskRegistry()
        partial.register_worker("auth.schema.v1", lambda ctx: {"ok": True})
        with self.assertRaises(SessionError) as caught:
            Session.load(self.dir, registry=partial)
        self.assertIn("auth.hash.bcrypt.v1", str(caught.exception))

    def test_a_session_without_an_execution_needs_no_registry(self):
        plain = Path(self.tmp.name) / "plain"
        Session.build(rounds=2).save(plain)
        self.assertIsNone(Session.load(plain).runner)


class SeamTest(unittest.TestCase):
    """Each companion can be sound on its own and still not belong here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "session"
        session_with_a_plan().save(self.dir)
        self.manifest = json.loads((self.dir / SESSION_FILE).read_text())

    def rewrite(self, field, payload):
        (self.dir / self.manifest[field]).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def refuses(self, fragment, registry=None):
        with self.assertRaises(SessionError) as caught:
            Session.load(self.dir, registry=registry or deployment())
        self.assertIn(fragment, str(caught.exception))

    def test_a_ledger_naming_a_task_the_plan_does_not_hold_is_refused(self):
        """Both files verify; they are simply about different runs."""
        from contextmesh.execute import Event, RunLedger

        stranger = RunLedger()
        stranger.record(1, Event.EXECUTED, "a_task_from_elsewhere", "ran")
        self.assertTrue(stranger.verify())
        self.rewrite("ledger", stranger.snapshot())
        self.refuses("not a task in")

    def test_a_ledger_running_ahead_of_the_plans_round_is_refused(self):
        from contextmesh.execute import Event, RunLedger

        ahead = RunLedger()
        ahead.record(99, Event.EXECUTED, "schema", "ran")
        self.rewrite("ledger", ahead.snapshot())
        self.refuses("runs ahead of the plan")

    def test_an_execution_without_a_ledger_is_refused(self):
        self.manifest["ledger"] = None
        (self.dir / SESSION_FILE).write_text(
            json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8"
        )
        self.refuses("committed together or not at all")

    def test_a_ledger_without_an_execution_is_refused(self):
        self.manifest["execution"] = None
        (self.dir / SESSION_FILE).write_text(
            json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8"
        )
        self.refuses("committed together or not at all")

    def test_an_execution_naming_a_plan_this_graph_never_ran_is_refused(self):
        plan = json.loads((self.dir / self.manifest["execution"]).read_text())
        plan["plan"] = "somewhere-else"
        self.rewrite("execution", plan)
        self.refuses("is not in this graph")

    def test_a_broken_ledger_chain_is_refused_at_the_session_boundary(self):
        ledger = json.loads((self.dir / self.manifest["ledger"]).read_text())
        ledger["entries"][0]["detail"] = "something else"
        self.rewrite("ledger", ledger)
        self.refuses("digest does not match")

    def test_the_companions_are_checked_by_their_own_loaders_not_re_implemented(self):
        """The seam checks add to the per-file ones; they do not replace them."""
        plan = json.loads((self.dir / self.manifest["execution"]).read_text())
        plan["tasks"][0]["state"] = "invented"
        self.rewrite("execution", plan)
        self.refuses("not a state this build knows")


class VersionOneTest(unittest.TestCase):
    """A v1 directory is a real thing this project shipped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "session"
        Session.build(rounds=2).save(self.dir)
        # Rewrite the manifest as v1 wrote it: two companions, no nulls.
        manifest = json.loads((self.dir / SESSION_FILE).read_text())
        manifest["version"] = 1
        del manifest["execution"], manifest["ledger"]
        (self.dir / SESSION_FILE).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def test_both_versions_are_readable(self):
        self.assertEqual(READABLE_VERSIONS, (1, 2))
        self.assertEqual(SESSION_VERSION, 2)

    def test_a_v1_directory_loads_as_a_session_with_no_execution(self):
        restored = Session.load(self.dir)
        self.assertIsNone(restored.runner)
        self.assertGreater(len(restored.graph.nodes), 0)

    def test_saving_a_v1_directory_upgrades_it(self):
        restored = Session.load(self.dir)
        restored.save(self.dir)
        manifest = json.loads((self.dir / SESSION_FILE).read_text())
        self.assertEqual(manifest["version"], 2)
        self.assertIn("execution", manifest)
        self.assertIsNone(manifest["execution"])

    def test_a_v1_manifest_missing_a_required_companion_is_still_refused(self):
        manifest = json.loads((self.dir / SESSION_FILE).read_text())
        del manifest["resolver"]
        (self.dir / SESSION_FILE).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaises(SessionError) as caught:
            Session.load(self.dir)
        self.assertIn("resolver", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
