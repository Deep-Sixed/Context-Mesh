"""Fold index.html and mesh.js into one file you can send someone.

    python dashboard/bundle.py [out.html]
    python dashboard/bundle.py --artifact [out.html]

The dashboard is two files so it can be read and reviewed; this makes the
single self-contained artefact when that matters more.

``--artifact`` additionally strips the document skeleton, for hosts that supply
their own ``<!doctype>``/``<head>``/``<body>`` and wrap the fragment.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def bundle(out: Path, *, fragment: bool = False) -> Path:
    html = (HERE / "index.html").read_text(encoding="utf-8")
    js = (HERE / "mesh.js").read_text(encoding="utf-8")
    if '<script src="mesh.js"></script>' not in html:
        raise SystemExit("index.html no longer loads mesh.js by src")
    html = html.replace(
        '<script src="mesh.js"></script>',
        # A literal "</script>" inside the source would close the tag early.
        "<script>\n" + js.replace("</script>", "<\\/script>") + "\n</script>",
    )
    if re.search(r'<script id="mesh-data" type="application/json">\s*\{\}\s*</script>', html):
        raise SystemExit("run `python -m contextmesh export --inline` first")
    if fragment:
        html = _strip_skeleton(html)
    out.write_text(html, encoding="utf-8")
    return out


def _strip_skeleton(html: str) -> str:
    """Drop the document wrapper, keeping the head contents and the body."""
    head = re.search(r"<head>(.*?)</head>", html, re.S)
    body = re.search(r"<body>(.*?)</body>", html, re.S)
    if not (head and body):
        raise SystemExit("index.html is not shaped as <head>…</head><body>…</body>")
    inner_head = re.sub(r'\s*<meta[^>]*>', "", head.group(1)).strip()
    return inner_head + "\n" + body.group(1).strip() + "\n"


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--artifact"]
    fragment = "--artifact" in sys.argv[1:]
    default = "context-mesh-artifact.html" if fragment else "context-mesh.html"
    target = Path(args[0]) if args else HERE / default
    bundle(target, fragment=fragment)
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")
