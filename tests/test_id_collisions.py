"""Fail-closed identity checks for slug()-derived node and edge ids
(GRAPH.md rule 8, "What a node/edge id collision means"): slug() truncates
its digest, so two different node labels, or two different edge triples,
can derive the same id. A shared id is only ever treated as a repeat
observation when the underlying identity actually agrees; otherwise it is
refused rather than silently merged.
"""

import tempfile
import unittest
from pathlib import Path

from contextmesh.demo import run as demo_run
from contextmesh.graph import ContextGraph, SnapshotError
from contextmesh.model import EdgeType, NodeType, Provenance
from contextmesh.ontology import OntologyError
from contextmesh_mcp.session import Checkpointer, Session
from contextmesh_mcp.writes import commit_mutation

# Two distinct texts that happen to share both the 40-char slug() prefix and
# the truncated SHA1 digest -- a real collision under contextmesh.model.slug,
# found once and hardcoded here so no test brute-forces it at run time.
# slug()'s digest depends only on the text, not the prefix, so this pair
# collides identically as a claim, an assumption statement, or any other
# node-type prefix.
COLLIDING_TEXT_A = (
    "This standardized claim across many entities and things happens to be "
    "the base text used for id 3438"
)
COLLIDING_TEXT_B = (
    "This standardized claim across many entities and things happens to be "
    "the base text used for id 3973"
)
COLLIDING_CLAIM_ID = "claim:this-standardized-claim-across-many-enti-a9cfb4"

# Two distinct (src, type, dst) triples that share a derived edge id.
COLLIDING_DST_A = "entity:this-is-a-very-long-canonical-entity-name-shared-prefix-5480"
COLLIDING_DST_B = "entity:this-is-a-very-long-canonical-entity-name-shared-prefix-8988"
COLLIDING_EDGE_ID = "edge:source-s-mentions-entity-this-is-a-very--b2f5d2"


def _graph_with_source(source_id: str = "source:s") -> ContextGraph:
    graph = ContextGraph()
    graph.build = 1
    graph.add_node(
        NodeType.SOURCE, "S", id=source_id, attrs={"origin": "t", "retrieved_at": "t"}
    )
    return graph


class NodeCollisionTest(unittest.TestCase):
    def setUp(self):
        self.graph = _graph_with_source()
        self.first = self.graph.add_node(
            NodeType.CLAIM, COLLIDING_TEXT_A, provenance=Provenance(source_id="source:s")
        )
        # sanity: this really is the fixed collision pair, not a stale one
        self.assertEqual(self.first.id, COLLIDING_CLAIM_ID)

    def test_a_colliding_second_write_is_refused(self):
        with self.assertRaises(OntologyError):
            self.graph.add_node(
                NodeType.CLAIM, COLLIDING_TEXT_B, provenance=Provenance(source_id="source:s")
            )

    def test_graph_is_unchanged_after_refusal(self):
        before = dict(self.graph.nodes)
        with self.assertRaises(OntologyError):
            self.graph.add_node(
                NodeType.CLAIM, COLLIDING_TEXT_B, provenance=Provenance(source_id="source:s")
            )
        self.assertEqual(self.graph.nodes, before)
        self.assertEqual(len(self.graph.by_type(NodeType.CLAIM)), 1)
        self.assertEqual(self.graph.node(self.first.id).label, COLLIDING_TEXT_A)

    def test_legitimate_duplicate_observation_still_works(self):
        # The same claim text, extracted a second time (e.g. from a second
        # document): same type, same label -- a repeat observation, not a
        # collision, and the existing merge behavior still applies.
        second_source = self.graph.add_node(
            NodeType.SOURCE, "S2", id="source:s2", attrs={"origin": "t", "retrieved_at": "t"}
        )
        again = self.graph.add_node(
            NodeType.CLAIM,
            COLLIDING_TEXT_A,
            attrs={"seen_twice": True},
            provenance=Provenance(source_id=second_source.id),
        )
        self.assertIs(again, self.first)
        self.assertEqual(len(self.graph.by_type(NodeType.CLAIM)), 1)
        self.assertTrue(self.graph.node(self.first.id).attrs["seen_twice"])
        # provenance was already set; a second, different one is not silently
        # substituted in -- unchanged from the pre-existing merge contract.
        self.assertEqual(self.graph.node(self.first.id).provenance.source_id, "source:s")


class EdgeCollisionTest(unittest.TestCase):
    def setUp(self):
        self.graph = _graph_with_source()
        self.graph.add_node(
            NodeType.ENTITY, "A", id=COLLIDING_DST_A, attrs={"canonical": "A", "aliases": []}
        )
        self.graph.add_node(
            NodeType.ENTITY, "B", id=COLLIDING_DST_B, attrs={"canonical": "B", "aliases": []}
        )
        self.first = self.graph.add_edge("source:s", EdgeType.MENTIONS, COLLIDING_DST_A)
        self.assertEqual(self.first.id, COLLIDING_EDGE_ID)  # sanity

    def _snapshot_indexes(self):
        return (
            dict(self.graph.edges),
            dict(self.graph._edge_key),
            {k: list(v) for k, v in self.graph._out.items()},
            {k: list(v) for k, v in self.graph._in.items()},
        )

    def test_a_colliding_second_write_is_refused(self):
        with self.assertRaises(OntologyError):
            self.graph.add_edge("source:s", EdgeType.MENTIONS, COLLIDING_DST_B)

    def test_internal_indexes_are_unchanged_after_refusal(self):
        before = self._snapshot_indexes()
        with self.assertRaises(OntologyError):
            self.graph.add_edge("source:s", EdgeType.MENTIONS, COLLIDING_DST_B)
        after = self._snapshot_indexes()
        self.assertEqual(before, after)
        self.assertEqual(len(self.graph.edges), 1)

    def test_legitimate_duplicate_edge_observation_still_works(self):
        # The identical (src, type, dst) asserted a second time: weight
        # grows, evidence merges, no new edge is created -- unchanged.
        again = self.graph.add_edge(
            "source:s", EdgeType.MENTIONS, COLLIDING_DST_A, weight=1.0
        )
        self.assertIs(again, self.first)
        self.assertEqual(len(self.graph.edges), 1)
        self.assertEqual(again.weight, 2.0)


class ControlledWriteCollisionTest(unittest.TestCase):
    """A collision raised inside a controlled write's `mutate` callback must
    behave exactly like any other mutation failure: commit_mutation runs
    every write against a throwaway clone, so an exception from add_node/
    add_edge deep inside it aborts before the clone is ever checkpointed or
    published, and the live session is untouched.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "session"
        base = Session.build(rounds=2)
        base.graph.add_node(
            NodeType.SOURCE, "S", id="source:s", attrs={"origin": "t", "retrieved_at": "t"}
        )
        base.graph.add_node(
            NodeType.CLAIM, COLLIDING_TEXT_A, provenance=Provenance(source_id="source:s")
        )
        base.save(root)
        self.session = Session.load(root)
        self.checkpointer = Checkpointer(self.session)

    def test_a_collision_inside_a_controlled_write_never_reaches_the_checkpoint(self):
        generation = self.session.generation

        def mutate(staged):
            staged.graph.add_node(
                NodeType.CLAIM, COLLIDING_TEXT_B, provenance=Provenance(source_id="source:s")
            )
            return {}, True

        with self.assertRaises(OntologyError):
            commit_mutation(self.session, self.checkpointer, mutate)

        self.assertEqual(self.session.generation, generation)
        self.assertEqual(
            self.session.graph.node(COLLIDING_CLAIM_ID).label, COLLIDING_TEXT_A
        )


class SnapshotCollisionBackstopTest(unittest.TestCase):
    """A hand-edited snapshot cannot smuggle in a state no live write could
    produce: to_dict() never emits two rows sharing an id (the graph only
    ever holds one Node/Edge per id in memory), so two rows sharing a
    literal id in the file is corruption, and from_dict refuses it before
    either row reaches add_node/add_edge.
    """

    def _valid_payload(self):
        graph = _graph_with_source()
        graph.add_node(
            NodeType.CLAIM, COLLIDING_TEXT_A, provenance=Provenance(source_id="source:s")
        )
        return graph.to_dict()

    def test_two_node_rows_sharing_an_id_are_rejected(self):
        payload = self._valid_payload()
        forged = dict(payload["nodes"][-1])
        forged["label"] = COLLIDING_TEXT_B  # same id, different content
        payload["nodes"].append(forged)
        with self.assertRaisesRegex(SnapshotError, "duplicate node id"):
            ContextGraph.from_dict(payload)

    def test_two_edge_rows_sharing_an_id_are_rejected(self):
        graph = _graph_with_source()
        graph.add_node(
            NodeType.ENTITY, "A", id=COLLIDING_DST_A, attrs={"canonical": "A", "aliases": []}
        )
        edge = graph.add_edge("source:s", EdgeType.MENTIONS, COLLIDING_DST_A)
        payload = graph.to_dict()
        forged = dict(payload["edges"][-1])
        forged["dst"] = COLLIDING_DST_B  # same id, different (src, type, dst)
        payload["edges"].append(forged)
        with self.assertRaisesRegex(SnapshotError, "duplicate edge id"):
            ContextGraph.from_dict(payload)
        self.assertEqual(edge.id, COLLIDING_EDGE_ID)  # sanity: the real fixture, not a stray


class HistoricalSnapshotStillLoadsTest(unittest.TestCase):
    def test_a_snapshot_from_the_real_corpus_round_trips_unchanged(self):
        result = demo_run(rounds=3)
        before = result.graph.to_dict()
        restored = ContextGraph.from_dict(before)
        self.assertEqual(restored.to_dict(), before)
        self.assertEqual(len(restored.nodes), len(result.graph.nodes))
        self.assertEqual(len(restored.edges), len(result.graph.edges))


if __name__ == "__main__":
    unittest.main()
