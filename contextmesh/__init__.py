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
- ``execute``     re-runs exactly the work an invalidation knocked down
- ``decisions``   append-only decision history
- ``health``      the conditions that quietly make a graph useless
- ``metrics``     the dashboard payload, computed from the live graph
"""

from .assumptions import AssumptionLedger, InvalidationReport
from .corpus import documents
from .decisions import DecisionLog, DecisionRecord
from .execute import (
    AuditContext,
    ExecutionError,
    RunContext,
    RunLedger,
    Runner,
    RunReport,
    Task,
    TaskState,
    Verdict,
)
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
from .resolve import ResolutionRecord, Resolver
from .traverse import DeadEnd, Walk, Walker

__version__ = "0.1.0"

__all__ = [
    "ONTOLOGY",
    "Assumption",
    "AssumptionLedger",
    "AssumptionStatus",
    "AuditContext",
    "BuildReport",
    "ContextGraph",
    "DeadEnd",
    "DecisionLog",
    "DecisionRecord",
    "Document",
    "Edge",
    "EdgeType",
    "ExecutionError",
    "InvalidationReport",
    "Node",
    "NodeType",
    "Ontology",
    "OntologyError",
    "Pipeline",
    "Provenance",
    "ResolutionRecord",
    "Resolver",
    "RunContext",
    "RunLedger",
    "RunReport",
    "Runner",
    "Signal",
    "Task",
    "TaskState",
    "Verdict",
    "Walk",
    "Walker",
    "check",
    "documents",
    "report",
    "snapshot",
]
