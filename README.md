**ContextMesh** is an independent graph-based context and reasoning system for AI agents. Its goal is to preserve not only **what an agent knows**, but also **where that information came from, why decisions were made, what assumptions supported them, and what becomes invalid when those assumptions change**.

ContextMesh organizes knowledge into typed graph structures such as **Entities, Claims, Sources, Decisions, Assumptions, and Evidence**. Relationships between them are represented with explicit typed edges rather than relying only on similarity search or flat retrieval.

The system is designed around several core capabilities:

* **Entity resolution** — normalize multiple references to the same real-world concept or object.
* **Typed relationships** — represent connections such as `supports`, `contradicts`, `cites`, `depends_on`, `produces`, `supersedes`, and `justified_by`.
* **Evidence provenance** — preserve the source, execution event, artifact, or validation check supporting a claim or decision.
* **Graph traversal** — answer questions by walking readable evidence paths rather than retrieving an opaque Top-K list of chunks.
* **Assumption tracking** — treat assumptions as first-class, versioned graph entities.
* **Selective invalidation** — when an assumption is disproven, identify the exact downstream work that depended on it while preserving unrelated work.
* **Decision history** — maintain an append-only record of why decisions were made and how they changed.
* **Graph health** — expose unresolved entities, missing relationships, dead ends, stale evidence, and other conditions that reduce graph usefulness.

ContextMesh is intended to complement document retrieval and agent memory rather than replace them. Retrieval systems can provide source material, memory systems can provide historical context, while ContextMesh provides the **structured relationships and provenance layer connecting evidence, reasoning, and decisions**.

The central design principle is:

> **The graph should remember not only what is connected, but why it is connected.**

This makes ContextMesh suitable for long-running AI agents where auditability, explainability, changing information, and controlled re-execution matter as much as retrieving the right context.
