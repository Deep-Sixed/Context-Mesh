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
import socket
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from contextmesh.assumptions import AssumptionLedger
from contextmesh.demo import run as demo_run
from contextmesh.execute import (
    ExecutionError,
    RunLedger,
    Runner,
    TaskRegistry,
)
from contextmesh.graph import ContextGraph
from contextmesh.model import NodeType
from contextmesh.resolve import Resolver
from contextmesh.traverse import DEFAULT_POLICY, EdgeType, Walker

DEFAULT_ROUNDS = 8

#: The session directory's own format. Bump for any change that makes a
#: directory this build writes unreadable to an older one, or vice versa.
SESSION_SCHEMA = "contextmesh.session"
SESSION_VERSION = 2

#: Versions this build can *read*. It writes ``SESSION_VERSION`` and only that.
#: v1 held a graph and a resolver; v2 adds the execution plan and its run
#: ledger. A v1 directory is a real thing this project shipped, so it is read
#: rather than orphaned — as a session with no execution — and the next save
#: upgrades it. A version from the *future* is still refused: this build cannot
#: know what it would be dropping.
READABLE_VERSIONS = (1, 2)

SESSION_FILE = "session.json"
LOCK_FILE = "session.lock"
#: Prefix for the short-lived file every write lands in first. Recognisable so
#: an abandoned one can be swept, random so it cannot be pre-empted by a name
#: planted in the directory.
STAGING_PREFIX = ".staging-"
STAGING_SUFFIX = ".tmp"
GRAPH_STEM = "graph"
RESOLVER_STEM = "resolver"
EXECUTION_STEM = "execution"
RUNLEDGER_STEM = "runledger"

#: The companions a manifest can name, and the stem each generation uses. Order
#: matters only for reading errors; the manifest is the commit point either way.
COMPANIONS = (
    ("graph", GRAPH_STEM),
    ("resolver", RESOLVER_STEM),
    ("execution", EXECUTION_STEM),
    ("ledger", RUNLEDGER_STEM),
)

#: The two a session cannot be without.
REQUIRED_COMPANIONS = ("graph", "resolver")

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


class SessionLockedError(Exception):
    """Raised when another process is already writing this session directory.

    Not a ``SessionError``: the directory is fine, and so is the session. The
    only problem is timing, and a caller that wants to wait and retry needs to
    tell that apart from "this directory cannot be restored". ``Checkpointer``
    treats it as a skipped commit; the mutation stays pending for the next one.
    """


#: How many times ``load`` re-reads a directory that moved under it. Small on
#: purpose: each retry only happens when a writer demonstrably committed during
#: the read, and a directory being rewritten faster than this is a problem the
#: reader cannot fix by waiting.
LOAD_ATTEMPTS = 4
LOAD_BACKOFF = 0.05


class _Swept(Exception):
    """Internal: a file the manifest named was gone by the time we read it.

    Not an error on its own. Committed companions are immutable — once a
    manifest names them they are never rewritten, only deleted by a later
    sweep — so the *only* way a read of them fails is that a writer moved the
    directory on mid-read. ``load`` re-reads; the caller never sees this.
    """

    def __init__(self, name: str, field_name: str) -> None:
        super().__init__(name)
        self.name = name
        self.field_name = field_name


class SessionError(ValueError):
    """Raised when a directory is not a session this build can restore.

    Loading fails closed. A session that half-loads is worse than one that
    refuses: the reads still answer, they just answer differently, and nothing
    in the output says so.
    """


def _no_session_constants(value: str) -> float:
    raise SessionError(f"session.json contains the non-JSON constant {value!r}")


def write_in_place(directory: Path, name: str, payload: str) -> Path:
    """Put ``payload`` at ``directory/name``, through a descriptor we own.

    Writing by name follows whatever is already there. A session directory is
    untrusted input on the way *out* as well as in: a directory handed over
    with ``graph-000006.json`` already present as a symlink would have the next
    save write through it, and with ``--checkpoint every-ask`` merely *asking a
    question* is enough to trigger that. Reproduced before it was fixed; four
    names were reachable that way, including the lock file.

    So every write lands in a fresh file created with ``O_EXCL`` under a random
    name — which cannot already exist, and so cannot already be a link — and is
    moved into place with ``os.replace``. Rename replaces a symlink *itself*
    rather than following it, so a planted link is destroyed rather than
    honoured, and the file it pointed at is never touched.

    The rename is also what makes the write atomic, which the manifest needs
    anyway. One mechanism, both properties.
    """
    handle, staged = tempfile.mkstemp(
        prefix=STAGING_PREFIX, suffix=STAGING_SUFFIX, dir=str(directory)
    )
    staged_path = Path(staged)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        target = directory / name
        os.replace(staged_path, target)
        return target
    except BaseException:
        try:
            staged_path.unlink()
        except OSError:  # pragma: no cover - already gone
            pass
        raise


def _open_lock_file(path: Path) -> Any:
    """Open the lock file without following a symlink, and check what it is.

    The lock is the one artifact that cannot use ``write_in_place``: it has to
    keep a single inode for its whole life, because that inode is what the
    kernel lock is attached to. Replacing it on every save would hand two
    processes locks on two different inodes and let both believe they had won.

    So it is opened directly, and defended directly. ``O_NOFOLLOW`` refuses a
    final component that is a symlink, and the ``fstat`` afterwards refuses a
    fifo or a device — the two other things a hostile directory can leave under
    a name you are about to write to.
    """
    flags = os.O_RDWR | os.O_CREAT
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:  # pragma: no cover - Windows
        # No O_NOFOLLOW: refuse anything that is already a link, then open.
        # A narrower guarantee than the POSIX path, and said so out loud rather
        # than left to look equivalent.
        if path.is_symlink():
            raise SessionError(
                f"{path} is a symbolic link; the session lock must be a regular "
                "file inside the session directory"
            )
    try:
        descriptor = os.open(str(path), flags | nofollow, 0o644)
    except OSError as exc:
        raise SessionError(
            f"{path} could not be opened as the session lock ({exc.strerror}); "
            "it must be a regular file inside the session directory"
        ) from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SessionError(
                f"{path} is not a regular file; the session lock cannot be a "
                "link, a device or a fifo"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return os.fdopen(descriptor, "r+", encoding="utf-8")


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


def _lock_exclusive(handle: Any) -> None:
    """Take an exclusive lock on an open file, without waiting.

    Kernel-held rather than advisory-by-convention, which matters more than the
    portability cost: a lock the kernel owns is released when the holder dies,
    however it dies. A lock file with a pid in it has to guess whether a stale
    holder is dead or merely slow, and it guesses wrong exactly when a machine
    is under the load that made the save slow in the first place.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: Any) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _holder(path: Path) -> str:
    """Best-effort description of whoever holds the lock, for the error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return f"pid {data['pid']} on {data['host']}"
    except (OSError, ValueError, KeyError, TypeError):
        return "another process"


@contextmanager
def writer_lock(directory: Any) -> Iterator[Path]:
    """Serialise writers over one session directory.

    Generations make a save atomic *against a crash*. They do nothing against a
    second writer, and the failure there is worse than a torn file because it
    ends with a valid-looking manifest. Two processes reading generation 5 both
    choose 6 and overwrite each other's companions; worse, one can commit 6 and
    sweep while the other has already written 7 but not yet swapped — leaving a
    manifest naming files that the first process just deleted.

    So the lock has to span the whole transaction, from reading the current
    generation to sweeping the superseded one. Locking only the swap would
    still allow both of those.

    The lock file is created once and never deleted. Deleting it on release
    would let a third process create a *new* file at the same name while a
    fourth still holds the old inode, and both would believe they had the lock.
    """
    target = Path(directory)
    path = target / LOCK_FILE
    handle = _open_lock_file(path)
    try:
        try:
            _lock_exclusive(handle)
        except OSError:
            raise SessionLockedError(
                f"{target} is already being written by {_holder(path)}; "
                "one writer at a time"
            ) from None
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps({"pid": os.getpid(), "host": socket.gethostname()}) + "\n"
            )
            handle.flush()
            yield path
        finally:
            _unlock(handle)
    finally:
        handle.close()


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


def _reconcile_execution(runner: Runner, execution: str, ledger: str) -> None:
    """The plan and its ledger have to be about each other.

    Each is checked on its own before this — the ledger's chain verifies, the
    plan's references close against the graph — and both can pass while
    describing different runs. These are the facts that only make sense across
    the pair::

        every ledger entry names a task the plan holds
        no entry is recorded in a round the plan never reached

    Kept to what the engine actually guarantees. The ledger does not hold an
    entry for every task (a plan can be saved before anything runs) and a task
    can appear many times (attempts, audits, repairs), so neither count is an
    invariant and neither is asserted.
    """
    names = {task.name for task in runner.tasks}
    for entry in runner.ledger.entries:
        if entry.task not in names:
            raise SessionError(
                f"{ledger} records {entry.task!r} at entry {entry.seq}, which is "
                f"not a task in {execution}. The plan and its history are from "
                "different runs"
            )
        if entry.round > runner.round:
            raise SessionError(
                f"{ledger} records entry {entry.seq} in round {entry.round}, but "
                f"{execution} has only reached round {runner.round}. The history "
                "runs ahead of the plan it belongs to"
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
    #: The execution plan this session carries, if any. Optional because a
    #: session is useful without one — the MCP server reads a graph and answers
    #: questions, and never executes anything. A session that has one carries
    #: its run ledger too: the plan is what happened, the ledger is the record
    #: of it happening, and splitting them across two saves would let a restore
    #: hold a plan whose history it cannot show.
    runner: Optional[Runner] = None

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
        """The commit point. Names every companion this generation holds.

        ``execution`` and ``ledger`` are ``null`` when the session carries no
        plan — present-and-null rather than absent, so a reader can tell "this
        session has no execution" from "this manifest is missing a field".
        """
        keyed = {
            "schema": SESSION_SCHEMA,
            "version": SESSION_VERSION,
            "generation": generation,
            "rounds": self.rounds,
            "walker": WalkerConfig.of(self.walker).to_dict(),
        }
        for field_name, stem in COMPANIONS:
            if field_name in REQUIRED_COMPANIONS or self.runner is not None:
                keyed[field_name] = generation_name(stem, generation)
            else:
                keyed[field_name] = None
        return keyed

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

        The whole transaction runs under the directory's writer lock, from
        reading the current generation to sweeping the superseded one. Readers
        never take it: the manifest swap already gives them a consistent view,
        and a read that had to wait for a checkpoint would be a worse trade.
        """
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        with writer_lock(target):
            return self._commit(target)

    def _commit(self, target: Path) -> Path:
        """The transaction itself. Only called holding the writer lock."""
        live = self._live_generation(target)
        # Serialising writers stops the directory being corrupted; it does not
        # stop the second writer from silently discarding the first one's work.
        # A session that has a home is checked against it: if the directory
        # moved on since this session last committed there, someone else has
        # written, and overwriting them wholesale is not a thing to do quietly.
        if self.path is not None and target == self.path and live != self.generation:
            raise SessionError(
                f"{target} is at generation {live} but this session last "
                f"committed generation {self.generation}; another writer has "
                "committed since, and saving now would discard their work"
            )
        generation = live + 1
        payload = self.manifest(generation)

        # Every one of these lands in a fresh O_EXCL file and is renamed into
        # place, so none of them can be written *through* something already
        # sitting under the name. See ``write_in_place``.
        write_in_place(target, payload["graph"], self.graph.to_json())
        write_in_place(target, payload["resolver"], self.resolver.to_json())
        if self.runner is not None:
            # Both or neither, in the same transaction as the graph they refer
            # into. The manifest is still the only thing that makes any of them
            # live, so a crash between these two leaves the old generation
            # serving exactly as it did before.
            write_in_place(target, payload["execution"], self.runner.to_json())
            write_in_place(target, payload["ledger"], self.runner.ledger.to_json())
        write_in_place(
            target,
            SESSION_FILE,
            json.dumps(
                payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
            )
            + "\n",
        )
        _fsync_dir(target)

        self._sweep(
            target,
            keep={SESSION_FILE, LOCK_FILE}
            | {payload[name] for name, _ in COMPANIONS if payload[name] is not None},
        )
        self.generation = generation
        return target

    @staticmethod
    def _sweep(directory: Path, keep: Any) -> None:
        """Delete superseded generations and abandoned staging files.

        Runs after the commit, still under the writer lock, so anything it
        removes is unreferenced and no other writer is mid-transaction.
        Deliberately narrow: only files this build's own naming produces are
        candidates, so a directory holding something else keeps it — the lock
        file included, since a queued writer is holding that very inode.
        """
        for entry in directory.iterdir():
            name = entry.name
            if name in keep or not entry.is_file():
                continue
            superseded = name.endswith(".json") and any(
                name.startswith(f"{stem}-") for _, stem in COMPANIONS
            )
            abandoned = name.startswith(STAGING_PREFIX) and name.endswith(STAGING_SUFFIX)
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
    def load(
        cls, directory: Any, *, registry: Optional[TaskRegistry] = None
    ) -> "Session":
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

        **Readers do not take the writer lock**, and they do not need to. The
        manifest swap is atomic and committed companions are immutable, so a
        pair that reads successfully is always a coherent generation — possibly
        one older than the directory's newest, which is staleness rather than
        corruption. What the swap alone does *not* cover is the sweep: a reader
        holding a manifest for generation 5 can find ``graph-000005.json``
        already deleted, and fail on a session that is perfectly healthy.

        So a read that loses that race is re-read rather than reported. The
        retry fires only when the directory's generation actually moved during
        the attempt, which keeps a genuinely missing file failing immediately,
        with its own message, instead of after a wait.
        """
        target = Path(directory)
        if not target.is_dir():
            raise SessionError(f"{target} is not a session directory")

        for attempt in range(LOAD_ATTEMPTS):
            before = cls._live_generation(target)
            try:
                return cls._load_once(target, registry)
            except _Swept as swept:
                if cls._live_generation(target) == before:
                    # Nothing committed while we read. The file is simply not
                    # there, and waiting will not conjure it.
                    raise SessionError(
                        f"{SESSION_FILE} names {swept.name!r} as the "
                        f"{swept.field_name}, but no such file is in the "
                        "session directory"
                    ) from None
                if attempt + 1 == LOAD_ATTEMPTS:
                    raise SessionError(
                        f"{target} was committed to {LOAD_ATTEMPTS} times while "
                        "this read was in progress; giving up rather than "
                        "spinning"
                    ) from None
                time.sleep(LOAD_BACKOFF * (attempt + 1))
        raise AssertionError("unreachable")  # pragma: no cover

    @classmethod
    def _load_once(
        cls, target: Path, registry: Optional[TaskRegistry] = None
    ) -> "Session":
        """One attempt. Raises ``_Swept`` if a writer removed what we named."""
        session_path = target / SESSION_FILE
        if not session_path.is_file():
            raise SessionError(
                f"{target} has no {SESSION_FILE}, so it is not a session "
                "directory this build can read"
            )
        try:
            raw = session_path.read_text(encoding="utf-8")
        except FileNotFoundError:  # pragma: no cover - vanishingly narrow
            raise _Swept(SESSION_FILE, "manifest") from None
        try:
            data = json.loads(raw, parse_constant=_no_session_constants)
        except json.JSONDecodeError as exc:
            raise SessionError(f"{session_path} is not valid JSON: {exc}") from None

        if not isinstance(data, dict):
            raise SessionError(
                f"{SESSION_FILE} must contain an object, got {type(data).__name__}"
            )
        for key in ("schema", "version", "generation", "rounds", "walker"):
            if key not in data:
                raise SessionError(f"{SESSION_FILE} is missing {key!r}")
        if data["schema"] != SESSION_SCHEMA:
            raise SessionError(
                f"not a {SESSION_SCHEMA} directory: schema is {data['schema']!r}"
            )
        version = data["version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise SessionError(f"session version must be an integer, got {version!r}")
        if version not in READABLE_VERSIONS:
            raise SessionError(
                f"session version {version!r} cannot be read by this build, "
                f"which writes {SESSION_VERSION} and reads "
                f"{', '.join(str(v) for v in READABLE_VERSIONS)}"
            )

        rounds = _session_int(data["rounds"], "rounds", minimum=0)
        generation = _session_int(data["generation"], "generation", minimum=1)
        config = WalkerConfig.from_dict(data["walker"])

        # v1 named a graph and a resolver; v2 names four, two of which may be
        # null. Both required companions are required in either version.
        expected_fields = [name for name, _ in COMPANIONS]
        if version == 1:
            expected_fields = list(REQUIRED_COMPANIONS)
        for field_name in expected_fields:
            if field_name not in data:
                raise SessionError(f"{SESSION_FILE} is missing {field_name!r}")

        paths: Dict[str, Optional[Path]] = {name: None for name, _ in COMPANIONS}
        for field_name, stem in COMPANIONS:
            if field_name not in expected_fields:
                continue
            if data[field_name] is None:
                if field_name in REQUIRED_COMPANIONS:
                    raise SessionError(
                        f"{SESSION_FILE} names no {field_name}; a session "
                        "without one is not a session"
                    )
                continue
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
                raise _Swept(name, field_name)
            paths[field_name] = path

        # A plan and its history travel together. One without the other would
        # restore an execution whose events cannot be shown, or a history with
        # nothing it describes.
        if (paths["execution"] is None) != (paths["ledger"] is None):
            present = "execution" if paths["execution"] is not None else "ledger"
            missing = "ledger" if present == "execution" else "execution"
            raise SessionError(
                f"{SESSION_FILE} names the {present} but no {missing}; a plan "
                "and the record of it running are committed together or not "
                "at all"
            )
        graph_path, resolver_path = paths["graph"], paths["resolver"]
        assert graph_path is not None and resolver_path is not None

        # SnapshotError and ResolverSnapshotError are both ValueError, and both
        # already say precisely what is wrong. Re-raising as SessionError keeps
        # one exception type at the session boundary without losing the reason.
        try:
            graph = ContextGraph.load_json(graph_path)
        except FileNotFoundError:
            # Swept between the check above and the open. Same race, one
            # instruction later.
            raise _Swept(graph_path.name, "graph") from None
        except ValueError as exc:
            raise SessionError(f"{graph_path.name}: {exc}") from exc
        try:
            resolver = Resolver.load_json(resolver_path)
        except FileNotFoundError:
            raise _Swept(resolver_path.name, "resolver") from None
        except ValueError as exc:
            raise SessionError(f"{resolver_path.name}: {exc}") from exc

        check_agreement(graph, resolver)

        runner = None
        if paths["execution"] is not None:
            assert paths["ledger"] is not None
            runner = cls._load_execution(
                paths["execution"], paths["ledger"], graph=graph, registry=registry
            )

        return cls(
            graph=graph,
            resolver=resolver,
            walker=config.walker(graph, resolver),
            ledger=AssumptionLedger(graph),
            rounds=rounds,
            source=f"session directory {target}",
            path=target,
            generation=generation,
            runner=runner,
        )

    @staticmethod
    def _load_execution(
        execution_path: Path,
        ledger_path: Path,
        *,
        graph: ContextGraph,
        registry: Optional[TaskRegistry],
    ) -> Runner:
        """Rebuild the plan and its history, and check they describe each other.

        The registry is the one thing a session directory cannot carry: a key
        means something only because a running process was configured to say so.
        So a session holding an execution can only be restored by a deployment
        that brought one, and saying that plainly beats a ``NoneType`` further
        in.
        """
        if registry is None:
            raise SessionError(
                f"{execution_path.name} holds an execution plan, but no "
                "TaskRegistry was given. A checkpoint names its workers and "
                "this process has to say what those names mean; pass "
                "registry= to Session.load"
            )
        try:
            ledger = RunLedger.load_json(ledger_path)
        except FileNotFoundError:
            raise _Swept(ledger_path.name, "ledger") from None
        except (ExecutionError, ValueError) as exc:
            # ExecutionError is not a ValueError, unlike SnapshotError and
            # ResolverSnapshotError above. Both are caught so every companion
            # fails at this boundary with one exception type.
            raise SessionError(f"{ledger_path.name}: {exc}") from exc

        try:
            runner = Runner.load_json(
                execution_path, graph=graph, registry=registry, ledger=ledger
            )
        except FileNotFoundError:
            raise _Swept(execution_path.name, "execution") from None
        except ExecutionError as exc:
            raise SessionError(f"{execution_path.name}: {exc}") from exc
        except ValueError as exc:
            raise SessionError(f"{execution_path.name}: {exc}") from exc

        _reconcile_execution(runner, execution_path.name, ledger_path.name)
        return runner

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
        #: Commits skipped because another process held the writer lock. The
        #: mutations stay pending, so nothing is lost — but a server that keeps
        #: losing the race is a configuration problem worth being able to see.
        self.contended = 0

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
        """Write pending mutations back, unless someone else is writing.

        Contention is expected and benign — the mutations stay pending and the
        next commit picks them up — so it is counted rather than raised. Any
        other failure is a real problem with the directory and propagates.
        """
        if not self.durable or not self.pending:
            return None
        try:
            written = self.session.checkpoint()
        except SessionLockedError:
            self.contended += 1
            return None
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
