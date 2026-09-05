"""Dashboard JSON stays data even when graph text looks like HTML."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from contextmesh.cli import _inline, _json_for_html

OPEN = '<script id="mesh-data" type="application/json">'
CLOSE = "</script>"


class DashboardJsonEmbeddingTest(unittest.TestCase):
    def test_script_terminator_cannot_escape_the_json_data_block(self):
        payload = {
            "label": "</script><script>window.pwned = true</script>",
            "comment": "<!-- still data -->",
            "ampersand": "a&b",
        }
        blob = _json_for_html(payload)

        self.assertNotIn("<", blob)
        self.assertNotIn(">", blob)
        self.assertNotIn("&", blob)
        self.assertIn(r"\u003c/script\u003e", blob)
        self.assertEqual(json.loads(blob), payload)

    def test_inline_round_trips_hostile_text_without_creating_a_second_script(self):
        payload = {
            "nested": [
                {"text": "</ScRiPt><img src=x onerror=alert(1)>"},
                {"text": "ordinary"},
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.html"
            path.write_text(
                "<!doctype html><body>" + OPEN + "{}" + CLOSE + "<p>after</p></body>",
                encoding="utf-8",
            )
            _inline(path, payload)
            html = path.read_text(encoding="utf-8")

        self.assertEqual(html.count("<script"), 1)
        self.assertEqual(html.count(CLOSE), 1)
        self.assertNotIn("<img", html)
        match = re.search(
            r'<script id="mesh-data" type="application/json">(.*?)</script>',
            html,
            re.S,
        )
        self.assertIsNotNone(match)
        self.assertEqual(json.loads(match.group(1)), payload)
        self.assertIn("<p>after</p>", html)

    def test_inline_replaces_an_existing_snapshot_idempotently(self):
        first = {"value": "first"}
        second = {"value": "second </script> value"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.html"
            path.write_text(OPEN + "{}" + CLOSE, encoding="utf-8")
            _inline(path, first)
            _inline(path, second)
            html = path.read_text(encoding="utf-8")

        match = re.search(
            r'<script id="mesh-data" type="application/json">(.*?)</script>',
            html,
            re.S,
        )
        self.assertIsNotNone(match)
        self.assertEqual(json.loads(match.group(1)), second)
        self.assertEqual(html.count(CLOSE), 1)

    def test_missing_data_block_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.html"
            path.write_text("<html></html>", encoding="utf-8")
            with self.assertRaises(SystemExit):
                _inline(path, {"value": "anything"})


if __name__ == "__main__":
    unittest.main()
