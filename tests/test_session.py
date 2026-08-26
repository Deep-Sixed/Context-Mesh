"""A session has to come back as the same session, in a different process.

Four things this suite is built around:

1. **A real process boundary.** `ProcessBoundaryTest` shells out twice. One
   interpreter writes the directory and exits; another reads it and is asked
   what it has. Nothing is shared but the files, so "it round-trips" cannot be
   an artefact of objects still being in memory.

2. **The resolver learns, and what it learns is not in the graph.**
   `resolve()` writes a scored match back into `aliases`, so after a run the
   resolver knows surface forms no entity label contains. `LearnedAliasTest`
   proves one survives, and proves it by *reason*: a mention that cost a scored
   match before the restart comes back off the alias table after it.

3. **Equivalence is a claim, so it is measured.** `SurfaceEquivalenceTest`
   compares every read tool across a saved and a restored session, and pins the
   one field that does *not* survive rather than leaving it to be discovered.

4. **A session directory is untrusted input.** `RefusalTest` corrupts a good
   directory one way at a time — including two ways only the session file can
   catch, because they are disagreements *between* the graph and the resolver.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from contextmesh.graph import ContextGraph
from contextmesh.model import NodeType
from contextmesh.resolve import Resolver, ResolverSnapshotError
from contextmesh.traverse import DEFAULT_POLICY, EdgeType
from contextmesh_mcp import tools
from contextmesh_mcp.session import (
    GRAPH_FILE,
    RESOLVER_FILE,
    SESSION_FILE,
    SESSION_SCHEMA,
    SESSION_VERSION,
    Session,
    SessionError,
    WalkerConfig,
    check_agreement,
)

ROUNDS = 4
REPO_ROOT = Path(__file__).resolve().parent.parent


def build(rounds: int = ROUNDS) -> Session:
    return Session.build(rounds=rounds)


class RoundTripTest(unittest.TestCase):
    """Saving and loading gives back the same graph and the same resolver."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.session = build()
        cls.tmp = tempfile.mkdtemp()
        cls.dir = Path(cls.tmp) / "session"
        cls.session.save(cls.dir)
        cls.restored = Session.load(cls.dir)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_directory_layout(self):
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            sorted([SESSION_FILE, GRAPH_FILE, RESOLVER_FILE]),
        )

    def test_session_file_is_self_describing(self):
        data = json.loads((self.dir / SESSION_FILE).read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], SESSION_SCHEMA)
        self.assertEqual(data["version"], SESSION_VERSION)
        self.assertEqual(data["graph"], GRAPH_FILE)
        self.assertEqual(data["resolver"], RESOLVER_FILE)
        self.assertEqual(data["rounds"], ROUNDS)

    def test_graph_is_byte_identical(self):
        self.assertEqual(self.restored.graph.to_dict(), self.session.graph.to_dict())

    def test_resolver_is_byte_identical(self):
        self.assertEqual(
            self.restored.resolver.to_dict(), self.session.resolver.to_dict()
        )

    def test_resaving_is_byte_identical(self):
        """A restore that re-saves differently is a restore that lost something."""
        again = Path(self.tmp) / "again"
        self.restored.save(again)
        for name in (SESSION_FILE, GRAPH_FILE, RESOLVER_FILE):
            self.assertEqual(
                (again / name).read_bytes(),
                (self.dir / name).read_bytes(),
                f"{name} differs after a save/load/save cycle",
            )

    def test_blocks_survive(self):
        self.assertEqual(
            {k: sorted(v) for k, v in self.restored.resolver.blocks.items()},
            {k: sorted(v) for k, v in self.session.resolver.blocks.items()},
        )

    def test_log_survives(self):
        self.assertEqual(len(self.restored.resolver.log), len(self.session.resolver.log))
        self.assertEqual(
            [r.to_dict() for r in self.restored.resolver.log],
            [r.to_dict() for r in self.session.resolver.log],
        )

    def test_describe_reports_it_is_persistent(self):
        self.assertFalse(self.session.describe()["persistent"])
        self.assertTrue(self.restored.describe()["persistent"])
        self.assertIn(str(self.dir), self.restored.describe()["source"])

    def test_counts_match(self):
        a, b = self.session.describe(), self.restored.describe()
        for key in (
            "build",
            "rounds",
            "nodes_live",
            "nodes_total",
            "edges_live",
            "edges_total",
            "assumptions",
            "node_types_live",
            "node_types_total",
        ):
            self.assertEqual(a[key], b[key], key)


class BlocksAreNotDerivableTest(unittest.TestCase):
    """Why ``blocks`` is persisted rather than rebuilt.

    ``_out``/``_in`` are rebuilt on load because they are a function of the edge
    list. ``blocks`` is not the same kind of thing: it is a function of *how* an
    alias arrived. ``register`` adds block keys for every name it is given; a
    scored match learned at query time adds an alias and no block key. The alias
    table does not record which is which, so neither rebuild is faithful — and
    blocks decide the candidate set, so an unfaithful one changes what resolves.
    """

    def test_rebuilding_from_canonical_alone_loses_keys(self):
        resolver = Resolver()
        resolver.register("entity:hnsw", "HNSW", aliases=["Hierarchical Navigable Small World"])
        from_canonical = Resolver()
        from_canonical.register("entity:hnsw", "HNSW")
        self.assertNotEqual(sorted(resolver.blocks), sorted(from_canonical.blocks))
        self.assertTrue(set(resolver.blocks) - set(from_canonical.blocks))

    def test_rebuilding_from_canonical_and_aliases_invents_keys(self):
        resolver = Resolver()
        resolver.register("entity:pgvector", "pgvector")
        record = resolver.resolve("PG Vector")
        self.assertEqual(record.reason, "scored match")
        self.assertIn("pg vector", resolver.aliases)

        rebuilt = Resolver()
        for entity_id, label in resolver.canonical.items():
            names = [a for a, e in resolver.aliases.items() if e == entity_id]
            rebuilt.register(entity_id, label, aliases=names)
        self.assertNotEqual(sorted(rebuilt.blocks), sorted(resolver.blocks))

    def test_registered_aliases_are_used_in_production(self):
        """The lossy case is not hypothetical: the pipeline registers aliases."""
        source = (REPO_ROOT / "contextmesh" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("self.resolver.register(", source)
        self.assertIn("aliases=", source)


class LearnedAliasTest(unittest.TestCase):
    """A surface form learned at query time is still known after a restart."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = Path(self.tmp) / "session"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scored_match_becomes_an_alias_hit(self):
        session = build()
        mention = "PG Vector"
        before = session.resolver.resolve(mention)
        self.assertEqual(before.reason, "scored match")
        self.assertIsNotNone(before.canonical_id)

        session.save(self.dir)
        restored = Session.load(self.dir)
        after = restored.resolver.resolve(mention)

        self.assertEqual(after.canonical_id, before.canonical_id)
        self.assertEqual(after.reason, "alias table")
        self.assertEqual(after.score, 1.0)

    def test_demo_learns_aliases_no_label_contains(self):
        """Not a contrived case: a plain demo run learns dozens of them."""
        session = build()
        registered = Resolver()
        for entity_id, label in session.resolver.canonical.items():
            registered.register(entity_id, label)
        learned = set(session.resolver.aliases) - set(registered.aliases)
        self.assertGreater(len(learned), 20)

        session.save(self.dir)
        restored = Session.load(self.dir)
        self.assertTrue(learned <= set(restored.resolver.aliases))


class SurfaceEquivalenceTest(unittest.TestCase):
    """Every read tool answers the same before and after a restart."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.session = build()
        cls.tmp = tempfile.mkdtemp()
        cls.dir = Path(cls.tmp) / "session"
        cls.session.save(cls.dir)
        cls.restored = Session.load(cls.dir)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_tool_is_covered(self):
        """So a sixth tool cannot be added without deciding whether it survives."""
        covered = {
            "mesh_ask",
            "mesh_get_node",
            "mesh_health",
            "mesh_lineage",
            "mesh_blast_radius",
        }
        self.assertEqual(set(tools.TOOLS), covered)

    def test_get_node_matches_for_every_node(self):
        for node_id in self.session.graph.nodes:
            self.assertEqual(
                tools.call(self.restored, "mesh_get_node", {"node_id": node_id}),
                tools.call(self.session, "mesh_get_node", {"node_id": node_id}),
                node_id,
            )

    def test_lineage_and_blast_radius_match_for_every_assumption(self):
        for assumption_id in self.session.assumption_ids():
            for name in ("mesh_lineage", "mesh_blast_radius"):
                self.assertEqual(
                    tools.call(self.restored, name, {"assumption_id": assumption_id}),
                    tools.call(self.session, name, {"assumption_id": assumption_id}),
                    f"{name} {assumption_id}",
                )

    def test_ask_matches_across_a_restart(self):
        """A hundred questions, asked in the same order on both sides.

        Not one question each on a fresh pair: asking *is* a write here — a walk
        moves telemetry and the resolver learns — so the interesting claim is
        that the two stay in lockstep over a sequence, not that they agree once.
        A divergence anywhere in the run shows up as the first mismatched answer.
        """
        from contextmesh.demo import questions

        fresh = build()
        restored = Session.load(self.dir)
        asked = questions(fresh.graph, 100)
        self.assertEqual(len(asked), 100)
        for question in asked:
            self.assertEqual(
                tools.call(restored, "mesh_ask", {"question": question}),
                tools.call(fresh, "mesh_ask", {"question": question}),
                question,
            )

    def test_health_matches_apart_from_the_walk_log(self):
        before = tools.call(self.session, "mesh_health", {})
        after = tools.call(self.restored, "mesh_health", {})

        for key in ("status", "nodes_live", "nodes_total", "edges_live", "edge_types_used"):
            self.assertEqual(before[key], after[key], key)

        signals_before = {s["kind"]: s for s in before["signals"]}
        signals_after = {s["kind"]: s for s in after["signals"]}
        self.assertEqual(
            set(signals_before) - set(signals_after),
            {"dead_ends"},
            "walk history is the only thing a restart is expected to lose",
        )
        self.assertEqual(set(signals_after) - set(signals_before), set())
        for kind, signal in signals_after.items():
            self.assertEqual(signal, signals_before[kind], kind)

    def test_dead_ends_come_back_once_this_process_walks(self):
        """The gap is a cold start, not a lost capability."""
        restored = Session.load(self.dir)
        tools.call(restored, "mesh_ask", {"question": "What is the on-call rotation?"})
        kinds = {s["kind"] for s in tools.call(restored, "mesh_health", {})["signals"]}
        self.assertIn("dead_ends", kinds)


class WalkerConfigTest(unittest.TestCase):
    """Walker settings shape reads, so they are part of the session."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = Path(self.tmp) / "session"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hop_budget_survives(self):
        session = build()
        session.walker.hop_budget = 2
        session.save(self.dir)
        self.assertEqual(Session.load(self.dir).walker.hop_budget, 2)

    def test_policy_survives_in_order(self):
        session = build()
        session.walker.policy = (EdgeType.SUPPORTS, EdgeType.MENTIONS)
        session.save(self.dir)
        self.assertEqual(
            Session.load(self.dir).walker.policy,
            (EdgeType.SUPPORTS, EdgeType.MENTIONS),
        )

    def test_a_narrower_budget_changes_answers(self):
        """Which is the whole reason it is persisted."""
        wide = build()
        narrow = build()
        narrow.walker.hop_budget = 1
        question = "What contradicts the sharding assumption?"
        self.assertNotEqual(
            tools.call(narrow, "mesh_ask", {"question": question}),
            tools.call(wide, "mesh_ask", {"question": question}),
        )

    def test_defaults_match_the_walker(self):
        session = build()
        self.assertEqual(
            WalkerConfig.of(session.walker).to_dict(),
            WalkerConfig(hop_budget=session.walker.hop_budget).to_dict(),
        )
        self.assertEqual(WalkerConfig().policy, DEFAULT_POLICY)


class AgreementTest(unittest.TestCase):
    """The check that neither file can make on its own."""

    def test_a_resolver_from_another_graph_is_refused(self):
        graph = ContextGraph()
        resolver = Resolver()
        resolver.register("entity:ghost", "Ghost")
        with self.assertRaises(SessionError) as caught:
            check_agreement(graph, resolver)
        self.assertIn("entity:ghost", str(caught.exception))

    def test_resolving_to_a_non_entity_is_refused(self):
        session = build()
        claim = next(iter(session.graph.by_type(NodeType.CLAIM)))
        session.resolver.canonical[claim.id] = "Not an entity"
        with self.assertRaises(SessionError) as caught:
            check_agreement(session.graph, session.resolver)
        self.assertIn("rather than an entity", str(caught.exception))

    def test_a_matched_pair_is_accepted(self):
        session = build()
        check_agreement(session.graph, session.resolver)


class RefusalTest(unittest.TestCase):
    """A session directory this build cannot faithfully restore is refused."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp()
        cls.good = Path(cls.tmp) / "good"
        build().save(cls.good)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def corrupted(self, mutate) -> Path:
        target = Path(self.tmp) / f"case-{self.id().rsplit('.', 1)[-1]}"
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(self.good, target)
        mutate(target)
        return target

    def refuses(self, mutate, fragment: str = "") -> str:
        with self.assertRaises(SessionError) as caught:
            Session.load(self.corrupted(mutate))
        message = str(caught.exception)
        if fragment:
            self.assertIn(fragment, message)
        return message

    @staticmethod
    def rewrite(path: Path, **changes):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in changes.items():
            if value is Ellipsis:
                data.pop(key, None)
            else:
                data[key] = value
        path.write_text(json.dumps(data), encoding="utf-8")

    def edit_session(self, **changes):
        return lambda d: self.rewrite(d / SESSION_FILE, **changes)

    # ── the directory itself ─────────────────────────────────────────────
    def test_good_directory_loads(self):
        self.assertTrue(Session.load(self.good).describe()["persistent"])

    def test_missing_directory(self):
        with self.assertRaises(SessionError):
            Session.load(Path(self.tmp) / "nowhere")

    def test_a_file_is_not_a_directory(self):
        with self.assertRaises(SessionError):
            Session.load(self.good / GRAPH_FILE)

    def test_no_session_file(self):
        self.refuses(lambda d: (d / SESSION_FILE).unlink(), SESSION_FILE)

    def test_session_file_is_not_json(self):
        self.refuses(
            lambda d: (d / SESSION_FILE).write_text("{oops", encoding="utf-8"),
            "not valid JSON",
        )

    def test_session_file_is_not_an_object(self):
        self.refuses(
            lambda d: (d / SESSION_FILE).write_text("[]", encoding="utf-8"),
            "must contain an object",
        )

    def test_session_file_carries_nan(self):
        self.refuses(
            lambda d: (d / SESSION_FILE).write_text(
                json.dumps(
                    {
                        "schema": SESSION_SCHEMA,
                        "version": 1,
                        "graph": GRAPH_FILE,
                        "resolver": RESOLVER_FILE,
                        "rounds": float("nan"),
                        "walker": WalkerConfig().to_dict(),
                    }
                ),
                encoding="utf-8",
            ),
            "NaN",
        )

    # ── required fields ──────────────────────────────────────────────────
    def test_every_field_is_required(self):
        for key in ("schema", "version", "graph", "resolver", "rounds", "walker"):
            with self.subTest(field=key):
                self.refuses(self.edit_session(**{key: Ellipsis}), f"missing {key!r}")

    def test_wrong_schema(self):
        self.refuses(self.edit_session(schema="contextmesh.graph"), "not a")

    def test_unsupported_version(self):
        self.refuses(self.edit_session(version=2), "cannot be read by this build")

    def test_version_true_is_not_version_one(self):
        self.refuses(self.edit_session(version=True), "must be an integer")

    def test_version_as_string(self):
        self.refuses(self.edit_session(version="1"), "must be an integer")

    def test_rounds_true(self):
        self.refuses(self.edit_session(rounds=True), "must be an integer")

    def test_rounds_as_string(self):
        self.refuses(self.edit_session(rounds="4"), "must be an integer")

    def test_rounds_negative(self):
        self.refuses(self.edit_session(rounds=-1), "must be >= 0")

    # ── the filenames are names, not paths ───────────────────────────────
    def test_graph_may_not_escape_the_directory(self):
        for value in ("../../etc/passwd", "/etc/passwd", "..", "sub/graph.json", ""):
            with self.subTest(value=value):
                self.refuses(self.edit_session(graph=value), "plain filename")

    def test_resolver_may_not_escape_the_directory(self):
        self.refuses(self.edit_session(resolver="../resolver.json"), "plain filename")

    def test_filename_must_be_a_string(self):
        self.refuses(self.edit_session(graph=None), "must be a string")

    def test_named_file_must_exist(self):
        self.refuses(self.edit_session(graph="nope.json"), "no such file")

    def test_graph_file_deleted(self):
        self.refuses(lambda d: (d / GRAPH_FILE).unlink(), "no such file")

    def test_resolver_file_deleted(self):
        self.refuses(lambda d: (d / RESOLVER_FILE).unlink(), "no such file")

    # ── walker configuration ─────────────────────────────────────────────
    def test_walker_must_be_an_object(self):
        self.refuses(self.edit_session(walker=[]), "must be an object")

    def test_every_walker_field_is_required(self):
        for key in ("hop_budget", "policy", "flat_k", "max_expand"):
            with self.subTest(field=key):
                walker = WalkerConfig().to_dict()
                walker.pop(key)
                self.refuses(self.edit_session(walker=walker), f"missing {key!r}")

    def test_walker_numbers_are_checked(self):
        for key, value, fragment in (
            ("hop_budget", 0, "must be >= 1"),
            ("hop_budget", True, "must be an integer"),
            ("flat_k", -3, "must be >= 1"),
            ("max_expand", "260", "must be an integer"),
        ):
            with self.subTest(field=key, value=value):
                walker = WalkerConfig().to_dict()
                walker[key] = value
                self.refuses(self.edit_session(walker=walker), fragment)

    def test_policy_must_name_real_edge_types(self):
        walker = WalkerConfig().to_dict()
        walker["policy"] = ["teleports_to"]
        self.refuses(self.edit_session(walker=walker), "ontology does not have")

    def test_policy_may_not_repeat(self):
        walker = WalkerConfig().to_dict()
        walker["policy"] = ["mentions", "mentions"]
        self.refuses(self.edit_session(walker=walker), "twice")

    def test_policy_must_be_a_list_of_strings(self):
        for value, fragment in (("mentions", "must be a list"), ([7], "must be a string")):
            with self.subTest(value=value):
                walker = WalkerConfig().to_dict()
                walker["policy"] = value
                self.refuses(self.edit_session(walker=walker), fragment)

    # ── the companion files ──────────────────────────────────────────────
    def test_a_graph_snapshot_this_build_cannot_read(self):
        def mutate(d):
            self.rewrite(d / GRAPH_FILE, version=99)

        self.refuses(mutate, GRAPH_FILE)

    def test_a_resolver_snapshot_with_the_wrong_schema(self):
        def mutate(d):
            self.rewrite(d / RESOLVER_FILE, schema="contextmesh.graph")

        self.refuses(mutate, RESOLVER_FILE)

    def test_a_graph_that_is_not_json(self):
        self.refuses(
            lambda d: (d / GRAPH_FILE).write_text("{oops", encoding="utf-8")
        )

    # ── disagreements only the session can see ───────────────────────────
    def test_resolver_naming_a_node_the_graph_does_not_have(self):
        def mutate(d):
            data = json.loads((d / RESOLVER_FILE).read_text(encoding="utf-8"))
            data["canonical"]["entity:ghost"] = "Ghost"
            (d / RESOLVER_FILE).write_text(json.dumps(data), encoding="utf-8")

        self.refuses(mutate, "entity:ghost")

    def test_resolver_naming_a_node_of_the_wrong_type(self):
        def mutate(d):
            graph = json.loads((d / GRAPH_FILE).read_text(encoding="utf-8"))
            claim = next(n["id"] for n in graph["nodes"] if n["type"] == "claim")
            data = json.loads((d / RESOLVER_FILE).read_text(encoding="utf-8"))
            data["canonical"][claim] = "Not an entity"
            (d / RESOLVER_FILE).write_text(json.dumps(data), encoding="utf-8")

        self.refuses(mutate, "rather than an entity")

    def test_a_graph_from_a_different_corpus(self):
        """Two valid files that were never a session together."""
        other = Path(self.tmp) / "other"
        shutil.rmtree(other, ignore_errors=True)
        shutil.copytree(self.good, other)
        ContextGraph().save_json(other / GRAPH_FILE)
        with self.assertRaises(SessionError) as caught:
            Session.load(other)
        self.assertIn("not a node in this session's graph", str(caught.exception))


class ResolverSnapshotTest(unittest.TestCase):
    """The resolver's own format, corrupted one field at a time.

    Separate from the session tests above because the resolver is separately
    versioned: it is a file another tool could be handed on its own, and it has
    to refuse a bad one without a session file to lean on. The references it
    *can* check are its own — every alias, block and log entry has to name an
    entity the resolver knows. Whether those entities exist in a graph is the
    session's question, and `AgreementTest` asks it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        resolver = Resolver()
        resolver.register("entity:hnsw", "HNSW", aliases=["Hierarchical Navigable Small World"])
        resolver.register("entity:pgvector", "pgvector")
        resolver.resolve("PG Vector")
        resolver.resolve("nothing here at all")
        cls.resolver = resolver
        cls.good = resolver.to_dict()

    def corrupted(self, **changes) -> dict:
        data = json.loads(json.dumps(self.good))
        for key, value in changes.items():
            if value is Ellipsis:
                data.pop(key, None)
            else:
                data[key] = value
        return data

    def refuses(self, fragment: str = "", **changes) -> str:
        with self.assertRaises(ResolverSnapshotError) as caught:
            Resolver.from_dict(self.corrupted(**changes))
        message = str(caught.exception)
        if fragment:
            self.assertIn(fragment, message)
        return message

    # ── round trip ───────────────────────────────────────────────────────
    def test_the_good_snapshot_loads(self):
        back = Resolver.from_dict(self.good)
        self.assertEqual(back.to_dict(), self.good)

    def test_threshold_survives(self):
        tight = Resolver(threshold=0.91)
        tight.register("entity:hnsw", "HNSW")
        self.assertEqual(Resolver.from_dict(tight.to_dict()).threshold, 0.91)

    def test_a_file_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resolver.json"
            self.resolver.save_json(path)
            self.assertEqual(Resolver.load_json(path).to_dict(), self.good)

    def test_nan_in_a_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resolver.json"
            data = json.loads(json.dumps(self.good))
            data["threshold"] = 0.5
            raw = json.dumps(data).replace('"threshold": 0.5', '"threshold": NaN')
            path.write_text(raw, encoding="utf-8")
            with self.assertRaises(ResolverSnapshotError) as caught:
                Resolver.load_json(path)
            self.assertIn("NaN", str(caught.exception))

    # ── shape ────────────────────────────────────────────────────────────
    def test_not_an_object(self):
        with self.assertRaises(ResolverSnapshotError):
            Resolver.from_dict([])

    def test_every_field_is_required(self):
        for key in ("schema", "version", "threshold", "canonical", "aliases", "blocks", "log"):
            with self.subTest(field=key):
                self.refuses(f"missing {key!r}", **{key: Ellipsis})

    def test_wrong_schema(self):
        self.refuses("not a contextmesh.resolver", schema="contextmesh.graph")

    def test_version(self):
        self.refuses("cannot be read by this build", version=2)
        self.refuses("must be an integer", version=True)
        self.refuses("must be an integer", version="1")

    def test_threshold_is_a_number_in_range(self):
        self.refuses("expected a number", threshold="0.62")
        self.refuses("expected a number", threshold=True)
        self.refuses("between 0 and 1", threshold=1.5)
        self.refuses("between 0 and 1", threshold=-0.1)

    # ── tables ───────────────────────────────────────────────────────────
    def test_canonical_must_be_a_string_map(self):
        self.refuses("must be an object", canonical=[])
        self.refuses("expected a string", canonical={"entity:hnsw": 7})

    def test_aliases_must_be_a_string_map(self):
        self.refuses("must be an object", aliases="hnsw")
        self.refuses("expected a string", aliases={"hnsw": None})

    def test_blocks_must_be_lists_of_strings(self):
        self.refuses("must be an object", blocks=[])
        self.refuses("must be a list", blocks={"hnsw": "entity:hnsw"})
        self.refuses("expected a string", blocks={"hnsw": [7]})

    def test_log_must_be_a_list_of_records(self):
        self.refuses("must be a list", log={})
        with self.assertRaises(ResolverSnapshotError):
            Resolver.from_dict(self.corrupted(log=[{"mention": "x"}]))

    def test_log_record_fields_are_typed(self):
        row = dict(self.good["log"][0])
        for key, value, fragment in (
            ("mention", 7, "expected a string"),
            ("score", "0.9", "expected a number"),
            ("score", True, "expected a number"),
            ("reason", None, "expected a string"),
            ("canonical_id", 7, "expected a string"),
        ):
            with self.subTest(field=key, value=value):
                broken = dict(row)
                broken[key] = value
                self.refuses(fragment, log=[broken])

    def test_log_record_fields_are_required(self):
        for key in ("mention", "canonical_id", "canonical_label", "score", "reason"):
            with self.subTest(field=key):
                broken = dict(self.good["log"][0])
                broken.pop(key)
                self.refuses(f"requires a {key!r}", log=[broken])

    def test_an_unresolved_record_keeps_its_nulls(self):
        """``canonical_id: null`` is a resolution that failed, not a missing field."""
        back = Resolver.from_dict(self.good)
        misses = [r for r in back.log if not r.resolved]
        self.assertTrue(misses)
        self.assertTrue(all(r.canonical_label is None for r in misses))

    # ── references, which is what a resolver is ──────────────────────────
    def test_an_alias_must_name_a_canonical_entity(self):
        aliases = dict(self.good["aliases"])
        aliases["ghost"] = "entity:ghost"
        self.refuses("not canonical", aliases=aliases)

    def test_dropping_an_entity_invalidates_its_aliases(self):
        """The realistic version: the entity goes, its aliases are left behind."""
        canonical = dict(self.good["canonical"])
        canonical.pop("entity:pgvector")
        self.refuses("not canonical", canonical=canonical)

    def test_a_block_must_name_a_canonical_entity(self):
        blocks = json.loads(json.dumps(self.good["blocks"]))
        blocks["ghos"] = ["entity:ghost"]
        self.refuses("not canonical", blocks=blocks)

    def test_a_log_entry_must_name_a_canonical_entity(self):
        row = dict(self.good["log"][0])
        row["canonical_id"] = "entity:ghost"
        row["canonical_label"] = "Ghost"
        self.refuses("not canonical", log=[row])

    def test_a_dangling_alias_would_otherwise_crash_the_next_question(self):
        """Why the reference check is not pedantry.

        ``resolve`` reads ``self.canonical[eid]`` for the label as soon as the
        alias table hits, so an alias pointing at an entity the resolver does
        not have raises ``KeyError`` — not at load, where it could be reported,
        but on whichever question happens to use that surface form. Refusing the
        file turns a landmine into a message.
        """
        aliases = dict(self.good["aliases"])
        aliases["ghost"] = "entity:ghost"
        loose = Resolver(
            threshold=self.good["threshold"],
            canonical=dict(self.good["canonical"]),
            aliases=aliases,
        )
        with self.assertRaises(KeyError):
            loose.resolve("ghost")

        self.refuses("not canonical", aliases=aliases)


class ProcessBoundaryTest(unittest.TestCase):
    """Two interpreters, one directory, nothing shared but the files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = Path(self.tmp) / "session"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, expect: int = 0):
        proc = subprocess.run(
            [sys.executable, "-m", "contextmesh_mcp", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.returncode, expect, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
        return proc

    def test_one_process_writes_and_another_reads(self):
        written = self.cli("--demo", "--rounds", str(ROUNDS), "--save", str(self.dir))
        wrote = json.loads(written.stdout)
        self.assertEqual(wrote["schema"], SESSION_SCHEMA)
        self.assertEqual(sorted(wrote["files"]), sorted([SESSION_FILE, GRAPH_FILE, RESOLVER_FILE]))

        read = json.loads(self.cli("--session", str(self.dir)).stdout)
        self.assertTrue(read["persistent"])
        self.assertEqual(read["rounds"], ROUNDS)

        local = Session.build(rounds=ROUNDS).describe()
        for key in ("nodes_live", "nodes_total", "edges_live", "edges_total", "assumptions"):
            self.assertEqual(read[key], local[key], key)

    def test_the_writing_process_leaves_nothing_behind(self):
        """The reader gets the graph from the files or not at all."""
        self.cli("--demo", "--rounds", str(ROUNDS), "--save", str(self.dir))
        (self.dir / GRAPH_FILE).unlink()
        proc = subprocess.run(
            [sys.executable, "-m", "contextmesh_mcp", "--session", str(self.dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no such file", proc.stderr)

    def test_a_learned_alias_crosses_the_boundary(self):
        self.cli("--demo", "--rounds", str(ROUNDS), "--save", str(self.dir))
        aliases = json.loads((self.dir / RESOLVER_FILE).read_text(encoding="utf-8"))["aliases"]
        registered = Resolver()
        canonical = json.loads((self.dir / RESOLVER_FILE).read_text(encoding="utf-8"))["canonical"]
        for entity_id, label in canonical.items():
            registered.register(entity_id, label)
        learned = set(aliases) - set(registered.aliases)
        self.assertTrue(learned, "the demo should learn aliases while it walks")

        restored = Session.load(self.dir)
        mention = sorted(learned)[0]
        record = restored.resolver.resolve(mention)
        self.assertEqual(record.reason, "alias table")
        self.assertEqual(record.canonical_id, aliases[mention])

    def test_a_corrupt_session_refuses_at_the_shell(self):
        self.cli("--demo", "--rounds", str(ROUNDS), "--save", str(self.dir))
        self.rewrite_version(self.dir / SESSION_FILE)
        proc = subprocess.run(
            [sys.executable, "-m", "contextmesh_mcp", "--session", str(self.dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("cannot be read by this build", proc.stderr)

    @staticmethod
    def rewrite_version(path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["version"] = 99
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_a_source_must_be_chosen(self):
        proc = subprocess.run(
            [sys.executable, "-m", "contextmesh_mcp"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--demo", proc.stderr)
        self.assertIn("--session", proc.stderr)

    def test_demo_and_session_are_mutually_exclusive(self):
        proc = subprocess.run(
            [sys.executable, "-m", "contextmesh_mcp", "--demo", "--session", str(self.dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not allowed with", proc.stderr)

    def test_rounds_with_session_is_an_error_not_an_ignored_flag(self):
        self.cli("--demo", "--rounds", str(ROUNDS), "--save", str(self.dir))
        proc = subprocess.run(
            [sys.executable, "-m", "contextmesh_mcp", "--session", str(self.dir), "--rounds", "2"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no meaning for --session", proc.stderr)


class DeterminismTest(unittest.TestCase):
    """Two runs of the same build write byte-identical directories."""

    def test_saves_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            Session.build(rounds=ROUNDS).save(first)
            Session.build(rounds=ROUNDS).save(second)
            for name in (SESSION_FILE, GRAPH_FILE, RESOLVER_FILE):
                self.assertEqual(
                    (first / name).read_bytes(), (second / name).read_bytes(), name
                )


if __name__ == "__main__":
    unittest.main()
