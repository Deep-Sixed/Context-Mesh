"""Entity resolution — one id per real-world thing.

The RESOLVE stage is where most spans die. A mention that cannot be tied to a
canonical entity is dropped rather than admitted as a near-duplicate node, and
the count of what was dropped is exactly the dashboard's DROPPED AT RESOLVE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Tuple

_NOISE = re.compile(r"[^a-z0-9 ]+")
# Only genuine corporate forms are noise. Stripping "service", "api" or a
# version number destroys identity instead of normalising it: "Retrieval
# Service" is the name of a thing, and a v2 encoder is emphatically not a v3
# encoder — the corpus this ships with says so in as many words.
_LEGAL_SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|gmbh|plc)\b")
_ARTICLES = frozenset({"the", "a", "an", "our", "their", "its"})

#: Words long enough to look distinctive but common enough to mean nothing on
#: their own. A mention is not resolved by one of these.
_GENERIC = frozenset(
    """index service model system builder engine layer server client rebuild
    target policy data record request response cluster storage database
    pipeline account project version release feature summary context
    decision claim source entity evidence assumption encoder""".split()
)


def _is_distinctive(token: str) -> bool:
    """A single token identifies a thing only if it is long and not generic."""
    return len(token) >= 7 and token not in _GENERIC


def normalise(text: str) -> str:
    """Casefold, strip punctuation, drop articles and corporate noise."""
    lowered = _NOISE.sub(" ", text.lower())
    lowered = _LEGAL_SUFFIX.sub(" ", lowered)
    parts = [p for p in lowered.split() if p and p not in _ARTICLES]
    return " ".join(parts)


def acronym(text: str) -> str:
    parts = normalise(text).split()
    return "".join(p[0] for p in parts) if len(parts) > 1 else ""


def _token_set(text: str) -> Set[str]:
    return set(normalise(text).split())


def similarity(a: str, b: str) -> float:
    """Jaccard over normalised tokens, with an acronym and containment bonus."""
    ta, tb = _token_set(a), _token_set(b)
    if not ta or not tb:
        return 0.0
    na, nb = normalise(a), normalise(b)
    # "PG Vector" and "pgvector" are the same string with the spaces argued
    # about. Token overlap scores that pair at zero, so check it first.
    if na.replace(" ", "") == nb.replace(" ", ""):
        return 0.95

    inter = len(ta & tb)
    union = len(ta | tb)
    score = inter / union
    # Containment is strong evidence ("the pgvector extension" ⊃ "pgvector"),
    # but a single shared *generic* word ("index") is not. Trust containment
    # when the smaller side carries two tokens, or when the one token it does
    # carry is distinctive enough to be an identifier on its own.
    if ta <= tb or tb <= ta:
        smaller = ta if len(ta) <= len(tb) else tb
        distinctive = len(smaller) == 1 and _is_distinctive(next(iter(smaller)))
        if len(smaller) >= 2 or distinctive:
            score = max(score, 0.85)
    if na and (na == acronym(b) or nb == acronym(a)):
        score = max(score, 0.9)
    return score


@dataclass
class ResolutionRecord:
    """One mention's fate at RESOLVE. Kept whether it resolved or not."""

    mention: str
    canonical_id: Optional[str]
    canonical_label: Optional[str]
    score: float
    reason: str

    @property
    def resolved(self) -> bool:
        return self.canonical_id is not None


@dataclass
class Resolver:
    """Alias-table + blocking + scoring resolver.

    ``threshold`` is deliberately high: admitting a wrong merge corrupts every
    walk that later crosses the entity, while dropping a mention only costs one
    span, and the drop is recorded.
    """

    threshold: float = 0.62
    canonical: Dict[str, str] = field(default_factory=dict)  # id -> label
    aliases: Dict[str, str] = field(default_factory=dict)  # normalised -> id
    blocks: Dict[str, Set[str]] = field(default_factory=dict)  # key -> ids
    log: List[ResolutionRecord] = field(default_factory=list)

    # ── registration ─────────────────────────────────────────────────────
    def register(self, entity_id: str, label: str, aliases: Iterable[str] = ()) -> None:
        self.canonical[entity_id] = label
        for name in (label, *aliases):
            key = normalise(name)
            if key:
                self.aliases[key] = entity_id
            for block in self._block_keys(name):
                self.blocks.setdefault(block, set()).add(entity_id)

    def _block_keys(self, name: str) -> Sequence[str]:
        norm = normalise(name)
        if not norm:
            return ()
        toks = norm.split()
        keys = {toks[0][:4], toks[-1][:4]}
        acr = acronym(name)
        if acr:
            keys.add(acr[:4])
        if len(norm) >= 4:
            keys.add(norm.replace(" ", "")[:4])
        return tuple(keys)

    # ── resolution ───────────────────────────────────────────────────────
    def match(self, mention: str) -> Optional[Tuple[str, float, str]]:
        """Best canonical id for a mention, without logging or learning.

        Used when registering a new name: a name that already resolves to a
        known entity is an alias of it, not a second entity for the same thing.
        """
        key = normalise(mention)
        if not key:
            return None
        if key in self.aliases:
            eid = self.aliases[key]
            return eid, 1.0, "alias table"

        candidates: Set[str] = set()
        for block in self._block_keys(mention):
            candidates |= self.blocks.get(block, set())

        best_id, best_score = None, 0.0
        for eid in candidates:
            score = similarity(mention, self.canonical[eid])
            if score > best_score:
                best_id, best_score = eid, score
        if best_id is not None and best_score >= self.threshold:
            return best_id, best_score, "scored match"
        return None

    def near_miss(self, mention: str) -> Tuple[Optional[str], float]:
        """The candidate that came closest without clearing the threshold."""
        candidates: Set[str] = set()
        for block in self._block_keys(mention):
            candidates |= self.blocks.get(block, set())
        best_id, best_score = None, 0.0
        for eid in candidates:
            score = similarity(mention, self.canonical[eid])
            if score > best_score:
                best_id, best_score = eid, score
        return best_id, best_score

    def resolve(self, mention: str) -> ResolutionRecord:
        if not normalise(mention):
            record = ResolutionRecord(mention, None, None, 0.0, "empty after normalisation")
            self.log.append(record)
            return record

        hit = self.match(mention)
        if hit is not None:
            eid, score, reason = hit
            if reason == "scored match":
                self.aliases[normalise(mention)] = eid  # learn it for next time
            record = ResolutionRecord(mention, eid, self.canonical[eid], score, reason)
            self.log.append(record)
            return record

        best_id, best_score = self.near_miss(mention)
        reason = (
            f"best candidate {self.canonical[best_id]!r} scored "
            f"{best_score:.2f} < {self.threshold:.2f}"
            if best_id
            else "no candidate in any block"
        )
        record = ResolutionRecord(mention, None, None, best_score, reason)
        self.log.append(record)
        return record

    # ── reporting ────────────────────────────────────────────────────────
    @property
    def resolved_count(self) -> int:
        return sum(1 for r in self.log if r.resolved)

    @property
    def dropped_count(self) -> int:
        return sum(1 for r in self.log if not r.resolved)

    def unresolved(self) -> List[ResolutionRecord]:
        """Distinct mentions that never resolved, first attempt each.

        The log records every attempt, and the same noisy word is tried on every
        question — counting attempts would make the health signal a measure of
        traffic rather than of the graph.
        """
        seen: Set[str] = set()
        out: List[ResolutionRecord] = []
        for record in self.log:
            if record.resolved:
                continue
            key = normalise(record.mention)
            if key in seen:
                continue
            seen.add(key)
            out.append(record)
        return out

    def borderline(self, floor: float = 0.25) -> List[ResolutionRecord]:
        """Unresolved mentions that came close to a real entity.

        A mention with no candidate at all is just prose — the extractor offers
        the resolver every phrase in a sentence, and most of them are nothing.
        The mentions worth a human's attention are the near misses: something in
        the graph nearly matched, and the alias table or the threshold is why it
        did not.
        """
        return [r for r in self.unresolved() if r.score >= floor]

    def collapsed(self) -> Dict[str, List[str]]:
        """canonical id -> the distinct surface forms that folded into it."""
        out: Dict[str, List[str]] = {}
        for record in self.log:
            if record.canonical_id:
                forms = out.setdefault(record.canonical_id, [])
                if record.mention not in forms:
                    forms.append(record.mention)
        return out
