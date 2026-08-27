"""A ledger that verifies is not the same as the ledger you committed.

A SHA-256 chain makes tampering *evident*: edit an entry and its digest stops
matching, drop one and the next entry's `previous` points at nothing, move one
and both disagree at once. That is real, and this suite proves each of them.

But it is worth being exact about what a hash chain does not do::

    A → B → C → D                    the history that ran
                  ↓
    A → B'→ C'→ D'                   every entry rewritten,
                  ↓                  every digest recomputed
              verify() == True

Both chains are internally perfect. Nothing inside either file distinguishes
them, because anyone who can rewrite the entries can rewrite the digests too.
What distinguishes them is a head you trusted *before* the file could be
rewritten — an anchor the attacker does not get to edit. So the load-bearing
test here is not that a tampered ledger fails `verify()`. It is that a flawless
forgery is refused because it does not continue the history it was checked
against.

That is why restoring the *exact* previous head is 7B's real invariant, rather
than restoring something that happens to hash correctly.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from contextmesh.execute import (
    LEDGER_SCHEMA,
    LEDGER_VERSION,
    Event,
    ExecutionError,
    LedgerEntry,
    LedgerIntegrityError,
    RunLedger,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def history():
    """The worked example's shape: a run, a disproof, a repair and a rerun."""
    ledger = RunLedger()
    ledger.record(
        1, Event.EXECUTED, "hash_password", "argon2id",
        node_id="dec-hash", data={"hasher": "Argon2id", "artefacts": ["hasher"]},
    )
    ledger.record(
        1, Event.DISPROVED, "hash_password", "CVE-2026-9999 published",
        assumption_id="assum-argon", data={"fell": ["auth_routes", "rate_limit"]},
    )
    ledger.record(1, Event.INVALIDATED, "auth_routes", "ground removed")
    ledger.record(2, Event.REPAIRED, "hash_password", "reground on bcrypt")
    ledger.record(2, Event.EXECUTED, "hash_password", "bcrypt", data={"hasher": "Bcrypt"})
    return ledger


def reseal(snapshot):
    """Recompute every digest and the head, the way a real forger would.

    Not a helper the library offers — that is the point. It exists here to build
    the one attack the chain cannot see, so the test can prove what stops it.
    """
    previous = RunLedger.GENESIS
    for index, row in enumerate(snapshot["entries"], start=1):
        row["seq"] = index
        entry = LedgerEntry(
            seq=index,
            round=row["round"],
            event=Event(row["event"]),
            task=row["task"],
            detail=row["detail"],
            node_id=row["node_id"],
            assumption_id=row["assumption_id"],
            data=row["data"],
        )
        row["digest"] = entry.compute_digest(previous)
        previous = row["digest"]
    snapshot["head"] = previous
    return snapshot


class SnapshotTest(unittest.TestCase):
    """The container: versioned, self-describing, and the same bytes every time."""

    def setUp(self):
        self.ledger = history()

    def test_the_snapshot_names_its_own_format(self):
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot["schema"], LEDGER_SCHEMA)
        self.assertEqual(snapshot["version"], LEDGER_VERSION)
        self.assertEqual(snapshot["head"], self.ledger.head)
        self.assertEqual(len(snapshot["entries"]), len(self.ledger))

    def test_the_head_is_stored_and_not_merely_derivable(self):
        """So a truncated array is a contradiction, not a shorter history."""
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot["head"], snapshot["entries"][-1]["digest"])
        snapshot["entries"].pop()
        with self.assertRaises(LedgerIntegrityError) as caught:
            RunLedger.from_snapshot(snapshot)
        self.assertIn("disagrees with itself", str(caught.exception))

    def test_a_round_trip_restores_every_entry_unchanged(self):
        restored = RunLedger.from_snapshot(self.ledger.snapshot())
        self.assertEqual(restored.entries, self.ledger.entries)
        self.assertEqual(restored.head, self.ledger.head)
        self.assertTrue(restored.verify())

    def test_re_saving_a_restored_ledger_reproduces_it_byte_for_byte(self):
        text = self.ledger.to_json()
        again = RunLedger.from_snapshot(json.loads(text)).to_json()
        self.assertEqual(text, again)

    def test_an_empty_ledger_round_trips_with_the_genesis_head(self):
        empty = RunLedger()
        snapshot = empty.snapshot()
        self.assertEqual(snapshot["head"], RunLedger.GENESIS)
        restored = RunLedger.from_snapshot(snapshot)
        self.assertEqual(len(restored), 0)
        self.assertEqual(restored.head, RunLedger.GENESIS)

    def test_an_empty_ledger_claiming_a_head_is_refused(self):
        snapshot = RunLedger().snapshot()
        snapshot["head"] = "a" * 64
        with self.assertRaises(LedgerIntegrityError):
            RunLedger.from_snapshot(snapshot)

    def test_the_rows_view_is_not_the_durable_format(self):
        """`to_dict` is for a report; it carries no schema, version or head."""
        self.assertIsInstance(self.ledger.to_dict(), list)
        self.assertIsInstance(self.ledger.snapshot(), dict)

    def test_the_file_holds_no_python_only_constants(self):
        text = self.ledger.to_json()
        for token in ("NaN", "Infinity"):
            self.assertNotIn(token, text)


class FailClosedTest(unittest.TestCase):
    """Every field the digest covers is checked before it becomes an object."""

    def setUp(self):
        self.snapshot = history().snapshot()

    def refuse(self, fragment):
        with self.assertRaises(LedgerIntegrityError) as caught:
            RunLedger.from_snapshot(self.snapshot)
        self.assertIn(fragment, str(caught.exception))

    def test_a_non_object_snapshot_is_refused(self):
        for bad in ([], "ledger", 7, None):
            with self.subTest(bad=bad):
                with self.assertRaises(LedgerIntegrityError):
                    RunLedger.from_snapshot(bad)

    def test_every_container_field_is_required(self):
        for key in ("schema", "version", "head", "entries"):
            with self.subTest(key=key):
                snapshot = history().snapshot()
                del snapshot[key]
                with self.assertRaises(LedgerIntegrityError) as caught:
                    RunLedger.from_snapshot(snapshot)
                self.assertIn(key, str(caught.exception))

    def test_another_schema_is_refused(self):
        self.snapshot["schema"] = "contextmesh.graph"
        self.refuse("not a contextmesh.runledger snapshot")

    def test_a_future_version_is_refused_rather_than_guessed_at(self):
        self.snapshot["version"] = LEDGER_VERSION + 1
        self.refuse("cannot be read by this build")

    def test_a_boolean_version_does_not_pass_for_one(self):
        """`True == 1`, so an equality check alone would load it as version 1."""
        self.snapshot["version"] = True
        self.refuse("must be an integer")

    def test_a_malformed_head_is_refused_on_its_shape_not_on_the_mismatch(self):
        """The distinction matters, and an earlier version of this test missed it.

        Asserting only that the message mentions "head" passed even with the
        shape check removed, because the head-vs-entries mismatch reported a
        few lines later also says "ledger head says …". A wrong-shaped head is
        refused for being wrong-shaped — before it is compared to anything.
        """
        for bad in ("", "abc", "A" * 64, "g" * 64, "0" * 63, "0" * 65, 12345, None):
            with self.subTest(bad=bad):
                snapshot = history().snapshot()
                snapshot["head"] = bad
                with self.assertRaises(LedgerIntegrityError) as caught:
                    RunLedger.from_snapshot(snapshot)
                self.assertIn(
                    "head must be 64 lowercase hex characters", str(caught.exception)
                )

    def test_entries_must_be_an_array(self):
        self.snapshot["entries"] = {"1": {}}
        self.refuse("must be an array")

    def test_an_entry_must_be_an_object(self):
        self.snapshot["entries"][0] = ["seq", 1]
        self.refuse("must be an object")

    def test_every_entry_field_is_required(self):
        for key in ("seq", "round", "event", "task", "detail",
                    "node_id", "assumption_id", "data", "digest"):
            with self.subTest(key=key):
                snapshot = history().snapshot()
                del snapshot["entries"][0][key]
                with self.assertRaises(LedgerIntegrityError) as caught:
                    RunLedger.from_snapshot(snapshot)
                self.assertIn(key, str(caught.exception))

    def test_an_unknown_entry_field_is_refused_not_dropped(self):
        """The digest covers a fixed set of fields; anything else is unsigned."""
        self.snapshot["entries"][0]["approved_by"] = "someone"
        self.refuse("which the digest does not cover")

    def test_seq_must_be_an_integer_starting_at_one_with_no_gaps(self):
        for position, value, fragment in (
            (0, 2, "this is entry 1"),
            (0, "1", "must be an integer"),
            (0, True, "must be an integer"),
            (2, 4, "this is entry 3"),
        ):
            with self.subTest(position=position, value=value):
                snapshot = history().snapshot()
                snapshot["entries"][position]["seq"] = value
                with self.assertRaises(LedgerIntegrityError) as caught:
                    RunLedger.from_snapshot(snapshot)
                self.assertIn(fragment, str(caught.exception))

    def test_round_must_be_a_non_negative_integer(self):
        for bad in (-1, "1", 1.0, True, None):
            with self.subTest(bad=bad):
                snapshot = history().snapshot()
                snapshot["entries"][0]["round"] = bad
                with self.assertRaises(LedgerIntegrityError) as caught:
                    RunLedger.from_snapshot(snapshot)
                self.assertIn("round", str(caught.exception))

    def test_an_unknown_event_is_refused_and_the_message_lists_the_known_ones(self):
        self.snapshot["entries"][0]["event"] = "exfiltrated"
        with self.assertRaises(LedgerIntegrityError) as caught:
            RunLedger.from_snapshot(self.snapshot)
        message = str(caught.exception)
        self.assertIn("not an event this build knows", message)
        for known in ("executed", "audited", "disproved", "repaired"):
            self.assertIn(known, message)

    def test_a_non_string_event_is_refused(self):
        self.snapshot["entries"][0]["event"] = 3
        self.refuse("event must be a string")

    def test_task_and_detail_must_be_strings(self):
        for key in ("task", "detail"):
            with self.subTest(key=key):
                snapshot = history().snapshot()
                snapshot["entries"][0][key] = 42
                with self.assertRaises(LedgerIntegrityError) as caught:
                    RunLedger.from_snapshot(snapshot)
                self.assertIn(f"{key} must be a string", str(caught.exception))

    def test_the_nullable_ids_reject_anything_that_is_not_a_string_or_null(self):
        for position, key in ((0, "node_id"), (1, "assumption_id")):
            with self.subTest(key=key):
                snapshot = history().snapshot()
                snapshot["entries"][position][key] = 7
                with self.assertRaises(LedgerIntegrityError) as caught:
                    RunLedger.from_snapshot(snapshot)
                self.assertIn("string or null", str(caught.exception))

    def test_nulling_out_an_id_that_was_set_breaks_the_digest(self):
        """Null is a legitimate value; it still has to be the one that was signed."""
        for position, key in ((0, "node_id"), (1, "assumption_id")):
            with self.subTest(key=key):
                snapshot = history().snapshot()
                self.assertIsNotNone(snapshot["entries"][position][key])
                snapshot["entries"][position][key] = None
                with self.assertRaises(LedgerIntegrityError) as caught:
                    RunLedger.from_snapshot(snapshot)
                self.assertIn("digest does not match", str(caught.exception))

    def test_data_must_be_an_object(self):
        self.snapshot["entries"][0]["data"] = ["hasher", "Argon2id"]
        self.refuse("data must be an object")

    def test_data_holding_a_value_with_no_canonical_json_form_is_refused(self):
        """`from_snapshot` also takes a dict that never went through JSON."""
        self.snapshot["entries"][0]["data"] = {"when": {1, 2}}
        self.refuse("not JSON-deterministic")

    def test_data_holding_a_non_json_float_is_refused(self):
        self.snapshot["entries"][0]["data"] = {"score": float("nan")}
        self.refuse("has no JSON form")

    def test_a_malformed_digest_is_refused_before_it_is_compared(self):
        for bad in ("", "deadbeef", "A" * 64, "z" * 64, 99, None):
            with self.subTest(bad=bad):
                snapshot = history().snapshot()
                snapshot["entries"][0]["digest"] = bad
                with self.assertRaises(LedgerIntegrityError) as caught:
                    RunLedger.from_snapshot(snapshot)
                self.assertIn("64 lowercase hex", str(caught.exception))

    def test_a_json_file_carrying_nan_is_refused_at_parse_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            snapshot = history().snapshot()
            snapshot["entries"][0]["data"] = {"score": float("nan")}
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            with self.assertRaises(LedgerIntegrityError) as caught:
                RunLedger.load_json(path)
            self.assertIn("non-JSON constant", str(caught.exception))

    def test_recording_a_non_json_value_is_refused_at_the_door(self):
        ledger = RunLedger()
        with self.assertRaises(ExecutionError):
            ledger.record(1, Event.EXECUTED, "t", "d", data={"score": float("nan")})

    def test_a_ledger_holding_a_non_json_value_refuses_to_serialise_it(self):
        """The second guard, for state `record` did not build.

        `record` refuses NaN, so in the normal course this can't happen — which
        is exactly why `allow_nan=False` needs its own test: without one, the
        flag can be flipped and no test notices. An entry assembled directly is
        the path that reaches it, and the file it would write is one no other
        JSON parser could read back.
        """
        ledger = RunLedger()
        ledger._entries.append(
            LedgerEntry(
                seq=1, round=1, event=Event.EXECUTED, task="t", detail="d",
                data={"score": float("nan")}, digest="0" * 64,
            )
        )
        with self.assertRaises(ValueError):
            ledger.to_json()


class TamperTest(unittest.TestCase):
    """The three attacks the chain catches, and the one it does not."""

    def setUp(self):
        self.ledger = history()
        self.trusted = self.ledger.head

    def test_editing_a_historical_entry_is_refused(self):
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot["entries"][0]["data"]["hasher"], "Argon2id")
        snapshot["entries"][0]["data"]["hasher"] = "Bcrypt"
        with self.assertRaises(LedgerIntegrityError) as caught:
            RunLedger.from_snapshot(snapshot)
        self.assertIn("ledger entry 1", str(caught.exception))

    def test_deleting_a_historical_entry_is_refused_even_when_renumbered(self):
        """Renumbering hides the gap from `seq` but not from the chain."""
        snapshot = self.ledger.snapshot()
        del snapshot["entries"][2]
        for index, row in enumerate(snapshot["entries"], start=1):
            row["seq"] = index
        with self.assertRaises(LedgerIntegrityError) as caught:
            RunLedger.from_snapshot(snapshot)
        self.assertIn("ledger entry 3", str(caught.exception))

    def test_deleting_a_historical_entry_without_renumbering_is_refused_too(self):
        snapshot = self.ledger.snapshot()
        del snapshot["entries"][2]
        with self.assertRaises(LedgerIntegrityError) as caught:
            RunLedger.from_snapshot(snapshot)
        self.assertIn("this is entry 3", str(caught.exception))

    def test_reordering_historical_entries_is_refused(self):
        snapshot = self.ledger.snapshot()
        snapshot["entries"][2], snapshot["entries"][3] = (
            snapshot["entries"][3], snapshot["entries"][2],
        )
        for index, row in enumerate(snapshot["entries"], start=1):
            row["seq"] = index
        with self.assertRaises(LedgerIntegrityError):
            RunLedger.from_snapshot(snapshot)

    def test_a_fully_recomputed_forgery_verifies_perfectly(self):
        """Stated as a fact about hash chains, not as a defect to be fixed.

        This is why the trusted head is load-bearing. If `verify()` could see
        this, an anchor would be unnecessary — and the next test would prove
        nothing.
        """
        forged = reseal(self.ledger.snapshot())
        forged["entries"][0]["data"]["hasher"] = "Bcrypt"
        forged = reseal(forged)

        restored = RunLedger.from_snapshot(forged)
        self.assertTrue(restored.verify())
        self.assertEqual(restored.entries[0].data["hasher"], "Bcrypt")

    def test_a_fully_recomputed_forgery_is_refused_against_the_trusted_head(self):
        """The one attack internal consistency cannot see."""
        forged = self.ledger.snapshot()
        forged["entries"][0]["data"]["hasher"] = "Bcrypt"
        forged = reseal(forged)

        self.assertNotEqual(forged["head"], self.trusted)
        with self.assertRaises(LedgerIntegrityError) as caught:
            RunLedger.from_snapshot(forged, expect_head=self.trusted)
        message = str(caught.exception)
        self.assertIn("does not continue the history", message)
        self.assertIn(self.trusted, message)

    def test_the_genuine_ledger_passes_the_same_anchored_check(self):
        """So the refusal above is about the forgery, not about anchoring."""
        restored = RunLedger.from_snapshot(
            self.ledger.snapshot(), expect_head=self.trusted
        )
        self.assertEqual(restored.head, self.trusted)

    def test_appending_to_a_genuine_ledger_moves_it_off_the_anchor(self):
        """An anchor pins the restore, not the future."""
        self.ledger.record(3, Event.EXECUTED, "hash_password", "again")
        with self.assertRaises(LedgerIntegrityError):
            RunLedger.from_snapshot(self.ledger.snapshot(), expect_head=self.trusted)


class LoaderDoesNotLaunderTest(unittest.TestCase):
    """The stored digest is evidence, not a field the loader regenerates."""

    def test_the_loader_keeps_the_digest_the_file_wrote(self):
        ledger = history()
        snapshot = ledger.snapshot()
        restored = RunLedger.from_snapshot(snapshot)
        for stored, entry in zip(snapshot["entries"], restored.entries):
            self.assertEqual(entry.digest, stored["digest"])

    def test_a_loader_that_replayed_record_would_have_accepted_the_tamper(self):
        """Pins why restoration is not `for row in rows: ledger.record(...)`.

        Replaying recomputes each digest from whatever the file says, so an
        edited entry comes back internally consistent and the chain verifies.
        The loader that was meant to catch the tamper would launder it.
        """
        tampered = history().snapshot()
        tampered["entries"][0]["data"]["hasher"] = "Bcrypt"

        replayed = RunLedger()
        for row in tampered["entries"]:
            replayed.record(
                row["round"], Event(row["event"]), row["task"], row["detail"],
                node_id=row["node_id"], assumption_id=row["assumption_id"],
                data=row["data"],
            )
        # A replaying loader would hand back a ledger that passes every check.
        self.assertTrue(replayed.verify())
        self.assertEqual(replayed.entries[0].data["hasher"], "Bcrypt")

        # The real loader refuses the same input.
        with self.assertRaises(LedgerIntegrityError):
            RunLedger.from_snapshot(tampered)


class ContinuityTest(unittest.TestCase):
    """The 7B invariant: process B continues A's history, it does not re-derive it."""

    def test_a_restored_ledger_continues_from_the_exact_committed_head(self):
        first = history()
        committed = first.head
        prefix = first.snapshot()["entries"]

        second = RunLedger.from_snapshot(first.snapshot(), expect_head=committed)
        self.assertEqual(second.head, committed)
        self.assertTrue(second.verify())

        appended = second.record(3, Event.AUDITED, "hash_password", "bcrypt holds")

        # The new entry was chained onto the committed head, not onto a
        # recomputed one that merely looks like it.
        self.assertEqual(appended.compute_digest(committed), appended.digest)
        self.assertEqual(appended.seq, len(prefix) + 1)
        self.assertTrue(second.verify())

        # And the history it continues is untouched, field for field.
        self.assertEqual(second.snapshot()["entries"][: len(prefix)], prefix)

    def test_the_prefix_stays_byte_identical_across_the_restore(self):
        first = history()
        before = json.dumps(first.snapshot()["entries"], sort_keys=True)

        second = RunLedger.from_snapshot(first.snapshot(), expect_head=first.head)
        second.record(3, Event.AUDITED, "hash_password", "bcrypt holds")

        after = json.dumps(
            second.snapshot()["entries"][: len(first)], sort_keys=True
        )
        self.assertEqual(before, after)

    def test_a_restore_that_lost_the_last_entry_cannot_be_continued(self):
        """The failure a silent truncation would otherwise cause much later."""
        first = history()
        committed = first.head
        truncated = first.snapshot()
        truncated["entries"].pop()
        truncated["head"] = truncated["entries"][-1]["digest"]

        # Internally consistent, and still not the history that was committed.
        self.assertTrue(RunLedger.from_snapshot(truncated).verify())
        with self.assertRaises(LedgerIntegrityError):
            RunLedger.from_snapshot(truncated, expect_head=committed)


class ProcessBoundaryTest(unittest.TestCase):
    """One process writes the file; another reads it and continues the chain."""

    def test_a_second_process_restores_the_head_the_first_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            first = history()
            first.save_json(path)
            committed = first.head

            script = (
                "import json,sys\n"
                "from contextmesh.execute import RunLedger, Event\n"
                "led = RunLedger.load_json(sys.argv[1], expect_head=sys.argv[2])\n"
                "assert led.head == sys.argv[2]\n"
                "assert led.verify()\n"
                "before = led.head\n"
                "e = led.record(3, Event.AUDITED, 'hash_password', 'bcrypt holds')\n"
                "assert e.compute_digest(before) == e.digest\n"
                "print(json.dumps({'restored': before, 'new_head': led.head, "
                "'count': len(led)}))\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", script, str(path), committed],
                capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["restored"], committed)
            self.assertEqual(result["count"], len(first) + 1)
            self.assertNotEqual(result["new_head"], committed)

    def test_a_second_process_refuses_a_file_tampered_between_the_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            first = history()
            first.save_json(path)
            committed = first.head

            # Someone with write access reseals the whole chain in between.
            forged = reseal(json.loads(path.read_text(encoding="utf-8")))
            forged["entries"][0]["data"]["hasher"] = "Bcrypt"
            path.write_text(json.dumps(reseal(forged)), encoding="utf-8")

            script = (
                "import sys\n"
                "from contextmesh.execute import RunLedger, LedgerIntegrityError\n"
                "try:\n"
                "    RunLedger.load_json(sys.argv[1], expect_head=sys.argv[2])\n"
                "except LedgerIntegrityError as exc:\n"
                "    print('REFUSED'); sys.exit(0)\n"
                "print('ACCEPTED'); sys.exit(1)\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", script, str(path), committed],
                capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("REFUSED", proc.stdout)


class DeterminismTest(unittest.TestCase):
    """The file is a function of the history, not of how the run happened to go."""

    def test_two_identical_histories_produce_identical_files(self):
        self.assertEqual(history().to_json(), history().to_json())

    def test_the_file_carries_no_wall_clock(self):
        text = history().to_json()
        for row in json.loads(text)["entries"]:
            self.assertNotIn("timestamp", row)
            self.assertNotIn("at", row)


if __name__ == "__main__":
    unittest.main()
