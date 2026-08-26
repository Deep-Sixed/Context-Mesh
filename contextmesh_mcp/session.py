"""The graph an MCP server serves, and how it survives a restart.

One session per process is right for a stdio server talking to one local
client. It is not right for a shared service, and this file is where that
assumption is written down rather than implied.

A session on disk is a *directory*, not a file::

    session/
      session.json           the manifest, and the commit point
      graph-000003.json      contextmesh.graph    v1 (contextmesh/graph.py)
      resolver-000003.json   contextmesh.resolver v1 (contextmesh/resolve.py)

Three formats rather than one, because they are versioned by three different
concerns. Graph snapshot v1 is closed: query-resolution state is not graph
state, and folding the resolver into it would have meant reopening a settled
format every time the resolver learned a new field. The session file is the
join, and it is the only place that can check the invariants neither of the
other two can see on its own — that the entities the resolver resolves *to*
are entities the graph actually has, under the labels the graph gives them.

**Generations, because a session gets overwritten.** Writing three files in
sequence is safe the first time and unsafe every time after: crash between the
graph and the resolver and the directory holds a new graph beside an old
resolver, a pairing that never existed and may still pass every check made on
it. So a save never overwrites a live file. It writes a whole new generation
under new names, and only then replaces ``session.json`` — one ``os.replace``,
which the filesystem makes atomic. The manifest is the commit:

    crash before the swap  →  the previous generation is still named, intact
    crash during the swap  →  one manifest or the other, never half of one
    crash after the swap   →  the new generation is named, and complete

Superseded generations are deleted after the swap, so an interrupted save costs
disk rather than correctness.
"""

from __future__ import annotations

import json
import os
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
GRAPH_STEM = "graph"
RESOLVER_STEM = "resolver"

#: How often a served session is written back. ``every-ask`` is the default
#: because a question is a write here and a lost write is silent; the cost is
#: one full serialisation per question, which is real and bounded.
CHECKPOINT_POLICIES = ("every-ask", "on-exit", "never")
DEFAULT_CHECKPOINT = "every-ask"


def generation_name(stem: str, generation: int) -> str:
    """The one filename this build writes for a stem and a generation.

    Zero-padded so a directory sorts readably, and checked on load: a manifest
    whose names disagree with its generation is refused, because the next save
    would compute a name that is already live and overwrite it in place — which
    is the exact failure generations exist to prevent.
    """
    return f"{stem}-{generation:06d}.json"


class SessionError(ValueError):
    """Raised when a directory is not a session this build can restore.

    Loading fails closed. A session that half-loads is worse than one that
    refuses: the reads still answer, they just answer differently, and nothing
    in the output says so.
    """


def _no_session_constants(value: str) -> float:
    raise SessionError(f"session.json contains the non-JSON constant {value!r}")


def _fsync_file(path: Path) -> None:
    """Get a written file onto the disk before anything points at it.

    Without this the manifest swap can reach the platter first, leaving a
    manifest that names a file the filesystem has not finished writing — which
    is precisely the mixed state generations are here to rule out.
    """
    handle = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(handle)
    except OSError:  # pragma: no cover - some filesystems refuse; not fatal
        pass
    finally:
        os.close(handle)


def _fsync_dir(path: Path) -> None:
    """Make the manifest rename itself durable, where the platform allows it."""
    try:
        handle = os.open(str(path), os.O_RDONLY)
    except OSError:  # pragma: no cover - Windows cannot open a directory
        return
    try:
        os.fsync(handle)
    except OSError:  # pragma: no cover - nor fsync one
        pass
    finally:
        os.close(handle)


def _bare_name(value: Any, field_name: str) -> str:
    """A filename, not a path.

    ``session.json`` names its two companion files so a reader can see the
    generation without parsing anything. Names are all it may do: a session
    directory is something you can be handed, and ``"graph":
    "../../etc/passwd"`` is not a graph.
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


def _contained(directory: Path, name: str, field_name: str) -> Path:
    """Resolve a companion filename, refusing one that leaves the directory.

    ``_bare_name`` stops the path from containing a traversal. It cannot stop
    the *file* from being a symlink to somewhere else, and a session directory
    is something you can be handed. Resolving both sides and requiring
    containment closes that: the loader reads what is in the directory, or it
    reads nothing.
    """
    root = Path(os.path.realpath(str(directory)))
    resolved = Path(os.path.realpath(str(directory / name)))
    if resolved != root / name:
        raise SessionError(
            f"session.{field_name} names {name!r}, which resolves to "
            f"{resolved} — outside the session directory {root}"
        )
    return directory / name


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
    """The invariants that span both files, and so live in neither.

    ``Resolver.from_dict`` checks that its aliases, blocks and log point at
    entities *it* knows. It cannot check them against the graph, because it has
    never seen the graph. Three things have to hold, and each fails differently:

    - The entity **exists**. Otherwise ``Walker.seed`` treats a canonical id
      that is not a node as an unresolved mention, so the failure surfaces as
      "the mesh does not know about that" — a different and wrong answer.
    - The entity **is an entity**. Resolving a mention to a claim would seed a
      walk from the middle of the evidence rather than at a thing.
    - The **labels match**. This is the quiet one: same id, right type, wrong
      name. ``Resolver.canonical`` is not a display string — it is scored
      against every mention that reaches ``near_miss``, so a resolver holding
      ``entity:pgvector -> "HNSW"`` over a graph holding ``"pgvector"`` keeps
      resolving, and resolves differently, with both files individually valid.
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
        if node.label != label:
            raise SessionError(
                f"the resolver calls {entity_id!r} {label!r} but the graph calls "
                f"it {node.label!r}; the resolver's label is matched against "
                "mentions, so the two disagreeing changes what resolves"
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
    #: The generation this session was loaded at, or last committed. 0 means
    #: it has never been on disk.
    generation: int = 0

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
    def manifest(self, generation: int) -> Dict[str, Any]:
        return {
            "schema": SESSION_SCHEMA,
            "version": SESSION_VERSION,
            "generation": generation,
            "graph": generation_name(GRAPH_STEM, generation),
            "resolver": generation_name(RESOLVER_STEM, generation),
            "rounds": self.rounds,
            "walker": WalkerConfig.of(self.walker).to_dict(),
        }

    @staticmethod
    def _live_generation(directory: Path) -> int:
        """The generation a directory is currently committed to, or 0.

        Read from the manifest rather than from memory, so saving the same
        session into two directories keeps each one's counter its own, and so a
        directory written by another process is never rolled backwards. A
        manifest too broken to read counts as 0 — the save that follows commits
        a generation 1 that supersedes it wholesale.
        """
        path = directory / SESSION_FILE
        if not path.is_file():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            generation = data["generation"]
        except (OSError, ValueError, KeyError, TypeError):
            return 0
        if isinstance(generation, bool) or not isinstance(generation, int):
            return 0
        return max(generation, 0)

    def save(self, directory: Any) -> Path:
        """Commit a new generation of this session. Returns the directory.

        Nothing already referenced is written to. The graph and the resolver go
        down under names no live manifest names, each one fsynced; only then is
        ``session.json`` replaced, in a single ``os.replace``. Until that call
        the directory still reads as the previous generation, and after it the
        new one is whole — so there is no instant at which a reader can see a
        graph from one save beside a resolver from another.

        Superseded files are removed afterwards. That cleanup is the only step
        allowed to fail without losing the save.
        """
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        generation = self._live_generation(target) + 1
        payload = self.manifest(generation)

        graph_path = target / payload["graph"]
        resolver_path = target / payload["resolver"]
        self.graph.save_json(graph_path)
        _fsync_file(graph_path)
        self.resolver.save_json(resolver_path)
        _fsync_file(resolver_path)

        # The commit. A partially written manifest would be worse than any
        # torn companion, so it lands under a temporary name and is renamed.
        staged = target / f"{SESSION_FILE}.tmp-{generation:06d}"
        with open(staged, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    payload,
                    sort_keys=True,
                    indent=2,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, target / SESSION_FILE)
        _fsync_dir(target)

        self._sweep(target, keep={SESSION_FILE, payload["graph"], payload["resolver"]})
        self.generation = generation
        return target

    @staticmethod
    def _sweep(directory: Path, keep: Any) -> None:
        """Delete superseded generations and abandoned staging files.

        Runs after the commit, so anything it removes is already unreferenced.
        Deliberately narrow: only files this build's own naming produces are
        candidates, so a directory holding something else keeps it.
        """
        for entry in directory.iterdir():
            name = entry.name
            if name in keep or not entry.is_file():
                continue
            superseded = name.endswith(".json") and (
                name.startswith(f"{GRAPH_STEM}-") or name.startswith(f"{RESOLVER_STEM}-")
            )
            abandoned = name.startswith(f"{SESSION_FILE}.tmp-")
            if superseded or abandoned:
                try:
                    entry.unlink()
                except OSError:  # pragma: no cover - cleanup is best effort
                    pass

    def checkpoint(self) -> Path:
        """Write what has happened since this session was loaded back to disk.

        Asking a question is a write in Context Mesh — a walk moves telemetry
        and the resolver learns aliases it did not have — so a served session
        that is never checkpointed is a durable *starting* snapshot rather than
        a durable session, and the difference only shows up as a quiet loss on
        restart.

        Only a loaded session has a home. A built one has nowhere to go, and
        saying so is better than inventing a directory.
        """
        if self.path is None:
            raise SessionError(
                "this session was built rather than loaded, so it has no "
                "directory to check point into — save it somewhere first, then "
                "load it from there"
            )
        return self.save(self.path)

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
        for key in (
            "schema",
            "version",
            "generation",
            "graph",
            "resolver",
            "rounds",
            "walker",
        ):
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
        generation = _session_int(data["generation"], "generation", minimum=1)
        config = WalkerConfig.from_dict(data["walker"])

        paths = {}
        for field_name, stem in (("graph", GRAPH_STEM), ("resolver", RESOLVER_STEM)):
            name = _bare_name(data[field_name], field_name)
            expected = generation_name(stem, generation)
            # Not pedantry about naming. The next save computes its filenames
            # from generation + 1, so a manifest whose names run ahead of its
            # counter would have that save overwrite a file this one still
            # points at — losing the very atomicity generations provide.
            if name != expected:
                raise SessionError(
                    f"{SESSION_FILE} is at generation {generation} but names "
                    f"{name!r} as the {field_name}; this build writes "
                    f"{expected!r} for that generation"
                )
            path = _contained(target, name, field_name)
            if not path.is_file():
                raise SessionError(
                    f"{SESSION_FILE} names {name!r} as the {field_name}, "
                    "but no such file is in the session directory"
                )
            paths[field_name] = path
        graph_path, resolver_path = paths["graph"], paths["resolver"]

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
            generation=generation,
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
            "generation": self.generation,
            "rounds": self.rounds,
            "nodes_live": sum(live_counts.values()),
            "nodes_total": len(self.graph.nodes),
            "node_types_live": live_counts,
            "node_types_total": self.graph.type_counts(live_only=False),
            "edges_live": sum(1 for e in self.graph.edges.values() if e.live),
            "edges_total": len(self.graph.edges),
            "assumptions": len(self.graph.assumptions),
        }


class Checkpointer:
    """Decides when a served session is written back to its directory.

    Lives here rather than in ``server.py`` so the policy is testable on every
    supported version with no SDK installed — the same reason ``tools`` does.

    Three policies, and the default is the expensive one on purpose:

    - ``every-ask`` — commit after each question. One full serialisation per
      question, which is real; a lost question is silent, which is worse.
    - ``on-exit`` — commit once when the server shuts down cleanly. Cheaper,
      and worth nothing if the process is killed rather than asked to stop.
    - ``never`` — serve a session without writing to it. The honest choice for
      a directory you were handed and do not own.

    A session with no directory (``--demo``) cannot be checkpointed, so the
    policy is inert rather than an error: refusing to serve a demo graph
    because it has nowhere to save to would help nobody.
    """

    def __init__(self, session: Session, policy: str = DEFAULT_CHECKPOINT) -> None:
        if policy not in CHECKPOINT_POLICIES:
            raise SessionError(
                f"unknown checkpoint policy {policy!r}; expected one of "
                + ", ".join(repr(p) for p in CHECKPOINT_POLICIES)
            )
        self.session = session
        self.policy = policy
        #: Mutations recorded since the last commit. Read by ``close``, so an
        #: ``on-exit`` server that was never asked anything writes nothing.
        self.pending = 0
        self.commits = 0

    @property
    def durable(self) -> bool:
        return self.policy != "never" and self.session.path is not None

    def record_mutation(self) -> Optional[Path]:
        """Note that a read changed something, and commit if the policy says so."""
        self.pending += 1
        if self.durable and self.policy == "every-ask":
            return self.commit()
        return None

    def commit(self) -> Optional[Path]:
        if not self.durable or not self.pending:
            return None
        written = self.session.checkpoint()
        self.pending = 0
        self.commits += 1
        return written

    def close(self) -> Optional[Path]:
        """Last chance to commit. Safe to call more than once."""
        if self.policy == "never":
            return None
        return self.commit()


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
    parser.add_argument(
        "--checkpoint",
        choices=CHECKPOINT_POLICIES,
        default=DEFAULT_CHECKPOINT,
        help=(
            "when to write a served session back to its directory "
            f"(default: {DEFAULT_CHECKPOINT}); --session only"
        ),
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
    if getattr(args, "checkpoint", DEFAULT_CHECKPOINT) != DEFAULT_CHECKPOINT:
        # Accepting it silently would suggest a demo graph could be kept.
        raise SessionError(
            "--checkpoint decides when a served session is written back and has "
            "no meaning for --demo, which has no directory to write to"
        )
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
                        **opened.manifest(opened.generation),
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
