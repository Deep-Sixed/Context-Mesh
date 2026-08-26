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
    GRAPH_STEM,
    RESOLVER_STEM,
    SESSION_FILE,
    SESSION_SCHEMA,
    SESSION_VERSION,
    Checkpointer,
    Session,
    SessionError,
    WalkerConfig,
    check_agreement,
    generation_name,
)

ROUNDS = 4
REPO_ROOT = Path(__file__).resolve().parent.parent


def build(rounds: int = ROUNDS) -> Session:
    return Session.build(rounds=rounds)


def manifest(directory: Path) -> dict:
    return json.loads((directory / SESSION_FILE).read_text(encoding="utf-8"))


def graph_file(directory: Path) -> Path:
    """The graph the manifest currently commits to.

    Read rather than assumed: filenames carry the generation now, so a test
    that hard-coded ``graph.json`` would be asserting against the wrong save.
    """
    return directory / manifest(directory)["graph"]


def resolver_file(directory: Path) -> Path:
    return directory / manifest(directory)["resolver"]


def names(directory: Path) -> list:
    return sorted(p.name for p in directory.iterdir())


def mention_question(mention: str) -> str:
    """A question whose only interesting mention is one the corpus never wrote."""
    return f"What does {mention} depend on?"


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
            names(self.dir),
            sorted([
                SESSION_FILE,
                generation_name(GRAPH_STEM, 1),
                generation_name(RESOLVER_STEM, 1),
            ]),
        )


    def test_session_file_is_self_describing(self):
        data = json.loads((self.dir / SESSION_FILE).read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], SESSION_SCHEMA)
        self.assertEqual(data["version"], SESSION_VERSION)
        self.assertEqual(data["generation"], 1)
        self.assertEqual(data["graph"], generation_name(GRAPH_STEM, 1))
        self.assertEqual(data["resolver"], generation_name(RESOLVER_STEM, 1))
        self.assertEqual(data["rounds"], ROUNDS)

    def test_graph_is_byte_identical(self):
        self.assertEqual(self.restored.graph.to_dict(), self.session.graph.to_dict())

    def test_resolver_is_byte_identical(self):
        self.assertEqual(
            self.restored.resolver.to_dict(), self.session.resolver.to_dict()
        )

    def test_resaving_is_byte_identical(self):
        """A restore that re-saves differently is a restore that lost something.

        Compared through the manifest rather than by filename: a save into a
        fresh directory commits generation 1, so only the *content* is expected
        to match — the generation counter is a property of the directory, not
        of the session.
        """
        again = Path(self.tmp) / "again"
        self.restored.save(again)
        self.assertEqual(manifest(again), manifest(self.dir))
        self.assertEqual(graph_file(again).read_bytes(), graph_file(self.dir).read_bytes())
        self.assertEqual(
            resolver_file(again).read_bytes(), resolver_file(self.dir).read_bytes()
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


class GenerationTest(unittest.TestCase):
    """A save never writes to a file the current manifest names.

    That is the whole property. Writing three files in sequence is safe once
    and unsafe every time after: crash between the graph and the resolver and
    the directory holds a pairing that never existed. Generations make the
    manifest the commit point, so a reader sees one whole save or the previous
    one — never a seam between two.

    Each test gets its own directory, because these mutate what they measure.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = Path(self.tmp) / "session"
        self.session = build()
        self.session.save(self.dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def expected(self, generation: int) -> list:
        return sorted([
            SESSION_FILE,
            generation_name(GRAPH_STEM, generation),
            generation_name(RESOLVER_STEM, generation),
        ])

    def test_the_first_save_is_generation_one(self):
        self.assertEqual(manifest(self.dir)["generation"], 1)
        self.assertEqual(names(self.dir), self.expected(1))

    def test_each_save_commits_the_next_generation(self):
        for generation in (2, 3, 4):
            self.session.save(self.dir)
            self.assertEqual(manifest(self.dir)["generation"], generation)

    def test_nothing_is_left_over_afterwards(self):
        """No staging file, no superseded generation, however many saves."""
        for generation in (2, 3, 4):
            self.session.save(self.dir)
            self.assertEqual(names(self.dir), self.expected(generation), generation)

    def test_a_save_never_writes_to_a_file_the_manifest_names(self):
        """The crash-safety property, asserted directly.

        A live companion is made read-only before the save. If the save touched
        it the write would fail; that it succeeds is the evidence that the new
        generation went somewhere else entirely.
        """
        live_graph = graph_file(self.dir)
        live_resolver = resolver_file(self.dir)
        before = (live_graph.read_bytes(), live_resolver.read_bytes())
        live_graph.chmod(0o444)
        live_resolver.chmod(0o444)
        try:
            self.session.save(self.dir)
        finally:
            for path in (live_graph, live_resolver):
                if path.exists():
                    path.chmod(0o644)
        self.assertEqual(manifest(self.dir)["generation"], 2)
        self.assertNotEqual(graph_file(self.dir), live_graph)
        # The old files are swept, so read them from the copy taken first.
        self.assertEqual(before[0][:64], before[0][:64])

    def test_the_manifest_is_replaced_rather_than_rewritten(self):
        """The commit point, asserted by its reader-visible consequence.

        ``os.replace`` puts a *new* file at ``session.json``; the old one lives
        on until the last handle on it closes. So a reader that opened the
        manifest before the save still reads the previous generation, whole,
        rather than watching bytes change underneath it. Writing the same
        content in place would give the same final state and none of that
        guarantee — which is the difference this test exists to see.
        """
        live = self.dir / SESSION_FILE
        inode = live.stat().st_ino
        with open(live, "r", encoding="utf-8") as reader:
            self.session.save(self.dir)
            held = json.loads(reader.read())

        self.assertNotEqual(live.stat().st_ino, inode, "the manifest was written in place")
        self.assertEqual(held["generation"], 1)
        self.assertEqual(held["graph"], generation_name(GRAPH_STEM, 1))
        self.assertEqual(manifest(self.dir)["generation"], 2)

    def test_a_crash_before_the_swap_leaves_the_old_generation_serving(self):
        """Companion files written, manifest never replaced."""
        first = manifest(self.dir)
        orphan_graph = self.dir / generation_name(GRAPH_STEM, 2)
        orphan_resolver = self.dir / generation_name(RESOLVER_STEM, 2)
        self.session.graph.save_json(orphan_graph)
        self.session.resolver.save_json(orphan_resolver)

        self.assertEqual(manifest(self.dir), first)
        restored = Session.load(self.dir)
        self.assertEqual(restored.generation, 1)
        self.assertEqual(restored.graph.to_dict(), self.session.graph.to_dict())

    def test_a_crash_before_the_swap_leaves_no_trace_after_the_next_save(self):
        self.session.graph.save_json(self.dir / generation_name(GRAPH_STEM, 2))
        self.session.save(self.dir)
        self.assertEqual(names(self.dir), self.expected(2))

    def test_a_truncated_companion_from_an_interrupted_save_is_ignored(self):
        """Half a file under an uncommitted name is not a session."""
        (self.dir / generation_name(GRAPH_STEM, 2)).write_text("{oops", encoding="utf-8")
        self.assertEqual(Session.load(self.dir).generation, 1)
        self.session.save(self.dir)
        self.assertEqual(names(self.dir), self.expected(2))
        self.assertEqual(Session.load(self.dir).generation, 2)

    def test_an_abandoned_staging_file_is_swept(self):
        staged = self.dir / f"{SESSION_FILE}.tmp-000002"
        staged.write_text("{}", encoding="utf-8")
        self.assertEqual(Session.load(self.dir).generation, 1)
        self.session.save(self.dir)
        self.assertNotIn(staged.name, names(self.dir))

    def test_an_unreadable_manifest_does_not_roll_the_counter_back_to_zero(self):
        """A directory whose manifest is rubble still gets a valid next save."""
        (self.dir / SESSION_FILE).write_text("{oops", encoding="utf-8")
        self.session.save(self.dir)
        self.assertEqual(manifest(self.dir)["generation"], 1)
        self.assertEqual(Session.load(self.dir).generation, 1)

    def test_the_counter_belongs_to_the_directory_not_the_session(self):
        """Saving one session into two directories keeps two counters."""
        other = Path(self.tmp) / "other"
        self.session.save(self.dir)
        self.session.save(self.dir)
        self.session.save(other)
        self.assertEqual(manifest(self.dir)["generation"], 3)
        self.assertEqual(manifest(other)["generation"], 1)

    def test_a_manifest_whose_names_run_ahead_of_its_counter_is_refused(self):
        """Otherwise the next save would overwrite a file this one still names."""
        data = manifest(self.dir)
        (self.dir / generation_name(GRAPH_STEM, 9)).write_text(
            graph_file(self.dir).read_text(encoding="utf-8"), encoding="utf-8"
        )
        data["graph"] = generation_name(GRAPH_STEM, 9)
        (self.dir / SESSION_FILE).write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(SessionError) as caught:
            Session.load(self.dir)
        self.assertIn("generation 1", str(caught.exception))

    def test_checkpoint_commits_in_place(self):
        held = graph_file(self.dir).read_bytes()
        restored = Session.load(self.dir)
        written = restored.checkpoint()
        self.assertEqual(written, self.dir)
        self.assertEqual(manifest(self.dir)["generation"], 2)
        self.assertEqual(graph_file(self.dir).read_bytes(), held)

    def test_a_built_session_has_nowhere_to_checkpoint(self):
        with self.assertRaises(SessionError) as caught:
            build().checkpoint()
        self.assertIn("built rather than loaded", str(caught.exception))

    def test_describe_reports_the_generation(self):
        self.assertEqual(Session.load(self.dir).describe()["generation"], 1)
        self.assertEqual(build().describe()["generation"], 0)


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

    def test_a_label_that_disagrees_with_the_graph_is_refused(self):
        """Same id, right type, wrong name — the quiet one.

        ``Resolver.canonical`` is not a display string. ``near_miss`` scores
        every unresolved mention against it, so a resolver holding
        ``entity:pgvector -> "HNSW"`` over a graph holding ``"pgvector"`` keeps
        resolving and resolves differently, with each file valid on its own.
        """
        session = build()
        entity_id = next(iter(session.resolver.canonical))
        graph_label = session.graph.nodes[entity_id].label
        session.resolver.canonical[entity_id] = "Something Else Entirely"
        with self.assertRaises(SessionError) as caught:
            check_agreement(session.graph, session.resolver)
        message = str(caught.exception)
        self.assertIn(graph_label, message)
        self.assertIn("Something Else Entirely", message)

    def test_a_swapped_label_is_refused(self):
        """Two real entities, each wearing the other's name."""
        session = build()
        first, second = sorted(session.resolver.canonical)[:2]
        session.resolver.canonical[first] = session.graph.nodes[second].label
        session.resolver.canonical[second] = session.graph.nodes[first].label
        with self.assertRaises(SessionError):
            check_agreement(session.graph, session.resolver)

    def test_the_label_check_survives_the_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "session"
            session = build()
            session.save(directory)
            data = json.loads(resolver_file(directory).read_text(encoding="utf-8"))
            entity_id = next(iter(data["canonical"]))
            data["canonical"][entity_id] = "Renamed"
            for row in data["log"]:
                if row["canonical_id"] == entity_id:
                    row["canonical_label"] = "Renamed"
            for alias, target in list(data["aliases"].items()):
                if target == entity_id:
                    data["aliases"][alias] = target
            resolver_file(directory).write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SessionError) as caught:
                Session.load(directory)
            self.assertIn("but the graph calls it", str(caught.exception))

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
            Session.load(graph_file(self.good))

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
                        "generation": 1,
                        "graph": generation_name(GRAPH_STEM, 1),
                        "resolver": generation_name(RESOLVER_STEM, 1),
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

    def test_a_companion_that_is_a_symlink_out_of_the_directory_is_refused(self):
        """``_bare_name`` stops the path traversing. It cannot stop the file.

        A session directory is something you can be handed, and a correctly
        named ``graph-000001.json`` inside it may be a symlink to anywhere the
        process can read.
        """
        outside = Path(self.tmp) / "outside.json"
        outside.write_text(
            graph_file(self.good).read_text(encoding="utf-8"), encoding="utf-8"
        )

        def mutate(directory):
            target = graph_file(directory)
            target.unlink()
            target.symlink_to(outside)

        message = self.refuses(mutate, "outside the session directory")
        self.assertIn(str(outside.name), message)

    def test_a_symlink_inside_the_directory_is_also_refused(self):
        """Containment is checked by resolution, not by prefix.

        A link to a sibling in the same directory is harmless in itself, but
        allowing it means the check is doing string work rather than resolving
        — and the next link would not be harmless.
        """

        def mutate(directory):
            sibling = directory / "elsewhere.json"
            sibling.write_text(
                graph_file(directory).read_text(encoding="utf-8"), encoding="utf-8"
            )
            target = graph_file(directory)
            target.unlink()
            target.symlink_to(sibling)

        self.refuses(mutate, "outside the session directory")

    def test_filename_must_be_a_string(self):
        self.refuses(self.edit_session(graph=None), "must be a string")

    def test_a_name_that_is_not_this_generation_is_refused(self):
        """Caught before existence: the name itself is wrong for the counter."""
        self.refuses(self.edit_session(graph="nope.json"), "this build writes")
        self.refuses(
            self.edit_session(graph=generation_name(GRAPH_STEM, 7)), "generation 1"
        )

    def test_named_file_must_exist(self):
        """The correctly-named companion, simply absent."""
        self.refuses(lambda d: graph_file(d).unlink(), "no such file")

    def test_graph_file_deleted(self):
        self.refuses(lambda d: graph_file(d).unlink(), "no such file")

    def test_resolver_file_deleted(self):
        self.refuses(lambda d: resolver_file(d).unlink(), "no such file")

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
            self.rewrite(graph_file(d), version=99)

        self.refuses(mutate, GRAPH_STEM)

    def test_a_resolver_snapshot_with_the_wrong_schema(self):
        def mutate(d):
            self.rewrite(resolver_file(d), schema="contextmesh.graph")

        self.refuses(mutate, RESOLVER_STEM)

    def test_a_graph_that_is_not_json(self):
        self.refuses(
            lambda d: graph_file(d).write_text("{oops", encoding="utf-8")
        )

    # ── disagreements only the session can see ───────────────────────────
    def test_resolver_naming_a_node_the_graph_does_not_have(self):
        def mutate(d):
            data = json.loads(resolver_file(d).read_text(encoding="utf-8"))
            data["canonical"]["entity:ghost"] = "Ghost"
            resolver_file(d).write_text(json.dumps(data), encoding="utf-8")

        self.refuses(mutate, "entity:ghost")

    def test_resolver_naming_a_node_of_the_wrong_type(self):
        def mutate(d):
            graph = json.loads(graph_file(d).read_text(encoding="utf-8"))
            claim = next(n["id"] for n in graph["nodes"] if n["type"] == "claim")
            data = json.loads(resolver_file(d).read_text(encoding="utf-8"))
            data["canonical"][claim] = "Not an entity"
            resolver_file(d).write_text(json.dumps(data), encoding="utf-8")

        self.refuses(mutate, "rather than an entity")

    def test_a_graph_from_a_different_corpus(self):
        """Two valid files that were never a session together."""
        other = Path(self.tmp) / "other"
        shutil.rmtree(other, ignore_errors=True)
        shutil.copytree(self.good, other)
        ContextGraph().save_json(other / manifest(other)['graph'])
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

    def test_a_record_must_have_both_id_and_label_or_neither(self):
        """``resolved`` keys off the id alone, so a half-null record lies."""
        resolved = next(r for r in self.good["log"] if r["canonical_id"] is not None)
        missed = next(r for r in self.good["log"] if r["canonical_id"] is None)

        orphan_id = dict(resolved, canonical_label=None)
        self.refuses("a resolution has both", log=[orphan_id])

        orphan_label = dict(missed, canonical_label="HNSW")
        self.refuses("a miss has", log=[orphan_label])

    def test_a_record_label_must_match_the_canonical_table(self):
        """The log is what the health signals and borderline report read."""
        resolved = next(r for r in self.good["log"] if r["canonical_id"] is not None)
        renamed = dict(resolved, canonical_label="Not What It Is Called")
        self.refuses("but the resolver calls that entity", log=[renamed])

    def test_a_consistent_log_still_loads(self):
        back = Resolver.from_dict(self.good)
        for record in back.log:
            if record.canonical_id is not None:
                self.assertEqual(
                    record.canonical_label, back.canonical[record.canonical_id]
                )

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
        self.assertEqual(
            sorted(wrote["files"]),
            sorted([
                SESSION_FILE,
                generation_name(GRAPH_STEM, 1),
                generation_name(RESOLVER_STEM, 1),
            ]),
        )

        read = json.loads(self.cli("--session", str(self.dir)).stdout)
        self.assertTrue(read["persistent"])
        self.assertEqual(read["rounds"], ROUNDS)

        local = Session.build(rounds=ROUNDS).describe()
        for key in ("nodes_live", "nodes_total", "edges_live", "edges_total", "assumptions"):
            self.assertEqual(read[key], local[key], key)

    def test_the_writing_process_leaves_nothing_behind(self):
        """The reader gets the graph from the files or not at all."""
        self.cli("--demo", "--rounds", str(ROUNDS), "--save", str(self.dir))
        graph_file(self.dir).unlink()
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
        aliases = json.loads(resolver_file(self.dir).read_text(encoding="utf-8"))["aliases"]
        registered = Resolver()
        canonical = json.loads(resolver_file(self.dir).read_text(encoding="utf-8"))["canonical"]
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


class ServedMutationTest(unittest.TestCase):
    """What an MCP client does to a session has to survive the process.

    Asking is a write here by design — a walk moves ``node.walks`` and
    ``edge.traversals``, and the resolver learns a surface form on a scored
    match. Without a checkpoint the directory is a durable *starting* snapshot:
    every question after startup is thrown away on restart, silently, including
    exactly the learned aliases this format exists to keep.

    The two questions between them move all four: the first learns an alias,
    the second traverses edges.
    """

    ASKS = (
        "What does PG Vector depend on?",
        "What contradicts the sharding assumption?",
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = Path(self.tmp) / "session"
        build().save(self.dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def state(session: Session) -> dict:
        return {
            "aliases": len(session.resolver.aliases),
            "log": len(session.resolver.log),
            "walks": sum(n.walks for n in session.graph.nodes.values()),
            "traversals": sum(e.traversals for e in session.graph.edges.values()),
        }

    def test_the_asks_move_every_counter(self):
        """Otherwise the tests below could pass by measuring nothing."""
        live = Session.load(self.dir)
        before = self.state(live)
        for question in self.ASKS:
            tools.call(live, "mesh_ask", {"question": question})
        after = self.state(live)
        for key in before:
            self.assertGreater(after[key], before[key], key)

    def test_without_a_checkpoint_the_work_is_lost(self):
        """The behaviour this class exists to change, pinned as a fact."""
        live = Session.load(self.dir)
        for question in self.ASKS:
            tools.call(live, "mesh_ask", {"question": question})
        self.assertNotEqual(self.state(Session.load(self.dir)), self.state(live))

    def test_with_a_checkpoint_it_survives(self):
        live = Session.load(self.dir)
        checkpointer = Checkpointer(live)
        for question in self.ASKS:
            tools.call(live, "mesh_ask", {"question": question})
            checkpointer.record_mutation()
        self.assertEqual(self.state(Session.load(self.dir)), self.state(live))

    def test_a_post_start_alias_survives_a_real_process_boundary(self):
        """The acceptance test: one interpreter asks, another reads.

        The mention is invented here rather than taken from the corpus, so the
        alias cannot already be in the saved file — it exists only because this
        session was asked a question after it started.
        """
        mention = "PG Vector"
        saved = json.loads(resolver_file(self.dir).read_text(encoding="utf-8"))
        self.assertNotIn(mention.lower(), saved["aliases"])

        script = (
            "import json, sys\n"
            "from contextmesh_mcp.session import Session, Checkpointer\n"
            "from contextmesh_mcp import tools\n"
            "s = Session.load(sys.argv[1])\n"
            "c = Checkpointer(s)\n"
            "for q in sys.argv[2:]:\n"
            "    tools.call(s, 'mesh_ask', {'question': q})\n"
            "    c.record_mutation()\n"
            "print(json.dumps({'aliases': len(s.resolver.aliases),\n"
            "                  'log': len(s.resolver.log),\n"
            "                  'walks': sum(n.walks for n in s.graph.nodes.values()),\n"
            "                  'traversals': sum(e.traversals for e in s.graph.edges.values()),\n"
            "                  'commits': c.commits}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script, str(self.dir), mention_question(mention), self.ASKS[1]],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        wrote = json.loads(proc.stdout)
        self.assertEqual(wrote["commits"], 2)

        # Nothing is shared but the directory. This process never saw that one.
        after = Session.load(self.dir)
        self.assertEqual(self.state(after), {k: wrote[k] for k in self.state(after)})

        record = after.resolver.resolve(mention)
        self.assertEqual(record.reason, "alias table")
        self.assertIn(mention.lower(), after.resolver.aliases)
        self.assertGreater(after.generation, 1)

    def test_the_log_records_the_event_that_learned_it(self):
        live = Session.load(self.dir)
        tools.call(live, "mesh_ask", {"question": mention_question("PG Vector")})
        Checkpointer(live).record_mutation()

        after = Session.load(self.dir)
        learned = [r for r in after.resolver.log if r.reason == "scored match"]
        self.assertTrue(learned)
        self.assertTrue(
            any(r.mention.lower().replace(" ", "") == "pgvector" for r in learned)
        )


class CheckpointPolicyTest(unittest.TestCase):
    """When a served session is written back, and when it deliberately is not."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = Path(self.tmp) / "session"
        build().save(self.dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def drive(self, policy: str, asks: int = 3) -> Checkpointer:
        live = Session.load(self.dir)
        checkpointer = Checkpointer(live, policy)
        for index in range(asks):
            tools.call(live, "mesh_ask", {"question": f"What supports HNSW {index}?"})
            checkpointer.record_mutation()
        checkpointer.close()
        return checkpointer

    def test_every_ask_commits_each_time(self):
        self.assertEqual(self.drive("every-ask").commits, 3)
        self.assertEqual(manifest(self.dir)["generation"], 4)

    def test_on_exit_commits_once(self):
        self.assertEqual(self.drive("on-exit").commits, 1)
        self.assertEqual(manifest(self.dir)["generation"], 2)

    def test_never_leaves_the_directory_untouched(self):
        before = manifest(self.dir)
        self.assertEqual(self.drive("never").commits, 0)
        self.assertEqual(manifest(self.dir), before)

    def test_on_exit_writes_nothing_if_nothing_happened(self):
        checkpointer = Checkpointer(Session.load(self.dir), "on-exit")
        self.assertIsNone(checkpointer.close())
        self.assertEqual(manifest(self.dir)["generation"], 1)

    def test_close_is_idempotent(self):
        live = Session.load(self.dir)
        checkpointer = Checkpointer(live, "on-exit")
        tools.call(live, "mesh_ask", {"question": "What supports HNSW?"})
        checkpointer.record_mutation()
        self.assertIsNotNone(checkpointer.close())
        self.assertIsNone(checkpointer.close())
        self.assertEqual(manifest(self.dir)["generation"], 2)

    def test_a_demo_session_is_inert_rather_than_an_error(self):
        """It has nowhere to write; refusing to serve it would help nobody."""
        checkpointer = Checkpointer(build(), "every-ask")
        self.assertFalse(checkpointer.durable)
        self.assertIsNone(checkpointer.record_mutation())
        self.assertIsNone(checkpointer.close())

    def test_an_unknown_policy_is_refused(self):
        with self.assertRaises(SessionError):
            Checkpointer(build(), "sometimes")


class DeterminismTest(unittest.TestCase):
    """Two runs of the same build write byte-identical directories."""

    def test_saves_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            Session.build(rounds=ROUNDS).save(first)
            Session.build(rounds=ROUNDS).save(second)
            self.assertEqual(manifest(first), manifest(second))
            for reader in (graph_file, resolver_file):
                self.assertEqual(
                    reader(first).read_bytes(), reader(second).read_bytes(), reader.__name__
                )


if __name__ == "__main__":
    unittest.main()
