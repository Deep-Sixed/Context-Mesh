"""The graph an MCP server serves, built once per process.

One session per process is right for a stdio server talking to one local
client. It is not right for a shared service, and this file is where that
assumption is written down rather than implied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from contextmesh.assumptions import AssumptionLedger
from contextmesh.demo import run as demo_run
from contextmesh.graph import ContextGraph
from contextmesh.resolve import Resolver
from contextmesh.traverse import Walker

DEFAULT_ROUNDS = 8


@dataclass
class Session:
    graph: ContextGraph
    resolver: Resolver
    walker: Walker
    ledger: AssumptionLedger
    rounds: int = DEFAULT_ROUNDS

    @classmethod
    def build(cls, rounds: int = DEFAULT_ROUNDS) -> "Session":
        """Build the bundled demo graph.

        The ledger comes from the demo run rather than being constructed fresh.
        Not for lineage: ``lineage()`` walks ``supersedes`` on the graph's own
        assumption records, so a new ``AssumptionLedger`` over the same graph
        reconstructs it identically. What a fresh one loses is ``history`` — the
        recorded sequence of assume / supersede / reject events, which is not
        derivable from the graph and is worth keeping alongside it.
        """
        result = demo_run(rounds=rounds)
        return cls(
            graph=result.graph,
            resolver=result.resolver,
            walker=result.walker,
            ledger=result.ledger,
            rounds=rounds,
        )

    def assumption_ids(self) -> list:
        return sorted(self.graph.assumptions)

    def describe(self) -> dict:
        """What this server is serving.

        Live and total are reported separately rather than as one ``nodes``
        number. ``type_counts()`` is live-only and ``len(graph.edges)`` is not,
        so a single pair of counts silently compared two different things — and
        in a graph whose whole point is that invalidated work is kept rather
        than deleted, the gap between them is information, not noise.
        """
        live_counts = self.graph.type_counts()
        return {
            "source": "bundled demo corpus",
            "persistent": False,
            "note": (
                "Rebuilt per process. ContextGraph has no from_dict yet, so "
                "nothing served here survives a restart."
            ),
            "build": self.graph.build,
            "rounds": self.rounds,
            "nodes_live": sum(live_counts.values()),
            "nodes_total": len(self.graph.nodes),
            "node_types_live": live_counts,
            "node_types_total": self.graph.type_counts(live_only=False),
            "edges_live": sum(1 for e in self.graph.edges.values() if e.live),
            "edges_total": len(self.graph.edges),
            "assumptions": len(self.graph.assumptions),
        }


_SESSION: Optional[Session] = None


def session(rounds: int = DEFAULT_ROUNDS) -> Session:
    """The process-wide session, built on first use."""
    global _SESSION
    if _SESSION is None:
        _SESSION = Session.build(rounds=rounds)
    return _SESSION


def reset() -> None:
    """Drop the cached session. Tests use this; a server does not."""
    global _SESSION
    _SESSION = None
