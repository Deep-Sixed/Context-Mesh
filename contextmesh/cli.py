"""Command line: build the graph, ask it things, and see what it is hiding.

    python -m contextmesh demo          # the worked example, end to end
    python -m contextmesh ask "..."     # one question, one readable path
    python -m contextmesh health        # graph health signals
    python -m contextmesh export        # write the dashboard's data
    python -m contextmesh invalidate    # break an assumption, show the fallout
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .demo import run
from .health import check
from .model import AssumptionStatus

DEFAULT_EXPORT = Path(__file__).resolve().parent.parent / "dashboard" / "data" / "mesh.json"
DASHBOARD_HTML = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"

RULE = "─" * 78


def _h(title: str) -> None:
    print(f"\n{title}\n{RULE}")


def _fmt(n: float) -> str:
    return f"{n:,}"


# ── commands ─────────────────────────────────────────────────────────────
def cmd_demo(args: argparse.Namespace) -> int:
    result = run(rounds=args.rounds)
    graph, walker, build = result.graph, result.walker, result.build

    _h("BUILD PATH · what earns a node in the graph")
    for stage in build.stages:
        bar = "█" * min(40, stage.admitted // max(1, build.spans_in // 40 or 1))
        print(
            f"  {stage.name:<8} {stage.caption:<22} "
            f"admitted {stage.admitted:>5}  dropped {stage.dropped:>5}  {bar}"
        )
    print(
        f"\n  spans in {_fmt(build.spans_in)} · dropped at resolve "
        f"{_fmt(build.dropped_at_resolve)} · committed → walkable "
        f"{_fmt(build.committed_walkable)}"
    )

    _h("CONTEXT MESH · the live graph")
    for name, count in graph.type_counts().items():
        if count:
            print(f"  {name:<12} {count:>4}")
    print("\n  edges by type")
    for name, count in graph.edge_counts().items():
        if count:
            print(f"  {name:<14} {count:>4}")
    print(f"\n  untyped edges: {graph.untyped_edges}")

    _h("WALKING · every answer walks a path you can read")
    print(
        f"  {len(walker.walks)} walks · {walker.resolved_rate:.1%} resolved · "
        f"median {walker.median_hops()} hops · "
        f"{walker.token_saving():.1%} fewer tokens than flat top-k"
    )
    for walk in [w for w in walker.walks if w.resolved][:3]:
        print(f"\n  Q: {walk.question}")
        for line in walk.path().splitlines():
            print(f"    {line}")

    _h("DEAD ENDS · walks that ended nowhere")
    for reason, count in walker.dead_end_ledger().items():
        print(f"  {reason:<20} {count:>4}")
    for walk in [w for w in walker.walks if w.dead_end][:2]:
        print(f"\n  Q: {walk.question}\n    {walk.dead_end.value}: {walk.detail}")

    _h("DECISION HISTORY · append-only")
    for record in result.decisions.records:
        marker = "→ supersedes " + record.supersedes if record.supersedes else ""
        print(f"  [{record.at_build}] {record.title} {marker}")
        print(f"        {record.rationale}")

    _h("SELECTIVE INVALIDATION · what fell, and what did not")
    inv = result.invalidation
    if inv is None:
        print("  nothing was rejected in this run")
    else:
        print(f"  rejected: {inv.statement}")
        print(f"  blast radius: {inv.blast_radius} node(s); preserved: {len(inv.preserved)}")
        for node_id in inv.invalidated:
            print(f"    ✗ {inv.why(node_id)}")
        if inv.replacement_id:
            replacement = graph.assumptions[inv.replacement_id]
            print(f"  replacement: {replacement.statement}")

    _h("GRAPH HEALTH")
    for signal in check(graph, result.resolver, walker):
        mark = {"error": "!!", "warn": " !", "info": "  "}[signal.severity]
        print(f"  {mark} {signal.kind:<24} {signal.count:>4}  {signal.detail}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    result = run(rounds=args.rounds)
    for question in args.question:
        walk = result.walker.ask(question)
        print(f"\nQ: {question}")
        if walk.resolved:
            answer = result.graph.node(walk.answer_id)
            print(f"A: {answer.label}")
            print(f"   ({walk.hops} hops, score {walk.score:.2f}, "
                  f"{walk.tokens_walked} tokens against {walk.tokens_flat} flat)")
            print("\n" + walk.path())
            if walk.evidence:
                print("\n   evidence:")
                for eid in walk.evidence:
                    print(f"     · {result.graph.node(eid).label[:72]}")
        else:
            print(f"A: dead end — {walk.dead_end.value}")
            print(f"   {walk.detail}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    result = run(rounds=args.rounds)
    signals = check(result.graph, result.resolver, result.walker)
    for signal in signals:
        mark = {"error": "!!", "warn": " !", "info": "  "}[signal.severity]
        print(f"{mark} {signal.kind} ({signal.count})")
        print(f"   {signal.detail}")
        print(f"   remedy: {signal.remedy}")
        for item in signal.items[:5]:
            print(f"     · {item[:80]}")
        print()
    return 0


def cmd_invalidate(args: argparse.Namespace) -> int:
    result = run(rounds=args.rounds)
    inv = result.invalidation
    if inv is None:
        print("nothing rejected")
        return 1
    print(f"rejected assumption: {inv.statement}\n")
    print(f"INVALIDATED ({inv.blast_radius})")
    for node_id in inv.invalidated:
        print(f"  ✗ {result.graph.node(node_id).label[:70]}")
        print(f"      because: {inv.why(node_id)}")
    print(f"\nPRESERVED ({len(inv.preserved)}) — untouched by the rejection")
    for node_id in inv.preserved[:8]:
        print(f"  ✓ {result.graph.node(node_id).label[:70]}")
    if len(inv.preserved) > 8:
        print(f"  … and {len(inv.preserved) - 8} more")

    print("\nASSUMPTION LINEAGE")
    for assumption in result.graph.assumptions.values():
        flag = {
            AssumptionStatus.ACTIVE: "active",
            AssumptionStatus.REJECTED: "REJECTED",
            AssumptionStatus.SUPERSEDED: "superseded",
        }[assumption.status]
        print(f"  v{assumption.version} [{flag}] {assumption.statement}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    result = run(rounds=args.rounds)
    payload = result.payload()
    out = Path(args.out) if args.out else DEFAULT_EXPORT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")

    if args.inline and DASHBOARD_HTML.exists():
        _inline(DASHBOARD_HTML, payload)
        print(f"inlined into {DASHBOARD_HTML}")
    return 0


def _inline(html_path: Path, payload: Dict[str, Any]) -> None:
    """Replace the dashboard's embedded snapshot so it works from file://."""
    html = html_path.read_text(encoding="utf-8")
    blob = json.dumps(payload, separators=(",", ":"))
    pattern = re.compile(
        r'(<script id="mesh-data" type="application/json">)(.*?)(</script>)',
        re.S,
    )
    if not pattern.search(html):
        raise SystemExit(f"{html_path} has no <script id='mesh-data'> block")
    rewritten = pattern.sub(lambda m: m.group(1) + blob + m.group(3), html)
    html_path.write_text(rewritten, encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="contextmesh",
        description="A graph that remembers why things are connected.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=8,
        help="walk rounds to run before reporting (default: 8)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="build, walk, break an assumption, report").set_defaults(
        func=cmd_demo
    )
    ask = sub.add_parser("ask", help="ask the graph a question")
    ask.add_argument("question", nargs="+")
    ask.set_defaults(func=cmd_ask)
    sub.add_parser("health", help="graph health signals").set_defaults(func=cmd_health)
    sub.add_parser(
        "invalidate", help="reject an assumption and show the blast radius"
    ).set_defaults(func=cmd_invalidate)
    export = sub.add_parser("export", help="write the dashboard payload")
    export.add_argument(
        "--rounds",
        type=int,
        default=45,
        help="walk rounds; the default fills the dashboard's 1,008-cell grid",
    )
    export.add_argument("--out", help=f"output path (default: {DEFAULT_EXPORT})")
    export.add_argument(
        "--inline",
        action="store_true",
        help="also embed the payload in dashboard/index.html",
    )
    export.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
