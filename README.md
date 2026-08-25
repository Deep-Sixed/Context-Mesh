# Context Mesh

**Claude Graph Engineering · Context Mesh**

> One graph · Four node types · Every answer walks a path you can read

Context Mesh is a typed context graph built from documents. It turns raw spans into a walkable knowledge structure so that answers are produced by following explicit, readable paths instead of opaque top-k retrieval.

## How a node earns a place in the graph

```
CHUNK → EXTRACT → RESOLVE → LINK → EMBED → PRUNE
```

| Stage | What happens |
|-------|--------------|
| **CHUNK** | Ingest spans from source material |
| **EXTRACT** | Pull entities and claims |
| **RESOLVE** | One ID per real-world thing |
| **LINK** | Create typed edges only |
| **EMBED** | Attach a vector to the node |
| **PRUNE** | Drop anything nobody walked |

Only nodes and edges that survive the pipeline (and are actually walked) remain in the live graph.

## Four node types

| Type | Role |
|------|------|
| **Entities** | Resolved real-world objects / concepts |
| **Claims** | Statements extracted from sources |
| **Sources** | Origin documents or spans |
| **Decisions** | Higher-level conclusions or choices |

Nodes cluster by type. A shared resolved ID becomes a typed edge.

## Core principles

- **A typed edge beats a top-k guess.**  
- **The graph is what survives into the next question.**  
- Answers are produced by walking readable paths, not by retrieving an unordered bag of chunks.

## What the live dashboard tracks

- **Hop budget** — depth required before an answer (median typically 4–6 hops)
- **Edge ledger** — traffic carried by Mentions, Derived-from, Cites, Contradicts
- **Walk vs Flat** — token cost of typed walks versus flat top-k retrieval (typically 91–97% reduction)
- **Traversal grid** — every walk recorded since the graph was built
- **Dead-end ledger** — walks that ended nowhere (no typed edge, unresolved entity, wrong node type, pruned too early)
- Graph health signals: unresolved entities, missing relationships, stale paths

## Design goal

Context Mesh complements document retrieval and agent memory. Retrieval systems supply source material; Context Mesh supplies the structured, typed relationships and the walkable paths that make answers auditable and efficient.

The graph remembers not only what is connected, but the typed relations that justify those connections.
