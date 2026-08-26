# The dashboard

A rebuild of the screen capture this repository was reverse-engineered from.
`docs/DASHBOARD_SPEC.md` is the observational record of the original; this
directory is the reconstruction.

```
python -m contextmesh export --inline    # build the graph, write this page's data
open dashboard/index.html                # works from file://, no server needed
```

`context-mesh.html` in this directory is the same page as one self-contained
file — open it, mail it, drop it on a static host, nothing else needed. It is
generated, so regenerate it whenever the page or the data changes:

```
python dashboard/bundle.py dashboard/context-mesh.html
python dashboard/bundle.py --artifact f  # same, minus the document skeleton,
                                         # for hosts that supply their own
```

CI fails if the committed copy has drifted from its sources. Committing a build
product is only worth it when something notices it going stale.

`index.html` is the layout and the palette, `mesh.js` is the renderer, and
`data/mesh.json` is a real export. `--inline` also writes that JSON into the
`<script id="mesh-data">` block so the page works when opened directly; served
over HTTP it re-fetches `data/mesh.json`, so a fresh export shows up on reload.

## Every panel is a query against the graph

| Panel | Where its numbers come from |
|---|---|
| `NODES RESOLVED` | live nodes in `ContextGraph` |
| `EDGES / TICK`, `WALKS / MIN`, `TRAVERSAL ms` | `Walker` ledger |
| Build strip, `BUILD PATH` | `BuildReport` — one row per pipeline stage |
| `DROPPED AT RESOLVE` | mentions `Resolver` refused to fold into an entity |
| `COMMITTED → WALKABLE` | edges that survived `PRUNE` |
| Live graph | `graph.nodes` / `graph.edges`, four display types, real degrees |
| `HOP BUDGET` | `Walker.hop_histogram()` over answered walks |
| `EDGE LEDGER` | `graph.edge_counts()`; the caret is each type's share of traversals |
| `UNTYPED EDGES: 0` | `graph.untyped_edges` — a computed invariant, not a target |
| `WALK VS FLAT` | `Walk.tokens_walked` against `Walk.tokens_flat` |
| `TRAVERSAL GRID` | one cell per walk, resolved or dead |
| `DEAD-END LEDGER` | `Walker.dead_end_ledger()`, by reason |
| `GRAPH.md` card | `Ontology` — edge types declared vs used, and orphan count |

## What is live and what is a snapshot

The export is a snapshot; the page is a stream. New walks are **resampled from
the measured distributions** in that snapshot — the resolved rate, the hop
histogram, the edge traffic split, the dead-end reasons — so the motion is a
replay of real measurements at a watchable rate. The build counters advance the
same way, at the ratios the real build produced.

Nothing on the page is a number the engine did not produce. Where a reason never
occurred, its row stays at zero and is dimmed rather than filled in for looks —
`NO TYPED EDGE` is usually 0 on this corpus, because after `PRUNE` every seed
entity has somewhere to go.

## Deliberate departures from the capture

- **Claims are drawn a shade deeper than entities.** In the capture the two reds
  sit almost on top of each other; at this node count they need separating.
- **Cluster distance scales with cluster size.** The real corpus produces 47
  entities and 3 decisions. Parking both at the same radius leaves a third of
  the panel holding three dots.
- **Counts are ours, not the capture's.** The original shows a graph with about
  113 nodes in four evenly sized clusters. This one shows whatever
  `contextmesh/corpus.py` built. Matching the original's numbers would have
  meant hard-coding them.
