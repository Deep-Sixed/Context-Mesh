"""Core records. Everything the graph stores is one of these."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, NoReturn, Optional, Sequence, Tuple


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
# rather than coerce them. Null is accepted only where the schema actually
# writes it — ``provenance``, ``provenance.span``, ``embedding``,
# ``assumption_id``, ``supersedes``, ``superseded_by`` and
# ``rejected_at_build``. Everywhere else a null is corruption, and normalising
# it to an empty list or object would quietly discard what the field named.
#
# Coercion is not a harmless convenience here:
# ``bool("false")`` is ``True``, so a malformed flag would silently turn a live
# node into an invalidated one, and ``list("abc")`` turns a string into a
# three-element "vector". A durable state format has to fail closed.


def _require(data: Dict[str, Any], key: str, where: str) -> Any:
    """Fetch a field that snapshot v1 always writes, or refuse the record.

    Defaulting a missing field is a quieter failure than a wrong type and a
    worse one: dropping ``invalidated`` restores a dead node as live, and
    dropping ``embedding`` restores a node that answers differently. Since
    ``to_dict`` writes every one of these, absence means the file was edited or
    truncated. Defaults belong to a later schema version that has an older
    shape to migrate from; v1 has none.
    """
    if key not in data:
        raise ValueError(f"{where}: snapshot v1 requires a {key!r} field")
    return data[key]


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
    # No ``None`` branch. These fields are never written as null, so a null is
    # corruption — and normalising it to an empty list would erase the very
    # associations it names. An assumption whose ``evidence_ids`` arrived as
    # null would restore with nothing recorded as having disproved it.
    if not isinstance(value, list):
        _fail(field, value, "a list of strings")
    return [_expect_str(v, f"{field}[{i}]") for i, v in enumerate(value)]


def _expect_dict(value: Any, field: str) -> Dict[str, Any]:
    # Same rule: an empty object and a missing one are different claims, and
    # only one of them is something ``to_dict`` ever writes.
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


def decision_id_looks_auto_minted(title: str, candidate_id: str, *, upper_bound: int) -> bool:
    """Whether `candidate_id` is a value `DecisionLog._mint_fresh_id(title)`
    could have produced -- i.e. `slug(f"{title}|{n}", "decision")` for some
    `n` in `[1, upper_bound]`.

    A pure function of `title` and `candidate_id` alone, so it answers two
    different questions the same way: whether an *existing* decision's id
    was genuinely auto-minted for its own title (making its
    `decision_identity` attr checkable against reality instead of merely
    trusted), and whether a *new* explicit id a caller is about to choose
    would collide with the auto-minted namespace for that title (so
    `decide()` can refuse it up front and keep that namespace reserved).
    `upper_bound` should be at least the number of ids this title could ever
    have collided with while auto-minting -- the caller's node count is
    always enough, since the real minting loop can never need more tries
    than there are existing ids to collide with.
    """
    for n in range(1, upper_bound + 1):
        if slug(f"{title}|{n}", "decision") == candidate_id:
            return True
    return False


def decision_fingerprint(
    *,
    title: str,
    rationale: str,
    source_id: Optional[str],
    cites: Iterable[str],
    derived_from: Iterable[str],
    depends_on: Iterable[str],
    produces: Iterable[str],
    supersedes: Optional[str],
) -> str:
    """A fingerprint of a decision's immutable graph-visible content.

    Defined purely over the *outcome* a `decide()` call produces -- title,
    rationale, the provenance source, and the target-id sets of the edge
    types it creates -- never over the raw call arguments a caller happened
    to pass. That makes it computable identically two ways: from a live
    call's arguments before anything is written, and from an existing
    node's actual provenance/edges after a snapshot restores it -- which is
    what lets a stored fingerprint be cross-checked against reality instead
    of trusted outright. Order never carries meaning (citing the same
    claims in a different order is the same decision), so every collection
    is deduplicated and sorted before hashing.

    `source_id` is its own field, kept out of `cites`: a decision's
    provenance is a distinct fact from what it additionally cites, and a
    fingerprint that folded both into one `cites` set could not tell a
    decision whose primary source and an additional cite were swapped
    (same combined set, different provenance) from one whose content
    genuinely never changed.
    """
    canonical = {
        "title": title,
        "rationale": rationale,
        "source_id": source_id,
        "cites": sorted(set(cites)),
        "derived_from": sorted(set(derived_from)),
        "depends_on": sorted(set(depends_on)),
        "produces": sorted(set(produces)),
        "supersedes": supersedes,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode("utf-8")
    ).hexdigest()


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
            source_id=_expect_str(
                _require(data, "source_id", "provenance"), "provenance.source_id"
            ),
            # A span is a tuple in memory and a list in JSON. Restoring it as a
            # list would make an otherwise identical graph compare unequal, and
            # a three-element one is not a span at all.
            span=_expect_span(_require(data, "span", "provenance"), "provenance.span"),
            extractor=_expect_str(
                _require(data, "extractor", "provenance"), "provenance.extractor"
            ),
            checks=_expect_str_list(
                _require(data, "checks", "provenance"), "provenance.checks"
            ),
            recorded_at_build=_expect_int(
                _require(data, "recorded_at_build", "provenance"),
                "provenance.recorded_at_build",
                minimum=0,
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
        node_id = _expect_str(_require(data, "id", "node"), "node.id")
        where = f"node[{node_id}]"
        provenance = _require(data, "provenance", where)
        if provenance is not None and not isinstance(provenance, dict):
            _fail(f"{where}.provenance", provenance, "null or an object")
        embedding = _expect_vector(_require(data, "embedding", where), f"{where}.embedding")
        flag = _expect_bool(_require(data, "embedded", where), f"{where}.embedded")
        # The flag is a convenience view of the vector. If they disagree the
        # snapshot has two answers to one question, so neither is trusted.
        if flag != (embedding is not None):
            raise ValueError(f"{where}: embedded={flag} disagrees with the vector")
        return cls(
            id=node_id,
            type=NodeType(_expect_str(_require(data, "type", where), f"{where}.type")),
            label=_expect_str(_require(data, "label", where), f"{where}.label"),
            attrs=_expect_dict(_require(data, "attrs", where), f"{where}.attrs"),
            provenance=Provenance.from_dict(provenance) if provenance else None,
            embedding=embedding,
            build=_expect_int(_require(data, "build", where), f"{where}.build", minimum=0),
            walks=_expect_int(_require(data, "walks", where), f"{where}.walks", minimum=0),
            pruned=_expect_bool(_require(data, "pruned", where), f"{where}.pruned"),
            invalidated=_expect_bool(
                _require(data, "invalidated", where), f"{where}.invalidated"
            ),
        )


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    type: EdgeType
    #: the assumption this relationship is conditional on, if any -- the edge
    #: holds only while it stands; see GRAPH.md, "What edge-level assumption
    #: binding means". A live edge is bound only through
    #: AssumptionLedger.justifies -- ContextGraph.add_edge takes no
    #: assumption_id, so it is not a second binding path. Snapshot
    #: restoration (ContextGraph.from_dict) sets this field directly, since
    #: it validates the restored snapshot's consistency on its own terms.
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
        edge_id = _expect_str(_require(data, "id", "edge"), "edge.id")
        where = f"edge[{edge_id}]"
        assumption_id = _require(data, "assumption_id", where)
        if assumption_id is not None:
            assumption_id = _expect_str(assumption_id, f"{where}.assumption_id")
        return cls(
            id=edge_id,
            src=_expect_str(_require(data, "src", where), f"{where}.src"),
            dst=_expect_str(_require(data, "dst", where), f"{where}.dst"),
            type=EdgeType(_expect_str(_require(data, "type", where), f"{where}.type")),
            assumption_id=assumption_id,
            evidence_ids=_expect_str_list(
                _require(data, "evidence_ids", where), f"{where}.evidence_ids"
            ),
            weight=_expect_number(_require(data, "weight", where), f"{where}.weight"),
            build=_expect_int(_require(data, "build", where), f"{where}.build", minimum=0),
            traversals=_expect_int(
                _require(data, "traversals", where), f"{where}.traversals", minimum=0
            ),
            invalidated=_expect_bool(
                _require(data, "invalidated", where), f"{where}.invalidated"
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
        aid = _expect_str(_require(data, "id", "assumption"), "assumption.id")
        where = f"assumption[{aid}]"
        rejected_at = _require(data, "rejected_at_build", where)
        if rejected_at is not None:
            rejected_at = _expect_int(rejected_at, f"{where}.rejected_at_build", minimum=0)
        optional = {}
        for name in ("supersedes", "superseded_by"):
            value = _require(data, name, where)
            optional[name] = (
                None if value is None else _expect_str(value, f"{where}.{name}")
            )
        return cls(
            id=aid,
            statement=_expect_str(_require(data, "statement", where), f"{where}.statement"),
            status=AssumptionStatus(
                _expect_str(_require(data, "status", where), f"{where}.status")
            ),
            version=_expect_int(
                _require(data, "version", where), f"{where}.version", minimum=1
            ),
            created_by=_expect_str(
                _require(data, "created_by", where), f"{where}.created_by"
            ),
            created_at_build=_expect_int(
                _require(data, "created_at_build", where),
                f"{where}.created_at_build",
                minimum=0,
            ),
            supersedes=optional["supersedes"],
            superseded_by=optional["superseded_by"],
            rejected_at_build=rejected_at,
            evidence_ids=_expect_str_list(
                _require(data, "evidence_ids", where), f"{where}.evidence_ids"
            ),
        )
