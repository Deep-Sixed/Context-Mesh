"""Selective re-execution: the rerun touches the blast radius and nothing else."""

import dataclasses
import unittest

from contextmesh.execute import (
    Advisories,
    AuditContext,
    ExecutionError,
    Runner,
    TaskState,
    Verdict,
    demo,
)
from contextmesh.model import AssumptionStatus, EdgeType, NodeType


def counted(name, calls, **output):
    """A worker that records that it ran, so 'nothing else ran' is checkable."""

    def run(ctx):
        calls.append(name)
        return dict(output)

    return run


def plan(calls, feed):
    """Two independent branches; only one of them stands on the feed."""
    runner = Runner("test plan")
    runner.task(
        "base",
        counted("base", calls, rows=3),
        assumes="the store is reachable",
        produces=("Store",),
        audit=lambda ctx: ctx.ok("store reachable"),
    )
    runner.task(
        "hashing",
        counted("hashing", calls, package="argon2-cffi"),
        assumes="argon2-cffi has no open advisory",
        produces=("Hasher", "Argon2 Parameters"),
        audit=lambda ctx: (
            ctx.disproved(f"open advisory: {feed.advisory(ctx.output['package'])}")
            if not feed.clear(ctx.output["package"])
            else ctx.ok("no open advisory")
        ),
    )
    runner.task(
        "routes",
        counted("routes", calls, endpoints=2),
        assumes="the hasher exposes a stable interface",
        needs=("hashing",),
        produces=("Login Endpoint",),
        audit=lambda ctx: ctx.ok("two endpoints"),
    )
    runner.task(
        "unrelated",
        counted("unrelated", calls, report="ok"),
        assumes="the metrics sink accepts writes",
        needs=("base",),
        produces=("Latency Report",),
        audit=lambda ctx: ctx.ok("sink accepted the write"),
    )
    return runner


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.feed = Advisories()
        self.runner = plan(self.calls, self.feed)

    def test_first_run_executes_everything_in_dependency_order(self):
        report = self.runner.run()
        self.assertEqual(sorted(report.executed), ["base", "hashing", "routes", "unrelated"])
        self.assertTrue(report.complete)
        self.assertLess(self.calls.index("hashing"), self.calls.index("routes"))
        self.assertLess(self.calls.index("base"), self.calls.index("unrelated"))

    def test_a_task_is_a_decision_node_wired_to_its_ground(self):
        self.runner.run()
        graph = self.runner.graph
        routes = self.runner["routes"]
        node = graph.node(routes.node_id)
        self.assertIs(node.type, NodeType.DECISION)
        grounds = {
            e.dst for e in graph.out_edges(node.id, [EdgeType.DEPENDS_ON])
        }
        self.assertIn(routes.assumption_id, grounds)
        self.assertIn(self.runner["hashing"].node_id, grounds)
        produced = {
            graph.node(e.dst).label
            for e in graph.out_edges(node.id, [EdgeType.PRODUCES])
        }
        self.assertEqual(produced, {"Login Endpoint"})

    def test_a_cycle_is_refused_rather_than_hung_on(self):
        runner = Runner("cyclic")
        runner.task("a", lambda ctx: {}, assumes="x", needs=("b",))
        runner.task("b", lambda ctx: {}, assumes="y", needs=("a",))
        with self.assertRaises(ExecutionError) as caught:
            runner.run()
        self.assertIn("cycle", str(caught.exception))

    def test_an_unknown_dependency_is_refused(self):
        runner = Runner("dangling")
        runner.task("a", lambda ctx: {}, assumes="x", needs=("nope",))
        with self.assertRaises(ExecutionError):
            runner.run()


class DiscoveryTest(unittest.TestCase):
    """Nothing rejects an assumption except an auditor that disproved it."""

    def setUp(self):
        self.calls = []
        self.feed = Advisories()
        self.runner = plan(self.calls, self.feed)
        self.runner.run()

    def test_a_quiet_world_invalidates_nothing(self):
        self.assertEqual(self.runner.recheck(), [])
        for task in self.runner.tasks:
            self.assertIs(task.state, TaskState.DONE)

    def test_the_auditor_finds_the_advisory_without_being_told(self):
        self.feed.publish("argon2-cffi", "CVE-2026-9999")
        reports = self.runner.recheck()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].statement, "argon2-cffi has no open advisory")

    def test_the_disproof_is_recorded_as_evidence_against_the_assumption(self):
        self.feed.publish("argon2-cffi", "CVE-2026-9999")
        report = self.runner.recheck()[0]
        graph = self.runner.graph
        contradictions = [
            e
            for e in graph.in_edges(report.assumption_id, [EdgeType.CONTRADICTS],
                                    live_only=False)
            if graph.node(e.src).type is NodeType.EVIDENCE
        ]
        self.assertEqual(len(contradictions), 1)
        self.assertIn("CVE-2026-9999", graph.node(contradictions[0].src).label)

    def test_recheck_executes_nothing(self):
        self.calls.clear()
        self.feed.publish("argon2-cffi", "CVE-2026-9999")
        self.runner.recheck()
        self.assertEqual(self.calls, [])


class SelectiveRerunTest(unittest.TestCase):
    """The claim the package could not previously make good on."""

    def setUp(self):
        self.calls = []
        self.feed = Advisories()
        self.runner = plan(self.calls, self.feed)
        self.runner.run()
        self.before = {t.name: t.node_id for t in self.runner.tasks}
        self.feed.publish("argon2-cffi", "CVE-2026-9999")
        self.report = self.runner.recheck()[0]
        self.calls.clear()

    def test_only_the_closure_is_marked_stale(self):
        stale = {t.name for t in self.runner.tasks if t.state is TaskState.STALE}
        self.assertEqual(stale, {"hashing", "routes"})

    def test_the_rerun_runs_the_closure_and_nothing_else(self):
        self.runner.repair(
            "hashing",
            assumes="bcrypt has no open advisory",
            run=counted("hashing", self.calls, package="bcrypt"),
            produces=("Hasher", "Bcrypt Parameters"),
        )
        report = self.runner.run()
        self.assertEqual(sorted(self.calls), ["hashing", "routes"])
        self.assertEqual(sorted(report.executed), ["hashing", "routes"])
        self.assertEqual(sorted(report.cached), ["base", "unrelated"])
        self.assertTrue(report.complete)

    def test_preserved_work_keeps_its_original_result(self):
        self.runner.repair(
            "hashing",
            assumes="bcrypt has no open advisory",
            run=counted("hashing", self.calls, package="bcrypt"),
            produces=("Hasher", "Bcrypt Parameters"),
        )
        self.runner.run()
        for name in ("base", "unrelated"):
            self.assertEqual(self.runner[name].attempt, 1)
            self.assertEqual(self.runner[name].node_id, self.before[name])
        self.assertEqual(self.runner["hashing"].attempt, 2)
        self.assertNotEqual(self.runner["hashing"].node_id, self.before["hashing"])

    def test_a_rerun_supersedes_the_old_decision_rather_than_reviving_it(self):
        self.runner.repair(
            "hashing",
            assumes="bcrypt has no open advisory",
            run=counted("hashing", self.calls, package="bcrypt"),
            produces=("Hasher", "Bcrypt Parameters"),
        )
        self.runner.run()
        graph = self.runner.graph
        old = graph.node(self.before["hashing"])
        new = graph.node(self.runner["hashing"].node_id)
        self.assertTrue(old.invalidated, "the superseded decision must stay invalidated")
        self.assertTrue(new.live)
        self.assertEqual(old.attrs["superseded_by"], new.id)
        supersedes = {e.dst for e in graph.out_edges(new.id, [EdgeType.SUPERSEDES],
                                                     live_only=False)}
        self.assertIn(old.id, supersedes)

    def test_a_rebuilt_artefact_comes_back_and_an_abandoned_one_does_not(self):
        self.runner.repair(
            "hashing",
            assumes="bcrypt has no open advisory",
            run=counted("hashing", self.calls, package="bcrypt"),
            produces=("Hasher", "Bcrypt Parameters"),
        )
        self.runner.run()
        by_label = {
            n.label: n
            for n in self.runner.graph.nodes.values()
            if n.type is NodeType.ENTITY
        }
        self.assertTrue(by_label["Hasher"].live, "rebuilt, so it exists again")
        self.assertFalse(
            by_label["Argon2 Parameters"].live, "no longer produced, so still gone"
        )
        self.assertTrue(by_label["Bcrypt Parameters"].live)
        self.assertTrue(by_label["Latency Report"].live, "never fell in the first place")

    def test_rejected_ground_blocks_the_rerun_until_it_is_repaired(self):
        report = self.runner.run()
        self.assertEqual(self.calls, [], "nothing may run on rejected ground")
        self.assertIn("hashing", report.blocked)
        self.assertIn("ground rejected", report.blocked["hashing"])
        self.assertIn("routes", report.blocked)
        self.assertFalse(report.complete)

    def test_the_rejected_assumption_stays_rejected_after_repair(self):
        old_id = self.runner["hashing"].assumption_id
        new = self.runner.repair("hashing", assumes="bcrypt has no open advisory")
        graph = self.runner.graph
        self.assertIs(graph.assumptions[old_id].status, AssumptionStatus.REJECTED)
        self.assertIs(new.status, AssumptionStatus.ACTIVE)
        self.assertEqual(new.supersedes, old_id)
        lineage = [a.statement for a in self.runner.assumptions.lineage(new.id)]
        self.assertEqual(
            lineage, ["argon2-cffi has no open advisory", "bcrypt has no open advisory"]
        )


class PlainFailureTest(unittest.TestCase):
    """A wrong output is not a false assumption, and must not behave like one."""

    def setUp(self):
        self.calls = []
        self.runner = Runner("failing plan")
        self.runner.task(
            "first",
            counted("first", self.calls, value=0),
            assumes="the input file is well formed",
            produces=("Parsed Input",),
            audit=lambda ctx: ctx.fail("value came back as zero"),
        )
        self.runner.task(
            "second",
            counted("second", self.calls),
            assumes="the sink accepts writes",
            needs=("first",),
            audit=lambda ctx: ctx.ok("fine"),
        )

    def test_the_task_fails_and_its_ground_survives(self):
        report = self.runner.run()
        self.assertIs(self.runner.state("first"), TaskState.FAILED)
        self.assertIn("first", report.failed)
        assumption = self.runner.graph.assumptions[self.runner["first"].assumption_id]
        self.assertIs(assumption.status, AssumptionStatus.ACTIVE)
        self.assertEqual(
            [n for n in self.runner.graph.nodes.values() if n.invalidated], []
        )

    def test_the_dependant_is_blocked_not_run(self):
        report = self.runner.run()
        self.assertNotIn("second", self.calls)
        self.assertIn("second", report.blocked)
        self.assertIn("waiting on first", report.blocked["second"])

    def test_a_worker_that_raises_fails_the_task_rather_than_the_run(self):
        runner = Runner("throwing plan")

        def boom(ctx):
            raise RuntimeError("connection refused")

        runner.task("boom", boom, assumes="the service is up")
        report = runner.run()
        self.assertIs(runner.state("boom"), TaskState.FAILED)
        self.assertIn("connection refused", report.failed["boom"])


class VerdictTest(unittest.TestCase):
    def context(self):
        runner = Runner("verdicts")
        task = runner.task("t", lambda ctx: {}, assumes="something")
        return AuditContext(
            task=task,
            output={},
            assumption=runner.graph.assumptions[task.assumption_id],
            graph=runner.graph,
        )

    def test_a_bare_false_is_a_failure_not_a_disproof(self):
        runner = Runner("bools")
        runner.task("t", lambda ctx: {"x": 1}, assumes="something", audit=lambda ctx: False)
        runner.run()
        self.assertIs(runner.state("t"), TaskState.FAILED)
        assumption = runner.graph.assumptions[runner["t"].assumption_id]
        self.assertIs(assumption.status, AssumptionStatus.ACTIVE)

    def test_a_bare_true_passes(self):
        runner = Runner("bools")
        runner.task("t", lambda ctx: {"x": 1}, assumes="something", audit=lambda ctx: True)
        runner.run()
        self.assertIs(runner.state("t"), TaskState.DONE)

    def test_anything_else_is_an_error(self):
        runner = Runner("bad auditor")
        runner.task("t", lambda ctx: {}, assumes="something", audit=lambda ctx: "sure")
        with self.assertRaises(ExecutionError):
            runner.run()

    def test_the_helpers_say_which_kind_of_failure_it_is(self):
        ctx = self.context()
        self.assertEqual(ctx.ok("fine"), Verdict(True, "fine"))
        self.assertFalse(ctx.fail("wrong").disproves)
        self.assertTrue(ctx.disproved("false ground").disproves)

    def test_an_unaudited_task_is_reported_rather_than_failed(self):
        runner = Runner("unaudited")
        runner.task("t", lambda ctx: {}, assumes="something")
        runner.run()
        self.assertIs(runner.state("t"), TaskState.DONE)
        self.assertEqual(runner.unaudited, ("t",))


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.runner = demo().runner

    def test_the_chain_verifies(self):
        self.assertTrue(self.runner.ledger.verify())

    def test_a_rewritten_entry_breaks_the_chain(self):
        ledger = self.runner.ledger
        entries = ledger._entries
        entries[1] = dataclasses.replace(entries[1], detail="never happened")
        self.assertFalse(ledger.verify())

    def test_a_dropped_entry_breaks_the_chain(self):
        ledger = self.runner.ledger
        del ledger._entries[2]
        self.assertFalse(ledger.verify())

    def test_the_public_view_cannot_be_mutated(self):
        entries = self.runner.ledger.entries
        self.assertIsInstance(entries, tuple)
        self.assertEqual(len(entries), len(self.runner.ledger))

    def test_the_invalidation_is_in_the_record_with_its_reason(self):
        details = [
            e.detail
            for e in self.runner.ledger.entries
            if e.event.value == "disproved"
        ]
        self.assertEqual(len(details), 1)
        self.assertIn("CVE-2026-9999", details[0])

    def test_the_rerun_is_in_the_record_as_a_second_attempt(self):
        executed = [e for e in self.runner.ledger.entries if e.event.value == "executed"]
        hashing = [e for e in executed if e.task == "hashing"]
        self.assertEqual(len(hashing), 2)
        self.assertIn("supersedes", hashing[1].detail)

    def test_cached_work_is_recorded_as_cached(self):
        cached = {e.task for e in self.runner.ledger.entries if e.event.value == "cached"}
        self.assertEqual(cached, {"schema", "tokens"})


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.run = demo()

    def test_the_blast_radius_is_three_decisions_and_their_artefacts(self):
        graph = self.run.runner.graph
        report = self.run.invalidations[0]
        kinds = {}
        for node_id in report.invalidated:
            kinds.setdefault(graph.node(node_id).type.value, []).append(node_id)
        self.assertEqual(len(kinds["decision"]), 3)
        self.assertEqual(len(kinds["entity"]), 5)

    def test_the_schema_and_token_work_is_preserved(self):
        report = self.run.invalidations[0]
        runner = self.run.runner
        self.assertIn(runner["schema"].node_id, report.preserved)
        self.assertIn(runner["tokens"].node_id, report.preserved)

    def test_everything_ends_standing(self):
        for task in self.run.runner.tasks:
            self.assertIs(task.state, TaskState.DONE)
        self.assertTrue(self.run.runner.rounds[-1].complete)

    def test_the_graph_stays_typed(self):
        self.assertEqual(self.run.runner.graph.untyped_edges, 0)

    def test_the_run_is_reproducible(self):
        a, b = demo(), demo()
        self.assertEqual(a.runner.ledger.head, b.runner.ledger.head)
        self.assertEqual(a.runner.graph.to_dict(), b.runner.graph.to_dict())
        self.assertEqual(a.runner.to_dict(), b.runner.to_dict())


if __name__ == "__main__":
    unittest.main()
