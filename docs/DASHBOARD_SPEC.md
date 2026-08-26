# Dashboard Spec — reverse-engineered from the source video

This file is the observational record behind `dashboard/index.html`. Everything
here was read off the original 42-second screen capture (1440×1790, 60fps,
one continuous scene — no cuts, no overlays, no cursor). It is kept in the repo
so the rebuild can be checked against the source instead of against memory.

## 1. Canvas

| Property | Value |
|---|---|
| Aspect | portrait, 1440 × 1790 (≈ 0.80) |
| Scene | single dashboard, always fully visible; nothing scrolls or transitions |
| Motion | every panel animates in place; the page itself never moves |
| Loop | ~18 s narrative cycle (four graph phases), counters drift monotonically |

## 2. Palette

Sampled directly from frame pixels.

| Token | Hex | Where |
|---|---|---|
| `--bone` | `#ede9e1` | page background, gutters |
| `--panel` | `#f7f5f1` | every panel interior |
| `--ink` | `#14120e` | titles, panel borders (1px), body text |
| `--rust` | `#c9604d` | primary accent, active stage fill, logo tile |
| `--rust-deep` | `#b8614f` | ledger bars |
| `--claim` | `#c15f4e` | node type 2 |
| `--source` | `#c79373` | node type 3 |
| `--decision` | `#e3d5b8` | node type 4 |
| `--rose` | `#dea296` | traversal grid cells, muted edges |
| `--green` | `#4f7c5f` | "good" numbers: walks/min, committed, deltas |
| `--green-soft` | `#82b795` | green sparkline stroke |

Type is monospace throughout. Labels are uppercase, letter-spaced, 9–11px.
Numbers are heavy weight. Nothing is anti-aliased into a gradient — flat fills,
hairline rules, corner brackets.

## 3. Panel inventory (top to bottom)

### 3.1 Masthead
- Rust tile with a hub-and-spoke glyph: one ringed centre node, four satellite
  nodes on the diagonals, cream spokes.
- `Claude Graph Engineering · Context Mesh` — "Graph Engineering" in rust.
  Recorded because it is what the frames show. The rebuild does **not** use this
  wording, here or in the two other places the capture carries it (§3.4 sub,
  §3.10 footer): it names an organisation this project has no connection to, and
  reproducing it would imply one. `dashboard/README.md` lists the substitutions.
- Kicker: `ONE GRAPH · FOUR NODE TYPES · EVERY ANSWER WALKS A PATH YOU CAN READ`
- Three live stats: `NODES RESOLVED` (ink), `EDGES / TICK` (rust), `WALKS / MIN` (green).
- Bar sparkline, bottom-aligned, mixed rust/grey bars, older bars fading left.
- Wall clock `HH:MM:SS`, and a green-bordered `TRAVERSAL <n>ms` badge.

Observed: `3,812 / 19 / 122 / 136ms` → `3,883 / 24 / 147 / 213ms` → `3,880 / 14 / 138 / 349ms`.

### 3.2 Build strip
`BUILD 234 · SPANS 1,165K · EDGES 1,442` then the six stages inline,
dashed leaders between them, the current stage in a filled rust box.
The strip's `EDGES` value always equals the build panel's `COMMITTED → WALKABLE`.

### 3.3 BUILD PATH — *what earns a node in the graph*
Six bracketed cards; the active one is filled rust with white text.

| # | Stage | Caption |
|---|---|---|
| 01 | CHUNK | `1.2M spans in` |
| 02 | EXTRACT | `entities + claims` |
| 03 | RESOLVE | `one id per thing` |
| 04 | LINK | `typed edges only` |
| 05 | EMBED | `vector on the node` |
| 06 | PRUNE | `nobody walked it` |

Badge: `STAGE 04 / 06`. Below the cards a dotted rail runs left→right with
travelling dots and a diamond gate labelled `RESOLVER` at centre.
Left counter `DROPPED AT RESOLVE` (rust, 4,161 → 5,760 over the clip),
right counter `COMMITTED → WALKABLE` (green, 1,387 → 1,920).

### 3.4 CONTEXT MESH · THE LIVE GRAPH · WALKING
- Sub: `WHAT CLAUDE RESOLVED, WIRED TO WHATEVER SHARES A TYPED EDGE`
- Left legend `NODE TYPES`: ENTITIES 32 · CLAIMS 24 · SOURCES 28 · DECISIONS 29
  (counts creep up across the clip: 33 / 27 / 31 / 32 by the end).
- Force layout, four type clusters, each with a bracketed label
  `TYPE 1 · ENTITIES · 32`. Labels track their cluster.
- The whole layout rotates slowly (footer says `4 NODE TYPES · TURNING`).
- Node size varies by degree; hub nodes get a concentric ring.
- Intra-cluster edges solid and faint; inter-cluster edges long dashed arcs.
- Small rust dots travel along edges — walks in flight.
- Top-right pill mirrors the current phase; right-hand caption box:

| Phase | Caption |
|---|---|
| `PHASE 1/4 · EXTRACT` | `THE PASS HANDS BACK WHAT IS WORTH A NODE` |
| `PHASE 2/4 · LINK` | `A SHARED ID BECOMES A TYPED EDGE` |
| `PHASE 3/4 · CLUSTER` | `THE TYPES PULL THEMSELVES APART` |
| `PHASE 4/4 · PUBLISH` | `HUBS WRITTEN OUT AS WALKABLE ANCHORS` |

Each phase saturates the node types it concerns and desaturates the rest.
Footer: `ONE NODE = ONE RESOLVED ENTITY · ONE EDGE = A TYPED RELATION BOTH SIDES AGREE ON`.

### 3.5 HOP BUDGET
`DEPTH BEFORE IT ANSWERS`, badge `MEDIAN 5 HOPS`. Column chart over hops 1–8,
counts printed above each column, median column filled rust and the rest sand.
A dashed vertical `p50` rule sits just right of the median column.
Footers: `HOPS PER ANSWER` · `FLAT RAG = 1 HOP, NO PATH`.

### 3.6 EDGE LEDGER
`WHAT CARRIES THE TRAFFIC`, badge `1.30M EDGES`. Four rows —
`MENTIONS 440K`, `DERIVED FROM 355K`, `CITES 310K`, `CONTRADICTS 194K` —
each a track with a tick-textured fill and a small ◄ caret marking that type's
share of traversals. Footers: `SHARE OF TRAVERSALS` · `UNTYPED EDGES: 0` (green).

### 3.7 WALK VS FLAT
`TOKENS PER ANSWER`, badge `−97% TOKENS`. Two stacked area series scrolling
right to left: `FLAT TOP-K` riding near 120K, `TYPED WALK` pinned near 4K.
Right edge is annotated with both endpoints. Footer right: `SAME ANSWER`.

### 3.8 THE TRAVERSAL GRID
`EVERY WALK SINCE THE GRAPH WAS BUILT · 1,008 CELLS`, `LIVE` pill.
72 columns × 14 rows. A filled rounded square is a walk that resolved on the
graph; a hollow ring is a dead end. The newest few cells flash a darker rust.
Legend: `ONE CELL = ONE WALK · ■ RESOLVED ON THE GRAPH · ○ DEAD END`.
Footer: `WALK 01 CRAWLS THE WHOLE MESH · WALK 10 TOUCHES FOUR NODES — THE PATH
IS TYPED AND STORED, NOT GUESSED AGAIN PER QUESTION`.

### 3.9 DEAD-END LEDGER
Badge `RESOLVED 67.8%`. `WALKS THAT ENDED NOWHERE` over a large rust count
(325 → 333). Four reason bars with right-aligned counts:
`NO TYPED EDGE 87` · `ENTITY UNRESOLVED 74` · `WRONG NODE TYPE 88` ·
`PRUNED TOO EARLY 76`.
Inset card: `THE ONTOLOGY FILE · READ ON EVERY WRITE` / `GRAPH.md` / a green
`WALK TIME` sparkline spanning `30s`, and three green stats:
`EDGE TYPES 50` · `WALK TIME −97%` · `ORPHANS 1`.

### 3.10 Footer rule
`A TYPED EDGE BEATS A TOP-K GUESS · THE GRAPH IS WHAT SURVIVES INTO THE NEXT QUESTION`
left, `CLAUDE · GRAPH ENGINEERING` right.

## 4. What the video asserts, restated as invariants

These are the claims the dashboard makes. `contextmesh/` implements them so the
numbers on the page are produced rather than scripted.

1. A node is only in the graph if it survived all six stages **and** something walked it.
2. Every edge carries a type from a closed vocabulary — `UNTYPED EDGES: 0` is an
   enforced invariant, not a metric.
3. An answer is a path. Depth is the cost, and the cost is reported (hop budget).
4. Flat top-k spends ~30× the tokens for the same answer and returns no path.
5. Walks that fail are kept and classified into exactly four failure modes.
6. The ontology (`GRAPH.md`) is read on every write, so edge types cannot drift.
