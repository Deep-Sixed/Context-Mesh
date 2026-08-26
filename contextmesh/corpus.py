"""A small, real corpus so the demo is a build, not a mock.

The documents are an engineering record: design notes, benchmark runs, incident
write-ups and review threads for a retrieval service. Real material is what
makes the graph interesting — contradictions, superseded decisions and an
assumption that turns out to be false all occur naturally in a record like this.
"""

from __future__ import annotations

from typing import List

from .pipeline import Document

DOCS: List[Document] = [
    Document(
        id="source:rfc-014",
        title="RFC 014 — retrieval layer for the agent runtime",
        origin="docs/rfc/014-retrieval.md",
        retrieved_at="2026-01-08",
        entities=["pgvector", "HNSW index", "Retrieval Service", "Agent Runtime", "recall at ten", "ninety-fifth percentile", "embedding column", "document span"],
        text=(
            "The Retrieval Service stores embeddings in pgvector behind the Agent Runtime. "
            "An HNSW index is built over the embedding column at write time. "
            "Recall at ten is the metric the team agreed to optimise. "
            "The service must answer within two hundred milliseconds at the ninety-fifth percentile. "
            "Every stored chunk keeps a pointer back to the document span it came from."
        ),
    ),
    Document(
        id="source:bench-2026-01",
        title="Benchmark run 2026-01 — recall and latency",
        origin="bench/2026-01/report.json",
        retrieved_at="2026-01-19",
        entities=["HNSW index", "IVFFlat index", "Retrieval Service", "recall at ten", "build time", "query latency", "resident memory"],
        text=(
            "The HNSW index reached recall of zero point ninety-one at ten on the evaluation set. "
            "The IVFFlat index reached recall of zero point eighty-two on the same set. "
            "HNSW build time was fourteen minutes against three minutes for IVFFlat. "
            "Query latency for HNSW was forty-one milliseconds at the ninety-fifth percentile. "
            "Memory resident size for the HNSW index was eleven gigabytes."
        ),
        relations=[
            ("The HNSW index reached recall", "contradicts", "The IVFFlat index reached recall"),
        ],
    ),
    Document(
        id="source:incident-221",
        title="Incident 221 — index rebuild exhausted memory",
        origin="incidents/221.md",
        retrieved_at="2026-02-03",
        entities=["HNSW index", "Retrieval Service", "Index Builder", "resident memory", "corpus size", "builder node", "Partitioned Rebuild"],
        text=(
            "The Index Builder was killed during a rebuild when resident memory passed sixteen gigabytes. "
            "The Retrieval Service served stale results for forty minutes. "
            "The rebuild assumed the corpus fits in the memory of one builder node. "
            "Corpus size had grown from nine million to twenty-six million chunks since the benchmark. "
            "A partitioned rebuild was proposed in review."
        ),
    ),
    Document(
        id="source:review-88",
        title="Review thread 88 — partitioned rebuild",
        origin="reviews/88.md",
        retrieved_at="2026-02-11",
        entities=["Index Builder", "Partitioned Rebuild", "HNSW index", "shard", "peak memory", "build time", "corpus size"],
        text=(
            "A Partitioned Rebuild splits the corpus into shards and builds each shard separately. "
            "Peak memory per shard measured two point one gigabytes. "
            "Total build time increased to thirty-one minutes across eight shards. "
            "The Index Builder no longer requires the whole corpus to be resident. "
            "Reviewers accepted the tradeoff of build time for bounded memory."
        ),
        relations=[
            ("The Index Builder no longer requires the whole corpus", "supersedes", "The rebuild assumed the corpus fits"),
        ],
    ),
    Document(
        id="source:embed-migration",
        title="Embedding model migration note",
        origin="docs/embeddings/migration.md",
        retrieved_at="2026-03-02",
        entities=["Embedding Model", "Retrieval Service", "pgvector", "v2 encoder", "v3 encoder", "keyword search", "re-embedding"],
        text=(
            "The Embedding Model was upgraded from the v2 encoder to the v3 encoder. "
            "Vectors produced by the v3 encoder are not comparable with v2 vectors. "
            "Every chunk in pgvector must be re-embedded before the switch. "
            "The Retrieval Service degraded to keyword search during the migration window. "
            "Re-embedding twenty-six million chunks took nine hours."
        ),
    ),
    Document(
        id="source:eval-rerank",
        title="Reranker evaluation",
        origin="bench/rerank/eval.md",
        retrieved_at="2026-03-14",
        entities=["Cross Encoder Reranker", "Retrieval Service", "HNSW index", "precision at five", "candidate set", "ninety-fifth percentile"],
        text=(
            "The Cross Encoder Reranker improved precision at five from zero point sixty-one to zero point seventy-nine. "
            "Reranking added ninety milliseconds to the ninety-fifth percentile latency. "
            "The Retrieval Service exceeded its two hundred millisecond budget under load with reranking on. "
            "Reranking only the top fifty candidates kept latency within budget. "
            "The HNSW index supplies the candidate set the reranker scores."
        ),
    ),
    Document(
        id="source:graph-note",
        title="Why the answers were not auditable",
        origin="docs/notes/auditability.md",
        retrieved_at="2026-04-06",
        entities=["Retrieval Service", "Context Mesh", "Agent Runtime", "top-k", "typed edge", "ranked list"],
        text=(
            "Answers from the Retrieval Service could not be traced back to a reason. "
            "The Agent Runtime received a ranked list with no relation between the items. "
            "Two chunks agreeing by coincidence looked identical to two chunks citing each other. "
            "Context Mesh was proposed to hold the relations that top-k discards. "
            "A typed edge records that both sides agree on the relation, not merely that they are similar."
        ),
        relations=[
            ("A typed edge records that both sides agree", "contradicts", "Two chunks agreeing by coincidence looked identical"),
        ],
    ),
    Document(
        id="source:mesh-design",
        title="Context Mesh design note",
        origin="docs/rfc/021-context-mesh.md",
        retrieved_at="2026-04-21",
        entities=["Context Mesh", "Agent Runtime", "Retrieval Service", "typed edge", "typed node", "assumption", "readable path"],
        text=(
            "Context Mesh stores entities, claims, sources and decisions as typed nodes. "
            "Relations between nodes are typed edges rather than similarity scores. "
            "The Agent Runtime asks Context Mesh for a path and receives the nodes on that path. "
            "Context Mesh does not replace the Retrieval Service; it consumes what retrieval returns. "
            "An assumption is a node, so the work standing on it can be found when it fails."
        ),
    ),
    Document(
        id="source:cost-review",
        title="Quarterly cost review",
        origin="finance/q1-review.md",
        retrieved_at="2026-04-28",
        entities=["Retrieval Service", "Agent Runtime", "Cross Encoder Reranker", "token spend", "retrieved context", "cost per answer"],
        text=(
            "Token spend for the Agent Runtime was dominated by retrieved context. "
            "The median answer carried one hundred and twenty thousand tokens of retrieved chunks. "
            "The Cross Encoder Reranker did not reduce the number of chunks sent downstream. "
            "Cutting context to the nodes on a path reduced the median answer to four thousand tokens. "
            "The Retrieval Service cost per answer fell by a factor of thirty."
        ),
    ),
    Document(
        id="source:drift-audit",
        title="Drift audit — stale claims in the index",
        origin="audits/drift-2026-05.md",
        retrieved_at="2026-05-12",
        entities=["Retrieval Service", "Embedding Model", "Context Mesh", "superseded decision", "stale claim", "similarity"],
        text=(
            "Fourteen percent of indexed claims referenced a decision that had been superseded. "
            "The Retrieval Service had no way to know a claim was stale. "
            "Context Mesh marks a superseded decision without deleting it. "
            "A stale claim remains readable but no longer supports a current decision. "
            "The Embedding Model cannot detect staleness because similarity does not encode time."
        ),
        relations=[
            ("Context Mesh marks a superseded decision", "cites", "Context Mesh stores entities, claims, sources and decisions"),
        ],
    ),
    Document(
        id="source:oncall-notes",
        title="On-call notes — partial invalidation",
        origin="ops/oncall/2026-05.md",
        retrieved_at="2026-05-30",
        entities=["Index Builder", "Partitioned Rebuild", "Retrieval Service", "shard", "partial invalidation", "recovery time"],
        text=(
            "A bad shard forced a rebuild of one partition rather than the whole index. "
            "The Partitioned Rebuild made partial invalidation possible for the first time. "
            "Unaffected shards continued serving during the rebuild. "
            "The Index Builder recorded which shard each chunk belonged to. "
            "Recovery time fell from forty minutes to four minutes."
        ),
        relations=[
            ("The Partitioned Rebuild made partial invalidation possible", "cites", "A Partitioned Rebuild splits the corpus into shards"),
        ],
    ),
    Document(
        id="source:capacity-model",
        title="Capacity model for the builder fleet",
        origin="docs/capacity/builder.md",
        retrieved_at="2026-06-09",
        entities=["Index Builder", "Partitioned Rebuild", "builder node", "shard", "region", "capacity model"],
        text=(
            "Each builder node provides sixteen gigabytes of usable memory. "
            "A shard is sized so that peak build memory stays under four gigabytes. "
            "The fleet runs eight builder nodes in the current region. "
            "Adding a region requires re-sharding because shard boundaries are region local. "
            "The capacity model assumes shard count grows linearly with corpus size."
        ),
    ),
    Document(
        id="source:postmortem-233",
        title="Postmortem 233 — the linear sharding assumption",
        origin="incidents/233.md",
        retrieved_at="2026-07-02",
        entities=["Partitioned Rebuild", "Index Builder", "Retrieval Service", "shard", "tenant", "sizing rule", "corpus size"],
        text=(
            "Shard count did not grow linearly with corpus size once documents clustered by tenant. "
            "One tenant accounted for thirty-one percent of all chunks in a single shard. "
            "That shard exceeded four gigabytes during rebuild and the Index Builder was killed again. "
            "The Partitioned Rebuild is sound but the sizing rule was wrong. "
            "Sharding must key on tenant as well as corpus position."
        ),
        relations=[
            ("Shard count did not grow linearly", "contradicts", "The capacity model assumes shard count grows linearly"),
            ("Sharding must key on tenant", "supersedes", "A shard is sized so that peak build memory"),
        ],
    ),
    Document(
        id="source:mesh-eval",
        title="Context Mesh evaluation against flat retrieval",
        origin="bench/mesh/eval.md",
        retrieved_at="2026-07-24",
        entities=["Context Mesh", "Retrieval Service", "Agent Runtime", "readable path", "audit question", "path depth", "dead end"],
        text=(
            "Context Mesh answered ninety-one of one hundred audit questions with a readable path. "
            "The Retrieval Service answered the same questions with no path in every case. "
            "Median path depth was five hops across the answered questions. "
            "Nine questions ended in a dead end and each dead end named its reason. "
            "The Agent Runtime preferred the path answers in blind review."
        ),
        relations=[
            ("The Retrieval Service answered the same questions with no path", "cites", "Answers from the Retrieval Service could not be traced"),
        ],
    ),
]


def documents() -> List[Document]:
    """The corpus, fresh each call."""
    return list(DOCS)
