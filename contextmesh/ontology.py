"""The ontology is a file, not a constant.

``GRAPH.md`` at the repository root is the schema. This module parses it on
import so that a change to the documented edge table is immediately a change to
what the engine will accept. That is the whole point of the "read on every
write" line in the dashboard: the ontology cannot drift away from the docs
because there is only one copy of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, Tuple

ONTOLOGY_FILE = Path(__file__).resolve().parent.parent / "GRAPH.md"

_ROW = re.compile(r"^\|(?P<cells>.+)\|\s*$")
_PAIR = re.compile(r"(?P<src>[a-z_]+)\s*(?:→|->)\s*(?P<dst>[a-z_]+)")


class OntologyError(Exception):
    """Raised when a write does not typecheck against GRAPH.md."""


@dataclass(frozen=True)
class Ontology:
    node_types: FrozenSet[str]
    edge_types: FrozenSet[str]
    #: edge type -> set of legal (source node type, target node type) pairs
    signatures: Dict[str, FrozenSet[Tuple[str, str]]]
    #: edge types whose failure propagates during selective invalidation
    propagating: FrozenSet[str] = frozenset({"depends_on", "derived_from", "produces"})

    def check_edge(self, edge_type: str, src_type: str, dst_type: str) -> None:
        if edge_type not in self.edge_types:
            raise OntologyError(
                f"no such edge type {edge_type!r}; GRAPH.md defines "
                f"{sorted(self.edge_types)}"
            )
        legal = self.signatures[edge_type]
        if (src_type, dst_type) not in legal:
            pretty = ", ".join(f"{a}->{b}" for a, b in sorted(legal))
            raise OntologyError(
                f"{src_type}-[{edge_type}]->{dst_type} is not a legal pair; "
                f"GRAPH.md allows {pretty}"
            )

    def check_node(self, node_type: str) -> None:
        if node_type not in self.node_types:
            raise OntologyError(
                f"no such node type {node_type!r}; GRAPH.md defines "
                f"{sorted(self.node_types)}"
            )


def _cells(line: str) -> list[str] | None:
    m = _ROW.match(line.strip())
    if not m:
        return None
    return [c.strip() for c in m.group("cells").split("|")]


def _is_rule_row(cells: Iterable[str]) -> bool:
    return all(set(c) <= set("-: ") and c for c in cells)


def load(path: Path | str = ONTOLOGY_FILE) -> Ontology:
    """Parse the node-type and edge-type tables out of GRAPH.md."""
    text = Path(path).read_text(encoding="utf-8")
    section = None
    node_types: set[str] = set()
    signatures: Dict[str, FrozenSet[Tuple[str, str]]] = {}

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip().lower()
            continue
        cells = _cells(line)
        if not cells or _is_rule_row(cells):
            continue
        head = cells[0].strip("`").lower()
        if section == "node types":
            if head in {"type", ""}:
                continue
            node_types.add(head)
        elif section == "edge types":
            if head in {"edge", ""}:
                continue
            pairs = frozenset(
                (m.group("src"), m.group("dst")) for m in _PAIR.finditer(cells[1])
            )
            if pairs:
                signatures[head] = pairs

    if not node_types or not signatures:
        raise OntologyError(f"{path} did not yield a usable ontology")

    unknown = {
        t
        for pairs in signatures.values()
        for pair in pairs
        for t in pair
        if t not in node_types
    }
    if unknown:
        raise OntologyError(f"edge table references undeclared node types: {sorted(unknown)}")

    return Ontology(
        node_types=frozenset(node_types),
        edge_types=frozenset(signatures),
        signatures=signatures,
    )


#: The live ontology. Imported by the graph, the pipeline and the linter.
ONTOLOGY = load()
