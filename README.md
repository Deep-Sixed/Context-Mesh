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
suite (183 tests). CI runs it on 3.9 through 3.13, plus ruff and a set of
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

## MCP — read-only, experimental

```bash
pip install 'contextmesh[mcp]'
contextmesh-mcp
```

An MCP server over the graph, so an agent can query it as memory instead of
being handed chunks. Five tools — `mesh_ask`, `mesh_get_node`, `mesh_health`,
`mesh_lineage`, `mesh_blast_radius` — plus `contextmesh://schema`, `health`,
`session`, `assumptions`, and templates for `node/{id}` and `assumption/{id}`.

```json
{
  "mcpServers": {
    "context-mesh": { "command": "contextmesh-mcp" }
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

**It does not persist.** The server builds the bundled demo graph per process,
because `ContextGraph` serialises but has no `from_dict`. This version is worth
having to prove the protocol surface and how an agent consumes evidence paths;
it is not agent memory yet. Lossless graph persistence is the next core
milestone, and it is what makes an MCP server that loads real state possible.

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
  resolve.py                 entity resolution — one id per real-world thing
  pipeline.py                CHUNK → EXTRACT → RESOLVE → LINK → EMBED → PRUNE
  traverse.py                walks, evidence paths, the four dead-end reasons
  assumptions.py             versioned assumptions, blast radius, rejection
  execute.py                 re-runs exactly the closure an invalidation felled
  decisions.py               append-only decision history
  health.py                  the signals that make a graph quietly useless
  metrics.py                 the dashboard payload
  corpus.py  demo.py  cli.py the worked example
contextmesh_mcp/             read-only MCP server (optional extra, 3.10+)
  session.py  tools.py       plain Python over the engine; no SDK import
  resources.py  server.py    server.py is the only file that needs the SDK
dashboard/                   the rebuilt dashboard
docs/                        the capture spec and the architecture notes
examples/                    the original standalone control-layer sketch
tests/                       183 tests over the invariants above
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
