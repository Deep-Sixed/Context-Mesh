"""A deterministic, dependency-free embedding.

The dashboard's EMBED stage says "vector on the node". Real vectors matter here
only for seed selection, so a hashed bag-of-tokens with character trigrams is
enough — and it keeps the whole engine importable with a bare Python install.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable, List, Sequence

# 512 buckets keeps hash collisions rare enough that cosine between unrelated
# short texts sits near zero. At 96 it did not, and the noise beat the signal.
DIM = 512
_TOKEN = re.compile(r"[a-z0-9]+")

STOPWORDS = frozenset(
    """a an the and or but if of to in on for with without from by at as is are was were
    be been being it its this that these those we you they he she them our your their
    do does did done how what which who whom when where why not no yes than then""".split()
)


def tokens(text: str) -> List[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


def _trigrams(token: str) -> Iterable[str]:
    padded = f"^{token}$"
    if len(padded) <= 3:
        yield padded
        return
    for i in range(len(padded) - 2):
        yield padded[i : i + 3]


def _bucket(feature: str) -> int:
    return int.from_bytes(hashlib.md5(feature.encode()).digest()[:4], "big") % DIM


def embed(text: str) -> List[float]:
    """Hashed feature vector, L2-normalised. Same text always gives same vector."""
    vec = [0.0] * DIM
    toks = tokens(text)
    for tok in toks:
        vec[_bucket(f"w:{tok}")] += 1.0
        for tri in _trigrams(tok):
            vec[_bucket(f"t:{tri}")] += 0.25
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def cosine(a: Sequence[float] | None, b: Sequence[float] | None) -> float:
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b))
