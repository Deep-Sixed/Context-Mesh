"""The graph itself: typed nodes, typed edges, and no way to add an untyped one."""

from __future__ import annotations

import dataclasses
import json
from collections import defaultdict
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set

from .model import (
    Assumption,
    AssumptionStatus,
    Edge,
    EdgeType,
    Node,
    NodeType,
    Provenance,
    slug,
)
from .ontology import ONTOLOGY, Ontology, OntologyError

#: Every field a ``Node`` can carry. A Must-carry name that matches one of
#: these is checked as that field (present means not ``None``); every other
#: name is checked as an ``attrs`` key instead. See ``_check_must_carry``.
_NODE_FIELD_NAMES = frozenset(f.name for f in dataclasses.fields(Node))


def _check_must_carry(node: Node, ontology: Ontology) -> None:
    """Fail closed if ``node`` is missing a name its type's row requires.

    Presence only, per GRAPH.md's "What `Must carry` means": a required name
    is satisfied by a ``Node`` field of that name that is not ``None``, or —
    for a name that is not a field at all — an ``attrs`` key. GRAPH.md says
    ``provenance`` is a field, not an attribute, so a name that names a real
    field is checked only as that field: an ``attrs`` entry of the same name
    (``attrs={"provenance": None}`` while ``node.provenance`` is still
    ``None``) does not get a second, easier route to satisfying it. Nothing
    here checks whether the value is well-formed, only whether it is there.
    """
    required = ontology.must_carry.get(node.type.value, frozenset())
    missing = [
        name
        for name in required
        if (getattr(node, name) is None if name in _NODE_FIELD_NAMES else name not in node.attrs)
    ]
    if missing:
        raise OntologyError(
            f"{node.type.value} node {node.id!r} is missing "
            f"{', '.join(sorted(missing))}; GRAPH.md's Must carry for "
            f"{node.type.value} is {sorted(required)}"
        )

#: The durable snapshot format. Separate from ``mesh.json``, which is the
#: dashboard's projection: a view may be shaped for whatever reads it, a state
#: contract may not. Bump ``SNAPSHOT_VERSION`` for any change that makes an
#: older file load into a different graph — including a change to how node or
#: edge ids are derived, since the loader checks the ids it recomputes.
SNAPSHOT_SCHEMA = "contextmesh.graph"
SNAPSHOT_VERSION = 1


class SnapshotError(ValueError):
    """Raised when a snapshot is not a graph this build can faithfully restore.

    Separate from :class:`OntologyError`, which the loader also lets through:
    an illegal edge pair in a file is an ontology violation and should say so,
    while a duplicate id or a missing record is a corrupt container.
    """


def _no_constants(value: str) -> float:
    """json.load hook. ``NaN``/``Infinity`` are not JSON and do not round-trip."""
    raise SnapshotError(
        f"snapshot contains the non-JSON constant {value!r}; a durable graph "
        "cannot hold values that other parsers will refuse"
    )


class ContextGraph:
    """A typed context graph.

    Every mutation typechecks against ``GRAPH.md``. ``add_edge`` has no code path
    that stores an edge without a type, which is what makes ``untyped_edges == 0``
    an invariant rather than a statistic.
    """

    def __init__(self, ontology: Ontology = ONTOLOGY) -> None:
        self.ontology = ontology
        self.nodes: Dict[str, Node] = {}
        self.edges: Dict[str, Edge] = {}
        self.assumptions: Dict[str, Assumption] = {}
        self._out: Dict[str, List[str]] = defaultdict(list)
        self._in: Dict[str, List[str]] = defaultdict(list)
        self._edge_key: Dict[tuple, str] = {}
        self.build: int = 0

    # ── nodes ────────────────────────────────────────────────────────────
    def add_node(
        self,
        type: NodeType,
        label: str,
        *,
        id: Optional[str] = None,
        attrs: Optional[Dict[str, Any]] = None,
        provenance: Optional[Provenance] = None,
        embedding: Optional[Sequence[float]] = None,
    ) -> Node:
        """Add or re-observe a node. Raises OntologyError if the pair is not
        legal, or if the id collides with a different node.

        ``slug`` truncates its digest, so an id is not proof of identity by
        itself: two different (type, label) pairs can derive the same id.
        Finding an existing node under ``node_id`` is only ever treated as a
        repeat observation of *the same thing* when its type and label agree
        with this call -- attrs (mutable metadata a repeat observation may
        legitimately extend) are never part of that comparison. A type or
        label mismatch under a shared id is a collision, not an update, and
        is refused rather than silently substituted -- see GRAPH.md's "What
        a node/edge id collision means".
        """
        self.ontology.check_node(type.value)
        node_id = id or slug(label, type.value)
        existing = self.nodes.get(node_id)
        if existing is not None:
            if existing.type is not type or existing.label != label:
                raise OntologyError(
                    f"node id {node_id!r} is already {existing.type.value} "
                    f"{existing.label!r}; refusing to treat {type.value} "
                    f"{label!r} as the same node"
                )
            if attrs:
                existing.attrs.update(attrs)
            if provenance is not None and existing.provenance is None:
                existing.provenance = provenance
            if embedding is not None:
                existing.embedding = embedding
            return existing
        node = Node(
            id=node_id,
            type=type,
            label=label,
            attrs=dict(attrs or {}),
            provenance=provenance,
            embedding=embedding,
            build=self.build,
        )
        _check_must_carry(node, self.ontology)
        self.nodes[node_id] = node
        return node

    def node(self, node_id: str) -> Node:
        return self.nodes[node_id]

    def get(self, node_id: str) -> Optional[Node]:
        return self.nodes.get(node_id)

    def by_type(self, type: NodeType, *, live_only: bool = True) -> List[Node]:
        return [
            n
            for n in self.nodes.values()
            if n.type is type and (n.live or not live_only)
        ]

    def type_counts(self, *, live_only: bool = True) -> Dict[str, int]:
        counts: Dict[str, int] = {t.value: 0 for t in NodeType}
        for n in self.nodes.values():
            if live_only and not n.live:
                continue
            counts[n.type.value] += 1
        return counts

    # ── edges ────────────────────────────────────────────────────────────
    def add_edge(
        self,
        src: str,
        type: EdgeType,
        dst: str,
        *,
        evidence_ids: Optional[Iterable[str]] = None,
        weight: float = 1.0,
    ) -> Edge:
        """Add a typed edge. Raises OntologyError if the pair is not legal.

        This is not a binding path: it never takes an ``assumption_id``. A
        live edge is bound through :meth:`AssumptionLedger.justifies`, which
        enforces the write boundary (assumption exists and is active, edge
        exists and is live, no silent rebinding) that this method has no way
        to. ``ContextGraph.from_dict`` sets ``edge.assumption_id`` directly
        after calling this, for the same reason: restoring a snapshot has to
        accept states ``justifies`` would refuse to *create* live -- an
        already-invalidated edge bound to a rejected assumption, say -- and
        validates them with its own snapshot-consistency checks instead.

        ``slug`` truncates its digest, so a colliding ``edge_id`` is possible
        for two distinct ``(src, type, dst)`` triples. ``key`` -- the actual
        triple, not the derived id -- is what decides whether this is a
        repeat observation of the same relationship; a fresh ``edge_id`` that
        already names a *different* triple is a collision and is refused
        before anything is written, not merged into the existing edge. See
        GRAPH.md's "What a node/edge id collision means".
        """
        if src not in self.nodes:
            raise OntologyError(f"unknown source node {src!r}")
        if dst not in self.nodes:
            raise OntologyError(f"unknown target node {dst!r}")
        if src == dst:
            raise OntologyError(f"self edge on {src!r}")
        self.ontology.check_edge(
            type.value, self.nodes[src].type.value, self.nodes[dst].type.value
        )
        key = (src, type.value, dst)
        if key in self._edge_key:
            edge = self.edges[self._edge_key[key]]
            edge.weight += weight
            if evidence_ids:
                edge.evidence_ids.extend(
                    e for e in evidence_ids if e not in edge.evidence_ids
                )
            return edge
        edge_id = slug(f"{src}|{type.value}|{dst}", "edge")
        if edge_id in self.edges:
            collision = self.edges[edge_id]
            raise OntologyError(
                f"edge id {edge_id!r} is already "
                f"{collision.src!r}-[{collision.type.value}]->{collision.dst!r}; "
                f"refusing to treat {src!r}-[{type.value}]->{dst!r} as the same edge"
            )
        edge = Edge(
            id=edge_id,
            src=src,
            dst=dst,
            type=type,
            evidence_ids=list(evidence_ids or []),
            weight=weight,
            build=self.build,
        )
        self.edges[edge_id] = edge
        self._edge_key[key] = edge_id
        self._out[src].append(edge_id)
        self._in[dst].append(edge_id)
        return edge

    def _discard_edge(self, edge_id: str) -> None:
        """Undo one ``add_edge`` call, for a caller rolling back a write that
        must not partially commit (a multi-edge operation where a later edge
        failed). Only sound when ``edge_id`` was created by that same call
        and nothing else has read or extended it since -- this is not a
        general "delete an edge" operation, and must never be exposed as one.
        """
        edge = self.edges.pop(edge_id, None)
        if edge is None:
            return
        key = (edge.src, edge.type.value, edge.dst)
        if self._edge_key.get(key) == edge_id:
            del self._edge_key[key]
        if edge_id in self._out.get(edge.src, ()):
            self._out[edge.src].remove(edge_id)
        if edge_id in self._in.get(edge.dst, ()):
            self._in[edge.dst].remove(edge_id)

    def _discard_node(self, node_id: str) -> None:
        """Undo one ``add_node`` call, for the same kind of rollback as
        ``_discard_edge``. Only sound when the node was freshly created by
        the call being rolled back -- never call this on a node any other
        write might already depend on.
        """
        self.nodes.pop(node_id, None)

    def out_edges(
        self,
        node_id: str,
        types: Optional[Iterable[EdgeType]] = None,
        *,
        live_only: bool = True,
    ) -> List[Edge]:
        wanted = set(types) if types else None
        result = []
        for eid in self._out.get(node_id, ()):
            edge = self.edges[eid]
            if live_only and not edge.live:
                continue
            if live_only and not self.nodes[edge.dst].live:
                continue
            if wanted and edge.type not in wanted:
                continue
            result.append(edge)
        return result

    def in_edges(
        self,
        node_id: str,
        types: Optional[Iterable[EdgeType]] = None,
        *,
        live_only: bool = True,
    ) -> List[Edge]:
        wanted = set(types) if types else None
        result = []
        for eid in self._in.get(node_id, ()):
            edge = self.edges[eid]
            if live_only and not edge.live:
                continue
            if live_only and not self.nodes[edge.src].live:
                continue
            if wanted and edge.type not in wanted:
                continue
            result.append(edge)
        return result

    def degree(self, node_id: str) -> int:
        return len(self._out.get(node_id, ())) + len(self._in.get(node_id, ()))

    def edge_counts(self) -> Dict[str, int]:
        counts = {t.value: 0 for t in EdgeType}
        for e in self.edges.values():
            if e.live:
                counts[e.type.value] += 1
        return counts

    @property
    def untyped_edges(self) -> int:
        """Always 0. Kept as a computed property so the claim stays checkable."""
        return sum(1 for e in self.edges.values() if not isinstance(e.type, EdgeType))

    # ── assumptions ──────────────────────────────────────────────────────
    def add_assumption(self, assumption: Assumption) -> Assumption:
        """Add an assumption record and its mirroring node.

        ``add_node`` runs first and does the collision check (rule 8): if
        ``assumption.id`` already names a node with a different label, it
        raises before either this method or ``add_node`` writes anything.
        Recording ``assumption`` into ``self.assumptions`` only after
        ``add_node`` returns means a refused collision leaves both
        ``self.assumptions`` and ``self.nodes`` exactly as they were --
        recording it first would let a rejected write still overwrite the
        existing record even though the node write it mirrors was refused.
        """
        self.add_node(
            NodeType.ASSUMPTION,
            assumption.statement,
            id=assumption.id,
            attrs={"status": assumption.status.value, "version": assumption.version},
        )
        self.assumptions[assumption.id] = assumption
        return assumption

    def sync_assumption(self, assumption: Assumption) -> Node:
        """Refresh the node's copy of an assumption's lifecycle fields.

        ``status`` and ``version`` live on the record *and* are projected onto
        the node so a walk can read them without a second lookup. Every path
        that changes the record has to call this, or the graph holds two
        answers to one question — which is exactly what a snapshot cannot
        represent, and what :meth:`from_dict` now refuses to restore.
        """
        node = self.nodes[assumption.id]
        node.attrs["status"] = assumption.status.value
        node.attrs["version"] = assumption.version
        return node

    # ── serialisation ────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        """The durable snapshot: everything needed to rebuild this graph.

        Records are emitted in **insertion order**, not sorted. That is not an
        oversight. The walker's frontier is a heap whose tie-breaker is an
        insertion counter (``traverse.py``), and expansions arrive in
        ``_out``/``_in`` order — so for two equal-cost branches, adjacency order
        decides which path an answer takes. Sorting the arrays would reorder
        those lists on reload and could change an answer without changing a
        single fact. Sorting the *keys* of each JSON object is safe and
        ``save_json`` does exactly that.

        ``_out``, ``_in`` and ``_edge_key`` are deliberately absent: they are
        rebuilt by :meth:`from_dict` through ``add_edge``. Persisting them would
        tie the format to today's internals and let a snapshot carry indexes
        that disagree with its own edges.
        """
        return {
            "schema": SNAPSHOT_SCHEMA,
            "version": SNAPSHOT_VERSION,
            "build": self.build,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges.values()],
            "assumptions": [a.to_dict() for a in self.assumptions.values()],
        }

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], *, ontology: Ontology = ONTOLOGY
    ) -> "ContextGraph":
        """Rebuild a graph from a snapshot, re-checking it on the way in.

        A snapshot is untrusted input. Every edge goes back through
        :meth:`add_edge`, so ``GRAPH.md`` typechecks it exactly as it would a
        live write, and the adjacency indexes are reconstructed rather than
        believed. A loader that writes straight into the internal dictionaries
        is faster and admits graphs the live API would refuse — which makes the
        ontology a convention that holds only until something is saved.
        """
        if not isinstance(data, dict):
            raise SnapshotError(f"snapshot must be an object, got {type(data).__name__}")
        for key in ("schema", "version", "build", "nodes", "edges", "assumptions"):
            if key not in data:
                raise SnapshotError(f"snapshot is missing its {key!r} field")
        schema = data["schema"]
        if schema != SNAPSHOT_SCHEMA:
            raise SnapshotError(
                f"not a {SNAPSHOT_SCHEMA} snapshot: schema is {schema!r}"
            )
        version = data["version"]
        # ``True == 1`` in Python, so a bool would sail through the equality
        # check below and load as version 1.
        if isinstance(version, bool) or not isinstance(version, int):
            raise SnapshotError(f"snapshot version must be an integer, got {version!r}")
        if version != SNAPSHOT_VERSION:
            raise SnapshotError(
                f"snapshot version {version!r} cannot be read by this build, "
                f"which writes and reads version {SNAPSHOT_VERSION}"
            )
        build = data["build"]
        if isinstance(build, bool) or not isinstance(build, int) or build < 0:
            raise SnapshotError(f"snapshot build must be a non-negative integer, got {build!r}")

        graph = cls(ontology)
        rows = {}
        for key in ("nodes", "edges", "assumptions"):
            if not isinstance(data[key], list):
                raise SnapshotError(
                    f"snapshot {key!r} must be a list, got {type(data[key]).__name__}"
                )
            rows[key] = data[key]
        node_rows, edge_rows, assumption_rows = (
            rows["nodes"], rows["edges"], rows["assumptions"]
        )

        # ── nodes, in snapshot order so adjacency order survives ──────────
        seen_nodes: Set[str] = set()
        for row in node_rows:
            node = Node.from_dict(row)
            if node.id in seen_nodes:
                raise SnapshotError(f"duplicate node id {node.id!r}")
            seen_nodes.add(node.id)
            restored = graph.add_node(
                node.type,
                node.label,
                id=node.id,
                attrs=node.attrs,
                provenance=node.provenance,
                embedding=node.embedding,
            )
            # State add_node does not take, because a live write never sets it.
            restored.build = node.build
            restored.walks = node.walks
            restored.pruned = node.pruned
            restored.invalidated = node.invalidated

        # ── assumption records ────────────────────────────────────────────
        for row in assumption_rows:
            assumption = Assumption.from_dict(row)
            if assumption.id in graph.assumptions:
                raise SnapshotError(f"duplicate assumption id {assumption.id!r}")
            node = graph.nodes.get(assumption.id)
            if node is None:
                raise SnapshotError(
                    f"assumption {assumption.id!r} has no node in the snapshot"
                )
            if node.type is not NodeType.ASSUMPTION:
                raise SnapshotError(
                    f"assumption {assumption.id!r} maps to a "
                    f"{node.type.value!r} node"
                )
            if node.label != assumption.statement:
                raise SnapshotError(
                    f"assumption {assumption.id!r}: node label and statement disagree"
                )
            # ``status`` and ``version`` are stored twice — on the record and
            # projected onto the node. Both are durable, so a snapshot where
            # they disagree has two answers to one question and neither can be
            # trusted; picking one silently would make a reload a repair.
            for field, recorded in (
                ("status", assumption.status.value),
                ("version", assumption.version),
            ):
                mirrored = node.attrs.get(field)
                if mirrored != recorded:
                    raise SnapshotError(
                        f"assumption {assumption.id!r}: node attrs say "
                        f"{field}={mirrored!r} but the record says {recorded!r}"
                    )
            graph.assumptions[assumption.id] = assumption
        for node in graph.nodes.values():
            if node.type is NodeType.ASSUMPTION and node.id not in graph.assumptions:
                raise SnapshotError(
                    f"assumption node {node.id!r} has no assumption record"
                )

        # ── edges, back through add_edge so the ontology gets a say ───────
        seen_edges: Set[str] = set()
        seen_keys: Set[tuple] = set()
        for row in edge_rows:
            stored = Edge.from_dict(row)
            if stored.id in seen_edges:
                raise SnapshotError(f"duplicate edge id {stored.id!r}")
            seen_edges.add(stored.id)
            key = (stored.src, stored.type.value, stored.dst)
            # add_edge treats a repeated relationship as another observation
            # and adds to its weight. That is right while building a graph and
            # wrong while restoring one: the weight is already in the snapshot,
            # so a duplicate here is corruption, not evidence.
            if key in seen_keys:
                raise SnapshotError(
                    f"duplicate relationship {stored.src!r}"
                    f"-[{stored.type.value}]->{stored.dst!r}"
                )
            seen_keys.add(key)
            edge = graph.add_edge(
                stored.src,
                stored.type,
                stored.dst,
                evidence_ids=stored.evidence_ids,
                weight=stored.weight,
            )
            # Edge identity is derived from (src, type, dst), so a snapshot id
            # that disagrees with the recomputed one means the file was edited
            # or written by a build with a different id scheme. Either way it
            # is not restorable here. Changing how ids are derived is therefore
            # a SNAPSHOT_VERSION bump, not a quiet refactor.
            if edge.id != stored.id:
                raise SnapshotError(
                    f"edge id {stored.id!r} does not match the id this build "
                    f"derives for the same relationship ({edge.id!r})"
                )
            # Set directly rather than through add_edge/justifies: restoring a
            # snapshot has to accept states the live write boundary would
            # refuse to create (an already-invalidated edge bound to a
            # rejected assumption), and the loop below validates the result
            # instead of re-deriving it through the live guard rails.
            edge.assumption_id = stored.assumption_id
            edge.build = stored.build
            edge.traversals = stored.traversals
            edge.invalidated = stored.invalidated

        # ── references between records ────────────────────────────────────
        # Each record validated on its own above; these only make sense once
        # every record exists. A dangling reference loads fine and fails later
        # — ``lineage`` raising KeyError on a graph that has been live for an
        # hour — which is exactly the delayed corruption a loader should catch.
        for aid, assumption in graph.assumptions.items():
            for field in ("supersedes", "superseded_by"):
                other_id = getattr(assumption, field)
                if other_id is None:
                    continue
                if other_id not in graph.assumptions:
                    raise SnapshotError(
                        f"assumption {aid!r}: {field} names {other_id!r}, "
                        "which is not in the snapshot"
                    )
            # Supersession is a two-sided relationship, and half of it is not a
            # weaker version of it — a lineage walk would end somewhere the
            # other record disagrees with.
            if assumption.superseded_by is not None:
                successor = graph.assumptions[assumption.superseded_by]
                if successor.supersedes != aid:
                    raise SnapshotError(
                        f"assumption {aid!r}: superseded_by {successor.id!r}, "
                        f"which supersedes {successor.supersedes!r} instead"
                    )
            if assumption.supersedes is not None:
                predecessor = graph.assumptions[assumption.supersedes]
                if predecessor.superseded_by != aid:
                    raise SnapshotError(
                        f"assumption {aid!r}: supersedes {predecessor.id!r}, "
                        f"which is superseded by {predecessor.superseded_by!r} instead"
                    )
            for evidence_id in assumption.evidence_ids:
                if evidence_id not in graph.nodes:
                    raise SnapshotError(
                        f"assumption {aid!r}: evidence {evidence_id!r} is not a node"
                    )

        for edge in graph.edges.values():
            if edge.assumption_id is not None:
                if edge.assumption_id not in graph.assumptions:
                    raise SnapshotError(
                        f"edge {edge.id!r}: assumption_id names {edge.assumption_id!r}, "
                        "which is not in the snapshot"
                    )
                # Rejection invalidates a bound edge directly (GRAPH.md, "What
                # edge-level assumption binding means"), so a snapshot where
                # the bound assumption fell but the edge did not is one where
                # that never happened -- two records disagreeing about
                # whether this relationship still holds. Supersession is not
                # this: a superseded assumption was replaced, not disproved,
                # so a bound edge surviving that is not a contradiction.
                bound = graph.assumptions[edge.assumption_id]
                if bound.status is AssumptionStatus.REJECTED and edge.live:
                    raise SnapshotError(
                        f"edge {edge.id!r} is bound to rejected assumption "
                        f"{edge.assumption_id!r} but restores as live"
                    )
            for evidence_id in edge.evidence_ids:
                if evidence_id not in graph.nodes:
                    raise SnapshotError(
                        f"edge {edge.id!r}: evidence {evidence_id!r} is not a node"
                    )

        graph.build = build
        return graph

    # ── files ────────────────────────────────────────────────────────────
    def to_json(self) -> str:
        """The snapshot as text. Object keys sorted; record arrays left alone.

        Separate from ``save_json`` so a caller that needs to place the bytes
        itself — atomically, or through a file descriptor it already owns —
        does not have to reproduce these arguments and risk drifting from them.
        """
        return (
            json.dumps(
                self.to_dict(),
                sort_keys=True,
                indent=2,
                ensure_ascii=False,
                # NaN and Infinity are Python's, not JSON's. Refusing them here
                # means a snapshot that saves is a snapshot other tools can read.
                allow_nan=False,
            )
            + "\n"
        )

    def save_json(self, path: Any) -> Any:
        """Write the snapshot to ``path``."""
        from pathlib import Path

        target = Path(path)
        target.write_text(self.to_json(), encoding="utf-8")
        return target

    @classmethod
    def load_json(cls, path: Any, *, ontology: Ontology = ONTOLOGY) -> "ContextGraph":
        from pathlib import Path

        text = Path(path).read_text(encoding="utf-8")
        data = json.loads(text, parse_constant=_no_constants)
        return cls.from_dict(data, ontology=ontology)

    def __iter__(self) -> Iterator[Node]:
        return iter(self.nodes.values())

    def __len__(self) -> int:
        return len(self.nodes)

    def __bool__(self) -> bool:
        # An empty graph is still a graph. Without this, ``graph or Graph()``
        # silently swaps in a fresh one.
        return True

    def __repr__(self) -> str:
        live = sum(1 for n in self.nodes.values() if n.live)
        return (
            f"<ContextGraph build={self.build} "
            f"nodes={live}/{len(self.nodes)} edges={len(self.edges)}>"
        )
