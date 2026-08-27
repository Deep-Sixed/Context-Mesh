# Context Mesh

> One graph · four node types · every answer walks a path you can read

Context Mesh is a typed context graph for long-running agents. It keeps not only
*what* is connected, but *why* — where a claim came from, what a decision rested
on, and what stops being true when an assumption fails.

This repository is a working implementation, reverse-engineered from a screen
capture of a dashboard posted publicly. It is an independent project with no
affiliation to, or endorsement by, anyone involved in that capture. The
dashboard is rebuilt too, and every number on it is computed by the engine
rather than written into the page.

![The Context Mesh dashboard: build path, the live typed graph, hop budget, edge
ledger, walk-vs-flat token cost, the traversal grid and the dead-end ledger](docs/img/dashboard.png)

Every panel above is a query against the graph — the node counts, the hop
histogram, the edge traffic, the dead-end reasons. Open
[`dashboard/context-mesh.html`](dashboard/context-mesh.html) for the live page
in a single file, or rebuild it from your own data:

```bash
python -m contextmesh export --inline && open dashboard/index.html
```

```bash
git clone https://github.com/charlessnydercareer/Context-Mesh
cd Context-Mesh

python -m contextmesh demo            # build, walk, break an assumption, report
python -m contextmesh ask "Why did the Index Builder run out of memory?"
python -m contextmesh health          # what is quietly wrong with the graph
python -m contextmesh invalidate      # reject an assumption, see the fallout
python -m contextmesh execute         # break one, then re-run only what fell
python -m contextmesh export --inline # regenerate the dashboard's data
```

No dependencies. Python 3.9+. `python -m unittest discover -s tests` runs the
suite (266 tests). CI runs it on 3.9 through 3.13, plus ruff and a set of
end-to-end smoke checks — including that a build is byte-identical across
`PYTHONHASHSEED`, and that `dashboard/data/mesh.json` still matches what the
engine produces.

## An answer is a path

```
$ python -m contextmesh ask "What made the sharding rule wrong?"

Q: What made the sharding rule wrong?
A: Sharding must key on tenant as well as corpus position.
   (2 hops, score 0.25, 60 tokens against 7,307 flat)

(claim) The Partitioned Rebuild is sound but the sizing rule was wrong.
  -[derived_from]-> (source) Postmortem 233 — the linear sharding assumption
    <-[derived_from]- (claim) Sharding must key on tenant as well as corpus position.
```

That path is the justification. There is no second pass that reconstructs a
rationale after the fact — the walk *is* the reasoning, and it is stored, so the
same question does not get re-guessed next time.

When the graph cannot answer, it says which of exactly four things went wrong
rather than returning its best guess:

```
Q: What is the refund policy for annual plans?
A: dead end — entity_unresolved
   no mention resolved to a canonical entity: 'refund', 'policy', 'annual', 'plans'
```

## The ontology is a file

[`GRAPH.md`](GRAPH.md) is not documentation of the schema. It **is** the schema:
`contextmesh/ontology.py` parses it at import time, and every write typechecks
against it.

```python
graph.add_edge(entity.id, EdgeType.MENTIONS, source.id)
# OntologyError: entity-[mentions]->source is not a legal pair;
#                GRAPH.md allows claim->entity, source->entity
```

Six node types — `entity`, `claim`, `source`, `decision`, `assumption`,
`evidence` — and ten edge types: `mentions`, `derived_from`, `cites`,
`contradicts`, `supports`, `depends_on`, `produces`, `supersedes`,
`justified_by`, `resolves_to`. There is no code path that stores an edge without
one, which is why `untyped_edges == 0` is an invariant rather than a metric.

## How a node earns its place

```
CHUNK → EXTRACT → RESOLVE → LINK → EMBED → PRUNE
```

| Stage | What happens | On the demo corpus |
|---|---|---|
| **CHUNK** | Spans in from each source | 70 spans, 14 sources |
| **EXTRACT** | A span becomes a claim only if it asserts something | 69 claims |
| **RESOLVE** | One id per real-world thing | 47 entities, 90 surface forms folded; **1,264 mentions dropped** |
| **LINK** | Typed edges only; an illegal pair is dropped, never stored untyped | 372 edges, 8 of the 10 types in use |
| **EMBED** | A vector on the node, for seeding walks | every node |
| **PRUNE** | Drop what nothing walked | orphans removed |

Most of the corpus dies at RESOLVE, and that is the point: admitting a wrong
merge corrupts every walk that later crosses the entity, while dropping a
mention costs one span — and the drop is recorded, not swallowed.

## Selective invalidation

Assumptions are first-class, versioned nodes. When one is disproven, exactly the
work that stood on it is invalidated, and the report proves both halves:

```
$ python -m contextmesh invalidate

rejected assumption: Shard count grows linearly with corpus size

INVALIDATED (2)
  ✗ Rebuild the index in partitions
      because: assumption(Shard count grows linearly…) <-depends_on- Rebuild the index in partitions
  ✗ Partitioned Rebuild
      because: assumption(…) <-depends_on- Rebuild the index in partitions -produces-> Partitioned Rebuild

PRESERVED (136) — untouched by the rejection
```

Failure travels along three edges and no others, and direction matters:
`depends_on` and `derived_from` are followed **backwards** (the thing that
depends falls with its ground), `produces` **forwards** (an artefact falls with
the decision that made it). `mentions`, `cites`, `supports` and `supersedes`
never propagate — that is what makes this selective rather than a purge.

`supersedes` never deletes, so decision history stays append-only and "why did
we change our mind" is answered by walking, not by reading a changelog.

## Selective re-execution

Knowing what fell is half the claim. `contextmesh/execute.py` is the half that
acts on it: a `Runner` binds units of work to the graph's own vocabulary — a
task *is* a `decision` node, its ground is a `depends_on` edge to an
`assumption`, its dependencies are `depends_on` edges to other decisions, its
outputs are `produces` edges to entities — so scheduling reads off the ontology
rather than a second copy of it.

```
$ python -m contextmesh execute

ROUND 1 · 5 executed, 0 cached
  layer 1: schema, hashing, tokens
  layer 2: routes
  layer 3: rate_limit

THE GROUND MOVED · discovered by an auditor, not announced
  hashing: argon2-cffi has an open advisory — CVE-2026-9999: memory-hard
           parameters silently downgraded
  rejected: argon2-cffi has no open advisory
  blast radius 8, preserved 15

ROUND 3 · 3 executed, 2 cached
  layer 1: hashing
  layer 2: routes
  layer 3: rate_limit
  cached  schema — untouched, previous result stands
  cached  tokens — untouched, previous result stands
```

Three properties are worth being precise about, because they are the ones a
demo can fake:

**The failure is discovered, not announced.** `recheck()` re-runs every standing
auditor against current facts and executes nothing. The CVE is published to a
feed *outside* the graph; the hashing auditor reads that feed and returns
`ctx.disproved(...)`, and that disproof is what rejects the assumption and
computes the blast radius. Nothing reaches in and marks a node false. An auditor
distinguishes "the output is wrong" (`ctx.fail`, which fails one task) from "the
ground is false" (`ctx.disproved`, which takes down everything standing on it),
because those have different blast radii and an engine should not have to guess.

**Re-running stays append-only.** A rerun never revives the old decision — it
appends a new one that `supersedes` it, so the superseded reasoning is still
walkable. Artefacts are the exception, and deliberately so: an `entity` fell
only because the decision that produced it fell, so rebuilding that decision
brings it back under the same id. An artefact the rerun *stops* producing — the
Argon2 parameters, once the hasher becomes bcrypt — stays invalidated, which is
the right answer for a thing that no longer exists.

**The ledger is append-only in a way you can check.** Each entry's digest is
SHA-256 over the canonical JSON of its own fields *and* the digest before it, so
a rewritten, reordered or dropped entry breaks the chain and `ledger.verify()`
says so. Canonical JSON rather than joined fields because `detail` is free text:
under a `|`-separated payload, `task="a", detail="b|c"` and `task="a|b",
detail="c"` produce identical bytes, and one entry can forge another's digest.
Payload values are refused unless they have a single JSON form — no sets, no
bytes, no `NaN` — since a digest over an unstable rendering is computed but not
meaningful. There are no timestamps either: this package promises builds that
are byte-identical across processes and hash seeds, and a clock would be the one
field that could not be reproduced.

**A disproof is one entry, not a pile of them.** The `DISPROVED` record carries
the whole receipt — which ground failed, what evidence disproved it, which
auditor found it, every invalidated node *with the chain that explains it*, and
the preserved set — so `ledger.receipts()` answers the full event without the
graph the run was performed against. The per-node `INVALIDATED` entries stay:
the receipt is the atomic event, those are each node's own history, and both are
worth having. Saying three nodes fell proves half of selective invalidation;
the preserved set is the other half, so it travels in the same record.

A rejected assumption is never edited back to life. It stays rejected, and
`repair()` grounds the task on a replacement that records what it supersedes —
so `lineage()` still answers "what did we used to believe, and why did we stop".
Until that repair happens the task is *blocked*, not run: nothing executes on
ground the graph knows to be false.

### The standalone control layer

`examples/assumption_control_layer.py` is the original single-file sketch of
these ideas — a seven-node executor that carries an assumption on an edge,
rejects it mid-run, and re-executes only what stood on it:

```
INTENT → DECOMPOSE → WORKER → AUDIT → DRIFT → LEDGER → ROOT
             ↘
              BRANCH_C1 → BRANCH_C2   (unrelated, must survive)
```

```bash
python examples/assumption_control_layer.py
# OVERALL: ALL ACCEPTANCE CHECKS PASSED
```

It runs on its own with nothing imported from the package, and it is kept
because the whole idea fits on one screen there. It is a sketch, not the
implementation: its `DRIFT` node is a stub that reports alignment without
checking anything. `contextmesh/assumptions.py`, `contextmesh/decisions.py` and
`contextmesh/execute.py` are the version the rest of this repository uses, and
the one to read if you want the behaviour rather than the shape.

## Graph health

The states a long-running graph drifts into, none of which are errors:

```
$ python -m contextmesh health
     untyped_edges               0  edges stored without a type from GRAPH.md
   ! unresolved_entities       301  near-miss mentions dropped at RESOLVE
   ! missing_relationships      10  resolved entities no claim ever mentions
   ! open_contradictions         4  claims contradicted with no decision on top
   ! dead_ends                   9  entity_unresolved=3; wrong_node_type=4; pruned_too_early=2
     invalidated_nodes           3  work invalidated by a rejected assumption, kept for audit
```

`unresolved_entities` counts *near misses* — something in the graph nearly
matched. The extractor offers the resolver every phrase in every sentence, and
most of them are simply not entities; counting those would turn the signal into
a measure of traffic.

## The dashboard

```bash
python -m contextmesh export --inline
open dashboard/index.html
```

Header stats, the build path, the live graph, the hop budget, the edge ledger,
walk-vs-flat token cost, the traversal grid and the dead-end ledger — each panel
is a query against the graph. [`dashboard/README.md`](dashboard/README.md) maps
every panel to the call that produces it and is explicit about what is a live
stream and what is a snapshot. [`docs/DASHBOARD_SPEC.md`](docs/DASHBOARD_SPEC.md)
is the frame-by-frame record of the original capture.

## Persistence

```python
graph.save_json("graph.json")
graph = ContextGraph.load_json("graph.json")
```

A versioned snapshot (`contextmesh.graph` v1) that restores the same graph, not
a graph that looks the same. Nodes carry their actual embedding vector, not a
`embedded: true` flag; provenance spans come back as tuples; `walks`,
`traversals`, `pruned` and `invalidated` all survive.

**A snapshot is untrusted input, and it fails closed.** Every edge is restored
through `add_edge`, so `GRAPH.md` typechecks a file exactly as it would a live
write, and `_out`, `_in` and `_edge_key` are rebuilt rather than persisted. A
loader that writes straight into the internal dictionaries is faster and admits
graphs the live API would refuse — which makes the ontology a convention that
holds only until something is saved.

Every field the writer emits is **required** on load — absence is corruption,
not a default. Dropping `invalidated` would restore a node the graph had
deliberately killed; dropping `embedding` would restore one that answers
differently. Defaults belong to a schema version with an older shape to migrate
from, and v1 has none.

`null` is accepted only where the schema writes it — `provenance`, its `span`,
`embedding`, `assumption_id`, `supersedes`, `superseded_by`, `rejected_at_build`.
Everywhere else a null is corruption, and normalising it to an empty list would
discard what the field named: an assumption whose `evidence_ids` arrived as
`null` would restore with nothing recorded as having disproved it.

References between records are checked once every record exists: a lineage that
names a missing assumption, a supersession only one side agrees with, an edge
grounded on an assumption that is not there, evidence that is not a node. Each
of those loads cleanly and fails much later — `lineage()` raising `KeyError` on
a graph that has been serving for an hour.

Fields are checked rather than coerced, because coercion is not harmless here:
`bool("false")` is `True`, so a malformed flag would quietly turn a live node
into an invalidated one; `list("abc")` would turn a string into a
three-element "vector"; and `bool` being a subclass of `int` means a count
written as a flag would arrive as `1`. The suite corrupts a good snapshot
97 ways and requires each to be refused — a counted figure, not an
estimate: `tests/test_persistence.py` builds and rejects that many distinct
corrupted snapshots across 55 test methods.

**Records are emitted in insertion order, never sorted.** That is load-bearing.
The walker's frontier is a heap whose tie-breaker is an insertion counter, and
the lexical fallback iterates `graph.nodes`, so two equal-cost branches are
decided by which was added first:

```
edges added alpha-first: answer = alpha  (hops 2, score 0.2750)
edges added bravo-first: answer = bravo  (hops 2, score 0.2750)
```

Same facts, same score, different answer. Sorting the arrays would reorder those
lists on reload and change answers without changing anything true, so
`tests/test_persistence.py` builds that tie deliberately and asserts a round
trip preserves it. Sorting the *keys* of each JSON object is safe, and
`save_json` does that — along with `allow_nan=False`, because a snapshot other
parsers refuse is not durable.

One thing the snapshot **cannot** represent: disagreement. An assumption's
`status` and `version` are stored on the record and projected onto its node so a
walk can read them without a second lookup. The loader refuses a file where the
two disagree — and enforcing that surfaced a real bug, since two paths bumped
`version` on the record alone and left the mirror stale. `graph.sync_assumption`
is now the one place that writes it.

What persistence does **not** yet cover: the execution state in `Runner`.
Resuming a selective re-execution still needs the task registry and the run
ledger's history. That is the next milestone.

### Sessions — a graph *and* the resolver that reads it

```bash
python -m contextmesh_mcp --demo --rounds 8 --save ./session   # write one
python -m contextmesh_mcp --session ./session                  # inspect it
```

A restored graph answers questions only if something can turn *"pgvector"* into
`entity:pgvector-6db608`, and that is the resolver's job. So a session is a
directory of three separately versioned files:

```
session/
  session.json           contextmesh.session  v1  — the manifest, and the commit
  session.lock           the writer lock, created once and never deleted
  graph-000003.json      contextmesh.graph    v1
  resolver-000003.json   contextmesh.resolver v1
```

Three formats rather than one because graph snapshot v1 is **closed**. Query
resolution is not graph state, and folding the resolver in would mean reopening
a settled format every time the resolver learns a new field. The session file is
the join.

**A save never writes to a file the current manifest names.** Writing three
files in sequence is safe the first time and unsafe every time after: crash
between the graph and the resolver and the directory holds a new graph beside an
old resolver — a pairing that never existed, and one that can still pass every
check made on it. So each save commits a whole new *generation* under new names
and then replaces `session.json` in a single `os.replace`:

```
crash before the swap  →  the previous generation is still named, intact
crash during the swap  →  one manifest or the other, never half of one
crash after the swap   →  the new generation is named, and complete
```

Superseded files are swept after the commit, so an interrupted save costs disk
rather than correctness. The guarantee is reader-visible and tested as such: a
process holding `session.json` open across a save still reads the previous
generation whole, because `os.replace` gives the manifest a new inode rather
than rewriting the old one in place.

**Generations are atomic against a crash. They do nothing against a second
writer**, and that failure is nastier because nothing about it looks torn. Two
processes reading generation 5 both choose 6 and overwrite each other's
companions. Worse, one can commit 6 and sweep while the other has already
written 7 but not yet swapped — leaving a manifest that is atomically valid and
names files the first process just deleted:

```
end   : ['session.json']
manifest -> generation 3   graph-000003.json exists: False
```

That is a real reproduction, staged across two processes, and it is now a test.
So a save takes the directory's writer lock for the **whole** transaction —
from reading the current generation to sweeping the superseded one. Locking only
the swap would still allow both races above. The lock is `flock`/`msvcrt`, held
by the kernel rather than by convention, so it is released when the holder dies
however it dies; there is no stale-lock heuristic to guess wrong under exactly
the load that made a save slow. A second writer is refused, not queued:

```
SessionLockedError: … is already being written by pid 4127 on host; one writer at a time
```

Readers never take it. The manifest swap already gives them a consistent view,
and a read that had to wait behind a checkpoint would be a worse trade.

Serialising writers stops corruption; it does not stop the second writer from
silently discarding the first one's work. So a session that has a home also
checks that home before committing: if the directory has moved on since this
session last committed there, the save is refused rather than clobbering. The
`Checkpointer` treats lock contention as a skipped commit — the mutation stays
pending for the next one — and counts it, because a server that keeps losing
the race is a configuration problem worth being able to see.

Multi-writer *coordination* is out of scope: two servers on one directory get
one clear refusal each time they collide, not a merge.

**Untrusted on the way out, too.** The read side refuses a companion that
resolves outside the directory. Making the directory *writable* opened the
mirror-image hole: writing by name follows whatever is already under that name,
so a directory handed over with the next generation's filename already present
as a symlink would have the next save write straight through it — and with
`--checkpoint every-ask`, merely asking a question is the trigger. Four names
were reachable that way, the lock file among them.

One mechanism rather than four patches: every write lands in a fresh `O_EXCL`
file under a random name — which cannot already exist, so cannot already be a
link — and is moved into place with `os.replace`. Rename replaces a symlink
*itself* instead of following it, so a planted link is destroyed and the file it
pointed at is never touched. The same rename is what makes the write atomic, so
the manifest's commit and the symlink defence are the same line of code.

The lock is the exception, because it has to keep one inode for its whole life —
that inode is what the kernel lock attaches to, and replacing it each save would
hand two processes locks on two different inodes. So it is opened `O_NOFOLLOW`
and checked with `fstat` to be a regular file, which also rules out a fifo or a
device left under the name.

**A checkpoint must not make a live session look broken.** Readers take no lock
and do not need one for correctness — the manifest swap is atomic and committed
companions are immutable, so a pair that reads successfully is always a coherent
generation, at worst a slightly old one. What the swap alone does not cover is
the *sweep*: a reader holding the manifest for generation 5 can find
`graph-000005.json` already deleted and fail on a perfectly healthy directory.
So a read that loses that race is re-read rather than reported, and the retry
fires only when the directory's generation actually moved during the attempt —
which keeps a genuinely missing file failing immediately, with its own message,
instead of after a wait.

**The resolver cannot be rebuilt from the graph, and that is the point.**
`resolve()` writes a scored match back into its alias table, so a run learns
surface forms no entity label contains: in the bundled demo, 72 of the
resolver's 120 aliases are learned that way rather than registered. Restart
without them and the same mention costs a full block scan and a scored match
instead of a table hit.

`blocks` is persisted rather than rebuilt, which is where the analogy with
`_out`/`_in` breaks. Those are a function of the edge list. `blocks` is a
function of *how* an alias arrived: `register` adds block keys for every name it
is given, a match learned at query time adds none, and the alias table does not
record which is which. Rebuilding from `canonical` alone loses keys; rebuilding
from `canonical` plus `aliases` invents keys the resolver never had. Blocks
decide the candidate set, so either version silently changes what resolves —
`tests/test_session.py` measures both and shows them differing.

Walker settings ride along in `session.json` for the same reason: restore a
session saved with `hop_budget=3` into the default `6` and the same question
comes back with a different answer, with nothing in the output to say why.

**A session directory is untrusted input.** `Session.load` refuses one it cannot
faithfully restore — wrong schema, unreadable version, a missing or mistyped
field, a `rounds` written as `true`, a policy naming an edge type this ontology
does not have, a manifest whose filenames run ahead of its generation counter.
The two filenames must be plain names, so a directory you were handed cannot
point the loader at `../../etc/passwd`, and the files they name must resolve
*inside* the directory — a correctly named `graph-000001.json` that is a symlink
to somewhere else is refused too.

Three checks live here because neither file can make them alone. Every entity
the resolver resolves *to* must exist in the graph, must be an entity, and must
carry **the same label**. The first two fail loudly; the third is the quiet one.
`Resolver.canonical` is not a display string — it is scored against every
mention that reaches `near_miss` — so a resolver holding `entity:pgvector ->
"HNSW"` over a graph holding `"pgvector"` keeps resolving, resolves differently,
and both files are individually valid. The resolver's own log is held to the
same standard: a record naming an id must carry the label that id has, and a
resolution has both an id and a label while a miss has neither.

### Checkpoints — because asking is a write

A saved session that is never written back is a durable *starting* snapshot, not
a durable session. Asking a question moves `node.walks` and `edge.traversals`,
grows the resolver's log, and teaches it aliases it did not have — so without a
checkpoint every question asked after startup is discarded on restart, silently,
including exactly the learned aliases the format exists to keep.

```bash
contextmesh-mcp --session ./session                       # commit after each ask
contextmesh-mcp --session ./session --checkpoint on-exit  # commit once, on a clean stop
contextmesh-mcp --session ./session --checkpoint never    # serve it, never write to it
```

`every-ask` is the default and it is the expensive one on purpose: one full
serialisation per question is a real cost, and a silently lost question is a
worse one. `on-exit` is cheaper and worth nothing if the process is killed
rather than asked to stop. `never` is the honest choice for a directory you were
handed and do not own. `mesh_ask` is the only tool that triggers a commit —
which is asserted at the call site rather than inferred, so a sixth tool forces
a decision about whether it writes.

**What a restart still does not restore**, stated plainly because the suite pins
it: the walker's in-process walk list, and the assumption ledger's event log.
`mesh_lineage` and `mesh_blast_radius` come back identical — both read the
graph's own assumption records — and every *count* in `mesh_health` restores
exactly. The one field that does not is health's `dead_ends` signal, computed
from the walk list and so absent until the new process has walked. That is a
cold start, not a lost capability, and `SurfaceEquivalenceTest` asserts it is
the *only* one.

## MCP — read-only, experimental

```bash
pip install 'contextmesh[mcp]'
contextmesh-mcp --demo               # a graph rebuilt for this process
contextmesh-mcp --session ./session  # a graph that outlives it
```

An MCP server over the graph, so an agent can query it as memory instead of
being handed chunks. Five tools — `mesh_ask`, `mesh_get_node`, `mesh_health`,
`mesh_lineage`, `mesh_blast_radius` — plus `contextmesh://schema`, `health`,
`session`, `assumptions`, and templates for `node/{id}` and `assumption/{id}`.

```json
{
  "mcpServers": {
    "context-mesh": { "command": "contextmesh-mcp", "args": ["--demo"] }
  }
}
```

**v0.1 is deliberately read-only — and one part of that is permanent.**

Those are two different statements and it is worth keeping them apart. *No
writes at all* is a property of this version; a later one may accept evidence,
trigger a recheck, or drive a repair. *No rejection by client fiat* is not a
phase: under GRAPH.md rule 7 an assumption falls only to evidence contradicting
it, produced by an auditor that looked at the world. A tool letting a caller
name an assumption and have it rejected would turn that rule into a convention,
so it will not be added — what a future version could offer is submitting
evidence and letting Context Mesh decide, which is a different thing.

`mesh_blast_radius` sits exactly on that line. It answers *what would fall if
this assumption were false* — the closure, the reason chain for each node, and
the preserved complement — and rejects nothing. No tool here adds a node, adds
an edge, rejects, repairs or executes.

**The read boundary is not "the graph is unchanged".** Asking a question moves
walk telemetry — `node.walks`, `edge.traversals` — and PRUNE later drops what
nothing walked, so a walk is a write by design. The invariant tested instead is
that no read changes graph structure, ontology state, assumptions, supersession
or invalidation, while telemetry is free to move. `tests/test_mcp.py` asserts
both halves separately.

**It can now serve a saved session**, and it makes you say which:

```bash
contextmesh-mcp --demo --rounds 8 --save ./session   # write one
contextmesh-mcp --session ./session                  # serve it
```

`--demo` used to be what you got by saying nothing. It has to be said now,
because the alternative is no longer *nothing* but a real session on disk, and
silently serving a throwaway graph when someone meant to serve theirs is the one
failure the format exists to prevent. `contextmesh://session` reports which it
is and whether anything a client reads outlives the server.

```json
{
  "mcpServers": {
    "context-mesh": {
      "command": "contextmesh-mcp",
      "args": ["--session", "/path/to/session"]
    }
  }
}
```

What a client does to a served session now survives it — see **Checkpoints**
above. What is still missing before this is agent memory: nothing writes
*structure or belief* into a session from the client side. Those change only
through the engine, and execution state does not persist at all.

The core stays untouched by all of this: `contextmesh` remains Python 3.9+ with
zero dependencies, the MCP SDK arrives only through the `[mcp]` extra, and
`contextmesh_mcp.server` is the only module that imports it — which is why the
safety tests above run on the whole 3.9–3.13 matrix with nothing installed.

## Layout

```
GRAPH.md                     the ontology, parsed at import
contextmesh/
  ontology.py                GRAPH.md → the schema every write is checked against
  model.py  graph.py         typed nodes, typed edges, no untyped code path
                             and the versioned snapshot it saves and reloads as
  resolve.py                 entity resolution — one id per real-world thing,
                             and the alias table it learns and reloads
  pipeline.py                CHUNK → EXTRACT → RESOLVE → LINK → EMBED → PRUNE
  traverse.py                walks, evidence paths, the four dead-end reasons
  assumptions.py             versioned assumptions, blast radius, rejection
  execute.py                 re-runs exactly the closure an invalidation felled
  decisions.py               append-only decision history
  health.py                  the signals that make a graph quietly useless
  metrics.py                 the dashboard payload
  corpus.py  demo.py  cli.py the worked example
contextmesh_mcp/             read-only MCP server (optional extra, 3.10+)
  session.py                 durable sessions: graph + resolver, on disk
  tools.py  resources.py     plain Python over the engine; no SDK import
  __main__.py                write or inspect a session without the SDK
  server.py                  the only file that needs the SDK
dashboard/                   the rebuilt dashboard
docs/                        the capture spec and the architecture notes
examples/                    the original standalone control-layer sketch
tests/                       422 tests over the invariants above
```

## Staying publishable

This repository is public and carries nothing internal. `ops/leak-check.sh`
makes that checkable rather than asserted — it scans tracked content for
secrets, email addresses and machine-local paths, and commit *metadata* for
private session URLs and non-no-reply author addresses, which is where leaks
of this kind actually hide.

```bash
ops/leak-check.sh              # the whole history
ops/leak-check.sh origin/main  # only what a branch adds
```

CI runs the second form on every pull request.

`ops/accepted-authors.txt` records addresses that are public on purpose, so the
check can tell a decision from an accident instead of failing forever on a known
case. An address that is not listed and not a no-reply still fails.

## Where it fits

Context Mesh complements retrieval and agent memory rather than replacing them.
Retrieval supplies the source material; memory supplies history; Context Mesh
supplies the typed relations and the provenance that make an answer auditable —
and, on this corpus, does it for about 0.7% of the tokens flat top-k would
spend, because it sends the nodes on the path instead of the top forty chunks.

> The graph should remember not only what is connected, but why it is connected.

## Licence

MIT — see [LICENSE](LICENSE).
