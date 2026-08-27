"""A plan that stops mid-repair has to come back as the same plan.

The state worth checkpointing is the awkward one::

    argon2 runs, everything green
          ↓  a CVE is published
    recheck() → the auditor disproves the ground
          ↓
    hashing and routes go STALE; schema and tokens stay DONE
          ↓
    repair hashing onto bcrypt
          ↓
    CHECKPOINT          ← bcrypt has never executed
          ↓
    process dies

What a restore has to reproduce is not a list of fields but a *schedule*: the
same tasks ready, the same ones settled, the same attempt counts. Get any of
those wrong and the restored plan reruns work that was fine, or skips work that
was not, and either way the selective part of selective re-execution is gone.

Three things are deliberately absent from the file — the callables, the registry
that explains their keys, and the ready/blocked sets. The first two are
deployment configuration and the last is a fact about a round, recomputed from
state, ground and dependencies. A file that asserted a schedule could contradict
its own contents.
"""

import json
import unittest

from contextmesh.execute import (
    EXECUTION_SCHEMA,
    EXECUTION_VERSION,
    ExecutionCheckpointError,
    ExecutionSnapshotError,
    Runner,
    TaskRegistry,
    TaskState,
)
from contextmesh.graph import ContextGraph
from contextmesh.model import AssumptionStatus, NodeType


class Advisory:
    """An auditor whose verdict changes when the advisory is published.

    The point of the acceptance case is that the first run is genuinely clean —
    the CVE arrives afterwards. An auditor that disproves from the start would
    never let the pipeline complete, and the interesting invalidation (routes
    falling while tokens stands) would never happen.
    """

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
    registry.register_worker("auth.routes.v1", lambda ctx: {"ok": True})
    registry.register_worker("auth.tokens.v1", lambda ctx: {"ok": True})
    registry.register_auditor("auth.hash.audit.v1", advisory or Advisory())
    return registry


def plan(registry, graph=None):
    runner = Runner("auth", graph=graph, registry=registry)
    runner.task("schema", worker_key="auth.schema.v1", assumes="sqlite is fine",
                produces=("Schema",))
    runner.task("hashing", worker_key="auth.hash.argon2.v1",
                auditor_key="auth.hash.audit.v1",
                assumes="Argon2 has no open advisory",
                needs=("schema",), produces=("Password Hasher",))
    runner.task("routes", worker_key="auth.routes.v1", assumes="REST is fine",
                needs=("hashing",), produces=("Routes",))
    runner.task("tokens", worker_key="auth.tokens.v1", assumes="JWT is fine",
                needs=("schema",), produces=("Tokens",))
    return runner


def to_the_boundary():
    """Run clean, publish the CVE, invalidate, repair — and stop before rerunning."""
    advisory = Advisory()
    runner = plan(deployment(advisory))
    runner.run()
    advisory.published = True
    runner.recheck()
    runner.repair("hashing", assumes="bcrypt has no open advisory",
                  worker_key="auth.hash.bcrypt.v1")
    return runner, advisory


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.runner, _ = to_the_boundary()
        self.snapshot = self.runner.snapshot()

    def test_the_snapshot_names_its_own_format(self):
        self.assertEqual(self.snapshot["schema"], EXECUTION_SCHEMA)
        self.assertEqual(self.snapshot["version"], EXECUTION_VERSION)
        self.assertEqual(self.snapshot["round"], self.runner.round)

    def test_tasks_are_in_declaration_order_and_not_sorted(self):
        """Order is scheduler semantics, not presentation.

        `_ready` walks `_order`, so for two tasks that become ready in the same
        round it decides which runs first. Sorting would reorder a restored
        plan's execution without changing a single field — and these names sort
        differently from how they were declared, so the test can tell.
        """
        names = [row["name"] for row in self.snapshot["tasks"]]
        self.assertEqual(names, ["schema", "hashing", "routes", "tokens"])
        self.assertNotEqual(names, sorted(names))

    def test_the_plan_name_is_persisted_because_ids_are_derived_from_it(self):
        """Not decoration: the source node and every decision id embed it."""
        self.assertEqual(self.snapshot["plan"], "auth")
        node_id = self.runner["schema"].node_id
        self.assertIn("auth", node_id)

    def test_the_file_carries_no_callables_and_no_registry(self):
        text = self.runner.to_json()
        for forbidden in ("function", "lambda", "<", "registry", "governed_by"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_the_file_carries_no_ready_or_blocked_set(self):
        """Derived per round; a stored one could contradict its own contents."""
        for key in ("ready", "blocked", "rounds"):
            self.assertNotIn(key, self.snapshot)

    def test_a_plan_of_plain_callables_cannot_be_snapshotted(self):
        runner = Runner("plain")
        runner.task("temporary", lambda ctx: {"ok": True}, assumes="input is valid")
        with self.assertRaises(ExecutionCheckpointError):
            runner.snapshot()

    def test_re_saving_a_restored_plan_reproduces_it_byte_for_byte(self):
        text = self.runner.to_json()
        restored = Runner.from_snapshot(
            json.loads(text), graph=self.runner.graph, registry=deployment()
        )
        self.assertEqual(text, restored.to_json())

    def test_two_identical_runs_produce_identical_files(self):
        first, _ = to_the_boundary()
        second, _ = to_the_boundary()
        self.assertEqual(first.to_json(), second.to_json())


class FailClosedTest(unittest.TestCase):
    def setUp(self):
        self.runner, _ = to_the_boundary()
        self.snapshot = self.runner.snapshot()

    #: `None` is a value worth testing, so "not given" needs its own marker.
    #: An earlier version used None for both and quietly tested nothing.
    MINE = object()

    def restore(self, snapshot=MINE):
        return Runner.from_snapshot(
            self.snapshot if snapshot is self.MINE else snapshot,
            graph=self.runner.graph,
            registry=deployment(),
        )

    def refuse(self, fragment, snapshot=MINE):
        with self.assertRaises(ExecutionSnapshotError) as caught:
            self.restore(snapshot)
        self.assertIn(fragment, str(caught.exception))

    def test_a_non_object_snapshot_is_refused(self):
        for bad in ([], "plan", 7, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ExecutionSnapshotError):
                    self.restore(bad)

    def test_every_container_field_is_required(self):
        for key in ("schema", "version", "plan", "round", "tasks"):
            with self.subTest(key=key):
                snapshot = self.runner.snapshot()
                del snapshot[key]
                self.refuse(key, snapshot)

    def test_an_unknown_container_field_is_refused(self):
        self.snapshot["approved_by"] = "alice"
        self.refuse("which v1 does not define")

    def test_another_schema_is_refused(self):
        self.snapshot["schema"] = "contextmesh.runledger"
        self.refuse("not a contextmesh.execution snapshot")

    def test_a_future_version_is_refused(self):
        self.snapshot["version"] = EXECUTION_VERSION + 1
        self.refuse("cannot be read by this build")

    def test_a_boolean_version_does_not_pass_for_one(self):
        self.snapshot["version"] = True
        self.refuse("must be an integer")

    def test_an_empty_or_non_string_plan_is_refused(self):
        for bad in ("", 7, None, []):
            with self.subTest(bad=bad):
                snapshot = self.runner.snapshot()
                snapshot["plan"] = bad
                self.refuse("plan must be a non-empty string", snapshot)

    def test_a_negative_or_boolean_round_is_refused(self):
        for bad in (-1, True, "2", 2.0):
            with self.subTest(bad=bad):
                snapshot = self.runner.snapshot()
                snapshot["round"] = bad
                self.refuse("round must be a non-negative integer", snapshot)

    def test_tasks_must_be_an_array(self):
        self.snapshot["tasks"] = {"schema": {}}
        self.refuse("must be an array")

    def test_every_task_field_is_required(self):
        for key in ("name", "title", "rationale", "state", "attempt", "assumes",
                    "needs", "produces", "worker_key", "auditor_key",
                    "node_id", "assumption_id", "output", "artefacts"):
            with self.subTest(key=key):
                snapshot = self.runner.snapshot()
                del snapshot["tasks"][0][key]
                self.refuse(key, snapshot)

    def test_an_unknown_task_field_is_refused(self):
        self.snapshot["tasks"][0]["approved_by"] = "alice"
        self.refuse("which v1 does not define")

    def test_an_unknown_state_is_refused_and_the_message_lists_the_known_ones(self):
        self.snapshot["tasks"][0]["state"] = "blocked"
        with self.assertRaises(ExecutionSnapshotError) as caught:
            self.restore()
        message = str(caught.exception)
        self.assertIn("not a state this build knows", message)
        for known in ("pending", "done", "stale", "failed"):
            self.assertIn(known, message)
        self.assertIn("fact about a round", message)

    def test_a_negative_or_boolean_attempt_is_refused(self):
        for bad in (-1, True, "1", 1.5):
            with self.subTest(bad=bad):
                snapshot = self.runner.snapshot()
                snapshot["tasks"][0]["attempt"] = bad
                self.refuse("attempt must be a non-negative integer", snapshot)

    def test_the_text_fields_must_be_strings(self):
        for key in ("name", "title", "rationale", "assumes"):
            with self.subTest(key=key):
                snapshot = self.runner.snapshot()
                snapshot["tasks"][0][key] = 42
                self.refuse(f"{key} must be a string", snapshot)

    def test_an_empty_task_name_is_refused(self):
        self.snapshot["tasks"][0]["name"] = ""
        self.refuse("name must not be empty")

    def test_the_string_lists_must_hold_strings(self):
        for key in ("needs", "produces", "artefacts"):
            for bad in ("schema", [7], {"a": 1}, [None]):
                with self.subTest(key=key, bad=bad):
                    snapshot = self.runner.snapshot()
                    snapshot["tasks"][0][key] = bad
                    self.refuse("must be an array of strings", snapshot)

    def test_a_duplicate_task_name_is_refused(self):
        self.snapshot["tasks"].append(dict(self.snapshot["tasks"][0]))
        self.refuse("appears twice")

    def test_a_task_without_a_worker_key_cannot_be_restored(self):
        self.snapshot["tasks"][0]["worker_key"] = None
        self.refuse("has no worker_key")

    def test_a_malformed_key_is_refused(self):
        self.snapshot["tasks"][0]["worker_key"] = "has a space"
        with self.assertRaises(ExecutionCheckpointError):
            self.restore()

    def test_the_nullable_ids_must_be_non_empty_strings_or_null(self):
        for key in ("node_id", "assumption_id"):
            for bad in ("", 7, []):
                with self.subTest(key=key, bad=bad):
                    snapshot = self.runner.snapshot()
                    snapshot["tasks"][0][key] = bad
                    self.refuse("non-empty string or null", snapshot)

    def test_output_must_be_an_object(self):
        self.snapshot["tasks"][0]["output"] = ["ok", True]
        self.refuse("output must be an object")

    def test_output_holding_a_value_with_no_canonical_json_form_is_refused(self):
        self.snapshot["tasks"][0]["output"] = {"when": {1, 2}}
        self.refuse("not JSON-deterministic")

    def test_output_holding_a_non_finite_float_is_refused(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                snapshot = self.runner.snapshot()
                snapshot["tasks"][0]["output"] = {"score": bad}
                self.refuse("has no JSON form", snapshot)

    def test_a_worker_returning_something_unstorable_is_caught_at_snapshot_time(self):
        """Better at the write than at the read: the run that did it is still here."""
        registry = TaskRegistry()
        registry.register_worker("bad.v1", lambda ctx: {"seen": {1, 2}})
        runner = Runner("bad", registry=registry)
        runner.task("odd", worker_key="bad.v1", assumes="x")
        runner.run()
        with self.assertRaises(ExecutionSnapshotError):
            runner.snapshot()


class FileTest(unittest.TestCase):
    def test_a_plan_round_trips_through_a_file(self):
        import tempfile
        from pathlib import Path

        runner, _ = to_the_boundary()
        path = Path(tempfile.mkdtemp()) / "execution.json"
        runner.save_json(path)
        restored = Runner.load_json(
            path, graph=runner.graph, registry=deployment(), ledger=runner.ledger
        )
        self.assertEqual(restored._ready(), runner._ready())
        self.assertEqual(restored.to_json(), runner.to_json())

    def test_a_syntactically_broken_file_arrives_as_an_execution_error(self):
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "execution.json"
        path.write_text('{"schema":', encoding="utf-8")
        with self.assertRaises(ExecutionSnapshotError) as caught:
            Runner.load_json(path, graph=ContextGraph(), registry=deployment())
        self.assertIn("not valid JSON", str(caught.exception))

    def test_a_duplicate_json_key_in_a_plan_file_is_refused(self):
        import tempfile
        from pathlib import Path

        runner, _ = to_the_boundary()
        path = Path(tempfile.mkdtemp()) / "execution.json"
        text = runner.to_json().replace('"plan": "auth"', '"plan": "auth", "plan": "other"', 1)
        self.assertIn('"plan": "other"', text)
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(ExecutionSnapshotError) as caught:
            Runner.load_json(path, graph=runner.graph, registry=deployment())
        self.assertIn("duplicate JSON key", str(caught.exception))


class ReferenceClosureTest(unittest.TestCase):
    """Ids that point nowhere fail here, not three rounds later."""

    def setUp(self):
        self.runner, _ = to_the_boundary()
        self.snapshot = self.runner.snapshot()

    def refuse(self, fragment, snapshot=None, graph=None):
        with self.assertRaises(ExecutionSnapshotError) as caught:
            Runner.from_snapshot(
                snapshot if snapshot is not None else self.snapshot,
                graph=graph or self.runner.graph,
                registry=deployment(),
            )
        self.assertIn(fragment, str(caught.exception))

    def row(self, name):
        return next(r for r in self.snapshot["tasks"] if r["name"] == name)

    def test_a_dependency_on_a_task_not_in_the_snapshot_is_refused(self):
        self.row("routes")["needs"] = ["hashing", "nonexistent"]
        self.refuse("not a task in this snapshot")

    def test_a_task_that_needs_itself_is_refused(self):
        self.row("routes")["needs"] = ["routes"]
        self.refuse("needs itself")

    def test_an_assumption_not_in_the_graph_is_refused(self):
        self.row("schema")["assumption_id"] = "assumption:invented"
        self.refuse("is not in this graph")

    def test_a_decision_not_in_the_graph_is_refused(self):
        self.row("schema")["node_id"] = "decision:invented"
        self.refuse("is not in this graph")

    def test_a_node_id_pointing_at_the_wrong_node_type_is_refused(self):
        entity = self.runner["schema"].artefacts[0]
        self.row("schema")["node_id"] = entity
        self.refuse("is a entity, not a decision")

    def test_an_artefact_not_in_the_graph_is_refused(self):
        self.row("schema")["artefacts"] = ["entity:invented"]
        self.refuse("is not in this graph")

    def test_an_artefact_pointing_at_the_wrong_node_type_is_refused(self):
        self.row("schema")["artefacts"] = [self.runner["schema"].node_id]
        self.refuse("not an entity")

    def test_a_snapshot_restored_against_a_different_graph_is_refused(self):
        """The commonest way a reference dangles: the wrong graph entirely."""
        self.refuse("is not in this graph", graph=ContextGraph())

    def test_a_task_claiming_done_on_an_invalidated_decision_is_refused(self):
        """Measured against the lifecycle, not assumed.

        A STALE task legitimately points at an invalidated decision — that is
        exactly what selective invalidation leaves behind, and the restore below
        depends on it being allowed. A DONE one does not: that plan would
        schedule as settled while the graph says its ground is gone.
        """
        stale = self.row("hashing")
        self.assertEqual(stale["state"], "stale")
        self.assertTrue(self.runner.graph.node(stale["node_id"]).invalidated)
        stale["state"] = "done"
        self.refuse("says done, but decision")

    # ── the two records of one fact have to agree ────────────────────────
    def test_a_task_whose_ground_text_disagrees_with_its_binding_is_refused(self):
        """`assumes` and `assumption_id` are the same fact written twice.

        The reporting side reads the text; the scheduler and the auditor read
        the bound assumption. A file can hold them disagreeing, and then the
        restored plan shows one ground and runs on another. Ids are content
        slugged (sha1 of the statement), so a real binding cannot drift here.
        """
        rows = {r["name"]: r for r in self.snapshot["tasks"]}
        rows["schema"]["assumption_id"] = rows["tokens"]["assumption_id"]
        self.refuse("cannot report one ground and be scheduled on another")

    def test_a_task_with_no_assumption_id_is_refused(self):
        """`Runner.task` binds one at declaration, so unbound is unreachable."""
        self.row("schema")["assumption_id"] = None
        self.refuse("has no assumption_id")

    def test_the_ids_are_content_slugged_so_agreement_is_checkable(self):
        """Why exact equality is safe rather than brittle."""
        from contextmesh.model import slug

        for task in self.runner.tasks:
            self.assertEqual(task.assumption_id, slug(task.assumes, "assumption"))

    # ── done is a claim about provenance ─────────────────────────────────
    def test_a_task_claiming_done_with_no_decision_is_refused(self):
        """`_commit` creates the decision and only then marks the task done.

        Dangerous as well as impossible: `_ready` skips DONE tasks, so a
        restored plan would cache work as complete that the graph has no record
        of ever happening.
        """
        row = self.row("schema")
        self.assertEqual(row["state"], "done")
        row["node_id"] = None
        self.refuse("says done, but names no decision")

    def test_a_task_claiming_done_after_no_attempts_is_refused(self):
        self.row("schema")["attempt"] = 0
        self.refuse("says done after 0 attempts")

    def test_a_task_claiming_done_on_rejected_ground_is_refused(self):
        """The disproved argon2 ground, worn by a task that says it finished.

        Note it is the *superseded* assumption that carries REJECTED — after the
        repair, `hashing` points at the new bcrypt one, which is active. An
        earlier version of this test reached for `hashing`'s current binding and
        proved nothing.
        """
        rejected = next(
            a for a in self.runner.graph.assumptions.values()
            if a.status is AssumptionStatus.REJECTED
        )
        settled = self.row("tokens")
        self.assertEqual(settled["state"], "done")
        settled["assumption_id"] = rejected.id
        settled["assumes"] = rejected.statement
        self.refuse("is rejected")

    def test_done_with_provenance_is_exactly_what_the_lifecycle_produces(self):
        """Pins the rule against the engine, so it cannot drift into fiction."""
        for task in self.runner.tasks:
            if task.state is TaskState.DONE:
                self.assertIsNotNone(task.node_id, task.name)
                self.assertGreaterEqual(task.attempt, 1, task.name)
                ground = self.runner.graph.assumptions[task.assumption_id]
                self.assertIsNot(ground.status, AssumptionStatus.REJECTED, task.name)

    def test_the_other_states_are_left_alone(self):
        """Only DONE is constrained; the rest are measured, not guessed.

        PENDING and FAILED legitimately have no decision and no attempts, and
        STALE legitimately points at an invalidated one. Over-constraining any
        of them would refuse plans the engine really produces.
        """
        rows = {r["name"]: r for r in self.snapshot["tasks"]}
        rows["routes"]["state"] = "pending"
        rows["routes"]["attempt"] = 0
        rows["routes"]["node_id"] = None
        rows["routes"]["artefacts"] = []
        restored = Runner.from_snapshot(
            self.snapshot, graph=self.runner.graph, registry=deployment()
        )
        self.assertIs(restored.state("routes"), TaskState.PENDING)

    # ── a plan that cannot be scheduled is not a plan ────────────────────
    def test_a_dependency_cycle_is_refused_at_load_not_at_run(self):
        """`_validate` finds it too, but not until `run()`.

        Without this the restore succeeds, the Runner looks healthy, and the
        contradiction surfaces later in code that has no idea a file existed.
        """
        rows = {r["name"]: r for r in self.snapshot["tasks"]}
        for row in self.snapshot["tasks"]:
            row["state"], row["attempt"] = "pending", 0
            row["node_id"], row["artefacts"] = None, []
        rows["schema"]["needs"] = ["tokens"]
        rows["tokens"]["needs"] = ["schema"]
        self.refuse("dependency cycle")

    def test_a_longer_cycle_is_refused_too(self):
        rows = {r["name"]: r for r in self.snapshot["tasks"]}
        for row in self.snapshot["tasks"]:
            row["state"], row["attempt"] = "pending", 0
            row["node_id"], row["artefacts"] = None, []
        rows["schema"]["needs"] = ["tokens"]
        rows["hashing"]["needs"] = ["schema"]
        rows["tokens"]["needs"] = ["hashing"]
        with self.assertRaises(ExecutionSnapshotError) as caught:
            Runner.from_snapshot(
                self.snapshot, graph=self.runner.graph, registry=deployment()
            )
        message = str(caught.exception)
        for name in ("schema", "hashing", "tokens"):
            self.assertIn(name, message)

    def test_a_refused_cycle_leaves_no_source_node_behind(self):
        """Checked before the Runner exists, so not even a source node lands.

        The observation has to be chosen carefully. Against a *fresh* graph the
        assumption check fires first and the cycle check is never reached — an
        earlier version of this test did that and passed without exercising the
        ordering at all. So: the real graph, so every reference closes, but a
        plan name whose source node does not exist yet. If the cycle check ran
        after construction, `Runner.__init__` would have created it on the way.
        """
        from contextmesh.model import slug

        for row in self.snapshot["tasks"]:
            row["state"], row["attempt"] = "pending", 0
            row["node_id"], row["artefacts"] = None, []
        rows = {r["name"]: r for r in self.snapshot["tasks"]}
        rows["schema"]["needs"] = ["tokens"]
        rows["tokens"]["needs"] = ["schema"]
        self.snapshot["plan"] = "auth-restored"

        source_id = slug("execution plan: auth-restored", "source")
        self.assertNotIn(source_id, self.runner.graph.nodes)

        with self.assertRaises(ExecutionSnapshotError):
            Runner.from_snapshot(
                self.snapshot, graph=self.runner.graph, registry=deployment()
            )
        self.assertNotIn(
            source_id, self.runner.graph.nodes,
            "the refused snapshot still put its source node in the graph",
        )

    def test_an_accepted_plan_does_create_its_source_node(self):
        """The control, so the assertion above cannot pass by never being true."""
        from contextmesh.model import slug

        self.snapshot["plan"] = "auth-restored"
        source_id = slug("execution plan: auth-restored", "source")
        Runner.from_snapshot(
            self.snapshot, graph=self.runner.graph, registry=deployment()
        )
        self.assertIn(source_id, self.runner.graph.nodes)

    def test_a_stale_task_on_an_invalidated_decision_restores_fine(self):
        restored = Runner.from_snapshot(
            self.snapshot, graph=self.runner.graph, registry=deployment()
        )
        self.assertIs(restored.state("hashing"), TaskState.STALE)


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.runner, _ = to_the_boundary()
        self.snapshot = self.runner.snapshot()

    def test_every_field_survives_the_round_trip(self):
        restored = Runner.from_snapshot(
            self.snapshot, graph=self.runner.graph, registry=deployment()
        )
        self.assertEqual(restored.plan, self.runner.plan)
        self.assertEqual(restored.round, self.runner.round)
        for name in ("schema", "hashing", "routes", "tokens"):
            before, after = self.runner[name], restored[name]
            for field in ("name", "title", "rationale", "assumes", "state",
                          "attempt", "needs", "produces", "worker_key",
                          "auditor_key", "node_id", "assumption_id",
                          "output", "artefacts"):
                self.assertEqual(getattr(before, field), getattr(after, field),
                                 f"{name}.{field}")

    def test_the_callables_come_back_from_the_registry_not_the_file(self):
        registry = deployment()
        restored = Runner.from_snapshot(
            self.snapshot, graph=self.runner.graph, registry=registry
        )
        self.assertIs(restored["hashing"].run, registry.worker("auth.hash.bcrypt.v1"))
        self.assertIs(restored["hashing"].audit, registry.auditor("auth.hash.audit.v1"))
        self.assertIsNone(restored["schema"].audit)

    def test_the_restored_runner_adopts_the_registry_that_bound_it(self):
        registry = deployment()
        restored = Runner.from_snapshot(
            self.snapshot, graph=self.runner.graph, registry=registry
        )
        self.assertIs(restored.registry, registry)
        for task in restored.tasks:
            self.assertIs(task.governed_by, restored)

    def test_a_deployment_missing_a_key_refuses_the_whole_plan(self):
        partial = TaskRegistry()
        partial.register_worker("auth.schema.v1", lambda ctx: {"ok": True})
        with self.assertRaises(ExecutionCheckpointError) as caught:
            Runner.from_snapshot(
                self.snapshot, graph=self.runner.graph, registry=partial
            )
        self.assertIn("auth.hash.bcrypt.v1", str(caught.exception))

    def test_the_source_node_dedupes_onto_the_one_the_plan_already_had(self):
        before = len(self.runner.graph.nodes)
        restored = Runner.from_snapshot(
            self.snapshot, graph=self.runner.graph, registry=deployment()
        )
        self.assertEqual(restored.source.id, self.runner.source.id)
        self.assertEqual(len(self.runner.graph.nodes), before)

    def test_the_ledger_is_carried_in_rather_than_nested_in_the_file(self):
        restored = Runner.from_snapshot(
            self.snapshot, graph=self.runner.graph, registry=deployment(),
            ledger=self.runner.ledger,
        )
        self.assertIs(restored.ledger, self.runner.ledger)
        self.assertEqual(restored.ledger.head, self.runner.ledger.head)

    def test_without_a_ledger_the_restored_plan_starts_an_empty_one(self):
        restored = Runner.from_snapshot(
            self.snapshot, graph=self.runner.graph, registry=deployment()
        )
        self.assertEqual(len(restored.ledger), 0)


class ArgonToBcryptRestartTest(unittest.TestCase):
    """The acceptance case: checkpoint post-repair, restore, rerun selectively."""

    def test_the_restored_plan_has_the_same_schedule(self):
        runner, _ = to_the_boundary()
        ready = runner._ready()
        self.assertEqual(ready, ["hashing"])

        restored = Runner.from_snapshot(
            runner.snapshot(), graph=runner.graph, registry=deployment(),
            ledger=runner.ledger,
        )
        self.assertEqual(restored._ready(), ready)
        self.assertIs(restored.state("hashing"), TaskState.STALE)
        self.assertEqual(restored["hashing"].worker_key, "auth.hash.bcrypt.v1")
        self.assertEqual(restored["hashing"].attempt, 1)
        for settled in ("schema", "tokens"):
            self.assertIs(restored.state(settled), TaskState.DONE)
            self.assertEqual(restored[settled].attempt, 1)
        self.assertIs(restored.state("routes"), TaskState.STALE)

    def test_the_restored_plan_reruns_exactly_what_the_original_would_have(self):
        """Behavioural equivalence, not just field equality."""
        control, control_advisory = to_the_boundary()
        restarted, _ = to_the_boundary()

        published = Advisory()
        published.published = True
        restored = Runner.from_snapshot(
            restarted.snapshot(), graph=restarted.graph,
            registry=deployment(published), ledger=restarted.ledger,
        )

        control.run()
        restored.run()

        for name in ("schema", "hashing", "routes", "tokens"):
            with self.subTest(task=name):
                self.assertIs(restored.state(name), control.state(name))
                self.assertEqual(restored[name].attempt, control[name].attempt)
                self.assertEqual(restored[name].output, control[name].output)

    def test_the_unaffected_tasks_never_reran(self):
        runner, _ = to_the_boundary()
        published = Advisory()
        published.published = True
        restored = Runner.from_snapshot(
            runner.snapshot(), graph=runner.graph,
            registry=deployment(published), ledger=runner.ledger,
        )
        restored.run()
        for untouched in ("schema", "tokens"):
            self.assertEqual(restored[untouched].attempt, 1, untouched)
        self.assertEqual(restored["hashing"].attempt, 2)
        self.assertEqual(restored["hashing"].output, {"impl": "bcrypt"})

    def test_the_restored_run_appends_to_the_ledger_it_was_given(self):
        runner, _ = to_the_boundary()
        head = runner.ledger.head
        published = Advisory()
        published.published = True
        restored = Runner.from_snapshot(
            runner.snapshot(), graph=runner.graph,
            registry=deployment(published), ledger=runner.ledger,
        )
        restored.run()
        self.assertNotEqual(restored.ledger.head, head)
        self.assertTrue(restored.ledger.verify())

    def test_a_restore_that_resurrected_argon2_would_be_visible_here(self):
        """Pins what the durable key prevents, so the case cannot pass vacuously."""
        runner, _ = to_the_boundary()
        snapshot = runner.snapshot()
        row = next(r for r in snapshot["tasks"] if r["name"] == "hashing")
        row["worker_key"] = "auth.hash.argon2.v1"

        published = Advisory()
        published.published = True
        resurrected = Runner.from_snapshot(
            snapshot, graph=runner.graph, registry=deployment(published)
        )
        resurrected.run()
        self.assertEqual(resurrected["hashing"].output, {"impl": "argon2"})
        self.assertIsNot(resurrected.state("hashing"), TaskState.DONE)


class NotPersistedTest(unittest.TestCase):
    """What the format leaves out, and why."""

    def test_the_task_row_holds_exactly_the_documented_fields(self):
        runner, _ = to_the_boundary()
        row = runner.snapshot()["tasks"][0]
        self.assertEqual(
            sorted(row),
            sorted(["name", "title", "rationale", "state", "attempt", "assumes",
                    "needs", "produces", "worker_key", "auditor_key", "node_id",
                    "assumption_id", "output", "artefacts"]),
        )

    def test_the_container_holds_exactly_the_documented_fields(self):
        runner, _ = to_the_boundary()
        self.assertEqual(
            sorted(runner.snapshot()), ["plan", "round", "schema", "tasks", "version"]
        )

    def test_the_node_types_the_references_point_at(self):
        """Documents the contract the closure checks enforce."""
        runner, _ = to_the_boundary()
        for task in runner.tasks:
            if task.node_id:
                self.assertIs(runner.graph.node(task.node_id).type, NodeType.DECISION)
            for artefact in task.artefacts:
                self.assertIs(runner.graph.node(artefact).type, NodeType.ENTITY)


if __name__ == "__main__":
    unittest.main()
