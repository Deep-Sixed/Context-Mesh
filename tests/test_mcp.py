"""MCP read/write authority boundaries.

Read tools may move walk telemetry but must not mutate structure or belief.
PR #8 adds controlled structural writes in a separate registry; those writes
have their own safety cases and never widen the read registry.
"""

import hashlib
import inspect
import json
import unittest

from contextmesh.model import AssumptionStatus, EdgeType, NodeType
from contextmesh_mcp import resources, tools, writes
from contextmesh_mcp.session import Session

TELEMETRY = {"node": {"walks"}, "edge": {"traversals"}}


def _without(payload, drop):
    return {k: v for k, v in payload.items() if k not in drop}


def structure(graph):
    """Everything a read must not touch, by subtraction from the full record."""

    def node_state(node):
        payload = _without(node.to_dict(), TELEMETRY["node"])
        payload["embedding_digest"] = (
            hashlib.sha256(
                json.dumps(list(node.embedding), sort_keys=True).encode("utf-8")
            ).hexdigest()
            if node.embedding is not None
            else None
        )
        return json.dumps(payload, sort_keys=True)

    return {
        "build": graph.build,
        "nodes": sorted(node_state(n) for n in graph.nodes.values()),
        "edges": sorted(
            json.dumps(_without(e.to_dict(), TELEMETRY["edge"]), sort_keys=True)
            for e in graph.edges.values()
        ),
        "assumptions": sorted(
            json.dumps(a.to_dict(), sort_keys=True)
            for a in graph.assumptions.values()
        ),
    }


def telemetry(graph):
    return {
        "node_walks": {n.id: n.walks for n in graph.nodes.values()},
        "edge_traversals": {e.id: e.traversals for e in graph.edges.values()},
    }


class ReadBoundaryTest(unittest.TestCase):
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
        result = tools.mesh_ask(
            self.session, "What is the refund policy for annual plans?"
        )
        self.assertFalse(result["resolved"])
        self.assertTrue(result["dead_end"])
        self.assertStructureUnchanged()

    def test_mesh_get_node(self):
        node_id = next(iter(self.session.graph.nodes))
        result = tools.mesh_get_node(self.session, node_id)
        self.assertEqual(result["id"], node_id)
        self.assertStructureUnchanged()

    def test_mesh_health(self):
        self.assertIn("signals", tools.mesh_health(self.session))
        self.assertStructureUnchanged()

    def test_mesh_lineage(self):
        for assumption_id in self.session.graph.assumptions:
            tools.mesh_lineage(self.session, assumption_id)
        self.assertStructureUnchanged()

    def test_mesh_blast_radius(self):
        for assumption_id in self.session.graph.assumptions:
            tools.mesh_blast_radius(self.session, assumption_id)
        self.assertStructureUnchanged()

    def test_every_read_tool_in_one_pass(self):
        node_id = next(iter(self.session.graph.nodes))
        assumption_id = sorted(self.session.graph.assumptions)[0]
        args = {
            "mesh_ask": {"question": "Why did the Index Builder run out of memory?"},
            "mesh_get_node": {"node_id": node_id},
            "mesh_health": {},
            "mesh_lineage": {"assumption_id": assumption_id},
            "mesh_blast_radius": {"assumption_id": assumption_id},
        }
        self.assertEqual(sorted(args), tools.names())
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


class FrozenStateTest(unittest.TestCase):
    def setUp(self):
        self.graph = Session.build(rounds=2).graph
        self.before = structure(self.graph)

    def test_a_zeroed_embedding_is_caught(self):
        node = next(n for n in self.graph.nodes.values() if n.embedding is not None)
        node.embedding = [0.0] * len(node.embedding)
        self.assertNotEqual(structure(self.graph), self.before)

    def test_rewritten_provenance_is_caught(self):
        node = next(n for n in self.graph.nodes.values() if n.provenance is not None)
        node.provenance.source_id = "source:forged"
        self.assertNotEqual(structure(self.graph), self.before)

    def test_a_changed_node_build_is_caught(self):
        next(iter(self.graph.nodes.values())).build = 999
        self.assertNotEqual(structure(self.graph), self.before)

    def test_a_changed_edge_build_is_caught(self):
        next(iter(self.graph.edges.values())).build = 999
        self.assertNotEqual(structure(self.graph), self.before)

    def test_a_changed_label_is_caught(self):
        next(iter(self.graph.nodes.values())).label = "forged"
        self.assertNotEqual(structure(self.graph), self.before)

    def test_telemetry_alone_is_not_caught(self):
        next(iter(self.graph.nodes.values())).walks += 1
        next(iter(self.graph.edges.values())).traversals += 1
        self.assertEqual(structure(self.graph), self.before)


class BlastRadiusIsADryRunTest(unittest.TestCase):
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
            len(self.graph.by_type(NodeType.EVIDENCE, live_only=False)),
            evidence_before,
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
        counts = len(self.graph.nodes), len(self.graph.edges)
        for assumption_id in self.graph.assumptions:
            tools.mesh_blast_radius(self.session, assumption_id)
        self.assertEqual((len(self.graph.nodes), len(self.graph.edges)), counts)

    def test_it_reports_both_halves(self):
        assumption_id = next(
            a.id
            for a in self.graph.assumptions.values()
            if a.status is AssumptionStatus.ACTIVE
        )
        result = tools.mesh_blast_radius(self.session, assumption_id)
        self.assertTrue(result["hypothetical"])
        self.assertIn("would_invalidate", result)
        self.assertIn("would_preserve", result)
        self.assertEqual(result["blast_radius"], len(result["would_invalidate"]))
        self.assertEqual(
            result["would_preserve_count"], len(result["would_preserve"])
        )
        for row in result["would_invalidate"]:
            self.assertTrue(row["because"])

    def test_a_rejected_assumption_says_why_its_radius_is_zero(self):
        rejected = [
            a
            for a in self.graph.assumptions.values()
            if a.status is AssumptionStatus.REJECTED
        ]
        self.assertTrue(rejected)
        result = tools.mesh_blast_radius(self.session, rejected[0].id)
        self.assertIsNotNone(result["note"])
        self.assertIn("already", result["note"])


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.session = Session.build(rounds=2)

    def test_asking_moves_walk_accounting(self):
        before = telemetry(self.session.graph)
        tools.mesh_ask(self.session, "Why did the Index Builder run out of memory?")
        after = telemetry(self.session.graph)
        self.assertNotEqual(after, before)
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
    FORBIDDEN = (
        "invalidate",
        "reject",
        "repair",
        "execute",
        "add_node",
        "add_edge",
        "supersede",
        "assume",
        "recheck",
        "prune",
        "write",
        "delete",
    )

    def setUp(self):
        self.session = Session.build(rounds=2)

    def test_exactly_five_read_tools(self):
        self.assertEqual(
            tools.names(),
            [
                "mesh_ask",
                "mesh_blast_radius",
                "mesh_get_node",
                "mesh_health",
                "mesh_lineage",
            ],
        )

    def test_no_mutating_tool_is_in_the_read_registry(self):
        for name in tools.names():
            for forbidden in self.FORBIDDEN:
                self.assertNotIn(forbidden, name)

    def test_the_only_8a_write_is_evidence_intake(self):
        self.assertEqual(writes.names(), ["mesh_submit_evidence"])

    def test_every_tool_declares_a_description(self):
        for registry in (tools.TOOLS, writes.WRITE_TOOLS):
            for name, entry in registry.items():
                self.assertTrue(entry["description"].strip(), name)
                self.assertTrue(callable(entry["fn"]), name)

    def test_unknown_read_tool_and_unknown_ids_raise_cleanly(self):
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

    def test_a_demo_session_says_it_will_not_survive_a_restart(self):
        described = self.session.describe()
        self.assertFalse(described["persistent"])
        self.assertIn("--session", described["note"])
        self.assertIn("per process", described["note"])

    def test_the_session_reports_live_and_total_separately(self):
        described = self.session.describe()
        for key in ("nodes_live", "nodes_total", "edges_live", "edges_total"):
            self.assertIn(key, described)
        self.assertLess(described["nodes_live"], described["nodes_total"])
        self.assertLessEqual(described["edges_live"], described["edges_total"])
        self.assertNotIn("nodes", described)

    def test_every_read_payload_is_json_serialisable(self):
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
    def setUp(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("mcp SDK not installed (pip install 'contextmesh[mcp]')")

    @staticmethod
    def _registry_entry(name):
        if name in tools.TOOLS:
            return tools.TOOLS[name]
        return writes.WRITE_TOOLS[name]

    def test_the_server_registers_read_tools_plus_controlled_writes(self):
        import asyncio

        from contextmesh_mcp import server

        registered = sorted(t.name for t in asyncio.run(server.mcp.list_tools()))
        self.assertEqual(registered, sorted([*tools.names(), *writes.names()]))

    def test_the_registered_schema_matches_each_tool_signature(self):
        import asyncio

        from contextmesh_mcp import server

        internal = {"session", "checkpointer"}
        for tool in asyncio.run(server.mcp.list_tools()):
            fn = self._registry_entry(tool.name)["fn"]
            params = inspect.signature(fn).parameters
            expected = [name for name in params if name not in internal]
            required = [
                name
                for name, param in params.items()
                if name not in internal and param.default is inspect.Parameter.empty
            ]
            schema = tool.input_schema
            self.assertEqual(schema["type"], "object", tool.name)
            self.assertEqual(sorted(schema["properties"]), sorted(expected), tool.name)
            self.assertEqual(
                sorted(schema.get("required", [])), sorted(required), tool.name
            )

    def test_evidence_schema_carries_no_verdict_or_edge_authority(self):
        import asyncio

        from contextmesh_mcp import server

        published = {
            tool.name: tool.input_schema for tool in asyncio.run(server.mcp.list_tools())
        }
        schema = published["mesh_submit_evidence"]
        self.assertEqual(
            set(schema["properties"]),
            {"text", "source_id", "external_id", "metadata"},
        )
        self.assertEqual(set(schema.get("required", [])), {"text", "source_id"})
        forbidden = {
            "edge",
            "edge_type",
            "target",
            "target_id",
            "assumption_id",
            "verdict",
            "reject",
            "invalidate",
            "status",
        }
        self.assertTrue(forbidden.isdisjoint(schema["properties"]))

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
