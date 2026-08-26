"""The graph itself: typed nodes, typed edges, and no way to add an untyped one."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from .model import (
    Assumption,
    Edge,
    EdgeType,
    Node,
    NodeType,
    Provenance,
    slug,
)
from .ontology import ONTOLOGY, Ontology, OntologyError


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
        self.ontology.check_node(type.value)
        node_id = id or slug(label, type.value)
        existing = self.nodes.get(node_id)
        if existing is not None:
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
        assumption_id: Optional[str] = None,
        evidence_ids: Optional[Iterable[str]] = None,
        weight: float = 1.0,
    ) -> Edge:
        """Add a typed edge. Raises OntologyError if the pair is not legal."""
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
            if assumption_id and not edge.assumption_id:
                edge.assumption_id = assumption_id
            if evidence_ids:
                edge.evidence_ids.extend(
                    e for e in evidence_ids if e not in edge.evidence_ids
                )
            return edge
        edge_id = slug(f"{src}|{type.value}|{dst}", "edge")
        edge = Edge(
            id=edge_id,
            src=src,
            dst=dst,
            type=type,
            assumption_id=assumption_id,
            evidence_ids=list(evidence_ids or []),
            weight=weight,
            build=self.build,
        )
        self.edges[edge_id] = edge
        self._edge_key[key] = edge_id
        self._out[src].append(edge_id)
        self._in[dst].append(edge_id)
        return edge

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
        self.assumptions[assumption.id] = assumption
        self.add_node(
            NodeType.ASSUMPTION,
            assumption.statement,
            id=assumption.id,
            attrs={"status": assumption.status.value, "version": assumption.version},
        )
        return assumption

    # ── serialisation ────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {
            "build": self.build,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges.values()],
            "assumptions": [a.to_dict() for a in self.assumptions.values()],
        }

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
        return f"<ContextGraph build={self.build} nodes={live}/{len(self.nodes)} edges={len(self.edges)}>"
