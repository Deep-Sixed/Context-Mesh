"""The same corpus must build the same graph, in every process.

A graph that sells itself on auditability cannot produce a different answer on
Tuesday. Blocking collects resolver candidates into a set, and iterating a set
of strings follows Python's per-process hash salt — so a score tie used to
resolve differently run to run, and the built graph differed by an edge.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from contextmesh.corpus import documents
from contextmesh.pipeline import Pipeline
from contextmesh.resolve import Resolver

REPO = Path(__file__).resolve().parent.parent

FINGERPRINT = """
import hashlib, json, sys
sys.path.insert(0, %r)
from contextmesh.corpus import documents
from contextmesh.pipeline import Pipeline
p = Pipeline(); p.build(documents())
g = p.graph
nodes = "|".join(sorted(n.id for n in g.nodes.values()))
edges = "|".join(sorted(
    f"{e.src}>{e.type.value}>{e.dst}:{e.weight}" for e in g.edges.values()
))
print(json.dumps({
    "nodes": hashlib.md5(nodes.encode()).hexdigest(),
    "edges": hashlib.md5(edges.encode()).hexdigest(),
    "n_nodes": len(g.nodes),
    "n_edges": len(g.edges),
}))
""" % str(REPO)


def fingerprint_in_subprocess(seed: str) -> dict:
    env = dict(os.environ, PYTHONHASHSEED=seed)
    out = subprocess.run(
        [sys.executable, "-c", FINGERPRINT],
        capture_output=True, text=True, env=env, cwd=str(REPO), check=True,
    )
    return json.loads(out.stdout)


class InProcessTest(unittest.TestCase):
    def test_two_builds_agree(self):
        first, second = Pipeline(), Pipeline()
        first.build(documents())
        second.build(documents())
        self.assertEqual(
            sorted(n.id for n in first.graph.nodes.values()),
            sorted(n.id for n in second.graph.nodes.values()),
        )
        self.assertEqual(
            sorted(f"{e.src}>{e.type.value}>{e.dst}" for e in first.graph.edges.values()),
            sorted(f"{e.src}>{e.type.value}>{e.dst}" for e in second.graph.edges.values()),
        )

    def test_candidate_ties_break_on_id_not_set_order(self):
        # Two entities that score identically against the same mention: the
        # winner must be the lower id, whatever order the block set yields.
        resolver = Resolver()
        resolver.register("entity:zzz", "Alpha Beta")
        resolver.register("entity:aaa", "Alpha Beta")
        self.assertEqual(resolver.match("Alpha Beta")[0], "entity:aaa")


class CrossProcessTest(unittest.TestCase):
    """The real check: different hash salts, same graph."""

    def test_graph_is_identical_under_different_hash_seeds(self):
        seeds = ["0", "1", "99", "424242"]
        results = [fingerprint_in_subprocess(s) for s in seeds]
        for seed, result in zip(seeds[1:], results[1:]):
            self.assertEqual(
                result, results[0],
                f"build under PYTHONHASHSEED={seed} differs from PYTHONHASHSEED={seeds[0]}",
            )


if __name__ == "__main__":
    unittest.main()
