"""The worked example: build a graph, decide things on it, walk it, then break
an assumption and watch exactly the right work fall over.

`python -m contextmesh demo` runs this end to end and prints the readable
version. `python -m contextmesh export` runs it and writes the dashboard's data.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .assumptions import AssumptionLedger, InvalidationReport
from .corpus import documents
from .decisions import DecisionLog
from .graph import ContextGraph
from .metrics import snapshot
from .model import EdgeType, NodeType, Provenance
from .pipeline import BuildReport, Pipeline
from .resolve import Resolver
from .traverse import Walker

QUESTIONS: Tuple[str, ...] = (
    "Why did the Index Builder run out of memory?",
    "What does the Partitioned Rebuild change about recovery?",
    "Which index did the benchmark prefer, HNSW or IVFFlat?",
    "What did the Cross Encoder Reranker cost in latency?",
    "Why was the Embedding Model migration expensive?",
    "What does Context Mesh add that the Retrieval Service cannot?",
    "How much did token spend fall per answer?",
    "What made the sharding rule wrong?",
    "Which decision superseded the single node rebuild?",
    "What does pgvector store?",
    "Why were answers not auditable before?",
    "What is the latency budget for the Retrieval Service?",
    "How stale were the indexed claims?",
    "What does the capacity model assume about shard count?",
    "Which tenant dominated a shard?",
    "What did the Agent Runtime prefer in blind review?",
    "How deep is the median path?",
    "What happens to a superseded decision?",
    "Did reranking stay inside the latency budget?",
    "What did the drift audit find?",
    "How does a shard get sized?",
    "What is the recall of the HNSW index?",
    "Why can similarity not detect staleness?",
    "What did on-call learn about partial invalidation?",
)


#: Templates asked of every entity in the graph, so the walk ledger reflects the
#: shape of the corpus rather than one hand-picked question list.
TEMPLATES: Tuple[str, ...] = (
    "What do we know about {name}?",
    "Why does {name} matter?",
    "What decision produced {name}?",
    "What supports {name}?",
    "Where did {name} come from?",
    "What contradicts {name}?",
    "What depends on {name}?",
    "What changed about {name}?",
)

#: Questions the corpus genuinely cannot answer. A graph that answers these is
#: lying, so they stay in the mix and are expected to dead-end.
OUT_OF_SCOPE: Tuple[str, ...] = (
    "Who is responsible for the quarterly margin target?",
    "What is the refund policy for annual plans?",
    "Which vendor signed the data processing addendum?",
    "When does the office lease expire?",
    "What is the on-call rotation for the payroll team?",
)


def questions(graph: ContextGraph, count: int) -> List[str]:
    """A deterministic, varied question stream for the walk ledger."""
    names = [n.label for n in graph.by_type(NodeType.ENTITY)]
    pool: List[str] = list(QUESTIONS)
    for template in TEMPLATES:
        for name in names:
            pool.append(template.format(name=name))
    pool.extend(OUT_OF_SCOPE)

    # Deterministic, but shuffled per cycle: a fixed stride looks varied until
    # the stride shares a factor with the pool size, and then silently asks the
    # same sixteen questions forever.
    rng = random.Random(20260825)
    stream: List[str] = []
    while len(stream) < count:
        cycle = list(pool)
        rng.shuffle(cycle)
        stream.extend(cycle)
    return stream[:count]


@dataclass
class DemoResult:
    graph: ContextGraph
    resolver: Resolver
    walker: Walker
    build: BuildReport
    decisions: DecisionLog
    ledger: AssumptionLedger
    invalidation: InvalidationReport
    rounds: int

    def payload(self) -> Dict[str, Any]:
        return snapshot(
            graph=self.graph,
            walker=self.walker,
            resolver=self.resolver,
            build=self.build,
            decisions=self.decisions,
            ledger=self.ledger,
            invalidation=self.invalidation,
        )


def _claims_mentioning(graph: ContextGraph, entity_label: str, limit: int = 3) -> List[str]:
    """Claim ids that mention an entity, busiest first."""
    target = None
    for node in graph.by_type(NodeType.ENTITY):
        if node.label.lower() == entity_label.lower():
            target = node
            break
    if target is None:
        return []
    claims = [
        edge.src
        for edge in graph.in_edges(target.id, (EdgeType.MENTIONS,))
        if graph.node(edge.src).type is NodeType.CLAIM
    ]
    claims.sort(key=lambda cid: graph.degree(cid), reverse=True)
    return claims[:limit]


def _entity_id(graph: ContextGraph, label: str) -> Optional[str]:
    for node in graph.by_type(NodeType.ENTITY):
        if node.label.lower() == label.lower():
            return node.id
    return None


def run(*, rounds: int = 40, reject: bool = True) -> DemoResult:
    """Build, decide, walk, and (optionally) break the sharding assumption."""
    graph = ContextGraph()
    resolver = Resolver()
    pipeline = Pipeline(graph, resolver)
    build = pipeline.build(documents())

    ledger = AssumptionLedger(graph)
    decisions = DecisionLog(graph)

    # ── assumptions the work stands on ───────────────────────────────────
    a_memory = ledger.assume(
        "The corpus fits in the memory of one builder node",
        created_by="index-team",
    )
    a_linear = ledger.assume(
        "Shard count grows linearly with corpus size",
        created_by="capacity-model",
    )
    a_typed = ledger.assume(
        "Typed edges can be produced at write time without a human in the loop",
        created_by="mesh-team",
    )

    # ── decisions, each with its reasoning and its ground ────────────────
    d_hnsw = decisions.decide(
        "Index with HNSW rather than IVFFlat",
        "HNSW gave 0.91 recall@10 against 0.82 for IVFFlat, and 41ms p95 query "
        "latency leaves room inside the 200ms budget.",
        source_id="source:bench-2026-01",
        supported_by=_claims_mentioning(graph, "HNSW index", 3),
        assumptions=[a_memory.id],
        produces=[_entity_id(graph, "HNSW index")] if _entity_id(graph, "HNSW index") else [],
    )

    decisions.decide(
        "Rebuild the index in partitions",
        "A single-node rebuild died at 16GB once the corpus reached 26M chunks. "
        "Per-shard peak memory is 2.1GB, at the cost of 31 minutes total build.",
        source_id="source:review-88",
        supported_by=_claims_mentioning(graph, "Partitioned Rebuild", 3),
        cites=["source:incident-221"],
        assumptions=[a_linear.id],
        produces=[_entity_id(graph, "Partitioned Rebuild")]
        if _entity_id(graph, "Partitioned Rebuild")
        else [],
        supersedes=d_hnsw.id,
    )

    decisions.decide(
        "Rerank only the top fifty candidates",
        "Full reranking pushed p95 past the 200ms budget; capping the candidate "
        "set keeps precision@5 at 0.79 inside budget.",
        source_id="source:eval-rerank",
        supported_by=_claims_mentioning(graph, "Cross Encoder Reranker", 2),
    )

    decisions.decide(
        "Adopt Context Mesh for provenance",
        "Flat top-k could not say why two chunks belonged together. A typed edge "
        "records agreement, not coincidence, and cuts context per answer 30x.",
        source_id="source:mesh-design",
        supported_by=_claims_mentioning(graph, "Context Mesh", 3),
        cites=["source:graph-note", "source:cost-review"],
        assumptions=[a_typed.id],
        produces=[_entity_id(graph, "Context Mesh")]
        if _entity_id(graph, "Context Mesh")
        else [],
    )

    # Re-embed and re-link the nodes the decision log just added.
    from .embed import embed as _embed

    for node in graph.nodes.values():
        if node.embedding is None:
            node.embedding = _embed(f"{node.label} {node.attrs.get('rationale', '')}")

    # ── walk, break the assumption, then keep walking ────────────────────
    # The second half of the run happens after the rejection on purpose: a
    # question that used to answer now walks into the hole, and the dead-end
    # ledger picks up the reasons that only exist once something has been
    # invalidated.
    walker = Walker(graph, resolver, hop_budget=6)
    stream = questions(graph, rounds * len(QUESTIONS))
    split = len(stream) // 2 if reject else len(stream)
    for question in stream[:split]:
        walker.walk(question)

    invalidation: Optional[InvalidationReport] = None
    if reject:
        evidence = graph.add_node(
            NodeType.EVIDENCE,
            "Postmortem 233: one tenant held 31% of chunks in a single shard, "
            "which exceeded the 4GB per-shard build ceiling",
            attrs={"kind": "postmortem"},
            provenance=Provenance(
                source_id="source:postmortem-233",
                extractor="incident-review",
                checks=["reproduced", "signed-off"],
                recorded_at_build=graph.build,
            ),
        )
        invalidation = ledger.reject(
            a_linear.id,
            evidence_id=evidence.id,
            replacement="Shard count grows with corpus size and tenant skew; "
            "shards are keyed on tenant as well as corpus position",
        )

    for question in stream[split:]:
        walker.walk(question)

    return DemoResult(
        graph=graph,
        resolver=resolver,
        walker=walker,
        build=build,
        decisions=decisions,
        ledger=ledger,
        invalidation=invalidation,
        rounds=rounds,
    )
