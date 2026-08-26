"""The graph an MCP server serves, and how it survives a restart.

One session per process is right for a stdio server talking to one local
client. It is not right for a shared service, and this file is where that
assumption is written down rather than implied.

A session on disk is a *directory*, not a file::

    session/
      session.json    what this is, and how to read the other two
      graph.json      contextmesh.graph  v1  (contextmesh/graph.py)
      resolver.json   contextmesh.resolver v1 (contextmesh/resolve.py)

Three formats rather than one, because they are versioned by three different
concerns. Graph snapshot v1 is closed: query-resolution state is not graph
state, and folding the resolver into it would have meant reopening a settled
format every time the resolver learned a new field. The session file is the
join, and it is the only place that can check the one invariant neither of the
other two can see on its own — that the entities the resolver resolves *to*
are entities the graph actually has.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from contextmesh.assumptions import AssumptionLedger
from contextmesh.demo import run as demo_run
from contextmesh.graph import ContextGraph
from contextmesh.model import NodeType
from contextmesh.resolve import Resolver
from contextmesh.traverse import DEFAULT_POLICY, EdgeType, Walker

DEFAULT_ROUNDS = 8

#: The session directory's own format. Bump for any change that makes a
#: directory this build writes unreadable to an older one, or vice versa.
SESSION_SCHEMA = "contextmesh.session"
SESSION_VERSION = 1

SESSION_FILE = "session.json"
GRAPH_FILE = "graph.json"
RESOLVER_FILE = "resolver.json"


class SessionError(ValueError):
    """Raised when a directory is not a session this build can restore.

    Loading fails closed. A session that half-loads is worse than one that
    refuses: the reads still answer, they just answer differently, and nothing
    in the output says so.
    """


def _no_session_constants(value: str) -> float:
    raise SessionError(f"session.json contains the non-JSON constant {value!r}")


def _bare_name(value: Any, field_name: str) -> str:
    """A filename, not a path.

    ``session.json`` names its two companion files so the format can grow one
    without a version bump. Names are all it may do: a session directory is
    something you can be handed, and ``"graph": "../../etc/passwd"`` is not a
    graph.
    """
    if not isinstance(value, str):
        raise SessionError(
            f"session.{field_name} must be a string, got {type(value).__name__}"
        )
    if value != Path(value).name or value in ("", ".", ".."):
        raise SessionError(
            f"session.{field_name} must be a plain filename inside the session "
            f"directory, got {value!r}"
        )
    return value


def _session_int(value: Any, field_name: str, *, minimum: int) -> int:
    # bool is an int in Python, so ``"rounds": true`` would otherwise load as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise SessionError(
            f"session.{field_name} must be an integer, got {value!r}"
        )
    if value < minimum:
        raise SessionError(
            f"session.{field_name} must be >= {minimum}, got {value}"
        )
    return value


def _walker_policy(value: Any) -> Tuple[EdgeType, ...]:
    if not isinstance(value, list):
        raise SessionError(
            f"session.walker.policy must be a list, got {type(value).__name__}"
        )
    policy: List[EdgeType] = []
    for index, name in enumerate(value):
        if not isinstance(name, str):
            raise SessionError(
                f"session.walker.policy[{index}] must be a string, got {name!r}"
            )
        try:
            edge = EdgeType(name)
        except ValueError:
            raise SessionError(
                f"session.walker.policy[{index}] names the edge type {name!r}, "
                "which this build's ontology does not have"
            ) from None
        if edge in policy:
            raise SessionError(
                f"session.walker.policy lists {name!r} twice; the policy is an "
                "ordered preference, so a repeat is ambiguous"
            )
        policy.append(edge)
    return tuple(policy)


@dataclass
class WalkerConfig:
    """The walker settings that decide what a read returns.

    Persisted because they are not derivable and they are not cosmetic: restore
    a session saved with ``hop_budget=3`` into the default 6 and the same
    question comes back with a different answer, with nothing to say why. The
    walk *history* is not here — see ``Session.load``.
    """

    hop_budget: int = 6
    policy: Tuple[EdgeType, ...] = DEFAULT_POLICY
    flat_k: int = 40
    max_expand: int = 260

    @classmethod
    def of(cls, walker: Walker) -> "WalkerConfig":
        return cls(
            hop_budget=walker.hop_budget,
            policy=tuple(walker.policy),
            flat_k=walker.flat_k,
            max_expand=walker.max_expand,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hop_budget": self.hop_budget,
            "policy": [edge.value for edge in self.policy],
            "flat_k": self.flat_k,
            "max_expand": self.max_expand,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "WalkerConfig":
        if not isinstance(data, dict):
            raise SessionError(
                f"session.walker must be an object, got {type(data).__name__}"
            )
        for key in ("hop_budget", "policy", "flat_k", "max_expand"):
            if key not in data:
                raise SessionError(f"session.walker is missing {key!r}")
        return cls(
            hop_budget=_session_int(data["hop_budget"], "walker.hop_budget", minimum=1),
            policy=_walker_policy(data["policy"]),
            flat_k=_session_int(data["flat_k"], "walker.flat_k", minimum=1),
            max_expand=_session_int(data["max_expand"], "walker.max_expand", minimum=1),
        )

    def walker(self, graph: ContextGraph, resolver: Resolver) -> Walker:
        return Walker(
            graph,
            resolver,
            hop_budget=self.hop_budget,
            policy=self.policy,
            flat_k=self.flat_k,
            max_expand=self.max_expand,
        )


def check_agreement(graph: ContextGraph, resolver: Resolver) -> None:
    """The invariant that spans both files, and so lives in neither.

    ``Resolver.from_dict`` checks that its aliases, blocks and log point at
    entities *it* knows. It cannot check that those entities exist in the
    graph, because it has never seen the graph. Without this, a resolver saved
    against one graph and restored against another loads clean and then quietly
    stops resolving: ``Walker.seed`` treats a canonical id that is not a node
    as an unresolved mention, so the failure surfaces as "the mesh does not
    know about that", which is a different and wrong answer.
    """
    for entity_id, label in sorted(resolver.canonical.items()):
        node = graph.nodes.get(entity_id)
        if node is None:
            raise SessionError(
                f"the resolver resolves {label!r} to {entity_id!r}, which is not "
                "a node in this session's graph"
            )
        if node.type is not NodeType.ENTITY:
            raise SessionError(
                f"the resolver resolves {label!r} to {entity_id!r}, which is a "
                f"{node.type.value} rather than an entity"
            )


@dataclass
class Session:
    graph: ContextGraph
    resolver: Resolver
    walker: Walker
    ledger: AssumptionLedger
    rounds: int = DEFAULT_ROUNDS
    #: Where this session came from, for ``describe()``. Set by the builders.
    source: str = "bundled demo corpus"
    path: Optional[Path] = field(default=None, repr=False)

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

    # ── persistence ──────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SESSION_SCHEMA,
            "version": SESSION_VERSION,
            "graph": GRAPH_FILE,
            "resolver": RESOLVER_FILE,
            "rounds": self.rounds,
            "walker": WalkerConfig.of(self.walker).to_dict(),
        }

    def save(self, directory: Any) -> Path:
        """Write the session directory. Returns the directory.

        The graph goes down first and ``session.json`` last, so a directory
        that has a session file has the files that file names. An interrupted
        save leaves something that fails to load rather than something that
        loads short.
        """
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        self.graph.save_json(target / GRAPH_FILE)
        self.resolver.save_json(target / RESOLVER_FILE)
        (target / SESSION_FILE).write_text(
            json.dumps(
                self.to_dict(),
                sort_keys=True,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, directory: Any) -> "Session":
        """Restore a session from a directory, or refuse it.

        Two things are rebuilt rather than restored, and both are deliberate:

        - The **ledger** is fresh. ``lineage()`` and ``blast_radius()`` read the
          graph's own assumption records, so both come back identical; what is
          gone is ``history``, the per-process event log.
        - The **walk history** is empty. Per-node walk counts are in the graph
          snapshot, so ``mesh_ask`` and every count in ``mesh_health`` restore
          exactly — but health's ``dead_ends`` signal is computed from the walk
          list, so it is absent until this process has walked. That gap is
          pinned by a test rather than left to be discovered.
        """
        target = Path(directory)
        if not target.is_dir():
            raise SessionError(f"{target} is not a session directory")

        session_path = target / SESSION_FILE
        if not session_path.is_file():
            raise SessionError(
                f"{target} has no {SESSION_FILE}, so it is not a session "
                "directory this build can read"
            )
        try:
            data = json.loads(
                session_path.read_text(encoding="utf-8"),
                parse_constant=_no_session_constants,
            )
        except json.JSONDecodeError as exc:
            raise SessionError(f"{session_path} is not valid JSON: {exc}") from None

        if not isinstance(data, dict):
            raise SessionError(
                f"{SESSION_FILE} must contain an object, got {type(data).__name__}"
            )
        for key in ("schema", "version", "graph", "resolver", "rounds", "walker"):
            if key not in data:
                raise SessionError(f"{SESSION_FILE} is missing {key!r}")
        if data["schema"] != SESSION_SCHEMA:
            raise SessionError(
                f"not a {SESSION_SCHEMA} directory: schema is {data['schema']!r}"
            )
        version = data["version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise SessionError(f"session version must be an integer, got {version!r}")
        if version != SESSION_VERSION:
            raise SessionError(
                f"session version {version!r} cannot be read by this build, "
                f"which writes and reads version {SESSION_VERSION}"
            )

        rounds = _session_int(data["rounds"], "rounds", minimum=0)
        config = WalkerConfig.from_dict(data["walker"])

        graph_path = target / _bare_name(data["graph"], "graph")
        resolver_path = target / _bare_name(data["resolver"], "resolver")
        for path, field_name in ((graph_path, "graph"), (resolver_path, "resolver")):
            if not path.is_file():
                raise SessionError(
                    f"{SESSION_FILE} names {path.name!r} as the {field_name}, "
                    "but no such file is in the session directory"
                )

        # SnapshotError and ResolverSnapshotError are both ValueError, and both
        # already say precisely what is wrong. Re-raising as SessionError keeps
        # one exception type at the session boundary without losing the reason.
        try:
            graph = ContextGraph.load_json(graph_path)
        except ValueError as exc:
            raise SessionError(f"{graph_path.name}: {exc}") from exc
        try:
            resolver = Resolver.load_json(resolver_path)
        except ValueError as exc:
            raise SessionError(f"{resolver_path.name}: {exc}") from exc

        check_agreement(graph, resolver)

        return cls(
            graph=graph,
            resolver=resolver,
            walker=config.walker(graph, resolver),
            ledger=AssumptionLedger(graph),
            rounds=rounds,
            source=f"session directory {target}",
            path=target,
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
        persistent = self.path is not None
        return {
            "source": self.source,
            "persistent": persistent,
            "note": (
                "Restored from disk. Structure, belief and resolution survive a "
                "restart; walk history and the ledger's event log do not."
                if persistent
                else "Rebuilt per process. Save it with --session to keep it."
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


def session(
    rounds: int = DEFAULT_ROUNDS, *, directory: Optional[Any] = None
) -> Session:
    """The process-wide session, built or loaded on first use."""
    global _SESSION
    if _SESSION is None:
        _SESSION = (
            Session.build(rounds=rounds)
            if directory is None
            else Session.load(directory)
        )
    return _SESSION


def adopt(opened: Session) -> Session:
    """Install an already-opened session as the process-wide one.

    ``main`` opens the session before serving so that a directory this build
    cannot restore fails at the shell. Without this the tool handlers would
    call ``session()`` and get a *second*, freshly built demo graph — the
    server would report the loaded session on stderr and then answer questions
    from a different one.
    """
    global _SESSION
    _SESSION = opened
    return _SESSION


def reset() -> None:
    """Drop the cached session. Tests use this; a server does not."""
    global _SESSION
    _SESSION = None


# ── command line ─────────────────────────────────────────────────────────
def add_source_arguments(parser: Any) -> Any:
    """The source flags, shared by both entry points.

    Which graph you are serving is not a detail to be defaulted. ``--demo``
    used to be what you got by saying nothing; now it has to be said, because
    the alternative is no longer "nothing" but a real session on disk, and
    silently serving a throwaway graph when someone meant to serve theirs is
    the one failure this whole format exists to prevent.
    """
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--demo",
        action="store_true",
        help="build the bundled demo corpus fresh (not persistent)",
    )
    source.add_argument(
        "--session",
        metavar="DIR",
        help=f"load a session directory written by --save ({SESSION_SCHEMA} v{SESSION_VERSION})",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=None,
        help=f"walk rounds to build the demo with (default: {DEFAULT_ROUNDS}); --demo only",
    )
    parser.add_argument(
        "--save",
        metavar="DIR",
        help="write the session to DIR and exit without serving",
    )
    return parser


def open_session(args: Any) -> Session:
    """Turn parsed source flags into a session, or explain why not.

    ``--rounds`` with ``--session`` is an error rather than an ignored flag. A
    restored session carries the round count it was built with; accepting a
    different number here would print one figure in ``describe()`` and mean
    another.
    """
    if args.session is not None:
        if args.rounds is not None:
            raise SessionError(
                "--rounds builds a demo graph and has no meaning for --session; "
                "a restored session carries the rounds it was built with"
            )
        return Session.load(args.session)
    return Session.build(rounds=DEFAULT_ROUNDS if args.rounds is None else args.rounds)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Save or inspect a session, without loading the MCP SDK.

        python -m contextmesh_mcp --demo --rounds 8 --save ./session
        python -m contextmesh_mcp --session ./session

    Separate from ``contextmesh-mcp`` on purpose: writing and checking a
    session directory is not serving one, it needs no transport, and it works
    on every Python version the core supports rather than only the ones the
    SDK does.
    """
    import argparse

    parser = add_source_arguments(
        argparse.ArgumentParser(
            prog="python -m contextmesh_mcp",
            description="Write or inspect a Context Mesh session directory.",
        )
    )
    args = parser.parse_args(argv)
    try:
        opened = open_session(args)
        if args.save is not None:
            target = opened.save(args.save)
            print(
                json.dumps(
                    {
                        "saved": str(target),
                        "files": sorted(p.name for p in target.iterdir()),
                        **opened.to_dict(),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        print(json.dumps(opened.describe(), indent=2, sort_keys=True, default=str))
        return 0
    except SessionError as exc:
        # Exit 2, not a traceback: a refused session is an answer, and a shell
        # can act on it.
        print(f"contextmesh session: {exc}", file=__import__("sys").stderr)
        return 2
