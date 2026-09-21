#!/usr/bin/env python3
"""A/B internalQueryEnableGraphLookupIndexScan on $graphLookup depth expansion.

Same binary, one mongod. Gate off = stock $graphLookup (before). Gate on = index-scan
BFS (after). path+depth is timed as the application baseline, not as a third arm of
the gate. Correctness: after must return the same node_id set as before.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_children_depth import (  # noqa: E402
    TREE_ID,
    build_tree,
    graphlookup,
    ingest,
    path_depth,
    percentile,
    pick_cohort,
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def set_gate(client: MongoClient, on: bool) -> None:
    client.admin.command({"setParameter": 1, "internalQueryEnableGraphLookupIndexScan": on})
    got = client.admin.command(
        {"getParameter": 1, "internalQueryEnableGraphLookupIndexScan": 1}
    )
    if bool(got["internalQueryEnableGraphLookupIndexScan"]) != on:
        raise RuntimeError(f"gate did not stick: {got}")
    # Absorption of the trailing $project is decided at optimizeAt from the gate.
    # Drop cached agg plans so gate-off and gate-on do not share a pipeline.
    try:
        client["bench"].command({"planCacheClear": "layout2_view"})
    except Exception:
        pass


def time_fn(fn, repeats: int) -> dict:
    samples = []
    ids = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        ids = fn()
        samples.append((time.perf_counter() - t0) * 1e3)
    return {
        "n": len(ids),
        "p50_ms": percentile(samples, 50),
        "p95_ms": percentile(samples, 95),
        "mean_ms": statistics.fmean(samples),
        "wall_ms": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="mongodb://127.0.0.1:57027/?directConnection=true")
    parser.add_argument("--nodes", type=int, default=80_000)
    parser.add_argument("--cohort", type=int, default=16)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--depths", default="1,2,3")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    depths = [int(x) for x in args.depths.split(",") if x]

    client = MongoClient(args.uri, maxPoolSize=1, minPoolSize=1)
    hello = client.admin.command("buildInfo")
    log(f"hello {hello['version']} git {hello.get('gitVersion', '')[:12]}")
    try:
        client.admin.command({"getParameter": 1, "internalQueryEnableGraphLookupIndexScan": 1})
    except Exception as exc:
        raise SystemExit(
            f"server does not have internalQueryEnableGraphLookupIndexScan: {exc}"
        ) from exc

    coll = client["bench"]["layout2_view"]
    if coll.count_documents({}) != args.nodes:
        log(f"ingest {args.nodes} docs")
        docs = build_tree(args.nodes, (6, 14), 8, seed=7)
        ingest(coll, docs)
    else:
        log("reusing existing collection")
        docs = list(coll.find({"tree_id": TREE_ID}, {"_id": 0, "node_id": 1, "parent_id": 1, "path": 1, "depth": 1}))

    by_parent: dict[str | None, list[str]] = defaultdict(list)
    by_id = {}
    for d in docs:
        by_id[d["node_id"]] = d
        if d.get("parent_id") is not None:
            by_parent[d["parent_id"]].append(d["node_id"])

    def expected(start: dict, depth: int) -> set[str]:
        out: set[str] = set()
        q = deque([(start["node_id"], 0)])
        while q:
            nid, dep = q.popleft()
            if dep >= depth:
                continue
            for cid in by_parent.get(nid, []):
                out.add(cid)
                q.append((cid, dep + 1))
        return out

    cohort = pick_cohort(docs, args.cohort, seed=11)
    log(f"cohort {len(cohort)}")

    out = {
        "mongo_version": hello["version"],
        "gitVersion": hello.get("gitVersion"),
        "n_docs": coll.count_documents({}),
        "cohort": len(cohort),
        "repeats": args.repeats,
        "depths": {},
        "correctness": {},
    }

    for depth in depths:
        label = f"d{depth}"
        log(f"=== {label} ===")
        per = {"before": [], "after": [], "path": []}
        mismatches = 0
        for i, start in enumerate(cohort):
            want = expected(start, depth)
            set_gate(client, False)
            before = time_fn(lambda s=start, d=depth: graphlookup(coll, s, d), args.repeats)
            set_gate(client, True)
            after = time_fn(lambda s=start, d=depth: graphlookup(coll, s, d), args.repeats)
            path = time_fn(lambda s=start, d=depth: path_depth(coll, s, d), args.repeats)
            set_gate(client, False)

            got_before = set(graphlookup(coll, start, depth))
            set_gate(client, True)
            got_after = set(graphlookup(coll, start, depth))
            set_gate(client, False)
            if got_before != want or got_after != want or got_before != got_after:
                mismatches += 1
                log(f"  MISMATCH {start['node_id']} want={len(want)} before={len(got_before)} after={len(got_after)}")

            rec = {
                "node_id": start["node_id"],
                "root_depth": start["depth"],
                "want_n": len(want),
            }
            per["before"].append({**before, **rec})
            per["after"].append({**after, **rec})
            per["path"].append({**path, **rec})
            if i == 0:
                log(f"  first root n={before['n']} before={before['p50_ms']:.3f} after={after['p50_ms']:.3f} path={path['p50_ms']:.3f} ms")

        def summarize(rows: list[dict]) -> dict:
            p50s = [r["p50_ms"] for r in rows]
            ns = [r["n"] for r in rows]
            return {
                "p50_ms": percentile(p50s, 50),
                "p95_ms": percentile(p50s, 95),
                "mean_ms": statistics.fmean(p50s),
                "median_n": percentile([float(n) for n in ns], 50),
            }

        summary = {name: summarize(rows) for name, rows in per.items()}
        b, a = summary["before"]["p50_ms"], summary["after"]["p50_ms"]
        delta = (a - b) / b * 100 if b else None
        log(f"  cohort before={b:.3f} after={a:.3f} ({delta:+.1f}%) path={summary['path']['p50_ms']:.3f} mismatches={mismatches}")
        out["depths"][label] = {"summary": summary, "per_root": per, "delta_pct_after_vs_before": delta}
        out["correctness"][label] = mismatches

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
