"""The seven read tools, as plain functions over the engine.

Nothing here imports the MCP SDK. Each returns a JSON-ready dict, so the tools
can be exercised — and their safety asserted — without a protocol, a transport,
or a dependency.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from contextmesh.health import report as health_report
from contextmesh.model import AssumptionStatus, EdgeType, NodeType
from contextmesh.reconstruct import explain_as_of, reconstruct_decision
from contextmesh.temporal import TemporalError

from .session import Session


class MeshToolError(Exception):
    """A tool was asked for something the graph does not contain."""


def _node_ref(session: Session, node_id: str) -> Dict[str, Any]:
    node = session.graph.get(node_id)
    if node is None:
        return {"id": node_id, "type": None, "label": None, "missing": True}
    return {
        "id": node.id,
        "type": node.type.value,
        "label": node.label,
        "live": node.live,
    }


def _require_node(session: Session, node_id: str):
    node = session.graph.get(node_id)
    if node is None:
        raise MeshToolError(f"no node {node_id!r} in the graph")
    return node


def _require_assumption(session: Session, assumption_id: str):
    assumption = session.graph.assumptions.get(assumption_id)
    if assumption is None:
        known = sorted(session.graph.assumptions)[:5]
        raise MeshToolError(
            f"no assumption {assumption_id!r}; the graph has "
            f"{len(session.graph.assumptions)} (e.g. {known})"
        )
    return assumption


# ── tools ────────────────────────────────────────────────────────────────
def mesh_ask(session: Session, question: str) -> Dict[str, Any]:
    """Ask the graph. Returns the evidence path, or a typed dead-end reason.

    This moves walk telemetry — ``node.walks`` and ``edge.traversals`` — by
    design: PRUNE drops what nothing walked, so a question that is never asked
    is information the graph is entitled to act on. Structure and belief are
    untouched.
    """
    if not question or not question.strip():
        raise MeshToolError("question is empty")
    walk = session.walker.ask(question)
    payload = walk.to_dict()
    if walk.resolved and walk.answer_id:
        payload["answer"] = session.graph.node(walk.answer_id).label
    payload["evidence_labels"] = [
        session.graph.node(e).label for e in walk.evidence if session.graph.get(e)
    ]
    return payload


def mesh_get_node(session: Session, node_id: str) -> Dict[str, Any]:
    """One node with its typed edges, including edges into invalidated work.

    ``live_only=False`` on purpose: an inspection tool that hides invalidated
    edges cannot answer "why is this node dead", which is most of what someone
    inspects a node for.
    """
    node = _require_node(session, node_id)
    out_edges = [
        {
            "type": e.type.value,
            "dst": e.dst,
            "dst_label": _node_ref(session, e.dst)["label"],
            "weight": e.weight,
            "assumption_id": e.assumption_id,
            "invalidated": e.invalidated,
        }
        for e in session.graph.out_edges(node_id, live_only=False)
    ]
    in_edges = [
        {
            "type": e.type.value,
            "src": e.src,
            "src_label": _node_ref(session, e.src)["label"],
            "weight": e.weight,
            "assumption_id": e.assumption_id,
            "invalidated": e.invalidated,
        }
        for e in session.graph.in_edges(node_id, live_only=False)
    ]
    payload = node.to_dict()
    payload.update(
        {
            "live": node.live,
            "degree": session.graph.degree(node_id),
            "out_edges": out_edges,
            "in_edges": in_edges,
        }
    )
    if node.type is NodeType.ASSUMPTION:
        assumption = session.graph.assumptions.get(node_id)
        if assumption is not None:
            payload["assumption"] = assumption.to_dict()
    return payload


def mesh_health(session: Session) -> Dict[str, Any]:
    """Graph health: the states a long-running graph drifts into."""
    return health_report(session.graph, session.resolver, session.walker)


def mesh_lineage(session: Session, assumption_id: str) -> Dict[str, Any]:
    """What the graph used to believe, oldest first.

    Follows ``supersedes`` back to the original. Nothing is deleted, so a
    rejected assumption is still here with the reason it fell.
    """
    _require_assumption(session, assumption_id)
    chain = session.ledger.lineage(assumption_id)
    return {
        "assumption_id": assumption_id,
        "depth": len(chain),
        "chain": [a.to_dict() for a in chain],
    }


def mesh_blast_radius(session: Session, assumption_id: str) -> Dict[str, Any]:
    """What *would* fall if this assumption turned out to be false.

    A dry run. It computes the closure and the preserved complement without
    rejecting anything: the assumption keeps its status, no evidence node is
    created, no ``contradicts`` edge is added, nothing is invalidated.

    Deciding an assumption *is* false is not offered over MCP. Under GRAPH.md
    rule 7 that takes evidence contradicting it, produced by an auditor looking
    at the world — a tool that let a caller name an assumption and have it
    rejected would turn the rule into a convention.
    """
    assumption = _require_assumption(session, assumption_id)
    radius = session.ledger.blast_radius(assumption_id)
    preserved = sorted(
        n.id
        for n in session.graph.nodes.values()
        if n.id not in radius and n.id != assumption_id and n.live
    )
    # A rejected assumption reports a radius of 0, because the walk follows live
    # edges and its dependants are already invalidated. That is right, and it
    # reads exactly like "nothing depends on this" unless the answer says so.
    note = None
    if assumption.status is AssumptionStatus.REJECTED:
        note = (
            "This assumption is already rejected, so its fallout has happened "
            "and the live graph no longer depends on it. A radius of 0 here "
            "means the work already fell, not that nothing rested on it."
        )
    elif assumption.status is AssumptionStatus.SUPERSEDED:
        note = (
            f"Superseded by {assumption.superseded_by}. Work has been re-grounded "
            "on the replacement, so this radius covers only what still points here."
        )

    return {
        "assumption_id": assumption_id,
        "statement": assumption.statement,
        "status": assumption.status.value,
        "note": note,
        "hypothetical": True,
        "blast_radius": len(radius),
        "would_invalidate": [
            {
                "id": node_id,
                "label": _node_ref(session, node_id)["label"],
                "type": _node_ref(session, node_id)["type"],
                "because": " → ".join(chain),
            }
            for node_id, chain in sorted(radius.items())
        ],
        "would_preserve_count": len(preserved),
        "would_preserve": preserved,
    }


#: Name -> callable and description. No JSON schema here on purpose: the server
#: registers each tool as a typed function and the SDK derives the published
#: schema from that signature, so a hand-written copy alongside it would be
#: documentation shaped like a contract and free to drift from the real one.
#: ``tests/test_mcp.py`` asserts the published schema against these signatures.
def mesh_explain_as_of(session: Session, question: str, as_of: str) -> Dict[str, Any]:
    """Answer a question from the graph as it stood, not from today's graph.

    Every temporal rule lives in :mod:`contextmesh.reconstruct`. This marshals
    arguments in and a dict out, and that is the whole of it: a wrapper that
    started deciding what counts as contemporary would be a second answer to a
    question the engine already answers, and the two would drift.

    ``TemporalError`` is translated rather than propagated so a caller who
    sends ``"June 2026"`` gets the tool error every other bad argument gets.
    """
    if not question or not question.strip():
        raise MeshToolError("question is empty")
    try:
        return explain_as_of(session.graph, session.resolver, question, as_of).to_dict()
    except TemporalError as exc:
        raise MeshToolError(str(exc)) from None


def mesh_reconstruct_decision(
    session: Session, decision_id: str, as_of: str, depth: int = 3
) -> Dict[str, Any]:
    """Why a decision was made, and what happened to its reasons afterwards.

    Read-only in the strict sense: unlike :func:`mesh_ask` this moves no walk
    telemetry either, because it does not walk — it follows the typed edges a
    decision already carries.

    Every refusal here is the engine's, restated in this layer's vocabulary.
    Whether an id names a decision is a fact about the graph, so checking it
    here as well would put the rule in two places and leave every direct Python
    caller — the engine's whole other audience — unguarded.
    """
    try:
        history = reconstruct_decision(session.graph, decision_id, as_of, depth=depth)
    except (KeyError, ValueError) as exc:
        # KeyError stringifies as the repr of its argument, so read the message
        # off args. TemporalError is a ValueError, so a loose date lands here
        # too, and the caller sees one error type for every bad argument.
        raise MeshToolError(str(exc.args[0]) if exc.args else str(exc)) from None
    return history.to_dict()


TOOLS: Dict[str, Dict[str, Any]] = {
    "mesh_ask": {
        "fn": mesh_ask,
        "description": (
            "Ask the Context Mesh a question. Returns a readable evidence path, "
            "hop count, and token cost against flat top-k retrieval — or one of "
            "four typed dead-end reasons rather than a guess."
        ),
    },
    "mesh_get_node": {
        "fn": mesh_get_node,
        "description": (
            "Fetch one node with its typed in and out edges, including "
            "invalidated ones."
        ),
    },
    "mesh_health": {
        "fn": mesh_health,
        "description": (
            "Graph health signals: untyped edges, unresolved entities, missing "
            "relationships, open contradictions, dead ends, invalidated work."
        ),
    },
    "mesh_lineage": {
        "fn": mesh_lineage,
        "description": (
            "The version history of an assumption, oldest first: what the graph "
            "used to believe and what replaced it."
        ),
    },
    "mesh_blast_radius": {
        "fn": mesh_blast_radius,
        "description": (
            "Dry run: what would be invalidated, and what would be preserved, if "
            "this assumption turned out to be false. Changes nothing."
        ),
    },
    "mesh_explain_as_of": {
        "fn": mesh_explain_as_of,
        "description": (
            "Answer a question from the graph as it stood on a past date "
            "(YYYY-MM-DD), walking a real projection rather than filtering "
            "today's answer. Splits what was known then, what was decided, what "
            "arrived later, and what carries no date at all."
        ),
    },
    "mesh_reconstruct_decision": {
        "fn": mesh_reconstruct_decision,
        "description": (
            "Walk back from one decision to the reasons it stood on, judged "
            "against the decision's own date rather than today's. Reports "
            "separately what came afterwards to contradict or supersede it."
        ),
    },
}


def call(session: Session, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Dispatch by name. Used by the server and by the tests."""
    entry = TOOLS.get(name)
    if entry is None:
        raise MeshToolError(f"unknown tool {name!r}; have {sorted(TOOLS)}")
    return entry["fn"](session, **(arguments or {}))


def names() -> List[str]:
    return sorted(TOOLS)


__all__ = [
    "TOOLS",
    "EdgeType",
    "MeshToolError",
    "call",
    "mesh_ask",
    "mesh_blast_radius",
    "mesh_explain_as_of",
    "mesh_get_node",
    "mesh_health",
    "mesh_lineage",
    "mesh_reconstruct_decision",
    "names",
]
