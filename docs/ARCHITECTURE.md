# Architecture

Nine modules, each one idea. This is the reasoning behind them — the code is the
reference, this is why it looks the way it does.

## The ontology is loaded, not written down twice

`GRAPH.md` is parsed by `contextmesh/ontology.py` at import time. Node types,
edge types and every legal `(source type, target type)` pair come out of the
markdown tables in that file, and `ContextGraph.add_edge` checks against them.

The alternative — an enum in the code and a table in the docs — has one failure
mode: they drift, and the docs lose. Here they cannot drift, because there is
one copy. `tests/test_ontology.py` asserts the file and the `NodeType` /
`EdgeType` enums agree, so adding an edge type means editing `GRAPH.md` first.

## Typed edges, structurally

There is no `add_edge(src, dst)` without a type, and no `type="other"` escape
hatch. An illegal pair raises `OntologyError`; the pipeline catches that and
counts a *dropped* edge. `untyped_edges` is a computed property that sums edges
whose type is not an `EdgeType` — it returns 0 because it cannot return anything
else, and it exists so the claim stays checkable rather than assumed.

## Entity resolution drops rather than guesses

`Resolver` normalises (casefold, strip punctuation, drop articles and corporate
forms), blocks on prefixes and acronyms, then scores candidates by token Jaccard
with three overrides:

- **squashed equality** — `PG Vector` and `pgvector` are the same string with the
  spaces argued about, and token overlap scores that pair at zero;
- **containment**, but only when the smaller side carries two tokens or one
  distinctive one — `index` must not swallow `HNSW index`;
- **acronym match** — `HNSW` for `Hierarchical Navigable Small World`.

The threshold is 0.62 and deliberately high. A wrong merge corrupts every walk
that later crosses the entity; a dropped mention costs one span, and the drop is
recorded in the log rather than swallowed.

Two normalisations that look helpful are deliberately absent: version numbers
and words like `service` are **not** stripped. A v2 encoder is not a v3 encoder,
and `Retrieval Service` is the name of a thing, not a service called Retrieval.

## The pipeline reports what it refused

Each of the six stages returns admitted and dropped counts. `EXTRACT` keeps a
span only if it matches an explicit verb lexicon — the lexicon is the
extractor's contract, and a span that misses it is dropped and counted. `RESOLVE`
is where most of the corpus dies, which is why the dashboard gives that number
its own counter.

`_candidate_mentions` offers the resolver single words *and* two- and three-word
phrases, because most real entity names are phrases. Without n-grams, entities
like `build time` and `resident memory` sit in the graph with nothing said about
them — which `health.check` reports as `missing_relationships`.

## A walk is the answer, and the answer is a path

`Walker.walk` does a uniform-cost search over typed edges. Three decisions shape
what comes out:

1. **Edges are read backwards only towards whoever asserted.** A claim mentions
   an entity; getting from the entity to the claim means reading that edge in
   reverse. Reversing into an entity, though, just hops through a hub, so
   reversal targets are restricted to claims, decisions and evidence.
2. **Crossing a hub costs more.** A source that every claim derives from is a
   cheap bridge to everywhere and tells you nothing. Without a degree penalty
   the shortest path between any two nodes runs through a hub and the "evidence
   path" reads like a phone directory.
3. **The walk pays for the justification.** Having found the best answering
   node, it keeps going — to the claim that supports it, the source it came
   from, the assumption it rests on. An answer that stops at the claim is a
   snippet.

Answer selection blends cosine against the hashed embedding with literal token
overlap, 45/55. Vectors alone accept coincidences; overlap alone misses
paraphrase. `DIM = 512` because at 96 the collision noise between unrelated short
texts beat the signal.

### The four dead ends

Every failure is one of these, and the reason is recorded, not the guess:

| Reason | What happened | What fixes it |
|---|---|---|
| `entity_unresolved` | No mention resolved to a canonical entity | Extend the alias table, or accept that the corpus does not cover it |
| `no_typed_edge` | The seed has no outgoing edge in the policy | Link it at the next LINK, or let PRUNE drop it |
| `wrong_node_type` | Reached an answering node that does not answer this | More extraction on that part of the corpus |
| `pruned_too_early` | The path needed a node PRUNE removed, or a rejected assumption invalidated | Re-run the build, or re-execute against the replacement assumption |

## Invalidation is directional

Rejecting an assumption invalidates exactly the transitive closure of:

- `depends_on` **backwards** — the thing that depends falls when its ground falls;
- `derived_from` **backwards** — same;
- `produces` **forwards** — an artefact falls with the decision that made it.

Nothing else propagates. `mentions`, `cites`, `supports` and `supersedes` all
survive, which is what makes invalidation selective rather than a purge. The
report carries the preserved set as well as the invalidated one, because "we
invalidated something" is not a useful answer without "and here is what we
deliberately kept". Every invalidated node comes with the readable chain that
made it depend on the assumption.

Nothing is deleted. Rejected assumptions and invalidated decisions stay in the
graph flagged, so an audit can still walk to them.

## Token accounting

`tokens ≈ words × 1.3`. A walk carries the nodes on its path and nothing else. A
flat top-k answer carries `k` chunks plus 160 tokens of window each — and it
carries the same budget whatever the question, which is its whole problem. The
sample is keyed on a stable hash of the question so an export is reproducible.

On the shipped corpus that is roughly 60 tokens against 7,300. The ratio is
large partly because these claims are single sentences; a corpus of long
documents would narrow it.

## What the dashboard is allowed to invent

Nothing. See `dashboard/README.md` — the live stream is a resampling of
distributions the engine measured, and a reason that never occurred renders as
zero rather than being filled in for looks.
