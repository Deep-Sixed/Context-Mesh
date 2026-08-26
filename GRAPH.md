# GRAPH.md — the ontology file

> Read on every write. If a write does not typecheck against this file, it does
> not enter the graph.

This is not documentation of the code; it is the schema the code loads. See
`contextmesh/ontology.py`, which parses this file at import time.

## Node types

| Type | Symbol | Means | Must carry |
|---|---|---|---|
| Entity | `entity` | A resolved real-world thing. One id per thing, forever. | `canonical`, `aliases` |
| Claim | `claim` | A statement lifted from a source. | `provenance` |
| Source | `source` | A document, span, run, or artifact that text came from. | `origin`, `retrieved_at` |
| Decision | `decision` | A choice that was made, with the reasoning attached. | `rationale`, `at_build` |
| Assumption | `assumption` | Something taken as true so work could proceed. Versioned. | `status`, `version` |
| Evidence | `evidence` | An observation that bears on a claim, decision, or assumption. | `kind` |

## Edge types

An edge is legal only if the pair `(source type, target type)` appears in its row.

| Edge | Legal pairs | Means |
|---|---|---|
| `mentions` | source→entity, claim→entity | The text refers to this resolved entity. |
| `derived_from` | claim→source, decision→claim, entity→source | This exists because that exists. |
| `cites` | decision→source, claim→claim | Explicit reference made by the author. |
| `contradicts` | claim→claim, evidence→claim, evidence→assumption | Both cannot hold. |
| `supports` | evidence→claim, claim→decision, evidence→decision | Raises confidence in the target. |
| `depends_on` | decision→assumption, decision→decision, claim→assumption | Target's failure invalidates the source. |
| `produces` | decision→entity, decision→source | The decision brought the target into being. |
| `supersedes` | decision→decision, assumption→assumption, claim→claim | Replaces, without deleting. |
| `justified_by` | decision→evidence, claim→evidence, assumption→evidence | The reason this was allowed to stand. |
| `resolves_to` | entity→entity | An alias node folding into its canonical id. |

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
5. A node is walkable only after `PRUNE`, and `PRUNE` drops what nothing walked.
6. Re-running invalidated work never revives a `decision` — it appends a new one
   that `supersedes` it, so rule 3 holds through re-execution. An `entity` *is*
   revived when the decision that produces it runs again, because the artefact
   was rebuilt; one the rerun stops producing stays invalidated.
7. An `assumption` is only ever rejected by `evidence` that `contradicts` it. A
   caller may not mark one false directly, because "why did this fall over" has
   to have an answer inside the graph.
