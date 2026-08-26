"""ContextMesh — a graph that remembers why things are connected.

    from contextmesh import Pipeline, Walker, Resolver, documents

    pipeline = Pipeline()
    pipeline.build(documents())
    walker = Walker(pipeline.graph, pipeline.resolver)
    print(walker.ask("Why did the Index Builder run out of memory?").path())

The pieces:

- ``ontology``    GRAPH.md parsed into the schema every write is checked against
- ``graph``       typed nodes and typed edges, with no untyped code path
- ``resolve``     entity resolution — one id per real-world thing
- ``pipeline``    CHUNK → EXTRACT → RESOLVE → LINK → EMBED → PRUNE
- ``traverse``    walks that return the evidence path, and classify the failures
- ``assumptions`` versioned assumptions and selective invalidation
- ``decisions``   append-only decision history
- ``health``      the conditions that quietly make a graph useless
- ``metrics``     the dashboard payload, computed from the live graph
"""

from .assumptions import AssumptionLedger, InvalidationReport
from .corpus import documents
from .decisions import DecisionLog, DecisionRecord
from .graph import ContextGraph
from .health import Signal, check, report
from .metrics import snapshot
from .model import (
    Assumption,
    AssumptionStatus,
    Edge,
    EdgeType,
    Node,
    NodeType,
    Provenance,
)
from .ontology import ONTOLOGY, Ontology, OntologyError
from .pipeline import BuildReport, Document, Pipeline
from .resolve import Resolver, ResolutionRecord
from .traverse import DeadEnd, Walk, Walker

__version__ = "0.1.0"

__all__ = [
    "ONTOLOGY",
    "Assumption",
    "AssumptionLedger",
    "AssumptionStatus",
    "BuildReport",
    "ContextGraph",
    "DeadEnd",
    "DecisionLog",
    "DecisionRecord",
    "Document",
    "Edge",
    "EdgeType",
    "InvalidationReport",
    "Node",
    "NodeType",
    "Ontology",
    "OntologyError",
    "Pipeline",
    "Provenance",
    "ResolutionRecord",
    "Resolver",
    "Signal",
    "Walk",
    "Walker",
    "check",
    "documents",
    "report",
    "snapshot",
]
