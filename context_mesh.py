"""
Context Mesh – Assumption-Aware Control Layer
=============================================
Companion runtime for the Context Mesh typed graph.

This module implements the control plane that sits on top of a Context Mesh
knowledge graph. It treats assumptions as first-class, versioned objects,
attaches them to edges, and performs selective invalidation of only the
downstream work that depended on a broken assumption.

Key properties:
- Assumptions are first-class objects with IDs, status, and lineage
- Edges carry the assumption that justifies them
- Blast radius is computed from a failed assumption → edges it justifies → dependent nodes
- Logging an event never silently activates an assumption for the logging node
- Multi-branch topology with explicit preservation proof for unrelated branches

Node roles used in the demonstration:
  INTENT, DECOMPOSE, WORKER, AUDIT, DRIFT, LEDGER, ROOT
"""

from __future__ import annotations
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Set, Callable
from enum import Enum


# ──────────────────────────────────────────────────────────────
# Core data model
# ──────────────────────────────────────────────────────────────

class AssumptionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


@dataclass
class Assumption:
    id: str
    claim: str
    status: AssumptionStatus
    created_by: str
    supersedes: Optional[str] = None          # id of previous assumption
    superseded_by: Optional[str] = None
    evidence: List[str] = field(default_factory=list)  # evidence ids
    created_at_step: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class Edge:
    """Directed edge that carries the assumption justifying its existence."""
    id: str
    source: str
    target: str
    assumption_id: Optional[str] = None   # None = unconditional structural edge


@dataclass
class Evidence:
    id: str
    kind: str                 # "audit_failure", "audit_pass", "worker_output", ...
    details: Dict[str, Any]
    related_assumption: Optional[str] = None


# ──────────────────────────────────────────────────────────────
# Graph state
# ──────────────────────────────────────────────────────────────

class GraphState:
    def __init__(self, intent: str):
        self.global_intent = intent
        self.step_counter = 0

        # First-class stores
        self.assumptions: Dict[str, Assumption] = {}
        self.evidence: Dict[str, Evidence] = {}
        self.edges: Dict[str, Edge] = {}                 # edge_id → Edge
        self.adj: Dict[str, List[str]] = {}              # source → [edge_ids]

        # Runtime
        self.node_outputs: Dict[str, Any] = {}
        self.ledger: List[Dict[str, Any]] = []
        self.invalidation_sets: List[Dict[str, Any]] = []
        self.drift_history: List[str] = []

        # Active assumption per worker/step (node → assumption_id)
        self.active_for_node: Dict[str, str] = {}

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:6]}"
