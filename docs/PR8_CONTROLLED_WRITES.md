# PR #8 — Controlled MCP writes

PR #7 made execution resumable across process death. PR #8 is the next boundary: allowing an MCP client to introduce new information without giving that client authority to rewrite belief by fiat.

The current MCP surface is deliberately read-only with one telemetry exception: `mesh_ask` moves walk/resolver state and checkpoints it. `mesh_blast_radius` is explicitly hypothetical. This PR must preserve the stronger rule behind that design: a caller may provide evidence, but a caller may not directly reject an assumption.

## Non-negotiable invariant

There will be no tool equivalent to:

```text
mesh_reject_assumption(assumption_id)
mesh_invalidate(node_id)
mesh_set_status(assumption_id, "rejected")
```

An assumption falls only through the engine's evidence/auditor path. The client can introduce material for Context Mesh to inspect; it cannot supply the verdict as authority.

## Planned slices

### 8A — Evidence intake

Add the first structural write surface. It must:

- accept evidence as data, never executable code;
- create an `evidence` node through the normal typed graph write path;
- carry provenance sufficient to answer where the evidence came from;
- return the created evidence id and its stored representation;
- checkpoint a persistent session after a successful write;
- make duplicate/replayed submissions deterministic rather than multiplying equivalent evidence;
- refuse malformed or non-canonical payloads before changing the graph;
- **not** reject, supersede, repair, execute, or mark anything invalidated.

The evidence submission itself is therefore an observation, not a conclusion.

### 8B — Recheck through registered auditors

Expose a controlled recheck only when the loaded session has a resumable execution plan and a `TaskRegistry` capable of binding its auditors.

The client may ask the engine to recheck standing work. The client may not provide the audit verdict. Any rejection must be produced by a registered auditor using the same `Runner.recheck()` path used in-process today.

Required acceptance property:

```text
client submits evidence
        ↓
client requests recheck
        ↓
registered auditor inspects current state
        ↓
engine decides holds / fails / disproves
        ↓
only a disproving auditor may reject ground
```

### 8C — Controlled repair and resume

A repair may select only durable worker/auditor keys that the session's registry already knows. No callable, module path, import string, pickle, or client-supplied executable identity crosses MCP.

The repair must retain PR #7's rule: changing implementation and changing durable identity are one operation. A keyed task cannot be repaired with a bare callable or with an unknown key.

After repair, execution resumes only the stale closure; unrelated DONE work remains cached.

### 8D — Durable write acceptance

Cross a real process boundary for the complete write path:

```text
process A  load session → submit evidence → checkpoint → exit
process B  fresh registry → load → recheck → repair → checkpoint → exit
process C  fresh registry → load → resume stale closure → checkpoint → exit
process D  load → verify graph + execution + ledger continuity
```

The acceptance case must prove that no direct client command can reject an assumption, that evidence survives restart, that the auditor is the actor producing any disproof, and that the final selective rerun preserves unrelated work.

## Security boundary

PR #7 established that strings naming code are safe only because they are lookups in a deployment-owned `TaskRegistry`. PR #8 keeps that boundary. MCP input is untrusted data. It must never become an import, a callable, a module path, or a deserialization route capable of producing behaviour.

Likewise, evidence text is not trusted merely because it arrived through a tool. Submission means "store this observation". Recheck means "ask the registered auditor whether current ground still holds". Those are separate operations on purpose.

## Persistence boundary

Every successful structural/belief mutation in a persistent session must go through the session checkpointer. A failed checkpoint is reported, not hidden. A failed validation must leave the in-memory graph unchanged.

Generation/manifest atomicity, writer locking, symlink confinement, graph/resolver/execution/ledger agreement, and the trusted ledger-head checks remain inherited requirements from PRs #5–#7; PR #8 does not weaken or fork them.

## PR #8 start gate

This document is the start line, not evidence that 8A is implemented. The first implementation commit should add only the evidence-intake primitive and its tests. Recheck, repair, and resume stay out until that primitive is fail-closed and checkpoint-safe.
