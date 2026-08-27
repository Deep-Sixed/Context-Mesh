"""A checkpoint cannot hold code, so it holds a name — and a name is a promise.

This suite is the boundary between the two, and it is built around one failure:

    run 1   task bound to argon2
              ↓  CVE published, auditor disproves the assumption
            repaired to bcrypt
              ↓  checkpoint written
            process dies
              ↓
    run 2   must come back bound to *bcrypt*

Everything else here exists to make sure the name in that checkpoint can only
ever mean one thing. A key that is registered twice, a worker key looked up in
the auditor namespace, a task carrying both a callable and a key that could
disagree with it — each is a way for the restored process to run code the
checkpoint did not name, so each is refused rather than resolved.

The registry itself is deployment configuration, never file state. There is no
import, no module path, no qualified name: the only route from a string to a
callable is a table this process filled in deliberately.
"""

import unittest
from pathlib import Path

from contextmesh.execute import (
    ExecutionCheckpointError,
    ExecutionError,
    Runner,
    Task,
    TaskRegistry,
    TaskState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def worker(tag):
    """A worker that reports which implementation ran."""

    def run(ctx):
        return {"implementation": tag}

    return run


def auditor(verdict=True):
    def audit(ctx):
        return verdict

    return audit


class RegistrationTest(unittest.TestCase):
    """A key names one implementation for the life of the process."""

    def setUp(self):
        self.registry = TaskRegistry()

    def test_a_registered_worker_resolves(self):
        fn = worker("argon2")
        self.registry.register_worker("auth.hash.argon2.v1", fn)
        self.assertIs(self.registry.worker("auth.hash.argon2.v1"), fn)

    def test_a_registered_auditor_resolves(self):
        fn = auditor()
        self.registry.register_auditor("auth.hash.audit.v1", fn)
        self.assertIs(self.registry.auditor("auth.hash.audit.v1"), fn)

    def test_a_duplicate_worker_key_is_refused(self):
        """The bcrypt-silently-replaces-argon2 case, caught at startup."""
        self.registry.register_worker("auth.hash.v1", worker("bcrypt"))
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.registry.register_worker("auth.hash.v1", worker("argon2"))
        self.assertIn("already registered", str(caught.exception))
        self.assertEqual(
            self.registry.worker("auth.hash.v1")(None)["implementation"], "bcrypt"
        )

    def test_a_duplicate_auditor_key_is_refused(self):
        self.registry.register_auditor("audit.v1", auditor(True))
        with self.assertRaises(ExecutionCheckpointError):
            self.registry.register_auditor("audit.v1", auditor(False))

    def test_re_registering_the_same_callable_is_still_refused(self):
        """Identity is the point, not the object. A repeat wants to be seen."""
        fn = worker("argon2")
        self.registry.register_worker("auth.hash.v1", fn)
        with self.assertRaises(ExecutionCheckpointError):
            self.registry.register_worker("auth.hash.v1", fn)

    def test_the_namespaces_do_not_overlap(self):
        """Same key, both kinds — refused, because they carry different authority."""
        self.registry.register_worker("auth.hash.v1", worker("argon2"))
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.registry.register_auditor("auth.hash.v1", auditor())
        self.assertIn("do not share a namespace", str(caught.exception))

    def test_an_invalid_key_is_refused(self):
        for key, fragment in (
            ("", "must not be empty"),
            (" padded ", "whitespace"),
            ("has\nnewline", "control character"),
            ("has\ttab", "control character"),
            (7, "must be a string"),
            (None, "must be a string"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(ExecutionCheckpointError) as caught:
                    self.registry.register_worker(key, worker("x"))
                self.assertIn(fragment, str(caught.exception))

    def test_a_non_callable_is_refused(self):
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.registry.register_worker("auth.hash.v1", "argon2")
        self.assertIn("must be callable", str(caught.exception))


class ResolutionTest(unittest.TestCase):
    """A key this deployment does not know fails closed, and says which key."""

    def setUp(self):
        self.registry = TaskRegistry()
        self.registry.register_worker("auth.hash.argon2.v1", worker("argon2"))
        self.registry.register_auditor("auth.hash.audit.v1", auditor())

    def test_a_missing_worker_key_names_itself(self):
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.registry.worker("auth.hash.bcrypt.v1")
        message = str(caught.exception)
        self.assertIn("auth.hash.bcrypt.v1", message)
        self.assertIn("not registered", message)

    def test_a_missing_auditor_key_names_itself(self):
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.registry.auditor("auth.hash.missing.v1")
        self.assertIn("auth.hash.missing.v1", str(caught.exception))

    def test_a_worker_key_looked_up_as_an_auditor_says_so(self):
        """The most confusing absence, so it is named rather than reported empty."""
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.registry.auditor("auth.hash.argon2.v1")
        self.assertIn("registered as a worker, not as an auditor", str(caught.exception))

    def test_an_auditor_key_looked_up_as_a_worker_says_so(self):
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.registry.worker("auth.hash.audit.v1")
        self.assertIn("registered as an auditor, not as a worker", str(caught.exception))

    def test_a_missing_key_never_falls_back_to_a_similar_one(self):
        """No nearest-match, no prefix, no version bump."""
        self.registry.register_worker("auth.hash.argon2.v2", worker("argon2-v2"))
        with self.assertRaises(ExecutionCheckpointError):
            self.registry.worker("auth.hash.argon2.v3")

    def test_membership_can_be_asked_without_raising(self):
        self.assertTrue(self.registry.has_worker("auth.hash.argon2.v1"))
        self.assertFalse(self.registry.has_worker("auth.hash.bcrypt.v1"))
        self.assertFalse(self.registry.has_worker("auth.hash.audit.v1"))
        self.assertTrue(self.registry.has_auditor("auth.hash.audit.v1"))
        self.assertFalse(self.registry.has_worker(None))


class NoCodeFromStringsTest(unittest.TestCase):
    """The one thing a durable key must never become is an import."""

    def test_resolution_is_a_lookup_and_nothing_else(self):
        registry = TaskRegistry()
        registry.register_worker("a.v1", worker("a"))
        # A key shaped exactly like a module path still resolves to nothing.
        for shaped_like_code in (
            "contextmesh.execute.demo",
            "os.system",
            "builtins.eval",
            "contextmesh/execute.py",
        ):
            with self.subTest(key=shaped_like_code):
                with self.assertRaises(ExecutionCheckpointError):
                    registry.worker(shaped_like_code)

    def test_the_module_has_no_machinery_to_turn_a_string_into_code(self):
        """Parsed, not grepped.

        A substring scan cannot tell a prose "no pickle" from an actual import,
        and it was this test's first version claiming a docstring was a defect.
        Walking the AST asks the real question: does this module import a
        loader, or call something that takes text and returns behaviour?
        """
        import ast

        tree = ast.parse(
            (REPO_ROOT / "contextmesh" / "execute.py").read_text(encoding="utf-8")
        )
        imported, called = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)

        loaders = {"importlib", "pickle", "marshal", "shelve", "runpy", "imp"}
        self.assertEqual(
            loaders & imported, set(), "execute.py imports a code loader"
        )
        makers = {"eval", "exec", "compile", "__import__"}
        self.assertEqual(
            makers & called, set(), "execute.py turns text into behaviour"
        )

    def test_describe_exposes_keys_and_no_internals(self):
        registry = TaskRegistry()
        registry.register_worker("auth.schema.v1", worker("schema"))
        registry.register_worker("auth.hash.argon2.v1", worker("argon2"))
        registry.register_auditor("auth.hash.audit.v1", auditor())

        described = registry.describe()
        self.assertEqual(
            described,
            {
                "workers": ["auth.hash.argon2.v1", "auth.schema.v1"],
                "auditors": ["auth.hash.audit.v1"],
            },
        )
        # Nothing in there is a route back to the code.
        flat = repr(described)
        for leak in ("function", "lambda", "<", "module", "contextmesh"):
            self.assertNotIn(leak, flat, f"describe() leaked {leak!r}")


class DeclarationTest(unittest.TestCase):
    """Plain callables still work. They simply cannot be checkpointed."""

    def setUp(self):
        self.registry = TaskRegistry()
        self.registry.register_worker("auth.hash.argon2.v1", worker("argon2"))
        self.registry.register_auditor("auth.hash.audit.v1", auditor())
        self.runner = Runner("auth", registry=self.registry)

    def test_a_plain_callable_task_runs(self):
        runner = Runner("plain")
        runner.task("temporary_task", worker("inline"), assumes="input is valid")
        runner.run()
        self.assertEqual(runner.state("temporary_task"), TaskState.DONE)
        self.assertEqual(runner.output("temporary_task")["implementation"], "inline")

    def test_a_plain_callable_task_cannot_be_checkpointed(self):
        runner = Runner("plain")
        runner.task("temporary_task", worker("inline"), assumes="input is valid")
        self.assertFalse(runner.checkpointable)
        with self.assertRaises(ExecutionCheckpointError) as caught:
            runner.require_checkpointable()
        message = str(caught.exception)
        self.assertIn("temporary_task", message)
        self.assertIn("no worker_key", message)

    def test_a_keyed_task_runs_and_is_checkpointable(self):
        self.runner.task(
            "hash_password",
            worker_key="auth.hash.argon2.v1",
            auditor_key="auth.hash.audit.v1",
            assumes="Argon2 has no open advisory",
        )
        self.runner.run()
        self.assertEqual(self.runner.state("hash_password"), TaskState.DONE)
        self.assertEqual(
            self.runner.output("hash_password")["implementation"], "argon2"
        )
        self.assertTrue(self.runner.checkpointable)
        self.runner.require_checkpointable()

    def test_run_and_worker_key_together_are_refused(self):
        """Two answers to 'which code is this' can disagree."""
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.runner.task(
                "hash_password",
                run=worker("bcrypt"),
                worker_key="auth.hash.argon2.v1",
                assumes="x",
            )
        self.assertIn("not both", str(caught.exception))

    def test_audit_and_auditor_key_together_are_refused(self):
        with self.assertRaises(ExecutionCheckpointError):
            self.runner.task(
                "hash_password",
                worker_key="auth.hash.argon2.v1",
                audit=auditor(),
                auditor_key="auth.hash.audit.v1",
                assumes="x",
            )

    def test_neither_run_nor_worker_key_is_refused(self):
        with self.assertRaises(ExecutionError):
            self.runner.task("hash_password", assumes="x")

    def test_a_durable_key_needs_a_registry(self):
        bare = Runner("no-registry")
        with self.assertRaises(ExecutionCheckpointError) as caught:
            bare.task("hash", worker_key="auth.hash.argon2.v1", assumes="x")
        self.assertIn("no TaskRegistry", str(caught.exception))

    def test_a_per_task_registry_overrides_the_runner_default(self):
        other = TaskRegistry()
        other.register_worker("other.v1", worker("other"))
        self.runner.task(
            "elsewhere", worker_key="other.v1", assumes="x", registry=other
        )
        self.runner.run()
        self.assertEqual(self.runner.output("elsewhere")["implementation"], "other")

    def test_an_unregistered_key_is_refused_at_declaration(self):
        """Not deferred to run time — the plan is wrong now."""
        with self.assertRaises(ExecutionCheckpointError):
            self.runner.task(
                "hash", worker_key="auth.hash.bcrypt.v1", assumes="x"
            )

    def test_a_key_from_the_wrong_namespace_is_refused_at_declaration(self):
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.runner.task(
                "hash", worker_key="auth.hash.audit.v1", assumes="x"
            )
        self.assertIn("registered as an auditor", str(caught.exception))

    def test_an_audited_task_without_an_auditor_key_cannot_be_checkpointed(self):
        """Otherwise the audit is silently dropped on restore."""
        self.runner.task(
            "hash_password",
            worker_key="auth.hash.argon2.v1",
            audit=auditor(),
            assumes="x",
        )
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.runner.require_checkpointable()
        message = str(caught.exception)
        self.assertIn("has an auditor with no auditor_key", message)
        self.assertIn("silently dropped", message)

    def test_bindings_are_names_only(self):
        self.runner.task(
            "hash_password",
            worker_key="auth.hash.argon2.v1",
            auditor_key="auth.hash.audit.v1",
            assumes="x",
        )
        self.assertEqual(
            self.runner.bindings(),
            {
                "hash_password": {
                    "worker_key": "auth.hash.argon2.v1",
                    "auditor_key": "auth.hash.audit.v1",
                }
            },
        )

    def test_to_dict_carries_the_keys_and_not_the_callables(self):
        task = self.runner.task(
            "hash_password",
            worker_key="auth.hash.argon2.v1",
            auditor_key="auth.hash.audit.v1",
            assumes="x",
        )
        payload = task.to_dict()
        self.assertEqual(payload["worker_key"], "auth.hash.argon2.v1")
        self.assertEqual(payload["auditor_key"], "auth.hash.audit.v1")
        self.assertNotIn("run", payload)
        self.assertNotIn("audit", payload)
        self.assertTrue(all(not callable(v) for v in payload.values()))

    def test_unbindable_reports_every_task_not_just_the_first(self):
        runner = Runner("plain")
        for name in ("one", "two", "three"):
            runner.task(name, worker(name), assumes=f"{name} is fine")
        self.assertEqual(len(runner.unbindable()), 3)


class RebindTest(unittest.TestCase):
    """The restore half: resolve, never discover."""

    def setUp(self):
        self.registry = TaskRegistry()
        self.registry.register_worker("auth.hash.argon2.v1", worker("argon2"))
        self.registry.register_auditor("auth.hash.audit.v1", auditor())
        self.runner = Runner("auth", registry=self.registry)
        self.runner.task(
            "hash_password",
            worker_key="auth.hash.argon2.v1",
            auditor_key="auth.hash.audit.v1",
            assumes="Argon2 has no open advisory",
        )

    def test_rebinding_against_a_fresh_registry_restores_the_callables(self):
        """What a later process does: same keys, its own registry."""
        fresh = TaskRegistry()
        replacement = worker("argon2-from-the-new-process")
        fresh.register_worker("auth.hash.argon2.v1", replacement)
        fresh.register_auditor("auth.hash.audit.v1", auditor())

        self.runner.rebind(fresh)
        self.assertIs(self.runner["hash_password"].run, replacement)

    def test_rebinding_without_the_worker_key_is_refused(self):
        empty = TaskRegistry()
        empty.register_auditor("auth.hash.audit.v1", auditor())
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.runner.rebind(empty)
        self.assertIn("auth.hash.argon2.v1", str(caught.exception))

    def test_rebinding_without_the_auditor_key_is_refused(self):
        partial = TaskRegistry()
        partial.register_worker("auth.hash.argon2.v1", worker("argon2"))
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.runner.rebind(partial)
        self.assertIn("auth.hash.audit.v1", str(caught.exception))

    def test_a_failed_rebind_leaves_no_task_half_bound(self):
        """All-or-nothing, so a partial registry cannot half-restore a plan."""
        self.registry.register_worker("auth.tokens.v1", worker("tokens"))
        self.runner.task("tokens", worker_key="auth.tokens.v1", assumes="y")
        before = {
            name: (self.runner[name].run, self.runner[name].audit)
            for name in ("hash_password", "tokens")
        }

        partial = TaskRegistry()
        partial.register_worker("auth.hash.argon2.v1", worker("new-argon2"))
        partial.register_auditor("auth.hash.audit.v1", auditor())
        # ``tokens`` is missing from this registry.
        with self.assertRaises(ExecutionCheckpointError):
            self.runner.rebind(partial)

        for name, (run, audit) in before.items():
            self.assertIs(self.runner[name].run, run, f"{name} was mutated")
            self.assertIs(self.runner[name].audit, audit, f"{name} was mutated")

    def test_rebinding_a_plain_callable_plan_is_refused(self):
        runner = Runner("plain", registry=self.registry)
        runner.task("temporary_task", worker("inline"), assumes="x")
        with self.assertRaises(ExecutionCheckpointError):
            runner.rebind()

    def test_rebind_needs_a_registry(self):
        bare = Runner("bare")
        with self.assertRaises(ExecutionCheckpointError) as caught:
            bare.rebind()
        self.assertIn("needs a TaskRegistry", str(caught.exception))

    def test_a_task_that_fails_on_its_auditor_keeps_its_old_worker(self):
        """Same all-or-nothing rule as the plan, at the level of one task.

        Resolving the worker first and assigning it before checking the auditor
        would leave a task running new code with no audit — which is exactly the
        half-restored state the boundary exists to rule out.
        """
        partial = TaskRegistry()
        partial.register_worker("solo.v1", worker("new"))
        original_run, original_audit = worker("old"), auditor()
        task = Task(
            name="solo", title="Solo", run=original_run, assumes="x",
            audit=original_audit,
            worker_key="solo.v1", auditor_key="solo.audit.v1",
        )
        with self.assertRaises(ExecutionCheckpointError):
            task.rebind(partial)
        self.assertIs(task.run, original_run, "the worker was swapped anyway")
        self.assertIs(task.audit, original_audit)

    def test_a_task_rebinds_on_its_own(self):
        fresh = TaskRegistry()
        replacement = worker("fresh")
        fresh.register_worker("solo.v1", replacement)
        task = Task(
            name="solo", title="Solo", run=worker("old"), assumes="x",
            worker_key="solo.v1",
        )
        task.rebind(fresh)
        self.assertIs(task.run, replacement)


class RepairKeepsTheKeyHonestTest(unittest.TestCase):
    """A repair that swaps the code must swap the name of the code."""

    def setUp(self):
        self.registry = TaskRegistry()
        self.registry.register_worker("auth.hash.argon2.v1", worker("argon2"))
        self.registry.register_worker("auth.hash.bcrypt.v1", worker("bcrypt"))
        self.registry.register_auditor("auth.hash.audit.v1", auditor())
        self.runner = Runner("auth", registry=self.registry)
        self.runner.task(
            "hash_password",
            worker_key="auth.hash.argon2.v1",
            auditor_key="auth.hash.audit.v1",
            assumes="Argon2 has no open advisory",
        )

    def test_repairing_a_durable_task_with_a_bare_callable_is_refused(self):
        """The exact inversion of the bug: new code, old name in the checkpoint."""
        with self.assertRaises(ExecutionCheckpointError) as caught:
            self.runner.repair(
                "hash_password",
                assumes="bcrypt has no open advisory",
                run=worker("bcrypt"),
            )
        message = str(caught.exception)
        self.assertIn("auth.hash.argon2.v1", message)
        self.assertIn("worker_key=", message)
        self.assertEqual(
            self.runner["hash_password"].worker_key, "auth.hash.argon2.v1"
        )

    def test_repairing_a_durable_auditor_with_a_bare_callable_is_refused(self):
        with self.assertRaises(ExecutionCheckpointError):
            self.runner.repair("hash_password", audit=auditor(False))

    def test_repair_updates_the_durable_key(self):
        self.runner.repair(
            "hash_password",
            assumes="bcrypt has no open advisory",
            worker_key="auth.hash.bcrypt.v1",
        )
        task = self.runner["hash_password"]
        self.assertEqual(task.worker_key, "auth.hash.bcrypt.v1")
        self.assertEqual(task.state, TaskState.STALE)
        self.assertEqual(task.run(None)["implementation"], "bcrypt")

    def test_a_repair_to_an_unregistered_key_changes_nothing(self):
        with self.assertRaises(ExecutionCheckpointError):
            self.runner.repair(
                "hash_password", assumes="scrypt", worker_key="auth.hash.scrypt.v1"
            )
        task = self.runner["hash_password"]
        self.assertEqual(task.worker_key, "auth.hash.argon2.v1")
        self.assertEqual(task.run(None)["implementation"], "argon2")

    def test_repair_rejects_run_and_worker_key_together(self):
        with self.assertRaises(ExecutionCheckpointError):
            self.runner.repair(
                "hash_password",
                run=worker("bcrypt"),
                worker_key="auth.hash.bcrypt.v1",
            )

    def test_a_plain_task_may_still_be_repaired_with_a_callable(self):
        """Backwards compatibility: no key, no constraint."""
        runner = Runner("plain")
        runner.task("t", worker("first"), assumes="x")
        runner.run()
        runner.repair("t", assumes="y", run=worker("second"))
        runner.run()
        self.assertEqual(runner.output("t")["implementation"], "second")


class ArgonToBcryptTest(unittest.TestCase):
    """The acceptance case: a repaired binding must survive the process.

        argon2 → CVE → repair to bcrypt → checkpoint → death → restore

    The restored task must run bcrypt. Bringing argon2 back would be the
    checkpoint faithfully resurrecting the vulnerability the repair removed,
    which is the whole reason durable identity is a key and not a callable.
    """

    def build(self, registry):
        runner = Runner("auth", registry=registry)
        runner.task(
            "hash_password",
            worker_key="auth.hash.argon2.v1",
            auditor_key="auth.hash.audit.v1",
            assumes="Argon2 has no open advisory",
            produces=("Password Hasher",),
        )
        return runner

    def deployment(self):
        registry = TaskRegistry()
        registry.register_worker("auth.hash.argon2.v1", worker("argon2"))
        registry.register_worker("auth.hash.bcrypt.v1", worker("bcrypt"))
        registry.register_auditor("auth.hash.audit.v1", auditor())
        return registry

    def test_the_repaired_binding_is_what_a_later_process_restores(self):
        first = self.deployment()
        runner = self.build(first)
        runner.run()
        self.assertEqual(runner.output("hash_password")["implementation"], "argon2")

        # A CVE lands and the assumption is regrounded on bcrypt.
        runner.repair(
            "hash_password",
            assumes="bcrypt has no open advisory",
            worker_key="auth.hash.bcrypt.v1",
        )
        runner.run()
        self.assertEqual(runner.output("hash_password")["implementation"], "bcrypt")

        # This is everything a checkpoint would carry: two strings.
        durable = runner.bindings()
        self.assertEqual(
            durable["hash_password"]["worker_key"], "auth.hash.bcrypt.v1"
        )

        # A second process. Nothing shared but the strings and its own registry.
        second = self.deployment()
        restored = Runner("auth", registry=second)
        restored.task(
            "hash_password",
            worker_key=durable["hash_password"]["worker_key"],
            auditor_key=durable["hash_password"]["auditor_key"],
            assumes="bcrypt has no open advisory",
        )
        restored.run()

        self.assertEqual(
            restored.output("hash_password")["implementation"],
            "bcrypt",
            "the restored process resurrected the worker the CVE was about",
        )

    def test_the_pre_repair_binding_is_the_one_that_would_resurrect_argon2(self):
        """Pins what the fix prevents, so the test above cannot pass vacuously."""
        registry = self.deployment()
        runner = self.build(registry)
        self.assertEqual(
            runner.bindings()["hash_password"]["worker_key"], "auth.hash.argon2.v1"
        )
        restored = Runner("auth", registry=self.deployment())
        restored.task(
            "hash_password", worker_key="auth.hash.argon2.v1", assumes="x"
        )
        restored.run()
        self.assertEqual(restored.output("hash_password")["implementation"], "argon2")

    def test_a_deployment_missing_bcrypt_cannot_resume(self):
        """The diagnostic `describe()` exists for."""
        stale_deployment = TaskRegistry()
        stale_deployment.register_worker("auth.hash.argon2.v1", worker("argon2"))
        stale_deployment.register_auditor("auth.hash.audit.v1", auditor())

        restored = Runner("auth", registry=stale_deployment)
        with self.assertRaises(ExecutionCheckpointError) as caught:
            restored.task(
                "hash_password", worker_key="auth.hash.bcrypt.v1", assumes="x"
            )
        self.assertIn("auth.hash.bcrypt.v1", str(caught.exception))
        self.assertNotIn(
            "auth.hash.bcrypt.v1", stale_deployment.describe()["workers"]
        )


if __name__ == "__main__":
    unittest.main()
