"""The MCP read boundary: telemetry may move, structure and belief may not.

`graph_before == graph_after` is the wrong test. A walk bumps `node.walks` and
`edge.traversals`, and PRUNE later drops what nothing walked — so asking a
question is a write in this system, deliberately. The invariant that matters is
narrower and stronger: no read tool may change graph structure, ontology state,
assumptions, supersession, or invalidation.

These tests import `contextmesh_mcp.tools` and never the MCP SDK, so the safety
boundary is checked on every Python version the core supports, with no
dependencies installed. The SDK-dependent server module is covered separately
and skips when the extra is absent.
"""

import json
import unittest

from contextmesh.model import AssumptionStatus, EdgeType, NodeType
from contextmesh_mcp import resources, tools
from contextmesh_mcp.session import Session


def structure(graph):
    """Everything a read must not touch. Deliberately excludes walk telemetry."""
    return {
        "build": graph.build,
        "nodes": sorted(
            (n.id, n.type.value, n.label, json.dumps(n.attrs, sort_keys=True),
             n.pruned, n.invalidated)
            for n in graph.nodes.values()
        ),
        "edges": sorted(
            (e.id, e.src, e.dst, e.type.value, e.assumption_id, e.weight,
             tuple(e.evidence_ids), e.invalidated)
            for e in graph.edges.values()
        ),
        "assumptions": sorted(
            (a.id, a.statement, a.status.value, a.version, a.supersedes,
             a.superseded_by, a.rejected_at_build, tuple(a.evidence_ids))
            for a in graph.assumptions.values()
        ),
    }


def telemetry(graph):
    """What a read is allowed to move."""
    return {
        "node_walks": {n.id: n.walks for n in graph.nodes.values()},
        "edge_traversals": {e.id: e.traversals for e in graph.edges.values()},
    }


class ReadBoundaryTest(unittest.TestCase):
    """Every v0.1 tool, against the frozen half of the graph."""

    @classmethod
    def setUpClass(cls):
        cls.session = Session.build(rounds=4)

    def setUp(self):
        self.before = structure(self.session.graph)

    def assertStructureUnchanged(self):
        self.assertEqual(structure(self.session.graph), self.before)

    def test_mesh_ask(self):
        result = tools.mesh_ask(self.session, "What made the sharding rule wrong?")
        self.assertIn("path", result)
        self.assertStructureUnchanged()

    def test_mesh_ask_on_an_unanswerable_question(self):
        result = tools.mesh_ask(self.session, "What is the refund policy for annual plans?")
        self.assertFalse(result["resolved"])
        self.assertTrue(result["dead_end"])
        self.assertStructureUnchanged()

    def test_mesh_get_node(self):
        node_id = next(iter(self.session.graph.nodes))
        result = tools.mesh_get_node(self.session, node_id)
        self.assertEqual(result["id"], node_id)
        self.assertStructureUnchanged()

    def test_mesh_health(self):
        result = tools.mesh_health(self.session)
        self.assertIn("signals", result)
        self.assertStructureUnchanged()

    def test_mesh_lineage(self):
        for assumption_id in self.session.graph.assumptions:
            tools.mesh_lineage(self.session, assumption_id)
        self.assertStructureUnchanged()

    def test_mesh_blast_radius(self):
        for assumption_id in self.session.graph.assumptions:
            tools.mesh_blast_radius(self.session, assumption_id)
        self.assertStructureUnchanged()

    def test_every_tool_in_the_registry_in_one_pass(self):
        node_id = next(iter(self.session.graph.nodes))
        assumption_id = sorted(self.session.graph.assumptions)[0]
        args = {
            "mesh_ask": {"question": "Why did the Index Builder run out of memory?"},
            "mesh_get_node": {"node_id": node_id},
            "mesh_health": {},
            "mesh_lineage": {"assumption_id": assumption_id},
            "mesh_blast_radius": {"assumption_id": assumption_id},
        }
        self.assertEqual(sorted(args), tools.names(), "a tool was added without a safety case")
        for name in tools.names():
            tools.call(self.session, name, args[name])
        self.assertStructureUnchanged()

    def test_every_resource_in_one_pass(self):
        node_id = next(iter(self.session.graph.nodes))
        assumption_id = sorted(self.session.graph.assumptions)[0]
        for uri in resources.uris():
            self.assertTrue(resources.read(self.session, uri))
        resources.read(self.session, f"contextmesh://node/{node_id}")
        resources.read(self.session, f"contextmesh://assumption/{assumption_id}")
        self.assertStructureUnchanged()


class BlastRadiusIsADryRunTest(unittest.TestCase):
    """The load-bearing safety case: computing fallout must not cause it."""

    def setUp(self):
        self.session = Session.build(rounds=2)
        self.graph = self.session.graph

    def test_the_assumption_keeps_its_status(self):
        for assumption in self.graph.assumptions.values():
            before = assumption.status
            tools.mesh_blast_radius(self.session, assumption.id)
            self.assertIs(assumption.status, before)

    def test_no_contradicting_evidence_is_created(self):
        before = sum(
            1 for e in self.graph.edges.values() if e.type is EdgeType.CONTRADICTS
        )
        evidence_before = len(self.graph.by_type(NodeType.EVIDENCE, live_only=False))
        for assumption_id in self.graph.assumptions:
            tools.mesh_blast_radius(self.session, assumption_id)
        after = sum(
            1 for e in self.graph.edges.values() if e.type is EdgeType.CONTRADICTS
        )
        self.assertEqual(after, before)
        self.assertEqual(
            len(self.graph.by_type(NodeType.EVIDENCE, live_only=False)), evidence_before
        )

    def test_nothing_becomes_invalidated(self):
        nodes_before = {n.id for n in self.graph.nodes.values() if n.invalidated}
        edges_before = {e.id for e in self.graph.edges.values() if e.invalidated}
        for assumption_id in self.graph.assumptions:
            tools.mesh_blast_radius(self.session, assumption_id)
        self.assertEqual(
            {n.id for n in self.graph.nodes.values() if n.invalidated}, nodes_before
        )
        self.assertEqual(
            {e.id for e in self.graph.edges.values() if e.invalidated}, edges_before
        )

    def test_no_node_or_edge_count_changes(self):
        n, e = len(self.graph.nodes), len(self.graph.edges)
        for assumption_id in self.graph.assumptions:
            tools.mesh_blast_radius(self.session, assumption_id)
        self.assertEqual((len(self.graph.nodes), len(self.graph.edges)), (n, e))

    def test_it_reports_both_halves(self):
        assumption_id = next(
            a.id for a in self.graph.assumptions.values()
            if a.status is AssumptionStatus.ACTIVE
        )
        result = tools.mesh_blast_radius(self.session, assumption_id)
        self.assertTrue(result["hypothetical"])
        self.assertIn("would_invalidate", result)
        self.assertIn("would_preserve", result)
        self.assertEqual(result["blast_radius"], len(result["would_invalidate"]))
        self.assertEqual(result["would_preserve_count"], len(result["would_preserve"]))
        for row in result["would_invalidate"]:
            self.assertTrue(row["because"], f"{row['id']} came back with no reason")

    def test_a_rejected_assumption_says_why_its_radius_is_zero(self):
        rejected = [
            a for a in self.graph.assumptions.values()
            if a.status is AssumptionStatus.REJECTED
        ]
        self.assertTrue(rejected, "the demo should leave one rejected assumption")
        result = tools.mesh_blast_radius(self.session, rejected[0].id)
        self.assertIsNotNone(result["note"])
        self.assertIn("already", result["note"])


class TelemetryTest(unittest.TestCase):
    """The half a read *is* allowed to move, asserted rather than assumed."""

    def setUp(self):
        self.session = Session.build(rounds=2)

    def test_asking_moves_walk_accounting(self):
        before = telemetry(self.session.graph)
        tools.mesh_ask(self.session, "Why did the Index Builder run out of memory?")
        after = telemetry(self.session.graph)
        self.assertNotEqual(after, before, "a walk should be recorded")
        for node_id, walks in before["node_walks"].items():
            self.assertGreaterEqual(after["node_walks"][node_id], walks)

    def test_the_other_four_tools_move_nothing_at_all(self):
        node_id = next(iter(self.session.graph.nodes))
        assumption_id = sorted(self.session.graph.assumptions)[0]
        before = telemetry(self.session.graph)
        tools.mesh_get_node(self.session, node_id)
        tools.mesh_health(self.session)
        tools.mesh_lineage(self.session, assumption_id)
        tools.mesh_blast_radius(self.session, assumption_id)
        self.assertEqual(telemetry(self.session.graph), before)


class SurfaceTest(unittest.TestCase):
    """What v0.1 exposes, and what it must not."""

    FORBIDDEN = (
        "invalidate", "reject", "repair", "execute", "add_node", "add_edge",
        "supersede", "assume", "recheck", "prune", "write", "delete",
    )

    def setUp(self):
        self.session = Session.build(rounds=2)

    def test_exactly_five_tools(self):
        self.assertEqual(
            tools.names(),
            ["mesh_ask", "mesh_blast_radius", "mesh_get_node", "mesh_health", "mesh_lineage"],
        )

    def test_no_mutating_tool_is_registered(self):
        for name in tools.names():
            for forbidden in self.FORBIDDEN:
                self.assertNotIn(
                    forbidden, name, f"{name} looks like a write tool; v0.1 is read-only"
                )

    def test_every_tool_declares_a_schema_and_a_description(self):
        for name, entry in tools.TOOLS.items():
            self.assertTrue(entry["description"].strip(), name)
            self.assertEqual(entry["schema"]["type"], "object", name)
            self.assertTrue(callable(entry["fn"]), name)

    def test_unknown_tool_and_unknown_ids_raise_cleanly(self):
        with self.assertRaises(tools.MeshToolError):
            tools.call(self.session, "mesh_reject", {})
        with self.assertRaises(tools.MeshToolError):
            tools.mesh_get_node(self.session, "entity:does-not-exist")
        with self.assertRaises(tools.MeshToolError):
            tools.mesh_lineage(self.session, "assumption:nope")
        with self.assertRaises(tools.MeshToolError):
            tools.mesh_ask(self.session, "   ")
        with self.assertRaises(tools.MeshToolError):
            resources.read(self.session, "contextmesh://nope")

    def test_the_session_admits_it_does_not_persist(self):
        described = self.session.describe()
        self.assertFalse(described["persistent"])
        self.assertIn("from_dict", described["note"])

    def test_every_payload_is_json_serialisable(self):
        node_id = next(iter(self.session.graph.nodes))
        assumption_id = sorted(self.session.graph.assumptions)[0]
        args = {
            "mesh_ask": {"question": "What does pgvector store?"},
            "mesh_get_node": {"node_id": node_id},
            "mesh_health": {},
            "mesh_lineage": {"assumption_id": assumption_id},
            "mesh_blast_radius": {"assumption_id": assumption_id},
        }
        for name in tools.names():
            json.dumps(tools.call(self.session, name, args[name]))

    def test_the_schema_resource_is_the_ontology_file(self):
        text = resources.schema_text()
        self.assertIn("# GRAPH.md", text)
        self.assertIn("untyped_edges == 0", text)


class ServerTest(unittest.TestCase):
    """Only this class needs the SDK, so only this class skips without it."""

    def setUp(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("mcp SDK not installed (pip install 'contextmesh[mcp]')")

    def test_the_server_registers_exactly_the_five_read_tools(self):
        import asyncio

        from contextmesh_mcp import server

        registered = sorted(t.name for t in asyncio.run(server.mcp.list_tools()))
        self.assertEqual(registered, tools.names())

    def test_the_server_registers_the_resources(self):
        import asyncio

        from contextmesh_mcp import server

        uris = {str(r.uri) for r in asyncio.run(server.mcp.list_resources())}
        for uri in resources.uris():
            self.assertIn(uri, uris)
        templates = {
            t.uri_template for t in asyncio.run(server.mcp.list_resource_templates())
        }
        self.assertIn("contextmesh://node/{node_id}", templates)
        self.assertIn("contextmesh://assumption/{assumption_id}", templates)


if __name__ == "__main__":
    unittest.main()
