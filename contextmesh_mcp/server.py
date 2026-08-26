"""Context Mesh MCP server — stdio transport.

    pip install 'contextmesh[mcp]'
    contextmesh-mcp --demo --rounds 8 --save ./session   # write one
    contextmesh-mcp --session ./session                  # serve it

Requires Python 3.10+, because the MCP SDK does. Writing and inspecting a
session needs neither the SDK nor 3.10, which is why that lives in
``session.py`` and is reachable as ``python -m contextmesh_mcp``. Everything the tools actually
do lives in ``tools.py`` and ``resources.py``, which do not, so the behaviour
this server exposes is tested without the SDK on every version the core
supports. This file is transport and nothing else.

Read-only by construction: the five tools registered below are the five in
``tools.TOOLS``, none of which mutate structure or belief. There is no code path
here that adds a node, adds an edge, rejects an assumption, repairs or executes.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional, Sequence

try:
    from mcp.server import MCPServer
except ImportError as exc:  # pragma: no cover - depends on the optional extra
    raise SystemExit(
        "the MCP SDK is not installed. Install the extra:\n"
        "    pip install 'contextmesh[mcp]'\n"
        "It needs Python 3.10 or newer; contextmesh itself supports 3.9.\n"
        f"(import failed: {exc})"
    ) from exc

from . import resources, tools
from .session import SessionError, add_source_arguments, adopt, open_session, session

SERVER_NAME = "context-mesh"
INSTRUCTIONS = """\
Context Mesh is a typed context graph: entities, claims, sources, decisions,
assumptions and evidence, connected by typed edges that record why they are
connected.

Ask it questions with mesh_ask. An answer comes back as a path you can read,
with the evidence on it — or as one of four typed dead-end reasons rather than a
guess, so "the graph cannot answer this" is a distinguishable outcome.

mesh_blast_radius is a dry run: it tells you what would fall if an assumption
turned out to be false, and what would survive. It changes nothing. This server
is read-only; it cannot reject an assumption, repair, or execute.

This instance may be serving a saved session directory or a demo graph rebuilt
for this process. contextmesh://session says which, and whether anything you see
here outlives the server.

Independent project. No affiliation with, or endorsement by, anyone involved in
the screen capture the dashboard was reverse-engineered from.
"""

mcp = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)


def _payload(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)


def _run(name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
    try:
        return _payload(tools.call(session(), name, arguments))
    except tools.MeshToolError as exc:
        return _payload({"error": "not_found", "tool": name, "message": str(exc)})
    except Exception as exc:  # surface engine errors as data, not a dead channel
        return _payload(
            {"error": type(exc).__name__, "tool": name, "message": str(exc)}
        )


# ── tools ────────────────────────────────────────────────────────────────
@mcp.tool(name="mesh_ask", description=tools.TOOLS["mesh_ask"]["description"])
def mesh_ask(question: str) -> str:
    return _run("mesh_ask", {"question": question})


@mcp.tool(name="mesh_get_node", description=tools.TOOLS["mesh_get_node"]["description"])
def mesh_get_node(node_id: str) -> str:
    return _run("mesh_get_node", {"node_id": node_id})


@mcp.tool(name="mesh_health", description=tools.TOOLS["mesh_health"]["description"])
def mesh_health() -> str:
    return _run("mesh_health", {})


@mcp.tool(name="mesh_lineage", description=tools.TOOLS["mesh_lineage"]["description"])
def mesh_lineage(assumption_id: str) -> str:
    return _run("mesh_lineage", {"assumption_id": assumption_id})


@mcp.tool(
    name="mesh_blast_radius",
    description=tools.TOOLS["mesh_blast_radius"]["description"],
)
def mesh_blast_radius(assumption_id: str) -> str:
    return _run("mesh_blast_radius", {"assumption_id": assumption_id})


# ── resources ────────────────────────────────────────────────────────────
@mcp.resource("contextmesh://schema", mime_type="text/markdown")
def resource_schema() -> str:
    return resources.schema_text()


@mcp.resource("contextmesh://health", mime_type="application/json")
def resource_health() -> str:
    return resources.read(session(), "contextmesh://health")


@mcp.resource("contextmesh://session", mime_type="application/json")
def resource_session() -> str:
    return resources.read(session(), "contextmesh://session")


@mcp.resource("contextmesh://assumptions", mime_type="application/json")
def resource_assumptions() -> str:
    return resources.read(session(), "contextmesh://assumptions")


@mcp.resource("contextmesh://node/{node_id}", mime_type="application/json")
def resource_node(node_id: str) -> str:
    return resources.read(session(), f"contextmesh://node/{node_id}")


@mcp.resource("contextmesh://assumption/{assumption_id}", mime_type="application/json")
def resource_assumption(assumption_id: str) -> str:
    return resources.read(session(), f"contextmesh://assumption/{assumption_id}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = add_source_arguments(
        argparse.ArgumentParser(
            prog="contextmesh-mcp",
            description="Read-only MCP server over a Context Mesh graph.",
            epilog=(
                "contextmesh-mcp --demo --rounds 8 --save ./session   write one\n"
                "contextmesh-mcp --session ./session                  serve it"
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
    )
    args = parser.parse_args(argv)

    # Build or load before serving, so the first tool call is not the slow one
    # and a session this build cannot restore fails at the shell rather than
    # halfway through a client's first question.
    try:
        opened = open_session(args)
    except SessionError as exc:
        print(f"contextmesh-mcp: {exc}", file=sys.stderr)
        return 2

    if args.save is not None:
        target = opened.save(args.save)
        print(f"contextmesh-mcp: wrote {target}", file=sys.stderr)
        return 0

    adopt(opened)
    described = opened.describe()
    print(
        f"context-mesh MCP: {described['nodes_live']}/{described['nodes_total']} nodes live, "
        f"{described['edges_live']}/{described['edges_total']} edges live, "
        f"read-only, {'from ' + str(opened.path) if described['persistent'] else 'not persistent'}",
        file=sys.stderr,
    )
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
