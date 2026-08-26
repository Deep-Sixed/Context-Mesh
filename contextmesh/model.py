"""Core records. Everything the graph stores is one of these."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, NoReturn, Optional, Sequence, Tuple


class NodeType(str, Enum):
    ENTITY = "entity"
    CLAIM = "claim"
    SOURCE = "source"
    DECISION = "decision"
    ASSUMPTION = "assumption"
    EVIDENCE = "evidence"


class EdgeType(str, Enum):
    MENTIONS = "mentions"
    DERIVED_FROM = "derived_from"
    CITES = "cites"
    CONTRADICTS = "contradicts"
    SUPPORTS = "supports"
    DEPENDS_ON = "depends_on"
    PRODUCES = "produces"
    SUPERSEDES = "supersedes"
    JUSTIFIED_BY = "justified_by"
    RESOLVES_TO = "resolves_to"


class AssumptionStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


#: The four types the dashboard draws as clusters, in display order.
DISPLAY_TYPES: Tuple[NodeType, ...] = (
    NodeType.ENTITY,
    NodeType.CLAIM,
    NodeType.SOURCE,
    NodeType.DECISION,
)


# ── snapshot field validation ────────────────────────────────────────────
# A snapshot is untrusted input, so the record loaders below check types
# rather than coerce them. Coercion is not a harmless convenience here:
# ``bool("false")`` is ``True``, so a malformed flag would silently turn a live
# node into an invalidated one, and ``list("abc")`` turns a string into a
# three-element "vector". A durable state format has to fail closed.


def _fail(field: str, value: Any, wanted: str) -> "NoReturn":
    raise ValueError(
        f"{field}: expected {wanted}, got {type(value).__name__} {value!r}"
    )


def _expect_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        _fail(field, value, "a boolean")
    return value


def _expect_int(value: Any, field: str, *, minimum: Optional[int] = None) -> int:
    # bool is a subclass of int, so True would pass an isinstance check and
    # arrive as 1. A count that was written as a flag is corruption, not a 1.
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(field, value, "an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field}: expected an integer >= {minimum}, got {value}")
    return value


def _expect_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(field, value, "a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{field}: {value!r} is not a finite number")
    return float(value)


def _expect_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        _fail(field, value, "a string")
    return value


def _expect_str_list(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        _fail(field, value, "a list of strings")
    return [_expect_str(v, f"{field}[{i}]") for i, v in enumerate(value)]


def _expect_dict(value: Any, field: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(field, value, "an object")
    for key in value:
        if not isinstance(key, str):
            raise ValueError(f"{field}: keys must be strings, got {key!r}")
    return dict(value)


def _expect_span(value: Any, field: str) -> Optional[Tuple[int, int]]:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        _fail(field, value, "null or a pair of integers")
    start, end = (_expect_int(v, f"{field}[{i}]") for i, v in enumerate(value))
    return (start, end)


def _expect_vector(value: Any, field: str) -> Optional[List[float]]:
    if value is None:
        return None
    if not isinstance(value, list):
        _fail(field, value, "null or a list of numbers")
    return [_expect_number(v, f"{field}[{i}]") for i, v in enumerate(value)]


def slug(text: str, prefix: str = "") -> str:
    """A stable id derived from content, so re-running a build is idempotent."""
    body = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
    stem = f"{body}-{digest}" if body else digest
    return f"{prefix}:{stem}" if prefix else stem


@dataclass
class Provenance:
    """Where a claim or decision came from, and what checked it."""

    source_id: str
    span: Optional[Tuple[int, int]] = None
    extractor: str = "rule"
    checks: List[str] = field(default_factory=list)
    recorded_at_build: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "span": list(self.span) if self.span else None,
            "extractor": self.extractor,
            "checks": list(self.checks),
            "recorded_at_build": self.recorded_at_build,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Provenance":
        return cls(
            source_id=_expect_str(data["source_id"], "provenance.source_id"),
            # A span is a tuple in memory and a list in JSON. Restoring it as a
            # list would make an otherwise identical graph compare unequal, and
            # a three-element one is not a span at all.
            span=_expect_span(data.get("span"), "provenance.span"),
            extractor=_expect_str(data.get("extractor", "rule"), "provenance.extractor"),
            checks=_expect_str_list(data.get("checks"), "provenance.checks"),
            recorded_at_build=_expect_int(
                data.get("recorded_at_build", 0), "provenance.recorded_at_build", minimum=0
            ),
        )


@dataclass
class Node:
    id: str
    type: NodeType
    label: str
    attrs: Dict[str, Any] = field(default_factory=dict)
    provenance: Optional[Provenance] = None
    embedding: Optional[Sequence[float]] = None
    build: int = 0
    walks: int = 0
    pruned: bool = False
    invalidated: bool = False

    @property
    def live(self) -> bool:
        return not self.pruned and not self.invalidated

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "label": self.label,
            "attrs": dict(self.attrs),
            "provenance": self.provenance.to_dict() if self.provenance else None,
            # ``embedded`` is the cheap view flag the dashboard-era code reads.
            # ``embedding`` is the state: without the actual vector a reloaded
            # graph seeds walks differently, so the snapshot would restore a
            # graph that looks identical and does not reason identically.
            "embedded": self.embedding is not None,
            "embedding": list(self.embedding) if self.embedding is not None else None,
            "build": self.build,
            "walks": self.walks,
            "pruned": self.pruned,
            "invalidated": self.invalidated,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Node":
        node_id = _expect_str(data["id"], "node.id")
        provenance = data.get("provenance")
        if provenance is not None and not isinstance(provenance, dict):
            _fail(f"node[{node_id}].provenance", provenance, "null or an object")
        embedding = _expect_vector(data.get("embedding"), f"node[{node_id}].embedding")
        if "embedded" in data:
            flag = _expect_bool(data["embedded"], f"node[{node_id}].embedded")
            # The flag is a convenience view of the vector. If they disagree the
            # snapshot has two answers to one question, so neither is trusted.
            if flag != (embedding is not None):
                raise ValueError(
                    f"node[{node_id}]: embedded={flag} disagrees with the vector"
                )
        return cls(
            id=node_id,
            type=NodeType(_expect_str(data["type"], f"node[{node_id}].type")),
            label=_expect_str(data["label"], f"node[{node_id}].label"),
            attrs=_expect_dict(data.get("attrs"), f"node[{node_id}].attrs"),
            provenance=Provenance.from_dict(provenance) if provenance else None,
            embedding=embedding,
            build=_expect_int(data.get("build", 0), f"node[{node_id}].build", minimum=0),
            walks=_expect_int(data.get("walks", 0), f"node[{node_id}].walks", minimum=0),
            pruned=_expect_bool(data.get("pruned", False), f"node[{node_id}].pruned"),
            invalidated=_expect_bool(
                data.get("invalidated", False), f"node[{node_id}].invalidated"
            ),
        )


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    type: EdgeType
    #: the assumption that justifies this edge existing at all, if any
    assumption_id: Optional[str] = None
    evidence_ids: List[str] = field(default_factory=list)
    weight: float = 1.0
    build: int = 0
    traversals: int = 0
    invalidated: bool = False

    @property
    def live(self) -> bool:
        return not self.invalidated

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "src": self.src,
            "dst": self.dst,
            "type": self.type.value,
            "assumption_id": self.assumption_id,
            "evidence_ids": list(self.evidence_ids),
            "weight": self.weight,
            "build": self.build,
            "traversals": self.traversals,
            "invalidated": self.invalidated,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Edge":
        edge_id = _expect_str(data["id"], "edge.id")
        assumption_id = data.get("assumption_id")
        if assumption_id is not None:
            assumption_id = _expect_str(assumption_id, f"edge[{edge_id}].assumption_id")
        return cls(
            id=edge_id,
            src=_expect_str(data["src"], f"edge[{edge_id}].src"),
            dst=_expect_str(data["dst"], f"edge[{edge_id}].dst"),
            type=EdgeType(_expect_str(data["type"], f"edge[{edge_id}].type")),
            assumption_id=assumption_id,
            evidence_ids=_expect_str_list(
                data.get("evidence_ids"), f"edge[{edge_id}].evidence_ids"
            ),
            weight=_expect_number(data.get("weight", 1.0), f"edge[{edge_id}].weight"),
            build=_expect_int(data.get("build", 0), f"edge[{edge_id}].build", minimum=0),
            traversals=_expect_int(
                data.get("traversals", 0), f"edge[{edge_id}].traversals", minimum=0
            ),
            invalidated=_expect_bool(
                data.get("invalidated", False), f"edge[{edge_id}].invalidated"
            ),
        )


@dataclass
class Assumption:
    """First-class and versioned. Rejecting one is what drives invalidation."""

    id: str
    statement: str
    status: AssumptionStatus = AssumptionStatus.ACTIVE
    version: int = 1
    created_by: str = "system"
    created_at_build: int = 0
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    rejected_at_build: Optional[int] = None
    evidence_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "status": self.status.value,
            "version": self.version,
            "created_by": self.created_by,
            "created_at_build": self.created_at_build,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "rejected_at_build": self.rejected_at_build,
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Assumption":
        aid = _expect_str(data["id"], "assumption.id")
        rejected_at = data.get("rejected_at_build")
        if rejected_at is not None:
            rejected_at = _expect_int(
                rejected_at, f"assumption[{aid}].rejected_at_build", minimum=0
            )
        optional = {}
        for name in ("supersedes", "superseded_by"):
            value = data.get(name)
            optional[name] = (
                None if value is None else _expect_str(value, f"assumption[{aid}].{name}")
            )
        return cls(
            id=aid,
            statement=_expect_str(data["statement"], f"assumption[{aid}].statement"),
            status=AssumptionStatus(
                _expect_str(data["status"], f"assumption[{aid}].status")
            ),
            version=_expect_int(
                data.get("version", 1), f"assumption[{aid}].version", minimum=1
            ),
            created_by=_expect_str(
                data.get("created_by", "system"), f"assumption[{aid}].created_by"
            ),
            created_at_build=_expect_int(
                data.get("created_at_build", 0),
                f"assumption[{aid}].created_at_build",
                minimum=0,
            ),
            supersedes=optional["supersedes"],
            superseded_by=optional["superseded_by"],
            rejected_at_build=rejected_at,
            evidence_ids=_expect_str_list(
                data.get("evidence_ids"), f"assumption[{aid}].evidence_ids"
            ),
        )
