import io
import sys
import unittest
from contextlib import redirect_stdout

from contextmesh import cli


class CliEncodingTest(unittest.TestCase):
    def test_demo_has_ascii_fallback_on_cp1252_stdout(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")
        original = sys.stdout
        try:
            sys.stdout = stream
            with redirect_stdout(stream):
                self.assertEqual(cli.main(["--rounds", "1", "demo"]), 0)
            stream.flush()
        finally:
            sys.stdout = original

        output = raw.getvalue().decode("cp1252")
        self.assertIn("BUILD PATH * what earns a node in the graph", output)
        self.assertIn("committed -> walkable", output)
        self.assertIn("#", output)
        self.assertNotIn("─", output)


if __name__ == "__main__":
    unittest.main()
