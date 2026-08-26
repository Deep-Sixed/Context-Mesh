"""Read-only resource mirrors. No SDK imports, same as the tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from contextmesh.ontology import ONTOLOGY_FILE

from . import tools
from .session import Session

SCHEME = "contextmesh"

#: uri -> (name, description, mime type)
CATALOGUE: List[Dict[str, str]] = [
    {
        "uri": f"{SCHEME}://schema",
        "name": "GRAPH.md",
        "description": "The ontology file itself — the schema every write typechecks against.",
        "mime_type": "text/markdown",
    },
    {
        "uri": f"{SCHEME}://health",
        "name": "Graph health",
        "description": "Current health signals for the served graph.",
        "mime_type": "application/json",
    },
    {
        "uri": f"{SCHEME}://session",
        "name": "Session",
        "description": "What this server is serving, and the fact that it does not persist.",
        "mime_type": "application/json",
    },
    {
        "uri": f"{SCHEME}://assumptions",
        "name": "Assumptions",
        "description": "Every assumption with status, version and lineage links.",
        "mime_type": "application/json",
    },
]


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def schema_text() -> str:
    return Path(ONTOLOGY_FILE).read_text(encoding="utf-8")


def read(session: Session, uri: str) -> str:
    """Resolve a contextmesh:// URI to its content."""
    if uri == f"{SCHEME}://schema":
        return schema_text()
    if uri == f"{SCHEME}://health":
        return _json(tools.mesh_health(session))
    if uri == f"{SCHEME}://session":
        return _json(session.describe())
    if uri == f"{SCHEME}://assumptions":
        return _json(
            {
                "assumptions": [
                    a.to_dict() for a in session.graph.assumptions.values()
                ]
            }
        )
    prefix = f"{SCHEME}://node/"
    if uri.startswith(prefix):
        return _json(tools.mesh_get_node(session, uri[len(prefix):]))
    prefix = f"{SCHEME}://assumption/"
    if uri.startswith(prefix):
        assumption_id = uri[len(prefix):]
        payload = tools.mesh_lineage(session, assumption_id)
        payload["node"] = tools.mesh_get_node(session, assumption_id)
        return _json(payload)
    raise tools.MeshToolError(f"unknown resource {uri!r}")


def uris() -> List[str]:
    return [entry["uri"] for entry in CATALOGUE]


__all__ = ["CATALOGUE", "SCHEME", "read", "schema_text", "uris"]
