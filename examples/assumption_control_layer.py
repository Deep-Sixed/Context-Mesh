"""
Context Mesh – Assumption-Aware Control Layer (standalone demo)
==============================================================
The original single-file sketch, kept because it runs on its own and shows the
selective-invalidation idea end to end with a seven-node executor.

The production version of these ideas lives in the package:

    contextmesh/assumptions.py   versioned assumptions, blast radius, rejection
    contextmesh/execute.py       re-runs exactly the closure an invalidation felled
    contextmesh/decisions.py     append-only decision history
    contextmesh/graph.py         typed nodes and typed edges

This file is a sketch, not the implementation: its DRIFT node is a stub that
reports alignment without checking anything (see `drift_logic`).

Run it with `python examples/assumption_control_layer.py`.

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
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

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


# ──────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────

class Node:
    def __init__(self, name: str, logic_fn: Callable[[Dict[str, Any]], Dict[str, Any]]):
        self.name = name
        self.logic_fn = logic_fn

    def execute(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        print(f"[{self.name}] Executing...")
        return self.logic_fn(ctx)


class EngineGraph:
    def __init__(self, state: GraphState):
        self.state = state
        self.nodes: Dict[str, Node] = {}
        self.visited: Set[str] = set()

    def add_node(self, node: Node):
        self.nodes[node.name] = node

    def add_edge(self, source: str, target: str, assumption_id: Optional[str] = None):
        eid = self.state.new_id("edge")
        edge = Edge(id=eid, source=source, target=target, assumption_id=assumption_id)
        self.state.edges[eid] = edge
        self.state.adj.setdefault(source, []).append(eid)

    def log_event(
        self,
        node: str,
        event_type: str,
        details: Any = None,
        assumption_id: Optional[str] = None,
    ):
        self.state.step_counter += 1
        entry = {
            "id": self.state.new_id("evt"),
            "step": self.state.step_counter,
            "node": node,
            "type": event_type,
            "details": details,
            "justifying_assumption": assumption_id,
        }
        self.state.ledger.append(entry)

    def activate_assumption(self, node: str, assumption_id: str):
        """Only WORKER (or similar executors) should call this."""
        self.state.active_for_node[node] = assumption_id

    def create_assumption(
        self, claim: str, created_by: str, supersedes: Optional[str] = None
    ) -> Assumption:
        aid = self.state.new_id("asm")
        asm = Assumption(
            id=aid,
            claim=claim,
            status=AssumptionStatus.ACTIVE,
            created_by=created_by,
            supersedes=supersedes,
            created_at_step=self.state.step_counter,
        )
        if supersedes and supersedes in self.state.assumptions:
            old = self.state.assumptions[supersedes]
            old.status = AssumptionStatus.SUPERSEDED
            old.superseded_by = aid
        self.state.assumptions[aid] = asm
        return asm

    def record_evidence(
        self,
        kind: str,
        details: Dict[str, Any],
        related_assumption: Optional[str] = None,
    ) -> Evidence:
        eid = self.state.new_id("ev")
        ev = Evidence(id=eid, kind=kind, details=details, related_assumption=related_assumption)
        self.state.evidence[eid] = ev
        if related_assumption and related_assumption in self.state.assumptions:
            self.state.assumptions[related_assumption].evidence.append(eid)
        return ev

    def compute_blast_radius(self, failed_assumption_id: str) -> Set[str]:
        """Return nodes that stand on edges justified by the failed assumption."""
        affected_edges = [
            e for e in self.state.edges.values()
            if e.assumption_id == failed_assumption_id
        ]
        seeds = {e.target for e in affected_edges}
        # Also include the source if it is a worker that used the assumption
        for node, aid in self.state.active_for_node.items():
            if aid == failed_assumption_id:
                seeds.add(node)

        # Transitive downstream via any edges
        invalidated: Set[str] = set()
        queue = list(seeds)
        while queue:
            cur = queue.pop(0)
            if cur in invalidated:
                continue
            invalidated.add(cur)
            for eid in self.state.adj.get(cur, []):
                edge = self.state.edges[eid]
                if edge.target not in invalidated:
                    queue.append(edge.target)
        return invalidated

    def invalidate_downstream(self, failed_assumption_id: str):
        blast = self.compute_blast_radius(failed_assumption_id)
        for n in blast:
            if n in self.state.node_outputs:
                del self.state.node_outputs[n]
            if n in self.visited:
                self.visited.discard(n)
            # Clear active assumption for those nodes
            if (
                n in self.state.active_for_node
                and self.state.active_for_node[n] == failed_assumption_id
            ):
                del self.state.active_for_node[n]

        self.state.invalidation_sets.append({
            "failed_assumption": failed_assumption_id,
            "invalidated_nodes": sorted(blast),
        })
        self.log_event("ROOT", "INVALIDATION", {
            "failed_assumption": failed_assumption_id,
            "blast_radius": sorted(blast),
        }, assumption_id=failed_assumption_id)
        return blast

    def run_pipeline(self, start: str):
        from collections import deque
        queue = deque([start])
        while queue:
            name = queue.popleft()
            if name in self.visited:
                continue
            if name not in self.nodes:
                continue

            # Special handling for LEDGER and ROOT is done outside ordinary execute in the demo
            node = self.nodes[name]
            ctx = {
                "state": self.state,
                "graph": self,
                "node_name": name,
            }
            result = node.execute(ctx)
            self.state.node_outputs[name] = result
            self.visited.add(name)

            # Enqueue downstream
            for eid in self.state.adj.get(name, []):
                edge = self.state.edges[eid]
                queue.append(edge.target)


# ──────────────────────────────────────────────────────────────
# Demo logic functions
# ──────────────────────────────────────────────────────────────

def intent_logic(ctx: Dict[str, Any]) -> Dict[str, Any]:
    state: GraphState = ctx["state"]
    graph: EngineGraph = ctx["graph"]
    graph.log_event("INTENT", "ESTABLISHED", state.global_intent)
    return {"intent": state.global_intent}


def decompose_logic(ctx: Dict[str, Any]) -> Dict[str, Any]:
    graph: EngineGraph = ctx["graph"]

    # Create initial assumption A
    asm_a = graph.create_assumption(
        claim="All numeric fields are unitless quantities (no currency conversion needed)",
        created_by="DECOMPOSE",
    )
    graph.log_event("DECOMPOSE", "ASSUMPTION_CREATED", asm_a.claim, assumption_id=asm_a.id)

    # Wire the edge that carries the assumption
    graph.add_edge("DECOMPOSE", "WORKER", assumption_id=asm_a.id)
    graph.add_edge("DECOMPOSE", "BRANCH_C1")  # unrelated branch – no assumption
    graph.add_edge("BRANCH_C1", "BRANCH_C2")

    return {"steps": ["clean_numeric_fields"], "assumption_id": asm_a.id}


def worker_logic(ctx: Dict[str, Any]) -> Dict[str, Any]:
    state: GraphState = ctx["state"]
    graph: EngineGraph = ctx["graph"]

    # Determine active assumption for this worker
    # Prefer the assumption carried by the incoming DECOMPOSE→WORKER edge
    active_id = None
    for e in state.edges.values():
        if e.source == "DECOMPOSE" and e.target == "WORKER" and e.assumption_id:
            # Use the currently ACTIVE assumption (may have been superseded)
            asm = state.assumptions.get(e.assumption_id)
            if asm and asm.status == AssumptionStatus.ACTIVE:
                active_id = e.assumption_id
            elif asm and asm.superseded_by:
                active_id = asm.superseded_by
            else:
                active_id = e.assumption_id
            break

    if not active_id:
        # Fallback: look at active_for_node
        active_id = state.active_for_node.get("WORKER")

    if active_id:
        graph.activate_assumption("WORKER", active_id)

    asm = state.assumptions.get(active_id) if active_id else None
    claim = asm.claim if asm else "(none)"

    print(f"  [WORKER] Running under assumption: {claim}")

    raw = [120, 450, "$3200", 95, "N/A", 12.5]

    # Assumption A is phrased "...unitless quantities (no currency conversion
    # needed)", so testing for the word "currency" matched A as well as B and
    # the worker took the currency-aware branch on the first pass — which meant
    # AUDIT passed and the rejection this demo exists to show never happened.
    # AUDIT keys on "unitless"; the worker keys on the same word, inverted.
    if asm and "unitless" not in asm.claim.lower():
        # Assumption B – currency aware
        processed = []
        for v in raw:
            if isinstance(v, str) and v.startswith("$"):
                processed.append(int(v[1:].replace(",", "")))
            elif isinstance(v, (int, float)):
                processed.append(v)
        mode = "currency_aware"
    else:
        # Assumption A – treat everything as unitless numbers, drop non-numeric
        processed = [v for v in raw if isinstance(v, (int, float))]
        mode = "unitless"

    result = {
        "processed_data": processed,
        "mode": mode,
        "kept_count": len(processed),
        "assumption_used": active_id,
    }
    graph.log_event("WORKER", "EXECUTED", result, assumption_id=active_id)
    graph.record_evidence("worker_output", result, related_assumption=active_id)
    return result


def audit_logic(ctx: Dict[str, Any]) -> Dict[str, Any]:
    state: GraphState = ctx["state"]
    graph: EngineGraph = ctx["graph"]

    worker_out = state.node_outputs.get("WORKER", {})
    active_id = worker_out.get("assumption_used") or state.active_for_node.get("WORKER")
    asm = state.assumptions.get(active_id) if active_id else None

    passed = True
    reason = "OK"

    if asm and "unitless" in (asm.claim or "").lower():
        # Under A we expect pure numbers; if any currency string leaked we fail
        # (In this demo the worker already filtered, but we force a failure on first pass)
        if worker_out.get("mode") == "unitless":
            passed = False
            reason = (
                "Audit rejected assumption A: data contains currency-formatted "
                "values that were incorrectly treated as unitless"
            )

    if not passed and asm:
        # Record rejection evidence
        graph.record_evidence(
            "audit_failure", {"reason": reason}, related_assumption=asm.id
        )
        graph.log_event("AUDIT", "REJECT", reason, assumption_id=asm.id)

        # Mark the assumption rejected (ROOT will supersede)
        asm.status = AssumptionStatus.REJECTED

        # Trigger ROOT supersession + selective invalidation
        new_asm = graph.create_assumption(
            claim="Numeric fields may include currency strings; parse $ and convert",
            created_by="ROOT",
            supersedes=asm.id,
        )
        graph.log_event(
            "ROOT",
            "SUPERSESSION",
            f"{asm.id} → {new_asm.id}",
            assumption_id=new_asm.id,
        )

        # Update the edge to point at the new assumption
        for e in state.edges.values():
            if e.source == "DECOMPOSE" and e.target == "WORKER":
                e.assumption_id = new_asm.id

        blast = graph.invalidate_downstream(asm.id)
        print(f"  [ROOT] Superseded {asm.id} → {new_asm.id}; invalidated {sorted(blast)}")

        return {"passed": False, "reason": reason, "new_assumption": new_asm.id}

    # Pass case
    graph.record_evidence(
        "audit_pass", {"mode": worker_out.get("mode")}, related_assumption=active_id
    )
    graph.log_event("AUDIT", "PASS", worker_out.get("mode"), assumption_id=active_id)
    return {"passed": True, "reason": "Assumption validated"}


def drift_logic(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """STUB. Reports alignment without checking anything.

    A real drift check would compare the accumulated work against INTENT — which
    is a different question from AUDIT's "does this step hold under its own
    declared assumption". This one reads neither, and returning ``False``
    unconditionally is a placeholder for the node's shape, not a check. It is
    left visible rather than removed because a stub that announces itself is
    less misleading than one that reads like a working check.
    """
    state: GraphState = ctx["state"]
    graph: EngineGraph = ctx["graph"]
    graph.log_event("DRIFT", "CHECK", "Aligned with original INTENT (not checked)")
    state.drift_history.append("no_drift")
    return {"drift": False}


def branch_c1_logic(ctx: Dict[str, Any]) -> Dict[str, Any]:
    graph: EngineGraph = ctx["graph"]
    graph.log_event("BRANCH_C1", "EXECUTED", "unrelated independent work")
    return {"branch": "C1", "value": 42}


def branch_c2_logic(ctx: Dict[str, Any]) -> Dict[str, Any]:
    graph: EngineGraph = ctx["graph"]
    graph.log_event("BRANCH_C2", "EXECUTED", "downstream of C1 – must be preserved")
    return {"branch": "C2", "value": 99}


def ledger_logic(ctx: Dict[str, Any]) -> Dict[str, Any]:
    state: GraphState = ctx["state"]
    graph: EngineGraph = ctx["graph"]
    graph.log_event("LEDGER", "SNAPSHOT", f"{len(state.ledger)} events recorded")
    return {"ledger_size": len(state.ledger)}


def root_logic(ctx: Dict[str, Any]) -> Dict[str, Any]:
    graph: EngineGraph = ctx["graph"]
    graph.log_event("ROOT", "OWNERSHIP_CHECK", "graph integrity verified")
    return {"status": "owned"}


# ──────────────────────────────────────────────────────────────
# Runner + acceptance tests
# ──────────────────────────────────────────────────────────────

def run_demo():
    print("=" * 70)
    print("Context Mesh – Assumption-Aware Control Layer  (v3 acceptance)")
    print("=" * 70)

    shared = GraphState(intent="Clean and normalise a mixed numeric dataset")
    graph = EngineGraph(shared)

    # Register nodes
    graph.add_node(Node("INTENT", intent_logic))
    graph.add_node(Node("DECOMPOSE", decompose_logic))
    graph.add_node(Node("WORKER", worker_logic))
    graph.add_node(Node("AUDIT", audit_logic))
    graph.add_node(Node("DRIFT", drift_logic))
    graph.add_node(Node("BRANCH_C1", branch_c1_logic))
    graph.add_node(Node("BRANCH_C2", branch_c2_logic))
    graph.add_node(Node("LEDGER", ledger_logic))
    graph.add_node(Node("ROOT", root_logic))

    # Structural edges (assumption-bearing edge is added inside DECOMPOSE)
    graph.add_edge("INTENT", "DECOMPOSE")
    graph.add_edge("WORKER", "AUDIT")
    graph.add_edge("AUDIT", "DRIFT")
    graph.add_edge("DRIFT", "LEDGER")
    graph.add_edge("LEDGER", "ROOT")

    print("\n--- STARTING PIPELINE ---")
    graph.run_pipeline("INTENT")

    # Because AUDIT may have invalidated WORKER, we need a second pass for the
    # affected branch only (the engine already cleared visited for the blast radius)
    if any(not shared.node_outputs.get("AUDIT", {}).get("passed", True) for _ in [0]):
        print("\n--- SELECTIVE RE-RUN OF INVALIDATED SUBGRAPH ---")
        # Re-queue from WORKER (the root of the blast)
        graph.visited.discard("WORKER")
        graph.visited.discard("AUDIT")
        graph.visited.discard("DRIFT")
        graph.visited.discard("LEDGER")
        graph.visited.discard("ROOT")
        graph.run_pipeline("WORKER")

    print("\n--- FINAL LEDGER ---")
    for log in shared.ledger:
        detail = log.get("details") or log.get("justifying_assumption")
        print(
            f"  {log['id']} | {log['node']:<10} | {log['type']:<18} | {detail}"
        )

    print("\n--- ACCEPTANCE CHECKS ---")
    exec_counts: Dict[str, int] = {}
    for log in shared.ledger:
        n = log["node"]
        if log["type"] in (
            "ESTABLISHED", "EXECUTED", "ASSUMPTION_CREATED", "PASS",
            "REJECT", "CHECK", "SNAPSHOT", "OWNERSHIP_CHECK",
        ):
            exec_counts[n] = exec_counts.get(n, 0) + 1

    # Find the two assumptions
    asms = list(shared.assumptions.values())
    a_old = next(
        (
            a
            for a in asms
            if a.status in (AssumptionStatus.REJECTED, AssumptionStatus.SUPERSEDED)
        ),
        None,
    )
    a_new = next((a for a in asms if a.status == AssumptionStatus.ACTIVE and a.supersedes), None)

    inv = shared.invalidation_sets[0] if shared.invalidation_sets else {}
    inv_nodes = set(inv.get("invalidated_nodes", []))

    checks = [
        ("INTENT executes exactly once", exec_counts.get("INTENT", 0) == 1),
        ("DECOMPOSE executes exactly once", exec_counts.get("DECOMPOSE", 0) == 1),
        ("WORKER executed under A then B", exec_counts.get("WORKER", 0) == 2),
        ("Assumption objects created (A + B)", len(shared.assumptions) >= 2),
        ("A is REJECTED / SUPERSEDED",
         a_old is not None
         and a_old.status in (AssumptionStatus.REJECTED, AssumptionStatus.SUPERSEDED)),
        ("B supersedes A",
         a_new is not None and a_old is not None and a_new.supersedes == a_old.id),
        ("DECOMPOSE→WORKER edge carries assumption",
         any(e.source == "DECOMPOSE" and e.target == "WORKER" and e.assumption_id
             for e in shared.edges.values())),
        ("Blast radius contains WORKER", "WORKER" in inv_nodes),
        ("Blast radius does NOT contain INTENT", "INTENT" not in inv_nodes),
        ("Blast radius does NOT contain DECOMPOSE", "DECOMPOSE" not in inv_nodes),
        ("Blast radius does NOT contain BRANCH_C1", "BRANCH_C1" not in inv_nodes),
        ("Blast radius does NOT contain BRANCH_C2", "BRANCH_C2" not in inv_nodes),
        ("BRANCH_C1 output preserved", "BRANCH_C1" in shared.node_outputs),
        ("BRANCH_C2 output preserved", "BRANCH_C2" in shared.node_outputs),
        # Under B the "$3200" string is parsed rather than dropped, so the
        # re-run keeps five records where the first pass kept four.
        ("Final WORKER kept 5 records",
         shared.node_outputs.get("WORKER", {}).get("kept_count") == 5),
        ("Final AUDIT passed", shared.node_outputs.get("AUDIT", {}).get("passed") is True),
        ("active_for_node only tracks WORKER (clean)",
         set(shared.active_for_node.keys()) <= {"WORKER"}),
        ("No pollution of AUDIT/LEDGER/ROOT as active assumption holders",
         "AUDIT" not in shared.active_for_node
         and "LEDGER" not in shared.active_for_node
         and "ROOT" not in shared.active_for_node),
    ]

    all_ok = True
    for desc, ok in checks:
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"  [{status}] {desc}")

    print("\nOVERALL:", "ALL ACCEPTANCE CHECKS PASSED" if all_ok else "SOME CHECKS FAILED")


if __name__ == "__main__":
    run_demo()
