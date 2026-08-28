# PR #8 — Controlled MCP writes

PR #7 made execution resumable across process death. PR #8 adds the next boundary: an MCP client may introduce new information and request controlled execution changes without receiving raw authority to rewrite belief or inject executable code.

## Non-negotiable invariant

There is no tool equivalent to:

```text
mesh_reject_assumption(assumption_id)
mesh_invalidate(node_id)
mesh_set_status(assumption_id, "rejected")
mesh_add_edge(...)
```

An assumption falls only through the engine's evidence/auditor path. The client can introduce material for Context Mesh to inspect and can request a recheck; it cannot supply the verdict as authority.

The MCP surface is split deliberately:

```text
READ
mesh_ask
mesh_blast_radius
mesh_get_node
mesh_health
mesh_lineage

CONTROLLED WRITE
mesh_submit_evidence
mesh_recheck
mesh_repair
mesh_resume
```

`mesh_ask` still moves walk/resolver telemetry. `mesh_blast_radius` remains hypothetical and changes no belief state.

## Implemented slices

### 8A — Evidence intake

`contextmesh/evidence.py` is the graph/data boundary for observations. It:

- accepts evidence as data, never executable code;
- creates a detached `NodeType.EVIDENCE` through the normal typed graph path;
- requires provenance to an existing `NodeType.SOURCE`;
- accepts an optional external id;
- recursively validates a strict JSON metadata subset with no coercion, NaN, infinity, callable, bytes, set, tuple, or non-string object keys;
- detaches stored metadata from caller-owned mutable objects;
- canonicalizes the full payload, stores a SHA-256 payload digest, and derives a deterministic evidence id;
- treats an exact replay as idempotent rather than multiplying evidence;
- refuses external-id conflicts, deterministic-id/type collisions, malformed stored evidence, and duplicate external-id records;
- creates no edges and cannot name an assumption, verdict, rejection, or invalidation target.

The evidence submission is an observation, not a conclusion.

### 8B — Recheck through registered auditors

`contextmesh/recheck.py` extends the native audit context with an optional existing `evidence_id`. The MCP recheck path calls it with `require_evidence=True`.

The authority chain is:

```text
client requests recheck
        ↓
registered auditor inspects current graph/output/ground
        ↓
engine decides holds / fails / disproves
        ↓
if disproved, auditor must identify a pre-ingested EVIDENCE node
        ↓
native AssumptionLedger rejects ground and computes the selective blast radius
```

The client supplies no verdict and no assumption target. A missing, non-evidence, or invalidated evidence id is refused before belief mutation. The controlled path never invents supporting evidence for a remote disproof.

### 8C — Controlled repair and resume

`contextmesh_mcp/writes.py` exposes repair and resume over a persistent session.

A repair may select only durable worker/auditor keys that the session's deployment-owned `TaskRegistry` already knows. No callable, module path, import string, pickle, registry object, or client-supplied executable identity crosses MCP.

The repair retains PR #7's rule: changing implementation and changing durable identity are one operation. An unknown key is refused before the committed session changes.

`mesh_resume` delegates to the native scheduler. Only PENDING/STALE work executes; unrelated DONE work remains cached. The Argon2→bcrypt acceptance case proves hashing and routes move to attempt 2 while schema and tokens stay at attempt 1.

### 8D — Durable write acceptance

The full controlled-write path crosses four fresh Python subprocesses that share only the committed session directory. The parent test process creates the initial persisted execution plan, so there are five process lifecycles if setup is counted.

```text
parent     create initial Argon2 plan → run → save
process A  load → submit CVE evidence → checkpoint → exit
process B  fresh registry → load → recheck → repair to bcrypt → checkpoint → exit
process C  fresh registry → load → resume stale closure → checkpoint → exit
process D  fresh registry → load → verify graph + execution + RunLedger → exit
```

The proof checks that bcrypt has not executed at the repair/checkpoint boundary, then verifies after restart that only hashing and routes rerun, schema and tokens remain cached, the bcrypt worker key survives, outputs are the repaired outputs, and the RunLedger still verifies.

## Security boundary

PR #7 established that strings naming code are safe only because they are lookups in a deployment-owned `TaskRegistry`. PR #8 keeps that boundary. MCP input is untrusted data. It never becomes an import, a callable, a module path, or a deserialization route capable of producing behaviour.

Evidence text is not trusted merely because it arrived through a tool. Submission means "store this observation". Recheck means "ask the registered auditor whether current ground still holds". Repair means "select a key this deployment already registered". Resume means "let the native scheduler run the stale closure". Those are separate operations on purpose.

## Persistence boundary

Structural writes use a stronger contract than read-side telemetry: a successful controlled-write reply means the mutation belongs to a committed session generation.

The write is applied to a lossless staged Session clone first. The staged clone is checkpointed using PR #7's generation/manifest mechanism and is adopted as the live session only after the committed generation exists. Failed validation, writer-lock contention, compare-and-swap refusal, or a pre-commit persistence failure therefore leaves the previously served session authoritative rather than attempting to reverse graph internals manually.

Generation/manifest atomicity, writer locking, symlink confinement, graph/resolver/execution/ledger agreement, and trusted ledger-head checks remain inherited requirements from PRs #5–#7; PR #8 does not weaken or fork them.

## Trust terminology

The RunLedger is **tamper-evident with trusted-head continuity**. The hash chain detects edits, deletions, reordering and mismatch against a previously trusted head. It is not cryptographically authenticated or immutable: a writer able to rewrite both a ledger and its trusted anchor can construct a different internally consistent history.

## Verification gate

The PR #8 head is required to pass the full existing Python 3.9–3.13 matrix, Ruff, the private-data guard, registry/ledger/execution/session jobs, MCP SDK authority-surface tests, and the 8A–8D controlled-write tests before merge.

At the reviewed head, GitHub Actions runs 694 tests successfully in the normal suite (the six MCP SDK-only tests are exercised in the dedicated MCP job where the optional SDK is installed). The dedicated four-process 8D acceptance test passes on the GitHub runner.
