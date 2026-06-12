#!/usr/bin/env python3
"""Render bench_databases.py JSON result(s) into comparison tables (markdown)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def human(n: float) -> str:
    if n < 1e6:
        return f"{n / 1e3:.0f} KB"
    if n < 1e9:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e9:.2f} GB"


ENGINE_ORDER = ["mongo", "postgres", "duckdb", "sqlite"]
ENGINE_LABEL = {"mongo": "MongoDB", "postgres": "PostgreSQL (JSONB)",
                "duckdb": "DuckDB", "sqlite": "SQLite"}
QUERY_LABEL = {
    "q_point": "Point lookup (node_id)",
    "q_children": "Children (1-hop)",
    "q_subtree": "Subtree descendants",
    "q_depth": "Filter by depth",
    "q_range": "start_index window",
    "q_search": "Summary substring scan",
}


def engines_in(res: dict) -> list[str]:
    present = [e for e in ENGINE_ORDER if e in res["engines"]]
    present += [e for e in res["engines"] if e not in present]
    return present


def render(res: dict) -> str:
    engs = engines_in(res)
    n = res["node_count"]
    out = [f"### `{Path(res['doc']).name}` ({n:,} nodes)\n"]

    out.append("**Storage & ingest**\n")
    out.append("| engine | total size | bytes/node | index size | ingest (nodes/s) | index build |")
    out.append("|---|--:|--:|--:|--:|--:|")
    for e in engs:
        d = res["engines"][e]
        if "error" in d:
            out.append(f"| {ENGINE_LABEL.get(e, e)} | ERROR: {d['error'][:40]} | | | | |")
            continue
        s = d["storage"]
        out.append(
            f"| {ENGINE_LABEL.get(e, e)} | {human(s['total'])} | {d['bytes_per_node']:.0f} B "
            f"| {human(s['index'])} | {d['ingest_nodes_per_sec']:,} | {d['index_build_seconds']:.1f}s |"
        )
    out.append("")

    for metric, mlabel in (("p50_ms", "P50 latency (ms)"), ("p95_ms", "P95 latency (ms)")):
        out.append(f"**{mlabel}** (lower is better)\n")
        header = "| query | " + " | ".join(ENGINE_LABEL.get(e, e) for e in engs) + " |"
        out.append(header)
        out.append("|---" * (len(engs) + 1) + "|")
        for q, qlabel in QUERY_LABEL.items():
            cells = []
            best = None
            vals = {}
            for e in engs:
                d = res["engines"][e]
                v = d.get("queries", {}).get(q, {}).get(metric)
                vals[e] = v
                if v is not None and (best is None or v < best):
                    best = v
            for e in engs:
                v = vals[e]
                if v is None:
                    cells.append("-")
                elif v == best:
                    cells.append(f"**{v:.3f}**")
                else:
                    cells.append(f"{v:.3f}")
            out.append(f"| {qlabel} | " + " | ".join(cells) + " |")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", help="bench result JSON file(s)")
    ap.add_argument("--out", help="write markdown here")
    args = ap.parse_args()

    sections = []
    for path in args.results:
        res = json.load(open(path))
        sections.append(render(res))
    md = "# Database Comparison: PageIndex Retrieval Workload\n\n" + "\n\n".join(sections)
    if args.out:
        Path(args.out).write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
