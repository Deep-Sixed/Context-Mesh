"""Fold index.html and mesh.js into one file you can send someone.

    python dashboard/bundle.py [out.html]

The dashboard is two files so it can be read and reviewed; this makes the
single self-contained artefact when that matters more.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def bundle(out: Path) -> Path:
    html = (HERE / "index.html").read_text(encoding="utf-8")
    js = (HERE / "mesh.js").read_text(encoding="utf-8")
    if '<script src="mesh.js"></script>' not in html:
        raise SystemExit("index.html no longer loads mesh.js by src")
    html = html.replace(
        '<script src="mesh.js"></script>',
        "<script>\n" + js.replace("</script>", "<\\/script>") + "\n</script>",
    )
    if re.search(r'<script id="mesh-data" type="application/json">\s*\{\}\s*</script>', html):
        raise SystemExit("run `python -m contextmesh export --inline` first")
    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "context-mesh.html"
    print(f"wrote {bundle(target)} ({target.stat().st_size:,} bytes)")
