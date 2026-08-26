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
            "embedded": self.embedding is not None,
            "build": self.build,
            "walks": self.walks,
            "pruned": self.pruned,
            "invalidated": self.invalidated,
        }


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
