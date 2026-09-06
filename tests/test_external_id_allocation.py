"""external_id is bounded before any normalization can copy it."""

import tracemalloc
import unittest

from contextmesh.evidence import EvidenceIntakeError, submit_evidence
from contextmesh.model import NodeType
from contextmesh_mcp.session import Session


class ExternalIdAllocationBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Session.build(rounds=1)
        self.source = self.session.graph.add_node(
            NodeType.SOURCE,
            "probe",
            attrs={"origin": "fixture", "retrieved_at": "fixture"},
        )

    def test_oversized_external_id_is_refused_before_strip_allocates_a_copy(self):
        external_id = " " + "x" * 8_000_000 + " "
        before_nodes = len(self.session.graph.nodes)
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(EvidenceIntakeError, "512-byte limit"):
                submit_evidence(
                    self.session.graph,
                    text="observation",
                    source_id=self.source.id,
                    external_id=external_id,
                )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertLess(peak, 1_000_000)
        self.assertEqual(len(self.session.graph.nodes), before_nodes)

    def test_bounded_whitespace_only_external_id_still_fails_nonempty_contract(self):
        with self.assertRaisesRegex(
            EvidenceIntakeError, "external_id must be null or a non-empty string"
        ):
            submit_evidence(
                self.session.graph,
                text="observation",
                source_id=self.source.id,
                external_id="   ",
            )

    def test_non_string_external_id_keeps_the_type_contract(self):
        with self.assertRaisesRegex(
            EvidenceIntakeError, "external_id must be null or a non-empty string"
        ):
            submit_evidence(
                self.session.graph,
                text="observation",
                source_id=self.source.id,
                external_id=123,
            )


if __name__ == "__main__":
    unittest.main()
