"""Controlled evidence intake: observations enter the graph without becoming verdicts.

PR #8A deliberately separates untrusted intake from trusted interpretation.  A
caller may register an observation as a detached ``EVIDENCE`` node.  It may not
name an edge, an assumption, a verdict, or an invalidation target; those remain
inside the auditor/assumption machinery.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .graph import ContextGraph
from .model import Node, NodeType, Provenance, slug

INTAKE_VERSION = 1
INTAKE_ATTR = "evidence_intake"


class EvidenceIntakeError(ValueError):
    """The observation cannot enter the graph under the PR #8A contract."""


class EvidenceConflictError(EvidenceIntakeError):
    """A durable evidence identity already means something different."""


@dataclass(frozen=True)
class EvidenceReceipt:
    evidence_id: str
    created: bool
    node: Node


def _json_value(value: Any, path: str) -> Any:
    """Validate the strict JSON subset used by evidence metadata.

    Validation is recursive and deliberately does no coercion: tuples do not
    become lists, integer mapping keys do not become strings, and NaN/Infinity
    do not become implementation-specific JSON constants.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvidenceIntakeError(f"{path}: {value!r} is not a finite JSON number")
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        copied: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceIntakeError(
                    f"{path}: object keys must be strings, got {key!r}"
                )
            copied[key] = _json_value(item, f"{path}.{key}")
        return copied
    raise EvidenceIntakeError(
        f"{path}: {type(value).__name__} is not accepted evidence metadata"
    )


def validate_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise EvidenceIntakeError(
            f"metadata must be an object, got {type(metadata).__name__}"
        )
    copied = _json_value(metadata, "metadata")
    assert isinstance(copied, dict)
    # The recursive validator above is the type gate.  This round trip detaches
    # caller-owned lists/dicts so mutating the input after return cannot mutate
    # the graph without a new evidence id or checkpoint.
    return json.loads(
        json.dumps(
            copied,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def canonical_payload(
    *,
    text: str,
    source_id: str,
    external_id: Optional[str],
    metadata: Dict[str, Any],
) -> str:
    return json.dumps(
        {
            "external_id": external_id,
            "metadata": metadata,
            "source_id": source_id,
            "text": text,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _intake_record(node: Node) -> Dict[str, Any]:
    attrs = node.attrs if isinstance(node.attrs, dict) else {}
    record = attrs.get(INTAKE_ATTR)
    if not isinstance(record, dict) or record.get("version") != INTAKE_VERSION:
        raise EvidenceConflictError(
            f"evidence node {node.id!r} is not a PR #8A intake record"
        )
    expected = {"version", "external_id", "payload_digest", "metadata"}
    if set(record) != expected:
        raise EvidenceConflictError(
            f"evidence node {node.id!r} has a malformed intake record"
        )
    return record


def _stored_canonical(node: Node) -> tuple[str, str, Optional[str]]:
    if node.type is not NodeType.EVIDENCE:
        raise EvidenceConflictError(
            f"deterministic evidence id {node.id!r} is already a {node.type.value}"
        )
    record = _intake_record(node)
    if node.provenance is None or not isinstance(node.provenance.source_id, str):
        raise EvidenceConflictError(
            f"evidence node {node.id!r} has no durable source provenance"
        )
    external_id = record["external_id"]
    if external_id is not None and not isinstance(external_id, str):
        raise EvidenceConflictError(
            f"evidence node {node.id!r} has a malformed external_id"
        )
    try:
        metadata = validate_metadata(record["metadata"])
    except EvidenceIntakeError as exc:
        raise EvidenceConflictError(
            f"evidence node {node.id!r} has malformed stored metadata: {exc}"
        ) from None
    canonical = canonical_payload(
        text=node.label,
        source_id=node.provenance.source_id,
        external_id=external_id,
        metadata=metadata,
    )
    recomputed = _digest(canonical)
    stored_digest = record["payload_digest"]
    if not isinstance(stored_digest, str) or stored_digest != recomputed:
        raise EvidenceConflictError(
            f"evidence node {node.id!r} disagrees with its stored payload digest"
        )
    return canonical, recomputed, external_id


def _external_matches(graph: ContextGraph, external_id: str) -> list[Node]:
    matches = []
    for node in graph.nodes.values():
        if node.type is not NodeType.EVIDENCE:
            continue
        record = node.attrs.get(INTAKE_ATTR) if isinstance(node.attrs, dict) else None
        if isinstance(record, dict) and record.get("external_id") == external_id:
            matches.append(node)
    return matches


class EvidenceIntake:
    """Graph-only intake boundary for untrusted observations."""

    def __init__(self, graph: ContextGraph) -> None:
        self.graph = graph

    def submit(
        self,
        *,
        text: str,
        source_id: str,
        external_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> EvidenceReceipt:
        if not isinstance(text, str) or not text.strip():
            raise EvidenceIntakeError("text must be a non-empty string")
        if not isinstance(source_id, str) or not source_id.strip():
            raise EvidenceIntakeError("source_id must be a non-empty string")
        source = self.graph.get(source_id)
        if source is None:
            raise EvidenceIntakeError(f"source {source_id!r} is not in the graph")
        if source.type is not NodeType.SOURCE:
            raise EvidenceIntakeError(
                f"source_id {source_id!r} names a {source.type.value}, not a source"
            )
        if external_id is not None:
            if not isinstance(external_id, str) or not external_id.strip():
                raise EvidenceIntakeError(
                    "external_id must be null or a non-empty string"
                )
            if not external_id.isprintable():
                raise EvidenceIntakeError("external_id contains a control character")

        clean_metadata = validate_metadata(metadata)
        canonical = canonical_payload(
            text=text,
            source_id=source_id,
            external_id=external_id,
            metadata=clean_metadata,
        )
        payload_digest = _digest(canonical)
        evidence_id = slug(canonical, "evidence")

        existing = self.graph.get(evidence_id)
        if existing is not None:
            stored_canonical, stored_digest, stored_external = _stored_canonical(existing)
            if (
                stored_canonical != canonical
                or stored_digest != payload_digest
                or stored_external != external_id
            ):
                raise EvidenceConflictError(
                    f"evidence id {evidence_id!r} already stores different content"
                )
            return EvidenceReceipt(evidence_id, False, existing)

        if external_id is not None:
            matches = _external_matches(self.graph, external_id)
            if len(matches) > 1:
                raise EvidenceConflictError(
                    f"external_id {external_id!r} is already attached to multiple evidence nodes"
                )
            if matches:
                other = matches[0]
                _, other_digest, _ = _stored_canonical(other)
                if other_digest != payload_digest or other.id != evidence_id:
                    raise EvidenceConflictError(
                        f"external_id {external_id!r} already identifies different evidence"
                    )
                return EvidenceReceipt(other.id, False, other)

        provenance = Provenance(
            source_id=source_id,
            span=None,
            extractor="mcp:evidence-intake-v1",
            checks=["evidence-intake-v1"],
            recorded_at_build=self.graph.build,
        )
        node = self.graph.add_node(
            NodeType.EVIDENCE,
            text,
            id=evidence_id,
            attrs={
                "kind": "observation",
                INTAKE_ATTR: {
                    "version": INTAKE_VERSION,
                    "external_id": external_id,
                    "payload_digest": payload_digest,
                    "metadata": clean_metadata,
                },
            },
            provenance=provenance,
        )
        return EvidenceReceipt(evidence_id, True, node)


def submit_evidence(
    graph: ContextGraph,
    *,
    text: str,
    source_id: str,
    external_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> EvidenceReceipt:
    return EvidenceIntake(graph).submit(
        text=text,
        source_id=source_id,
        external_id=external_id,
        metadata=metadata,
    )


__all__ = [
    "EvidenceConflictError",
    "EvidenceIntake",
    "EvidenceIntakeError",
    "EvidenceReceipt",
    "canonical_payload",
    "submit_evidence",
    "validate_metadata",
]
