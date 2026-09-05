# GRAPH.md — the ontology file

> Read on every write. If a write does not typecheck against this file, it does
> not enter the graph.

This is not documentation of the code; it is the schema the code loads. See
`contextmesh/ontology.py`, which parses this file at import time.

## Reading this file

**Authority.** This file is authoritative for the node types, the edge types and
their legal pairs, and the rules below. Where an implementation and this file
disagree about any of those, this file is right and the implementation is a bug.

**What is mechanically enforced.** `ontology.py` parses the `Type` and `Symbol`
columns of the node table, the `Must carry` column of the node table, and the
`Edge`, `Legal pairs` and `Invalidation` columns of the edge table; every write
is checked against them, and `tests/test_ontology.py` asserts that the
`NodeType` and `EdgeType` enums equal what this file declares. `Means` is prose
and is not parsed. The rules below are enforced, or not, one at a time — the
*Declared, not yet realized* section names the ones that currently are not.

**What `Must carry` means.** It is a *minimum*, not an exhaustive list: a node
may carry more. It is satisfied by a node `attrs` key **or** by a `Node` field of
that name — `provenance` is a field, not an attribute. It requires the value to
be **present**, not to be well-formed: `contextmesh/execute.py` mints sources
with `retrieved_at="at plan time"`, which satisfies the presence requirement
defined here, and the temporal layer separately classifies such a source as
`UNDATED` rather than guessing a date. `ContextGraph.add_node` enforces this at
the write boundary — presence only, nothing more — and refuses a node missing a
required name with `OntologyError`. Every path that constructs a node,
including restoring a persisted snapshot, goes through `add_node`, so this is
one check rather than one per caller.

**What edge-level assumption binding means.** `Edge.assumption_id` makes one
relationship conditional on one assumption: the edge holds only while that
assumption stands. Rejecting the assumption invalidates the bound edge
directly — `apply_invalidation` marks it regardless of what else falls — but
binding an edge is not a second way to reach a node. `src` and `dst` fall only
if rule 2's propagation reaches them along an edge of a propagating type; a
bound `mentions` or `supports` edge, say, does not pull its endpoints down
just because it was bound. A node that itself needs to fall when an
assumption falls has to say so explicitly with `depends_on` — rule 2 is the
one mechanism for node-to-node invalidation, and edge binding does not open a
second one. `AssumptionLedger.justifies` enforces the binding side of this at
the write boundary: the assumption must exist and be active, the edge must
exist and be live, binding the same pair again is a no-op, and binding an
edge already bound to a *different* assumption is refused rather than
silently replaced. `ContextGraph.add_edge` takes no `assumption_id`, so it
is not a second, unguarded way to bind one; only snapshot restoration sets
the field directly, and it validates what it restores on its own terms.

**What walk-driven PRUNE means.** Rule 5's second half drops a node only once
an observation window has closed: `Pipeline.prune_unwalked_nodes` takes the
`Walker` that produced the telemetry, and is a no-op below
`MIN_OBSERVATION_WALKS` walks in that window, so one question failing to reach
a node is never mistaken for the corpus having judged it irrelevant.
`Node.walks` — incremented once per node an actual walk visits, in
`Walker.walk` — is the only signal a node's own eligibility is judged on;
degree, inherited unchanged from the build-time half this pass has always
shared it with, is the one other factor, and nothing about a node's label,
age, or type enters the decision beyond `source`, `assumption`, and
`evidence` nodes being preserved outright — an assumption nothing has
questioned yet is not thereby irrelevant, and evidence is what rule 7
requires an audit be able to find. A node this pass drops cannot be revived
by a later walk: `Walker.seed` will not seed a pruned node and
`out_edges`/`in_edges` will not traverse into one, so its `walks` count is
frozen at whatever it was the moment it fell. A node already not live for a
different reason — invalidated by a rejected assumption — is left alone
rather than separately marked pruned, so this pass never creates a second
state change on top of one that already happened. The demo's export path
(`contextmesh/demo.py`) never opts in, so this pass changes nothing about the
dashboard unless a caller explicitly asks for it.

**What a node/edge id collision means.** `slug` (`contextmesh/model.py`) truncates
its digest to keep ids short, so it is not injective: two different node
labels, or two different `(src, type, dst)` edge triples, can derive the same
id. `add_node` and `add_edge` are also the merge path for a genuine repeat
observation — the same claim text extracted from a second document, the same
`mentions` edge asserted twice — so a shared id has to be told apart from a
coincidence before it is treated as one. For a node, "the same thing" means
the existing node's `type` and `label` agree with the incoming call; `attrs`
is never part of that comparison, since a repeat observation may legitimately
carry new metadata the first one did not. For an edge, "the same thing" means
the existing edge's `(src, type, dst)` triple — not merely its id — matches.
A collision found either way is refused with `OntologyError` before anything
is written: no node's `label`/`type` is silently overwritten, no edge's
`(src, type, dst)` is silently repointed, and none of the internal indexes
(`edges`, `_edge_key`, `_out`, `_in`) are touched. `from_dict` inherits this
for free, since every node and edge it restores goes through `add_node` and
`add_edge` the same as a live write — but a snapshot's own `nodes`/`edges`
lists are also checked for two rows sharing a literal id before either method
ever runs, since `to_dict` can never produce that either. This is
collision *detection*, not a stronger id scheme: `slug`'s truncation is
unchanged, and a caller that keeps minting distinct content into the same
id space will eventually have a write refused rather than silently lose one.

**Code is evidence, not authority.** A behaviour may be written into this file
when the implementation *structurally guarantees* it — when no caller can make
it false without changing the implementation's own contract. A behaviour that is
merely one unconstrained implementation choice among several does not become
normative by having been written first; it requires a decision recorded here.

## Node types

| Type | Symbol | Means | Must carry |
|---|---|---|---|
| Entity | `entity` | A resolved real-world thing. One id per thing, forever. | `canonical`, `aliases` |
| Claim | `claim` | A statement lifted from a source. | `provenance` |
| Source | `source` | A document, span, run, or artifact that text came from. | `origin`, `retrieved_at` |
| Decision | `decision` | A choice that was made, with the reasoning attached. | `rationale`, `provenance` |
| Assumption | `assumption` | Something taken as true so work could proceed. Versioned. | `status`, `version` |
| Evidence | `evidence` | An observation that bears on a claim, decision, or assumption. | `kind` |

## Edge types

An edge is legal only if the pair `(source type, target type)` appears in its row.

`Invalidation` is rule 2's propagation direction, machine-readable: `backward`
means the edge is walked from target to source when an assumption falls (the
thing that depends falls when its ground falls), `forward` means source to
target (an artefact falls with the decision that made it), and `none` means
the edge never propagates a fall. `contextmesh/assumptions.py` reads this
column through `ontology.py` rather than restating it, so the two cannot
disagree.

| Edge | Legal pairs | Invalidation | Means |
|---|---|---|---|
| `mentions` | source→entity, claim→entity | none | The text refers to this resolved entity. |
| `derived_from` | claim→source, decision→claim, entity→source | backward | This exists because that exists. |
| `cites` | decision→source, claim→claim | none | Explicit reference made by the author. |
| `contradicts` | claim→claim, evidence→claim, evidence→assumption | none | Both cannot hold. |
| `supports` | evidence→claim, claim→decision, evidence→decision | none | Raises confidence in the target. |
| `depends_on` | decision→assumption, decision→decision, claim→assumption | backward | Target's failure invalidates the source. |
| `produces` | decision→entity, decision→source | forward | The decision brought the target into being. |
| `supersedes` | decision→decision, assumption→assumption, claim→claim | none | Replaces, without deleting. |
| `justified_by` | decision→evidence, claim→evidence, assumption→evidence | none | The reason this was allowed to stand. |
| `resolves_to` | entity→entity | none | An alias node folding into its canonical id. |

## Rules

1. `untyped_edges == 0`. There is no constructor for an edge without a type.
2. Invalidation propagates along three edges and no others, and direction
   matters: `depends_on` and `derived_from` are followed *backwards* (the thing
   that depends falls when its ground falls), `produces` is followed *forwards*
   (an artefact falls with the decision that made it). `mentions`, `cites`,
   `supports` and `supersedes` never propagate — that is what makes
   invalidation selective rather than a purge.
3. `supersedes` never deletes. Decision history is append-only.
4. Every `claim` and `decision` carries provenance to a `source`. A node without
   a path to a source is an orphan and is reported by graph health.
5. A node is walkable only after `PRUNE`. `PRUNE` has two halves: the build-time
   pass drops what nothing *linked* (degree zero), and a second pass,
   `Pipeline.prune_unwalked_nodes`, drops what an observation window of walks
   left untouched — see *What walk-driven PRUNE means*.
6. Re-running invalidated work never revives a `decision` — it appends a new one
   that `supersedes` it, so rule 3 holds through re-execution. An `entity` *is*
   revived when the decision that produces it runs again, because the artefact
   was rebuilt; one the rerun stops producing stays invalidated.
7. An `assumption` is only ever rejected by `evidence` that `contradicts` it. A
   caller may not mark one false directly, because "why did this fall over" has
   to have an answer inside the graph.
8. A node or edge id names one semantic identity. A derived id that would
   collide with a different node or edge is refused, never silently merged
   into it — see *What a node/edge id collision means*.

## Declared, not yet realized

Each of these is declared here or in the type system, is modelled and in some
cases persisted and validated, and has **no production path**. They are recorded
so a reader does not mistake a declaration for a live capability, and so that
deciding their intent is a visible act rather than an accident of first use.
None of them may be relied on until it appears above this line.

| Declared | State | Missing |
|---|---|---|
| `resolves_to` | In the edge table; in `EdgeType`; weighted `0.3` by the walker | No realized lifecycle today: no production writer creates it, and no node-level justification, contradiction, supersession, support, dependency or citation path is legal for `entity` endpoints. |

## Open items

Questions this file does not yet answer. Each is a decision, not a bug; none is
resolved by pointing at current behaviour.

1. **A decision's processing-time field has no name here.** The `Must carry`
   cell for Decision previously read `at_build`, which names nothing on a
   decision node: the node carries `Node.build`, while `at_build` lives on
   `DecisionRecord`, which is not part of the graph snapshot. The false token is
   removed; naming the intended field is open.

Resolved since the list above was first written, kept here as a record rather
than deleted, since a reader following an old reference should land somewhere
that says what happened to it:

- **`Symbol` was unverified.** `ontology.py` now reads the column and refuses
  to load if it disagrees with `Type`; see *What is mechanically enforced*.
- **Rule 2's propagation set was restated in code.** The edge table's
  `Invalidation` column is now that machine-readable form, and
  `contextmesh/assumptions.py` reads it through `ontology.py` instead of
  restating it.
- **`AssumptionLedger.justifies` propagated further than its wording.** Its
  docstring marked an *edge* as standing on an assumption, but rejection
  reached through the edge to invalidate `edge.dst` too — a second,
  undeclared invalidation path alongside rule 2. Binding an edge no longer
  seeds its endpoints into the blast radius; see *What edge-level assumption
  binding means*. A node that needs to fall with the assumption still has to
  say so with `depends_on`, the same as every other node-to-node case.
- **`Must carry` had no enforcement path.** `ContextGraph.add_node` now
  enforces it; see *What `Must carry` means*.
- **Walk-driven `PRUNE` had no caller.** `Pipeline.prune_unwalked_nodes` now
  takes the `Walker` that produced the telemetry and runs for real, gated on
  an observation window rather than firing on the first walk that happened
  not to reach a node; see rule 5 and *What walk-driven PRUNE means*.

Out of scope for this file today: how entity identity should be asserted,
recorded or retracted. `resolves_to` exists as a declaration only, and the
design that would give it a lifecycle is not settled.
