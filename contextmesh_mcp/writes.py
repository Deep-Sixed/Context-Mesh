"""Controlled MCP writes over a durable Context Mesh session.

The read tool layer deliberately knows nothing about persistence.  Structural
writes need a stronger contract than ``mesh_ask`` telemetry: a successful reply
means the mutation is in the committed session generation.  To make failure
atomic, writes are applied to a lossless in-memory clone first; only the clone is
made live after its manifest has committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from contextmesh.assumptions import AssumptionLedger
from contextmesh.evidence import EvidenceIntakeError, submit_evidence
from contextmesh.execute import RunLedger, Runner
from contextmesh.graph import ContextGraph
from contextmesh.resolve import Resolver

from .session import (
    Checkpointer,
    Session,
    SessionError,
    SessionLockedError,
    WalkerConfig,
)


class ControlledWriteError(EvidenceIntakeError):
    """A requested MCP mutation could not be committed durably."""


@dataclass(frozen=True)
class WriteResult:
    payload: Dict[str, Any]
    session: Session
    changed: bool


def _clone_session(source: Session) -> Session:
    """Losslessly clone the durable state without sharing mutable graph objects."""
    graph = ContextGraph.from_dict(source.graph.to_dict())
    resolver = Resolver.from_dict(source.resolver.to_dict())
    walker = WalkerConfig.of(source.walker).walker(graph, resolver)

    assumption_ledger = AssumptionLedger(graph)
    assumption_ledger.history = [dict(row) for row in source.ledger.history]

    runner: Optional[Runner] = None
    if source.runner is not None:
        registry = source.runner.registry
        if registry is None:
            raise ControlledWriteError(
                "the session has execution state but no TaskRegistry; controlled writes refuse it"
            )
        run_ledger = RunLedger.from_snapshot(
            source.runner.ledger.snapshot(),
            expect_head=source.runner.ledger.head,
        )
        runner = Runner.from_snapshot(
            source.runner.snapshot(),
            graph=graph,
            registry=registry,
            ledger=run_ledger,
        )
        # ``rounds`` is intentionally not durable, but a write inside one live
        # process should not erase the reports it already had in memory.
        runner.rounds = list(source.runner.rounds)
        runner.assumptions.history = [
            dict(row) for row in source.runner.assumptions.history
        ]

    return Session(
        graph=graph,
        resolver=resolver,
        walker=walker,
        ledger=assumption_ledger,
        rounds=source.rounds,
        source=source.source,
        path=source.path,
        generation=source.generation,
        runner=runner,
    )


def _require_durable(
    session: Session, checkpointer: Optional[Checkpointer]
) -> Checkpointer:
    if checkpointer is None:
        raise ControlledWriteError(
            "controlled writes require an active session checkpointer"
        )
    if checkpointer.session is not session:
        raise ControlledWriteError(
            "the checkpointer belongs to a different session; "
            "refusing to commit the wrong graph"
        )
    if session.path is None:
        raise ControlledWriteError(
            "controlled writes require a saved session directory; "
            "the demo graph is ephemeral"
        )
    if checkpointer.policy == "never":
        raise ControlledWriteError(
            "checkpoint policy 'never' cannot acknowledge a durable structural write"
        )
    return checkpointer


def commit_mutation(
    session: Session,
    checkpointer: Optional[Checkpointer],
    mutate: Callable[[Session], Tuple[Dict[str, Any], bool]],
) -> WriteResult:
    """Run ``mutate`` on a clone and publish it only after the manifest commits."""
    cp = _require_durable(session, checkpointer)
    staged = _clone_session(session)
    payload, changed = mutate(staged)
    if not changed:
        return WriteResult(payload=payload, session=session, changed=False)

    before_generation = session.generation
    try:
        staged.checkpoint()
    except SessionLockedError as exc:
        cp.contended += 1
        raise ControlledWriteError(str(exc)) from None
    except SessionError as exc:
        # SessionError includes compare-and-swap refusal.  It is known to happen
        # before this writer replaces the manifest, so the live session remains
        # authoritative and needs no rollback at all.
        raise ControlledWriteError(str(exc)) from None
    except Exception as exc:
        # ``session.json`` is the commit point.  A best-effort cleanup failure
        # after its swap must not make us resurrect the previous in-memory
        # generation.  If the directory moved exactly one generation, adopt the
        # clone; otherwise the mutation never became committed.
        assert staged.path is not None
        live = Session._live_generation(staged.path)
        if live != before_generation + 1:
            raise ControlledWriteError(f"session checkpoint failed: {exc}") from exc
        staged.generation = live

    cp.session = staged
    cp.pending = 0
    cp.commits += 1
    return WriteResult(payload=payload, session=staged, changed=True)


def _receipt_payload(receipt: Any) -> Dict[str, Any]:
    node = receipt.node
    return {
        "evidence_id": receipt.evidence_id,
        "created": receipt.created,
        "node": {
            "id": node.id,
            "type": node.type.value,
            "label": node.label,
            "attrs": node.attrs,
            "provenance": node.provenance.to_dict() if node.provenance else None,
        },
    }


def mesh_submit_evidence(
    session: Session,
    checkpointer: Optional[Checkpointer],
    *,
    text: str,
    source_id: str,
    external_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> WriteResult:
    """Store one observation.  It creates no edge and expresses no verdict."""

    def mutate(staged: Session) -> Tuple[Dict[str, Any], bool]:
        receipt = submit_evidence(
            staged.graph,
            text=text,
            source_id=source_id,
            external_id=external_id,
            metadata=metadata,
        )
        return _receipt_payload(receipt), receipt.created

    return commit_mutation(session, checkpointer, mutate)


WRITE_TOOLS: Dict[str, Dict[str, Any]] = {
    "mesh_submit_evidence": {
        "fn": mesh_submit_evidence,
        "description": (
            "Submit a raw observation as an EVIDENCE node. The client supplies "
            "no edge, assumption, verdict, rejection or invalidation target; "
            "successful new evidence is committed before this call returns."
        ),
    },
}


def names() -> list[str]:
    return sorted(WRITE_TOOLS)


__all__ = [
    "ControlledWriteError",
    "WRITE_TOOLS",
    "WriteResult",
    "commit_mutation",
    "mesh_submit_evidence",
    "names",
]
