# Context Mesh

**Claude Graph Engineering · Context Mesh**

> One graph · four node types · every answer walks a path you can read

Context Mesh is a typed context graph for long-running agents. It keeps not only
*what* is connected, but *why* — where a claim came from, what a decision rested
on, and what stops being true when an assumption fails.

This repository is a working implementation, reverse-engineered from a screen
capture of the dashboard. The dashboard is rebuilt too, and every number on it
is computed by the engine rather than written into the page.

```bash
git clone https://github.com/charlessnydercareer/Context-Mesh
cd Context-Mesh

python -m contextmesh demo            # build, walk, break an assumption, report
python -m contextmesh ask "Why did the Index Builder run out of memory?"
python -m contextmesh health          # what is quietly wrong with the graph
python -m contextmesh invalidate      # reject an assumption, see the fallout
python -m contextmesh export --inline # regenerate the dashboard's data
```

No dependencies. Python 3.9+. `python -m unittest discover -s tests` runs the
suite (84 tests).

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
  decisions.py               append-only decision history
  health.py                  the signals that make a graph quietly useless
  metrics.py                 the dashboard payload
  corpus.py  demo.py  cli.py the worked example
dashboard/                   the rebuilt dashboard
docs/                        the capture spec and the architecture notes
examples/                    the original standalone control-layer sketch
tests/                       84 tests over the invariants above
```

## Where it fits

Context Mesh complements retrieval and agent memory rather than replacing them.
Retrieval supplies the source material; memory supplies history; Context Mesh
supplies the typed relations and the provenance that make an answer auditable —
and, on this corpus, does it for about 0.7% of the tokens flat top-k would
spend, because it sends the nodes on the path instead of the top forty chunks.

> The graph should remember not only what is connected, but why it is connected.

## Licence

MIT — see [LICENSE](LICENSE).
