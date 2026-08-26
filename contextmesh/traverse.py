"""Answering by walking.

A walk starts at resolved entities, follows typed edges under a policy, picks
the best answering node it can reach inside the hop budget, and then keeps
walking to that node's justification. The path it returns *is* the answer's
reasoning — there is no separate explain step reconstructing a rationale
afterwards.

Failed walks are kept, not discarded, and classified into the four failure modes
the dashboard's dead-end ledger reports.
"""

from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .embed import cosine, embed, tokens
from .graph import ContextGraph
from .model import Edge, EdgeType, Node, NodeType
from .resolve import Resolver

#: Traversal follows provenance-bearing edges first. `contradicts` is always
#: followed, because an answer that steps over its own counter-evidence is wrong.
DEFAULT_POLICY: Tuple[EdgeType, ...] = (
    EdgeType.MENTIONS,
    EdgeType.DERIVED_FROM,
    EdgeType.SUPPORTS,
    EdgeType.CITES,
    EdgeType.CONTRADICTS,
    EdgeType.DEPENDS_ON,
    EdgeType.PRODUCES,
    EdgeType.SUPERSEDES,
    EdgeType.JUSTIFIED_BY,
)

#: Edge types worth following backwards. A claim mentions an entity, so getting
#: from the entity to the claim means reading that edge in reverse.
REVERSIBLE: Tuple[EdgeType, ...] = (
    EdgeType.MENTIONS,
    EdgeType.SUPPORTS,
    EdgeType.DERIVED_FROM,
    EdgeType.PRODUCES,
)

#: Node types that can terminate a walk with an answer.
ANSWERING: Tuple[NodeType, ...] = (NodeType.CLAIM, NodeType.DECISION)

#: Node types that assert something, and so are worth reaching backwards.
ASSERTING: Tuple[NodeType, ...] = (NodeType.CLAIM, NodeType.DECISION, NodeType.EVIDENCE)

#: Cost per edge type. The cheap edges are the ones that carry provenance.
EDGE_COST: Dict[EdgeType, float] = {
    EdgeType.DERIVED_FROM: 0.6,
    EdgeType.SUPPORTS: 0.7,
    EdgeType.CITES: 0.8,
    EdgeType.JUSTIFIED_BY: 0.8,
    EdgeType.CONTRADICTS: 0.9,
    EdgeType.MENTIONS: 1.0,
    EdgeType.DEPENDS_ON: 1.0,
    EdgeType.PRODUCES: 1.1,
    EdgeType.SUPERSEDES: 1.2,
    EdgeType.RESOLVES_TO: 0.3,
}

#: A candidate must clear this to count as an answer rather than a near miss.
ANSWER_FLOOR = 0.18
#: A vector-only seed needs this much cosine *and* a shared token to be trusted.
VECTOR_FLOOR = 0.45


class DeadEnd(str, Enum):
    """The only four ways a walk fails. The dashboard tallies exactly these."""

    NO_TYPED_EDGE = "no_typed_edge"
    ENTITY_UNRESOLVED = "entity_unresolved"
    WRONG_NODE_TYPE = "wrong_node_type"
    PRUNED_TOO_EARLY = "pruned_too_early"


@dataclass
class Step:
    edge_type: Optional[EdgeType]
    node_id: str
    node_type: NodeType
    label: str
    reverse: bool = False

    def render(self, width: int = 74) -> str:
        label = self.label if len(self.label) <= width else self.label[: width - 1] + "…"
        if self.edge_type is None:
            return f"({self.node_type.value}) {label}"
        arrow = (
            f"<-[{self.edge_type.value}]-"
            if self.reverse
            else f"-[{self.edge_type.value}]->"
        )
        return f"{arrow} ({self.node_type.value}) {label}"


@dataclass
class Walk:
    question: str
    seeds: List[str] = field(default_factory=list)
    steps: List[Step] = field(default_factory=list)
    answer_id: Optional[str] = None
    score: float = 0.0
    dead_end: Optional[DeadEnd] = None
    detail: str = ""
    hops: int = 0
    cost: float = 0.0
    visited: int = 0
    evidence: List[str] = field(default_factory=list)
    tokens_walked: int = 0
    tokens_flat: int = 0

    @property
    def resolved(self) -> bool:
        return self.answer_id is not None

    def path(self) -> str:
        """The readable evidence path. This is what an audit reads."""
        return "\n".join("  " * i + step.render() for i, step in enumerate(self.steps))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "seeds": list(self.seeds),
            "resolved": self.resolved,
            "answer_id": self.answer_id,
            "score": round(self.score, 4),
            "dead_end": self.dead_end.value if self.dead_end else None,
            "detail": self.detail,
            "hops": self.hops,
            "cost": round(self.cost, 3),
            "visited": self.visited,
            "evidence": list(self.evidence),
            "tokens_walked": self.tokens_walked,
            "tokens_flat": self.tokens_flat,
            "path": [s.render() for s in self.steps],
        }


class Walker:
    """Runs walks and keeps the ledger of what they cost and where they died."""

    def __init__(
        self,
        graph: ContextGraph,
        resolver: Resolver,
        *,
        hop_budget: int = 6,
        policy: Sequence[EdgeType] = DEFAULT_POLICY,
        flat_k: int = 40,
        max_expand: int = 260,
    ) -> None:
        self.graph = graph
        self.resolver = resolver
        self.hop_budget = hop_budget
        self.policy = tuple(policy)
        self.flat_k = flat_k
        self.max_expand = max_expand
        self.walks: List[Walk] = []

    # ── seeds ────────────────────────────────────────────────────────────
    def seed(self, question: str) -> Tuple[List[str], Optional[DeadEnd], str]:
        """Resolve the question's mentions to canonical entities."""
        mentions = _question_mentions(question)
        q_tokens = set(tokens(question))
        seeds: List[str] = []
        unresolved: List[str] = []
        pruned_hit: Optional[Node] = None

        for mention in mentions:
            record = self.resolver.resolve(mention)
            if not record.resolved or record.canonical_id not in self.graph.nodes:
                unresolved.append(mention)
                continue
            node = self.graph.node(record.canonical_id)
            if node.pruned or node.invalidated:
                pruned_hit = node
                continue
            if node.id not in seeds:
                seeds.append(node.id)

        if seeds:
            return seeds, None, ""
        if pruned_hit is not None:
            why = (
                "invalidated by a rejected assumption"
                if pruned_hit.invalidated
                else "dropped by PRUNE because nothing walked it"
            )
            return [], DeadEnd.PRUNED_TOO_EARLY, (
                f"{pruned_hit.label!r} resolved, but is no longer walkable: {why}"
            )

        # No entity resolved. A question can still name a document or quote a
        # claim, so try a literal match next — but demand two shared content
        # words, because one is a coincidence.
        lexical_best, lexical_overlap = None, 1
        for node in self.graph.nodes.values():
            if not node.live or node.type is NodeType.ASSUMPTION:
                continue
            overlap = len(q_tokens & set(tokens(node.label)))
            if overlap > lexical_overlap:
                lexical_best, lexical_overlap = node.id, overlap
        if lexical_best is not None:
            return [lexical_best], None, f"literal match on {lexical_overlap} tokens"

        # Last resort: the vectors EMBED attached, but only where the vector
        # agrees with a literal token too. A vector match with no shared word is
        # a coincidence, not a resolution.
        best_id, best_score = None, 0.0
        vector = embed(question)
        for node in self.graph.nodes.values():
            if not node.live or node.embedding is None:
                continue
            if node.type not in (NodeType.ENTITY, NodeType.CLAIM, NodeType.DECISION):
                continue
            if not (q_tokens & set(tokens(node.label))):
                continue
            score = cosine(vector, node.embedding)
            if score > best_score:
                best_id, best_score = node.id, score
        if best_id is not None and best_score >= VECTOR_FLOOR:
            return [best_id], None, f"vector fallback at {best_score:.2f}"

        return [], DeadEnd.ENTITY_UNRESOLVED, (
            "no mention resolved to a canonical entity: "
            + ", ".join(repr(m) for m in unresolved[:4])
        )

    # ── the walk ─────────────────────────────────────────────────────────
    def walk(self, question: str) -> Walk:
        walk = Walk(question=question)
        seeds, dead_end, detail = self.seed(question)
        walk.seeds = seeds
        walk.tokens_flat = self._flat_cost(question)

        if dead_end is not None:
            walk.dead_end = dead_end
            walk.detail = detail
            self.walks.append(walk)
            return walk

        graph = self.graph
        vector = embed(question)
        q_tokens = set(tokens(question))

        frontier: List[Tuple[float, int, str, List[Step]]] = []
        counter = 0
        for seed_id in seeds:
            node = graph.node(seed_id)
            heapq.heappush(
                frontier, (0.0, counter, seed_id, [Step(None, seed_id, node.type, node.label)])
            )
            counter += 1

        candidates: List[Tuple[float, float, List[Step]]] = []
        near_miss: Optional[List[Step]] = None
        saw_pruned = False
        expanded = 0
        seen: Set[str] = set()

        while frontier and expanded < self.max_expand:
            cost, _, node_id, steps = heapq.heappop(frontier)
            if node_id in seen:
                continue
            seen.add(node_id)
            expanded += 1
            node = graph.node(node_id)
            node.walks += 1
            hops = len(steps) - 1

            if node.type in ANSWERING and hops >= 1:
                score = self._relevance(vector, q_tokens, node)
                if score >= ANSWER_FLOOR:
                    candidates.append((score, cost, steps))
                elif near_miss is None:
                    near_miss = steps

            if hops >= self.hop_budget:
                continue

            expansions = self._expand(node_id)
            if not expansions:
                if any(
                    not graph.nodes[e.dst].live
                    for e in graph.out_edges(node_id, self.policy, live_only=False)
                ):
                    saw_pruned = True
                continue

            for edge, reverse in expansions:
                nxt = graph.nodes[edge.dst]
                if not nxt.live:
                    saw_pruned = True
                    continue
                if nxt.id in seen:
                    continue
                step_cost = self._step_cost(edge)
                heapq.heappush(
                    frontier,
                    (
                        cost + step_cost,
                        counter,
                        nxt.id,
                        [*steps, Step(edge.type, nxt.id, nxt.type, nxt.label, reverse)],
                    ),
                )
                counter += 1

        walk.visited = expanded

        if not candidates:
            if saw_pruned:
                walk.dead_end = DeadEnd.PRUNED_TOO_EARLY
                walk.detail = "the only continuation ran through a node PRUNE removed"
            elif near_miss is not None:
                walk.dead_end = DeadEnd.WRONG_NODE_TYPE
                walk.steps = near_miss
                walk.hops = len(near_miss) - 1
                walk.detail = (
                    "reached a node of an answering type that does not answer this question"
                )
            elif expanded <= len(seeds):
                walk.dead_end = DeadEnd.NO_TYPED_EDGE
                walk.detail = "seed entity has no outgoing edge in the traversal policy"
            else:
                walk.dead_end = DeadEnd.NO_TYPED_EDGE
                walk.detail = (
                    f"exhausted {expanded} nodes inside a {self.hop_budget}-hop budget "
                    "without reaching an answering node"
                )
            self.walks.append(walk)
            return walk

        # Best answer. Where scores are within a rounding step of each other,
        # prefer the path that crossed more distinct relations — the better
        # evidenced answer, not the shortest one.
        score, cost, steps = max(
            candidates,
            key=lambda c: (round(c[0], 2), _distinct_relations(c[2]), -c[1]),
        )
        steps = self._justify(steps)

        walk.answer_id = steps[_answer_index(steps)].node_id
        walk.score = score
        walk.steps = steps
        walk.hops = len(steps) - 1
        walk.cost = cost
        walk.evidence = self._evidence_for(walk.answer_id)
        walk.tokens_walked = self._walk_cost(steps)
        self._credit(steps)
        self.walks.append(walk)
        return walk

    def ask(self, question: str) -> Walk:
        return self.walk(question)

    # ── expansion ────────────────────────────────────────────────────────
    def _expand(self, node_id: str) -> List[Tuple[Edge, bool]]:
        """Forward along the policy, backward only towards whoever asserted.

        Reading an edge backwards answers "who said this?", so the useful
        reversals land on a claim or a decision. An entity never asserts
        anything, so reversing into one just hops through a hub.
        """
        graph = self.graph
        out: List[Tuple[Edge, bool]] = [
            (e, False) for e in graph.out_edges(node_id, self.policy)
        ]
        for edge in graph.in_edges(node_id, REVERSIBLE):
            if graph.nodes[edge.src].type in ASSERTING:
                out.append((_flip(edge), True))
        return out

    def _step_cost(self, edge: Edge) -> float:
        """Edge type sets the base price; crossing a hub raises it.

        A source that every claim derives from is a cheap bridge to everywhere
        and tells you almost nothing, so routes through it are priced up. Without
        this the shortest path between any two nodes runs through a hub and the
        resulting "evidence path" reads like a phone directory.
        """
        base = EDGE_COST.get(edge.type, 1.0)
        hub = 1.0 + math.log2(1 + self.graph.degree(edge.dst)) / 6.0
        return base * hub / max(edge.weight, 1.0) ** 0.25

    def _relevance(self, vector: Sequence[float], q_tokens: Set[str], node: Node) -> float:
        """Blend of vector similarity and literal overlap, plus a type prior.

        Vectors alone accept coincidences; token overlap alone misses paraphrase.
        A decision outranks a claim at equal fit because it is the more complete
        answer — it carries its own rationale.
        """
        lexical = 0.0
        if q_tokens:
            node_tokens = set(tokens(node.label))
            lexical = len(q_tokens & node_tokens) / len(q_tokens)
        blended = 0.45 * cosine(vector, node.embedding) + 0.55 * lexical
        if node.type is NodeType.DECISION:
            blended += 0.05
        return blended

    def _justify(self, steps: List[Step]) -> List[Step]:
        """Keep walking from the answer to whatever justifies it.

        An answer that stops at the claim is a snippet. The tail — the claim that
        supports it, the source it came from, the assumption it rests on — is
        what makes the path auditable, so the walk pays for it.
        """
        graph = self.graph
        seen = {s.node_id for s in steps}
        tail: List[Step] = []
        current = steps[-1].node_id

        for _ in range(3):
            node = graph.node(current)
            nxt: Optional[Tuple[Edge, bool]] = None

            if node.type is NodeType.DECISION:
                supports = [
                    e for e in graph.in_edges(node.id, (EdgeType.SUPPORTS,))
                    if e.src not in seen
                ]
                depends = [
                    e for e in graph.out_edges(node.id, (EdgeType.DEPENDS_ON,))
                    if e.dst not in seen
                ]
                if supports:
                    nxt = (_flip(supports[0]), True)
                elif depends:
                    nxt = (depends[0], False)
            if nxt is None:
                provenance = [
                    e
                    for e in graph.out_edges(
                        node.id, (EdgeType.DERIVED_FROM, EdgeType.CITES)
                    )
                    if e.dst not in seen
                ]
                if provenance:
                    nxt = (provenance[0], False)
            if nxt is None:
                break

            edge, reverse = nxt
            target = graph.nodes[edge.dst]
            tail.append(Step(edge.type, target.id, target.type, target.label, reverse))
            seen.add(target.id)
            current = target.id
            if target.type is NodeType.SOURCE:
                break

        return steps + tail

    # ── accounting ───────────────────────────────────────────────────────
    def _credit(self, steps: Sequence[Step]) -> None:
        graph = self.graph
        for previous, step in zip(steps, steps[1:]):
            a, b = (
                (step.node_id, previous.node_id)
                if step.reverse
                else (previous.node_id, step.node_id)
            )
            for edge in graph.out_edges(a, (step.edge_type,) if step.edge_type else None):
                if edge.dst == b:
                    edge.traversals += 1

    def _evidence_for(self, node_id: str) -> List[str]:
        graph = self.graph
        found: List[str] = []
        for edge in graph.out_edges(
            node_id, (EdgeType.DERIVED_FROM, EdgeType.CITES, EdgeType.JUSTIFIED_BY)
        ):
            found.append(edge.dst)
        for edge in graph.in_edges(node_id, (EdgeType.SUPPORTS,)):
            found.append(edge.src)
        return found

    #: Tokens per word, the usual rough conversion for English prose.
    TOKENS_PER_WORD = 1.3
    #: Tokens of surrounding window a retrieved chunk drags along with it.
    CHUNK_WINDOW = 160

    def _text_tokens(self, label: str) -> int:
        return int(len(label.split()) * self.TOKENS_PER_WORD) + 8

    def _walk_cost(self, steps: Sequence[Step]) -> int:
        """Tokens the answer carries: the nodes on the path, and nothing else."""
        return sum(self._text_tokens(self.graph.node(s.node_id).label) for s in steps)

    def _flat_cost(self, question: str) -> int:
        """What flat top-k would have spent to say the same thing.

        Top-k spends the same budget whatever the question — that is its whole
        problem. The only variation is which k chunks it happens to pull, so the
        sample is keyed on the question and the total barely moves.
        """
        claims = [n for n in self.graph.nodes.values() if n.type is NodeType.CLAIM and n.live]
        if not claims:
            return 0
        offset = _stable_hash(question) % len(claims)
        picked = [claims[(offset + i) % len(claims)] for i in range(min(self.flat_k, len(claims)))]
        return sum(self._text_tokens(c.label) + self.CHUNK_WINDOW for c in picked)

    # ── ledgers ──────────────────────────────────────────────────────────
    def hop_histogram(self) -> Dict[int, int]:
        hist: Dict[int, int] = {}
        for walk in self.walks:
            if walk.resolved:
                hist[walk.hops] = hist.get(walk.hops, 0) + 1
        return dict(sorted(hist.items()))

    def median_hops(self) -> int:
        depths = sorted(w.hops for w in self.walks if w.resolved)
        if not depths:
            return 0
        return depths[len(depths) // 2]

    def dead_end_ledger(self) -> Dict[str, int]:
        ledger = {reason.value: 0 for reason in DeadEnd}
        for walk in self.walks:
            if walk.dead_end:
                ledger[walk.dead_end.value] += 1
        return ledger

    @property
    def resolved_rate(self) -> float:
        if not self.walks:
            return 0.0
        return sum(1 for w in self.walks if w.resolved) / len(self.walks)

    def token_saving(self) -> float:
        flat = sum(w.tokens_flat for w in self.walks) or 1
        walked = sum(w.tokens_walked for w in self.walks)
        return 1.0 - walked / flat


def _stable_hash(text: str) -> int:
    """PYTHONHASHSEED-independent, so an export is reproducible."""
    import hashlib

    return int.from_bytes(hashlib.md5(text.encode()).digest()[:4], "big")


def _flip(edge: Edge) -> Edge:
    """Read an edge backwards without inventing a new edge type."""
    return Edge(
        id=edge.id,
        src=edge.dst,
        dst=edge.src,
        type=edge.type,
        assumption_id=edge.assumption_id,
        evidence_ids=list(edge.evidence_ids),
        weight=edge.weight,
        build=edge.build,
        traversals=edge.traversals,
    )


def _distinct_relations(steps: Sequence[Step]) -> int:
    return len({s.edge_type for s in steps if s.edge_type is not None})


def _answer_index(steps: Sequence[Step]) -> int:
    """The answering node is the last CLAIM or DECISION before the tail."""
    for i in range(len(steps) - 1, -1, -1):
        if steps[i].node_type in ANSWERING:
            return i
    return len(steps) - 1


_STOP_Q = frozenset(
    """what why how when where which who whom did does do is are was were the a an
    of to in on for with from by at as and or but we our us they them their it its
    tell show give me about that this these those than then into out up down over
    much many more most less least been being have has had will would can could""".split()
)

_PROPER = re.compile(r"\b([A-Z][A-Za-z0-9+.-]*(?:\s+[A-Z][A-Za-z0-9+.-]*)*)\b")
_TECH = re.compile(r"\b[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)+\b")


def _question_mentions(question: str) -> List[str]:
    found: List[str] = []

    def add(text: str) -> None:
        if text and text not in found:
            found.append(text)

    for match in _PROPER.finditer(question):
        text = match.group(1).strip()
        if match.start() == 0 and text.lower() in _STOP_Q:
            continue
        add(text)
    for match in _TECH.finditer(question):
        add(match.group(0))
    for word in re.findall(r"\b[a-z]{4,}\b", question.lower()):
        if word not in _STOP_Q:
            add(word)
    return found
