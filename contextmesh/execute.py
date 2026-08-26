"""Selective re-execution: what has to *run again* when the ground moves.

``assumptions.reject`` answers what falls. Until this module, nothing answered
what has to be recomputed, so rejecting an assumption left a graph full of nodes
flagged ``invalidated`` and no work scheduled to replace them — the expensive
half of the idea (redo the blast radius, keep everything else cached) was
described but never performed.

A :class:`Runner` binds units of work to the graph's own vocabulary, so
scheduling reads off the ontology instead of a second copy of it kept here:

===============  =========================================
``task``         a ``decision`` node
``task.assumes`` ``decision -[depends_on]-> assumption``
``task.needs``   ``decision -[depends_on]-> decision``
``task.produces````decision -[produces]-> entity``
an audit         ``decision -[justified_by]-> evidence``
a disproof       ``evidence -[contradicts]-> assumption``
===============  =========================================

Two rules keep re-execution compatible with an append-only history:

1. **A decision is never revived; it is superseded.** Re-running a step appends
   a new ``decision`` node that ``supersedes`` the invalidated one, so the old
   reasoning stays walkable (GRAPH.md rule 3).
2. **An artefact is revived, because it was rebuilt.** An ``entity`` fell only
   because the decision that produced it fell. When that work is redone the
   thing exists again — under the same id, since it is the same real thing. An
   artefact the rerun *stops* producing simply stays invalidated.

And one rule keeps invalidation honest: an assumption is rejected here only
because an auditor disproved it. :meth:`Runner.recheck` re-runs every standing
auditor against current facts without re-executing anything, and a disproof
there is what computes a blast radius and schedules the reruns. No caller
reaches in and flips a node.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .assumptions import AssumptionLedger, InvalidationReport
from .decisions import DecisionLog
from .graph import ContextGraph
from .model import Assumption, AssumptionStatus, EdgeType, NodeType, slug


class ExecutionError(Exception):
    """Raised when a plan cannot be scheduled, or an auditor misbehaves."""


class TaskState(str, Enum):
    """What the runner believes about a task.

    ``BLOCKED`` is deliberately absent: being blocked is a fact about the
    current round, not about the task, so it is recomputed every run rather
    than stored and left to go stale.
    """

    PENDING = "pending"
    DONE = "done"
    STALE = "stale"
    FAILED = "failed"


class Event(str, Enum):
    EXECUTED = "executed"
    AUDITED = "audited"
    DISPROVED = "disproved"
    INVALIDATED = "invalidated"
    REPAIRED = "repaired"
    CACHED = "cached"
    BLOCKED = "blocked"
    FAILED = "failed"


# ── verdicts ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Verdict:
    """An auditor's finding.

    ``disproves`` is the whole distinction. "The output is wrong" and "the
    ground this stood on is false" are different failures with different
    blast radii, and an auditor that cannot say which one it found forces the
    engine to guess. A plain failure fails one task; a disproof rejects an
    assumption and takes down everything that stood on it.
    """

    ok: bool
    reason: str
    disproves: bool = False


def _coerce(result: Any) -> Verdict:
    if isinstance(result, Verdict):
        return result
    if isinstance(result, bool):
        # A bare ``False`` is read as "the work is wrong", never as a disproof.
        # Tearing down a blast radius is not something to infer from a boolean.
        return Verdict(result, "audit passed" if result else "audit failed")
    raise ExecutionError(
        f"an auditor must return a Verdict or a bool, got {type(result).__name__}"
    )


@dataclass(frozen=True)
class RunContext:
    """What a worker is handed: its ground, and its dependencies' outputs."""

    task: "Task"
    attempt: int
    graph: ContextGraph
    assumption: Assumption
    inputs: Dict[str, Dict[str, Any]]


@dataclass(frozen=True)
class AuditContext:
    """What an auditor is handed: the output, and the assumption it must hold under."""

    task: "Task"
    output: Dict[str, Any]
    assumption: Assumption
    graph: ContextGraph

    def ok(self, reason: str) -> Verdict:
        return Verdict(True, reason)

    def fail(self, reason: str) -> Verdict:
        """The work is wrong. Fails this task; invalidates nothing."""
        return Verdict(False, reason)

    def disproved(self, reason: str) -> Verdict:
        """The ground is false. Rejects the assumption and invalidates its radius."""
        return Verdict(False, reason, disproves=True)


Worker = Callable[[RunContext], Dict[str, Any]]
Auditor = Callable[[AuditContext], Any]


@dataclass
class Task:
    """A unit of work and the assumption it is only valid under."""

    name: str
    title: str
    run: Worker
    assumes: str
    rationale: str = ""
    needs: Tuple[str, ...] = ()
    produces: Tuple[str, ...] = ()
    audit: Optional[Auditor] = None

    # ── runner-owned state ───────────────────────────────────────────────
    state: TaskState = TaskState.PENDING
    attempt: int = 0
    node_id: Optional[str] = None
    assumption_id: Optional[str] = None
    output: Dict[str, Any] = field(default_factory=dict)
    artefacts: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "state": self.state.value,
            "attempt": self.attempt,
            "assumes": self.assumes,
            "needs": list(self.needs),
            "produces": list(self.produces),
            "audited": self.audit is not None,
            "node_id": self.node_id,
            "assumption_id": self.assumption_id,
            "artefacts": list(self.artefacts),
        }


# ── the ledger ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    round: int
    event: Event
    task: str
    detail: str
    node_id: Optional[str] = None
    assumption_id: Optional[str] = None
    digest: str = ""

    def payload(self) -> str:
        return "|".join(
            [
                str(self.seq),
                str(self.round),
                self.event.value,
                self.task,
                self.detail,
                self.node_id or "",
                self.assumption_id or "",
            ]
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "round": self.round,
            "event": self.event.value,
            "task": self.task,
            "detail": self.detail,
            "node_id": self.node_id,
            "assumption_id": self.assumption_id,
            "digest": self.digest,
        }


class RunLedger:
    """Append-only, and checkably so.

    Every entry's digest covers the digest before it, so a rewritten or dropped
    entry breaks the chain and :meth:`verify` says so. "Append-only" as a
    convention is just a promise; as a hash chain it is a test.

    There are no wall-clock timestamps in it. The package guarantees builds that
    are byte-identical across runs and hash seeds, and a clock would be the one
    field in the record that could not be reproduced.
    """

    GENESIS = "contextmesh"

    def __init__(self) -> None:
        self._entries: List[LedgerEntry] = []

    @property
    def entries(self) -> Tuple[LedgerEntry, ...]:
        """A copy. The list itself is private so nothing can rewrite history."""
        return tuple(self._entries)

    def record(
        self,
        round: int,
        event: Event,
        task: str,
        detail: str,
        *,
        node_id: Optional[str] = None,
        assumption_id: Optional[str] = None,
    ) -> LedgerEntry:
        previous = self._entries[-1].digest if self._entries else self.GENESIS
        entry = LedgerEntry(
            seq=len(self._entries) + 1,
            round=round,
            event=event,
            task=task,
            detail=detail,
            node_id=node_id,
            assumption_id=assumption_id,
        )
        digest = hashlib.sha1(f"{previous}|{entry.payload()}".encode("utf-8")).hexdigest()[:12]
        entry = LedgerEntry(**{**entry.__dict__, "digest": digest})
        self._entries.append(entry)
        return entry

    def verify(self) -> bool:
        previous = self.GENESIS
        for index, entry in enumerate(self._entries, start=1):
            if entry.seq != index:
                return False
            expect = hashlib.sha1(f"{previous}|{entry.payload()}".encode("utf-8")).hexdigest()[:12]
            if expect != entry.digest:
                return False
            previous = entry.digest
        return True

    @property
    def head(self) -> str:
        return self._entries[-1].digest if self._entries else self.GENESIS

    def of(self, task: str) -> Tuple[LedgerEntry, ...]:
        return tuple(e for e in self._entries if e.task == task)

    def count(self, event: Event) -> int:
        return sum(1 for e in self._entries if e.event is event)

    def to_dict(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self._entries]

    def __len__(self) -> int:
        return len(self._entries)


@dataclass
class RunReport:
    round: int
    layers: List[List[str]] = field(default_factory=list)
    executed: List[str] = field(default_factory=list)
    cached: List[str] = field(default_factory=list)
    blocked: Dict[str, str] = field(default_factory=dict)
    failed: Dict[str, str] = field(default_factory=dict)
    invalidations: List[InvalidationReport] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.blocked and not self.failed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round": self.round,
            "layers": [list(layer) for layer in self.layers],
            "executed": list(self.executed),
            "cached": list(self.cached),
            "blocked": dict(self.blocked),
            "failed": dict(self.failed),
            "invalidations": [r.to_dict() for r in self.invalidations],
            "complete": self.complete,
        }


class Runner:
    """Schedules work onto a ContextGraph, and reschedules exactly what fell.

    The scheduling rule is the same one the graph already uses for
    invalidation, read the other way round: a task runs when everything it
    ``depends_on`` is standing, and it reruns when something it depends on
    stopped standing.
    """

    #: A guard against a plan that keeps re-invalidating itself. Reruns are
    #: bounded by the graph, not by the runner, so hitting this means the plan
    #: has a live cycle the topological check could not see statically.
    MAX_PASSES = 6

    def __init__(
        self,
        plan: str,
        *,
        graph: Optional[ContextGraph] = None,
        assumptions: Optional[AssumptionLedger] = None,
        decisions: Optional[DecisionLog] = None,
    ) -> None:
        # ``is None``, not ``or``: an empty graph is falsy-looking but real, and
        # ``graph or ContextGraph()`` would silently substitute a fresh one.
        self.plan = plan
        self.graph = graph if graph is not None else ContextGraph()
        self.assumptions = (
            assumptions if assumptions is not None else AssumptionLedger(self.graph)
        )
        self.decisions = decisions if decisions is not None else DecisionLog(self.graph)
        self.ledger = RunLedger()
        self.rounds: List[RunReport] = []
        self.round = 0
        self._tasks: Dict[str, Task] = {}
        self._order: List[str] = []
        self._blocked: Dict[str, str] = {}
        self.source = self.graph.add_node(
            NodeType.SOURCE,
            f"execution plan: {plan}",
            attrs={"origin": "runner", "retrieved_at": "at plan time"},
        )

    # ── declaring work ───────────────────────────────────────────────────
    def task(
        self,
        name: str,
        run: Worker,
        *,
        assumes: str,
        title: Optional[str] = None,
        rationale: str = "",
        needs: Iterable[str] = (),
        produces: Iterable[str] = (),
        audit: Optional[Auditor] = None,
    ) -> Task:
        if name in self._tasks:
            raise ExecutionError(f"duplicate task {name!r}")
        task = Task(
            name=name,
            title=title or name.replace("_", " ").strip().capitalize(),
            run=run,
            assumes=assumes,
            rationale=rationale,
            needs=tuple(needs),
            produces=tuple(produces),
            audit=audit,
        )
        task.assumption_id = self._assume(assumes).id
        self._tasks[name] = task
        self._order.append(name)
        return task

    def __getitem__(self, name: str) -> Task:
        return self._tasks[name]

    @property
    def tasks(self) -> Tuple[Task, ...]:
        return tuple(self._tasks[n] for n in self._order)

    @property
    def unaudited(self) -> Tuple[str, ...]:
        """Tasks running on nothing but hope. Worth surfacing, not worth failing on."""
        return tuple(n for n in self._order if self._tasks[n].audit is None)

    def state(self, name: str) -> TaskState:
        return self._tasks[name].state

    def blocked_reason(self, name: str) -> Optional[str]:
        return self._blocked.get(name)

    def output(self, name: str) -> Dict[str, Any]:
        return dict(self._tasks[name].output)

    # ── running ──────────────────────────────────────────────────────────
    def run(self) -> RunReport:
        """Execute every task that is pending or stale and whose ground stands.

        Anything already ``DONE`` is reported as cached and not touched. That is
        the payoff for selective invalidation: the second run is small.
        """
        self._validate()
        self.round += 1
        self.graph.build += 1
        report = RunReport(round=self.round)
        self._blocked = {}
        done_at_start = {n for n in self._order if self._tasks[n].state is TaskState.DONE}

        passes = 0
        while True:
            ready = self._ready()
            if not ready:
                break
            passes += 1
            if passes > self.MAX_PASSES * max(1, len(self._order)):
                raise ExecutionError(
                    "scheduler made no progress towards a stable graph; "
                    "a task is invalidating something it depends on"
                )
            report.layers.append(list(ready))
            for name in ready:
                task = self._tasks[name]
                if task.state not in (TaskState.PENDING, TaskState.STALE):
                    # An earlier task in this same layer disproved its ground.
                    continue
                invalidation = self._execute(task)
                if invalidation is not None:
                    report.invalidations.append(invalidation)
                if task.state is TaskState.DONE:
                    report.executed.append(name)
                elif task.state is TaskState.FAILED:
                    report.failed[name] = self._last_detail(name)

        for name in self._order:
            task = self._tasks[name]
            already = name in done_at_start and name not in report.executed
            if task.state is TaskState.DONE and already:
                report.cached.append(name)
                self.ledger.record(
                    self.round, Event.CACHED, name,
                    "unaffected by this round; previous result stands",
                    node_id=task.node_id,
                )
            elif task.state in (TaskState.PENDING, TaskState.STALE):
                reason = self._blocked.get(name) or "no reason recorded"
                report.blocked[name] = reason
                self.ledger.record(
                    self.round, Event.BLOCKED, name, reason,
                    assumption_id=task.assumption_id,
                )

        self.rounds.append(report)
        return report

    def recheck(self) -> List[InvalidationReport]:
        """Re-audit standing work against current facts. Executes nothing.

        This is where the outside world gets to change its mind. Each auditor is
        handed the output its task already produced and asked whether it still
        holds; a disproof rejects the assumption through the ledger, which is
        what produces the blast radius and marks the reruns.
        """
        self.round += 1
        self.graph.build += 1
        reports: List[InvalidationReport] = []
        for name in self._order:
            task = self._tasks[name]
            if task.state is not TaskState.DONE or task.audit is None:
                continue
            assumption = self.graph.assumptions.get(task.assumption_id or "")
            if assumption is None or assumption.status is not AssumptionStatus.ACTIVE:
                continue
            verdict = self._audit(task, task.output, assumption)
            self.ledger.record(
                self.round,
                Event.AUDITED,
                name,
                f"{'holds' if verdict.ok else 'fails'}: {verdict.reason}",
                node_id=task.node_id,
                assumption_id=assumption.id,
            )
            if verdict.ok:
                continue
            if verdict.disproves:
                reports.append(self._disprove(task, assumption, verdict.reason))
            else:
                task.state = TaskState.FAILED
                self.ledger.record(
                    self.round, Event.FAILED, name, verdict.reason, node_id=task.node_id
                )
        return reports

    def repair(
        self,
        name: str,
        *,
        assumes: Optional[str] = None,
        run: Optional[Worker] = None,
        audit: Optional[Auditor] = None,
        produces: Optional[Iterable[str]] = None,
        rationale: Optional[str] = None,
    ) -> Assumption:
        """Put new ground under a task so it can run again.

        A rejected assumption is never edited back to life — it stays rejected,
        and the replacement records that it supersedes it. That is what keeps
        "why did this change" answerable by walking the assumption's lineage.
        """
        task = self._tasks[name]
        old = self.graph.assumptions.get(task.assumption_id or "")
        new = old
        if assumes:
            new = self._assume(assumes)
            if old is not None and new.id != old.id and new.supersedes is None:
                new.version = old.version + 1
                new.supersedes = old.id
                old.superseded_by = new.id
                self.graph.add_edge(new.id, EdgeType.SUPERSEDES, old.id)
            task.assumes = assumes
            task.assumption_id = new.id
        if run is not None:
            task.run = run
        if audit is not None:
            task.audit = audit
        if produces is not None:
            task.produces = tuple(produces)
        if rationale is not None:
            task.rationale = rationale
        task.state = TaskState.STALE
        self.ledger.record(
            self.round,
            Event.REPAIRED,
            name,
            f"reground on: {new.statement}" if new is not None else "repaired",
            node_id=task.node_id,
            assumption_id=new.id if new is not None else None,
        )
        if new is None:
            raise ExecutionError(f"task {name!r} has no assumption to repair")
        return new

    # ── internals ────────────────────────────────────────────────────────
    def _assume(self, statement: str) -> Assumption:
        existing = self.graph.assumptions.get(slug(statement, "assumption"))
        if existing is not None:
            return existing
        return self.assumptions.assume(statement, created_by="runner")

    def _validate(self) -> None:
        for name in self._order:
            for need in self._tasks[name].needs:
                if need not in self._tasks:
                    raise ExecutionError(f"task {name!r} needs unknown task {need!r}")
        remaining = {n: set(self._tasks[n].needs) for n in self._order}
        settled: Set[str] = set()
        while True:
            ready = [n for n in self._order if n not in settled and not remaining[n] - settled]
            if not ready:
                break
            settled.update(ready)
        if len(settled) != len(self._order):
            stuck = sorted(set(self._order) - settled)
            raise ExecutionError(f"dependency cycle among tasks: {', '.join(stuck)}")

    def _ready(self) -> List[str]:
        """Tasks that can run right now, in declaration order.

        Also the place blocked-ness is decided, because "why is this not
        running" and "what runs next" are the same question asked twice.
        """
        ready: List[str] = []
        for name in self._order:
            task = self._tasks[name]
            if task.state not in (TaskState.PENDING, TaskState.STALE):
                continue
            assumption = self.graph.assumptions.get(task.assumption_id or "")
            if assumption is None:
                self._blocked[name] = "no assumption bound"
                continue
            if assumption.status is AssumptionStatus.REJECTED:
                self._blocked[name] = (
                    f"ground rejected: {assumption.statement} — repair before rerunning"
                )
                continue
            if assumption.status is AssumptionStatus.SUPERSEDED:
                self._blocked[name] = (
                    f"ground superseded by {assumption.superseded_by}; rebind the task"
                )
                continue
            unmet = [
                need
                for need in task.needs
                if self._tasks[need].state is not TaskState.DONE
            ]
            if unmet:
                self._blocked[name] = "waiting on " + ", ".join(sorted(unmet))
                continue
            self._blocked.pop(name, None)
            ready.append(name)
        return ready

    def _audit(self, task: Task, output: Dict[str, Any], assumption: Assumption) -> Verdict:
        if task.audit is None:
            return Verdict(True, "no auditor declared")
        return _coerce(
            task.audit(
                AuditContext(
                    task=task, output=output, assumption=assumption, graph=self.graph
                )
            )
        )

    def _execute(self, task: Task) -> Optional[InvalidationReport]:
        assumption = self.graph.assumptions[task.assumption_id]
        inputs = {need: dict(self._tasks[need].output) for need in task.needs}
        context = RunContext(
            task=task,
            attempt=task.attempt + 1,
            graph=self.graph,
            assumption=assumption,
            inputs=inputs,
        )
        try:
            output = task.run(context)
        except Exception as exc:  # a worker that raises is a failed task, not a crash
            task.state = TaskState.FAILED
            self.ledger.record(
                self.round,
                Event.FAILED,
                task.name,
                f"{type(exc).__name__}: {exc}",
                assumption_id=assumption.id,
            )
            return None

        task.attempt += 1
        task.output = dict(output or {})

        verdict = self._audit(task, task.output, assumption)
        self.ledger.record(
            self.round,
            Event.AUDITED,
            task.name,
            f"{'holds' if verdict.ok else 'fails'}: {verdict.reason}",
            assumption_id=assumption.id,
        )
        if not verdict.ok:
            if verdict.disproves:
                return self._disprove(task, assumption, verdict.reason)
            task.state = TaskState.FAILED
            self.ledger.record(
                self.round, Event.FAILED, task.name, verdict.reason, assumption_id=assumption.id
            )
            return None

        self._commit(task, assumption, verdict)
        return None

    def _commit(self, task: Task, assumption: Assumption, verdict: Verdict) -> None:
        """Append the decision, its artefacts, and the evidence that cleared it."""
        labels = list(task.produces)
        artefacts = [
            self.graph.add_node(
                NodeType.ENTITY, label, attrs={"canonical": label, "aliases": []}
            ).id
            for label in labels
        ]

        previous = task.node_id
        rationale = task.rationale or f"Executed under: {assumption.statement}"
        if task.attempt > 1:
            rationale = (
                f"{rationale} (rerun {task.attempt}: the previous ground did not survive audit)"
            )
        node = self.decisions.decide(
            task.title,
            rationale,
            id=slug(f"{self.plan}|{task.name}|v{task.attempt}", "decision"),
            source_id=self.source.id,
            assumptions=[assumption.id],
            produces=artefacts,
            supersedes=previous,
        )
        for need in task.needs:
            dependency = self._tasks[need]
            if dependency.node_id:
                self.graph.add_edge(node.id, EdgeType.DEPENDS_ON, dependency.node_id)

        # Rule 2: the artefact exists again because it was rebuilt. An artefact
        # this rerun no longer produces is simply not in ``artefacts`` and stays
        # invalidated, which is the correct answer for a thing that is now gone.
        rebuilt = []
        for artefact_id in artefacts:
            artefact = self.graph.node(artefact_id)
            if artefact.invalidated:
                artefact.invalidated = False
                rebuilt.append(artefact.label)

        evidence = self.graph.add_node(
            NodeType.EVIDENCE,
            f"audit of {task.name}: {verdict.reason}",
            attrs={"kind": "audit"},
        )
        self.graph.add_edge(node.id, EdgeType.JUSTIFIED_BY, evidence.id)

        task.node_id = node.id
        task.artefacts = tuple(artefacts)
        task.state = TaskState.DONE
        detail = f"attempt {task.attempt}"
        if previous:
            detail += f", supersedes {previous}"
        if rebuilt:
            detail += f", rebuilt {', '.join(rebuilt)}"
        self.ledger.record(
            self.round,
            Event.EXECUTED,
            task.name,
            detail,
            node_id=node.id,
            assumption_id=assumption.id,
        )

    def _disprove(
        self, task: Task, assumption: Assumption, reason: str
    ) -> InvalidationReport:
        """An auditor found the ground false. Reject it and mark what fell."""
        evidence = self.graph.add_node(
            NodeType.EVIDENCE,
            f"disproof from {task.name}: {reason}",
            attrs={"kind": "disproof"},
        )
        self.ledger.record(
            self.round,
            Event.DISPROVED,
            task.name,
            reason,
            node_id=evidence.id,
            assumption_id=assumption.id,
        )
        report = self.assumptions.reject(assumption.id, evidence_id=evidence.id)

        for name in self._order:
            other = self._tasks[name]
            grounded_on_it = other.assumption_id == assumption.id
            in_radius = bool(other.node_id) and other.node_id in report.invalidated
            if not grounded_on_it and not in_radius:
                continue
            if other.state is TaskState.DONE or other is task:
                other.state = TaskState.STALE
                self.ledger.record(
                    self.round,
                    Event.INVALIDATED,
                    name,
                    report.why(other.node_id) if in_radius else f"stood on: {assumption.statement}",
                    node_id=other.node_id,
                    assumption_id=assumption.id,
                )
        return report

    def _last_detail(self, name: str) -> str:
        entries = self.ledger.of(name)
        return entries[-1].detail if entries else ""

    # ── reporting ────────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan": self.plan,
            "round": self.round,
            "tasks": [self._tasks[n].to_dict() for n in self._order],
            "unaudited": list(self.unaudited),
            "rounds": [r.to_dict() for r in self.rounds],
            "ledger": self.ledger.to_dict(),
            "ledger_head": self.ledger.head,
            "ledger_intact": self.ledger.verify(),
        }


# ── the worked example ───────────────────────────────────────────────────
class Advisories:
    """A stand-in for the outside world: a security feed the auditors read.

    The runner never writes here, and neither does the demo script's
    invalidation step. That is the point. The CVE is not a flag flipped on a
    node by the code that wants the demo to be dramatic — it is a fact that
    appears outside the graph, and the hashing auditor *discovers* it on the
    next recheck. If the auditor were removed, nothing would fall over, which
    is exactly the property a fake demo cannot have.
    """

    def __init__(self, open_advisories: Iterable[Tuple[str, str]] = ()) -> None:
        self._open: Dict[str, str] = dict(open_advisories)

    def publish(self, package: str, advisory: str) -> None:
        self._open[package] = advisory

    def withdraw(self, package: str) -> None:
        self._open.pop(package, None)

    def clear(self, package: str) -> bool:
        return package not in self._open

    def advisory(self, package: str) -> Optional[str]:
        return self._open.get(package)


@dataclass
class DemoRun:
    runner: Runner
    feed: Advisories
    invalidations: List[InvalidationReport] = field(default_factory=list)


def _hasher(package: str, params: str, artefact: str) -> Worker:
    def run(ctx: RunContext) -> Dict[str, Any]:
        return {
            "package": package,
            "params": params,
            "artefact": artefact,
            "verified": True,
        }

    return run


def _audit_hashing(feed: Advisories) -> Auditor:
    """The only auditor that reads the world instead of just the output."""

    def audit(ctx: AuditContext) -> Verdict:
        package = ctx.output["package"]
        advisory = feed.advisory(package)
        if advisory is not None:
            return ctx.disproved(f"{package} has an open advisory — {advisory}")
        return ctx.ok(f"{package} carries no open advisory")

    return audit


def demo(feed: Optional[Advisories] = None) -> DemoRun:
    """Build a service, break its ground from outside, rebuild only what fell.

    Five steps, two of which are independent of the hashing choice. A CVE lands
    against the hashing package *after* everything is green; the next recheck
    finds it; exactly the three steps that stood on it rerun.
    """
    feed = feed if feed is not None else Advisories()
    runner = Runner("secure authentication service")

    runner.task(
        "schema",
        lambda ctx: {"tables": ["users", "sessions"], "migrations": 2},
        title="Define the auth schema",
        assumes="the user store is reachable and empty",
        produces=("Auth Schema",),
        audit=lambda ctx: (
            ctx.ok(f"{len(ctx.output['tables'])} tables created")
            if ctx.output["tables"]
            else ctx.fail("no tables were created")
        ),
    )
    runner.task(
        "hashing",
        _hasher("argon2-cffi", "t=3,m=64MiB,p=4", "Argon2 Parameters"),
        title="Implement password hashing",
        assumes="argon2-cffi has no open advisory",
        produces=("Password Hasher", "Argon2 Parameters"),
        audit=_audit_hashing(feed),
    )
    runner.task(
        "tokens",
        lambda ctx: {"algorithm": "EdDSA", "key_bits": 256},
        title="Implement the token generator",
        assumes="the signing key is provisioned from the environment",
        produces=("Token Generator",),
        audit=lambda ctx: (
            ctx.ok(f"{ctx.output['key_bits']}-bit {ctx.output['algorithm']} key")
            if ctx.output["key_bits"] >= 256
            else ctx.fail("signing key is under 256 bits")
        ),
    )
    runner.task(
        "routes",
        lambda ctx: {
            "endpoints": ["POST /login", "POST /register"],
            "hasher": ctx.inputs["hashing"]["package"],
        },
        title="Implement the auth routes",
        assumes="the hasher and token generator expose stable interfaces",
        needs=("hashing", "tokens"),
        produces=("Login Endpoint", "Register Endpoint"),
        audit=lambda ctx: (
            ctx.ok(f"{len(ctx.output['endpoints'])} endpoints over {ctx.output['hasher']}")
            if len(ctx.output["endpoints"]) == 2
            else ctx.fail("expected a login and a register endpoint")
        ),
    )
    runner.task(
        "rate_limit",
        lambda ctx: {"window_seconds": 60, "limit": 10},
        title="Implement rate limiting",
        assumes="the rate limit store is thread safe",
        needs=("routes",),
        produces=("Rate Limiter",),
        audit=lambda ctx: (
            ctx.ok(f"{ctx.output['limit']} per {ctx.output['window_seconds']}s")
            if ctx.output["limit"] > 0
            else ctx.fail("rate limit is not enforcing anything")
        ),
    )

    runner.run()

    # The world moves. Nothing in the graph is touched by this line.
    feed.publish("argon2-cffi", "CVE-2026-9999: memory-hard parameters silently downgraded")

    invalidations = runner.recheck()

    runner.repair(
        "hashing",
        assumes="bcrypt has no open advisory",
        run=_hasher("bcrypt", "cost=12", "Bcrypt Parameters"),
        produces=("Password Hasher", "Bcrypt Parameters"),
    )
    runner.run()

    return DemoRun(runner=runner, feed=feed, invalidations=invalidations)
