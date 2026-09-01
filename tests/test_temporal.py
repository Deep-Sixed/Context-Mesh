"""Standing at a past moment: what was known, what was decided, what came after.

The fixture is the calendar the reconstruction has to respect:

    2026-06-10  evidence lands           a benchmark nobody disputes
    2026-06-15  an assumption is made    build 2, and no source date ever
    2026-06-21  a decision is taken      standing on both of the above
    2026-07-08  evidence contradicts     the assumption falls
    2026-08-12  a decision supersedes    the June decision is replaced

Every date here is source time, carried by a ``source`` node's ``retrieved_at``.
Nothing in these tests reads a wall clock, and nothing in the library does
either — a test that passed on Tuesday and failed on Wednesday would not be
testing history, it would be reporting the weather.
"""

import unittest
from datetime import date

from contextmesh.assumptions import AssumptionLedger
from contextmesh.decisions import DecisionLog
from contextmesh.graph import ContextGraph
from contextmesh.model import AssumptionStatus, EdgeType, NodeType, Provenance
from contextmesh.reconstruct import reconstruct_decision
from contextmesh.temporal import (
    Horizon,
    TemporalError,
    Timeline,
    as_of_graph,
    parse_date,
    source_date,
)

JUNE_10 = "2026-06-10"
JUNE_15 = "2026-06-15"
JUNE_21 = "2026-06-21"
JULY_08 = "2026-07-08"
AUGUST_12 = "2026-08-12"


def _source(graph, key, when, label):
    return graph.add_node(
        NodeType.SOURCE, label, id=f"source:{key}", attrs={"retrieved_at": when}
    )


def calendar():
    """The five-beat record above, built in the order it happened."""
    graph = ContextGraph()
    ledger = AssumptionLedger(graph)
    decisions = DecisionLog(graph)

    # ── 10 June: the benchmark everyone agreed on ────────────────────────
    graph.build = 1
    bench = _source(graph, "bench-june", JUNE_10, "Shard sizing benchmark, June")
    measured = graph.add_node(
        NodeType.CLAIM,
        "Peak shard memory measured 2.1GB across the corpus",
        provenance=Provenance(source_id=bench.id, recorded_at_build=1),
    )
    graph.add_edge(measured.id, EdgeType.DERIVED_FROM, bench.id)

    # ── 15 June: the assumption, which has builds and nothing else ───────
    graph.build = 2
    assumption = ledger.assume(
        "Shard count grows linearly with corpus size",
        created_by="capacity-model",
    )

    # ── 21 June: the decision, standing on both ──────────────────────────
    graph.build = 3
    review = _source(graph, "review-june", JUNE_21, "Review thread, June")
    decision = decisions.decide(
        "Rebuild the index in partitions",
        "Per-shard peak memory is 2.1GB, inside the 4GB build ceiling.",
        source_id=review.id,
        supported_by=[measured.id],
        assumptions=[assumption.id],
    )

    # ── 8 July: evidence the June decision could not have had ────────────
    graph.build = 4
    postmortem = _source(graph, "postmortem-july", JULY_08, "Postmortem 233, July")
    contradiction = graph.add_node(
        NodeType.EVIDENCE,
        "One tenant held 31% of chunks in a single shard, past the 4GB ceiling",
        provenance=Provenance(source_id=postmortem.id, recorded_at_build=4),
    )
    # Evidence is dated through its provenance, not through an edge: GRAPH.md
    # gives ``derived_from`` to claims and entities, and the ontology says so.
    graph.add_edge(contradiction.id, EdgeType.CONTRADICTS, assumption.id)
    ledger.reject(assumption.id, evidence_id=contradiction.id)

    # A July claim wired under the June decision as if it had supported it.
    # This is not a contrived case: ingest a corpus in one build and the
    # supporting-claim lookup matches on subject, not on date, so the graph
    # genuinely records a June decision as derived from a July finding.
    hindsight = graph.add_node(
        NodeType.CLAIM,
        "One tenant can hold a third of the corpus in a single shard",
        provenance=Provenance(source_id=postmortem.id, recorded_at_build=4),
    )
    graph.add_edge(hindsight.id, EdgeType.DERIVED_FROM, postmortem.id)
    graph.add_edge(decision.id, EdgeType.DERIVED_FROM, hindsight.id)

    # An entity the June document names but only the August document dates.
    # Its edge runs from inside the June projection to outside it, which is the
    # one direction an endpoint check has to catch.
    tenant_mix = graph.add_node(NodeType.ENTITY, "Tenant mix")
    graph.add_edge(review.id, EdgeType.MENTIONS, tenant_mix.id)

    # An entity both June and August name. Entities are hubs, and this one is
    # here to prove the walk-back does not use them as bridges: follow
    # ``mentions`` out of a hub and the next hop is every document that ever
    # named it, which is how August arrives underneath a June decision.
    shard = graph.add_node(NodeType.ENTITY, "Shard")
    graph.add_edge(shard.id, EdgeType.DERIVED_FROM, bench.id)
    graph.add_edge(review.id, EdgeType.MENTIONS, shard.id)

    # ── 12 August: the replacement ───────────────────────────────────────
    graph.build = 5
    rethink = _source(graph, "rethink-august", AUGUST_12, "Capacity rethink, August")
    graph.add_edge(rethink.id, EdgeType.MENTIONS, shard.id)
    graph.add_edge(tenant_mix.id, EdgeType.DERIVED_FROM, rethink.id)
    successor = decisions.decide(
        "Size shards by tenant distribution, not corpus size",
        "Linear sizing under-provisions whenever one tenant dominates.",
        source_id=rethink.id,
        supersedes=decision.id,
    )

    return {
        "graph": graph,
        "assumption": assumption.id,
        "decision": decision.id,
        "successor": successor.id,
        "measured": measured.id,
        "contradiction": contradiction.id,
        "bench": bench.id,
        "postmortem": postmortem.id,
        "review": review.id,
        "shard": shard.id,
        "rethink": rethink.id,
        "hindsight": hindsight.id,
        "tenant_mix": tenant_mix.id,
    }


class ParsingDates(unittest.TestCase):
    """Loose dates are refused, because a guess would decide what was known."""

    def test_an_iso_date_is_read_exactly(self):
        self.assertEqual(parse_date("2026-06-21"), date(2026, 6, 21))

    def test_a_date_passes_through_unchanged(self):
        self.assertEqual(parse_date(date(2026, 6, 21)), date(2026, 6, 21))

    def test_a_month_is_not_a_date(self):
        with self.assertRaises(TemporalError):
            parse_date("June 2026")

    def test_a_narrative_time_is_not_a_date(self):
        with self.assertRaises(TemporalError):
            parse_date("at plan time")

    def test_a_timestamp_is_not_a_date(self):
        # Accepting this would smuggle a clock in through the calendar.
        with self.assertRaises(TemporalError):
            parse_date("2026-06-21T14:03:00Z")

    def test_the_thirtieth_of_february_is_refused(self):
        with self.assertRaises(TemporalError):
            parse_date("2026-02-30")

    def test_a_number_is_not_a_date(self):
        with self.assertRaises(TemporalError):
            parse_date(20260621)

    def test_an_unparseable_source_date_is_absent_not_fatal(self):
        graph = ContextGraph()
        node = graph.add_node(NodeType.SOURCE, "Undated note", attrs={"retrieved_at": "soon"})
        self.assertIsNone(source_date(node))


class PlacingNodes(unittest.TestCase):
    """Every node gets a horizon, and it is one of three, never one of two."""

    def setUp(self):
        self.f = calendar()
        self.timeline = Timeline(self.f["graph"])

    def test_a_source_is_its_own_anchor(self):
        anchor = self.timeline.anchor(self.f["bench"])
        self.assertEqual(anchor.via, "self")
        self.assertEqual(anchor.when, date(2026, 6, 10))

    def test_a_claim_inherits_the_date_of_the_source_it_came_from(self):
        anchor = self.timeline.anchor(self.f["measured"])
        self.assertEqual(anchor.source_id, self.f["bench"])
        self.assertEqual(anchor.via, "provenance")
        self.assertEqual(anchor.when, date(2026, 6, 10))

    def test_an_assumption_has_no_source_date_and_says_so(self):
        # Not an omission: an assumption is not lifted from a document, so it
        # has no source time to inherit. Its builds are its honest clock.
        anchor = self.timeline.anchor(self.f["assumption"])
        self.assertIsNone(anchor.when)
        self.assertEqual(anchor.via, "none")
        self.assertEqual(
            self.timeline.horizon(self.f["assumption"], date(2026, 8, 1)),
            Horizon.UNDATED,
        )

    def test_the_horizon_is_inclusive_of_its_own_day(self):
        self.assertIs(
            self.timeline.horizon(self.f["bench"], date(2026, 6, 10)), Horizon.THEN
        )

    def test_the_day_before_is_not_yet(self):
        self.assertIs(
            self.timeline.horizon(self.f["bench"], date(2026, 6, 9)), Horizon.LATER
        )

    def test_july_evidence_is_later_than_a_june_horizon(self):
        self.assertIs(
            self.timeline.horizon(self.f["contradiction"], date(2026, 6, 21)),
            Horizon.LATER,
        )

    def test_an_unknown_node_is_refused_rather_than_guessed(self):
        with self.assertRaises(TemporalError):
            self.timeline.anchor("claim:never-existed")

    def test_the_span_is_the_dated_extent(self):
        self.assertEqual(self.timeline.span(), (date(2026, 6, 10), date(2026, 8, 12)))


class TheGraphAsItStood(unittest.TestCase):
    """The projection is a real graph, not today's with the future greyed out."""

    def setUp(self):
        self.f = calendar()
        self.june = as_of_graph(self.f["graph"], JUNE_21)

    def test_june_holds_what_june_had(self):
        self.assertIn(self.f["measured"], self.june.nodes)
        self.assertIn(self.f["decision"], self.june.nodes)

    def test_june_does_not_hold_july(self):
        self.assertNotIn(self.f["contradiction"], self.june.nodes)
        self.assertNotIn(self.f["postmortem"], self.june.nodes)

    def test_june_does_not_hold_august(self):
        self.assertNotIn(self.f["successor"], self.june.nodes)

    def test_an_assumption_standing_in_june_is_active_in_june(self):
        # It was rejected in July. In June it had not fallen, and a projection
        # that showed it already rejected would be August wearing a June label.
        record = self.june.assumptions[self.f["assumption"]]
        self.assertIs(record.status, AssumptionStatus.ACTIVE)
        self.assertIsNone(record.rejected_at_build)

    def test_the_june_assumption_does_not_cite_evidence_from_july(self):
        record = self.june.assumptions[self.f["assumption"]]
        self.assertNotIn(self.f["contradiction"], record.evidence_ids)

    def test_the_projection_survives_the_ordinary_loader(self):
        # The point of building a graph rather than a filtered view: whatever
        # reads a ContextGraph can read this one, with no special case for it.
        rebuilt = ContextGraph.from_dict(self.june.to_dict())
        self.assertEqual(set(rebuilt.nodes), set(self.june.nodes))

    def test_no_edge_dangles_out_of_the_projection(self):
        for edge in self.june.edges.values():
            self.assertIn(edge.src, self.june.nodes)
            self.assertIn(edge.dst, self.june.nodes)

    def test_an_entity_dated_only_by_august_is_not_in_june(self):
        self.assertNotIn(self.f["tenant_mix"], self.june.nodes)

    def test_august_holds_everything(self):
        august = as_of_graph(self.f["graph"], AUGUST_12)
        self.assertIn(self.f["contradiction"], august.nodes)
        self.assertIn(self.f["successor"], august.nodes)

    def test_by_august_the_assumption_has_fallen(self):
        august = as_of_graph(self.f["graph"], AUGUST_12)
        self.assertIs(
            august.assumptions[self.f["assumption"]].status, AssumptionStatus.REJECTED
        )

    def test_the_source_graph_is_not_mutated_by_projecting_it(self):
        as_of_graph(self.f["graph"], JUNE_21)
        self.assertIs(
            self.f["graph"].assumptions[self.f["assumption"]].status,
            AssumptionStatus.REJECTED,
        )
        self.assertIn(self.f["contradiction"], self.f["graph"].nodes)


class WalkingBackFromADecision(unittest.TestCase):
    """A decision's ground is its ground, and it does not grow with the calendar."""

    def setUp(self):
        self.f = calendar()

    def history(self, when):
        return reconstruct_decision(self.f["graph"], self.f["decision"], when)

    def ids(self, rows):
        return [row.placed.node_id for row in rows]

    def test_it_stood_on_what_june_had(self):
        stood = self.ids(self.history(JUNE_21).stood_on)
        self.assertIn(self.f["measured"], stood)
        self.assertIn(self.f["review"], stood)

    def test_it_did_not_stand_on_july(self):
        self.assertNotIn(self.f["contradiction"], self.ids(self.history(JUNE_21).stood_on))

    def test_asking_from_august_does_not_grow_what_it_stood_on(self):
        # The whole point. Judge grounding against the caller's vantage point
        # instead of the decision's own moment and July material files itself
        # as a June reason the moment somebody asks in August.
        self.assertEqual(
            self.ids(self.history(JUNE_21).stood_on),
            self.ids(self.history(AUGUST_12).stood_on),
        )

    def test_a_later_claim_wired_in_as_ground_is_not_counted_as_ground(self):
        # The graph says the June decision was derived from this July claim.
        # It was not, and the reconstruction has to say so however it is asked.
        self.assertNotIn(self.f["hindsight"], self.ids(self.history(JUNE_21).stood_on))
        self.assertNotIn(self.f["hindsight"], self.ids(self.history(AUGUST_12).stood_on))

    def test_a_later_claim_wired_in_as_ground_is_reported_as_later(self):
        # Not counted, but not swept up either: it is in the record, and the
        # record is what a reconstruction is for.
        self.assertIn(self.f["hindsight"], self.ids(self.history(AUGUST_12).later))

    def test_the_contradiction_is_reported_as_what_came_later(self):
        self.assertIn(self.f["contradiction"], self.ids(self.history(AUGUST_12).later))

    def test_the_supersession_is_reported_as_what_came_later(self):
        self.assertIn(self.f["successor"], self.ids(self.history(AUGUST_12).later))

    def test_the_assumption_it_rested_on_is_reached_though_it_has_no_date(self):
        # It can only ever land in ``undated``, and it is the single most
        # important thing in the walk: it is what July knocked out.
        self.assertIn(self.f["assumption"], self.ids(self.history(JUNE_21).undated))

    def test_hindsight_is_found_through_the_undated_assumption(self):
        # Seed hindsight from dated ground alone and the contradiction is
        # unreachable, because the thing it contradicts has no date.
        later = self.history(JUNE_21).later
        through = {row.placed.node_id: row.through for row in later}
        self.assertEqual(through.get(self.f["contradiction"]), self.f["assumption"])

    def test_a_june_vantage_point_marks_july_material_as_not_yet_arrived(self):
        later = {row.placed.node_id: row.placed.horizon for row in self.history(JUNE_21).later}
        self.assertIs(later[self.f["contradiction"]], Horizon.LATER)

    def test_an_august_vantage_point_marks_the_same_item_as_arrived(self):
        later = {row.placed.node_id: row.placed.horizon for row in self.history(AUGUST_12).later}
        self.assertIs(later[self.f["contradiction"]], Horizon.THEN)

    def test_the_two_vantage_points_disagree_only_about_the_horizon(self):
        # Both clocks survive: the bucket comes from the decision's moment, the
        # horizon from the caller's. Collapse either and this test goes quiet.
        self.assertEqual(
            self.ids(self.history(JUNE_21).later), self.ids(self.history(AUGUST_12).later)
        )
        self.assertNotEqual(
            [r.placed.horizon for r in self.history(JUNE_21).later],
            [r.placed.horizon for r in self.history(AUGUST_12).later],
        )

    def test_the_walk_does_not_bridge_through_an_entity_hub(self):
        # August's source names the same entity June's does. If the walk-back
        # expanded through entities it would arrive at the August document by
        # way of "Shard" and file it under a June decision.
        history = self.history(AUGUST_12)
        reached = self.ids(history.stood_on + history.later + history.undated)
        self.assertNotIn(self.f["shard"], reached)
        self.assertNotIn(self.f["rethink"], reached)

    def test_the_ground_stays_small_enough_to_read(self):
        # A provenance chain is an answer somebody reads. Bridging through hubs
        # turned this into thirty-one rows against the bundled corpus.
        history = self.history(AUGUST_12)
        self.assertLessEqual(len(history.stood_on), 4)

    def test_every_row_says_how_it_was_reached(self):
        history = self.history(AUGUST_12)
        for row in history.stood_on + history.later + history.undated:
            self.assertTrue(row.relation)
            self.assertIn(row.through, self.f["graph"].nodes)
            self.assertGreaterEqual(row.hops, 1)

    def test_the_walk_is_deterministic(self):
        self.assertEqual(
            self.history(JUNE_21).to_dict(), self.history(JUNE_21).to_dict()
        )

    def test_an_unknown_decision_is_refused(self):
        with self.assertRaises(KeyError):
            reconstruct_decision(self.f["graph"], "decision:never-taken", JUNE_21)


if __name__ == "__main__":
    unittest.main()
