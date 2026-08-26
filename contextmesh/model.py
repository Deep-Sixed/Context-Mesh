"""Core records. Everything the graph stores is one of these."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


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
        span = data.get("span")
        return cls(
            source_id=data["source_id"],
            # A span is a tuple in memory and a list in JSON. Restoring it as a
            # list would make an otherwise identical graph compare unequal.
            span=tuple(span) if span else None,
            extractor=data.get("extractor", "rule"),
            checks=list(data.get("checks") or []),
            recorded_at_build=int(data.get("recorded_at_build", 0)),
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
        provenance = data.get("provenance")
        embedding = data.get("embedding")
        return cls(
            id=data["id"],
            type=NodeType(data["type"]),
            label=data["label"],
            attrs=dict(data.get("attrs") or {}),
            provenance=Provenance.from_dict(provenance) if provenance else None,
            embedding=list(embedding) if embedding is not None else None,
            build=int(data.get("build", 0)),
            walks=int(data.get("walks", 0)),
            pruned=bool(data.get("pruned", False)),
            invalidated=bool(data.get("invalidated", False)),
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
        return cls(
            id=data["id"],
            src=data["src"],
            dst=data["dst"],
            type=EdgeType(data["type"]),
            assumption_id=data.get("assumption_id"),
            evidence_ids=list(data.get("evidence_ids") or []),
            weight=float(data.get("weight", 1.0)),
            build=int(data.get("build", 0)),
            traversals=int(data.get("traversals", 0)),
            invalidated=bool(data.get("invalidated", False)),
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
        return cls(
            id=data["id"],
            statement=data["statement"],
            status=AssumptionStatus(data["status"]),
            version=int(data.get("version", 1)),
            created_by=data.get("created_by", "system"),
            created_at_build=int(data.get("created_at_build", 0)),
            supersedes=data.get("supersedes"),
            superseded_by=data.get("superseded_by"),
            rejected_at_build=data.get("rejected_at_build"),
            evidence_ids=list(data.get("evidence_ids") or []),
        )
