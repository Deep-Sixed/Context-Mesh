"""Fail-closed extras for contextmesh.runledger v1: exact container, unique JSON keys."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contextmesh.execute import LedgerIntegrityError, RunLedger
from test_ledger import history


class ExactContainerTest(unittest.TestCase):
    def test_an_unknown_container_field_is_refused_not_dropped(self):
        snapshot = history().snapshot()
        snapshot["approved_by"] = "alice"
        with self.assertRaises(LedgerIntegrityError) as caught:
            RunLedger.from_snapshot(snapshot)
        self.assertIn("approved_by", str(caught.exception))
        self.assertIn("which v1 does not define", str(caught.exception))


class DuplicateJsonKeyTest(unittest.TestCase):
    def test_duplicate_top_level_key_is_refused(self):
        path = Path(tempfile.mkdtemp()) / "ledger.json"
        text = history().to_json()
        text = text.replace(
            '"schema": "contextmesh.runledger"',
            '"schema": "contextmesh.runledger", "schema": "other"',
            1,
        )
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(LedgerIntegrityError) as caught:
            RunLedger.load_json(path)
        self.assertIn("duplicate JSON key", str(caught.exception))
        self.assertIn("schema", str(caught.exception))

    def test_duplicate_key_inside_signed_data_is_refused(self):
        path = Path(tempfile.mkdtemp()) / "ledger.json"
        snapshot = history().snapshot()
        text = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        needle = '"data":{'
        self.assertIn(needle, text)
        text = text.replace(needle, '"data":{"hasher":"Argon2id","hasher":"Bcrypt",', 1)
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(LedgerIntegrityError) as caught:
            RunLedger.load_json(path)
        self.assertIn("duplicate JSON key", str(caught.exception))
        self.assertIn("hasher", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
