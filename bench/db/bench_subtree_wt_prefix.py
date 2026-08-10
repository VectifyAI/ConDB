#!/usr/bin/env python3
"""What does WiredTiger's key prefix compression cost this scan?

WiredTiger compresses row-store keys on leaf pages by storing, for each key, a shared-prefix length
plus the differing suffix. MongoDB turns this on for every index by default
(`wiredtiger_global_options.idl`, `storage.wiredTiger.indexConfig.prefixCompression`, default true).

The trade is asymmetric for this index. A key of `(path, node_id, title, summary)` is about 420
bytes, of which `summary` is ~340; adjacent keys share only the ~28-byte `path` prefix. So prefix
compression saves little space here -- measured at 10.5% of the index -- but it forces WiredTiger to
**materialise every key into a buffer** on read, where an uncompressed key can be handed back as a
pointer into the page with no copy at all.

Two collections with identical documents, one index each, differing only in that WT config. Same
query, same rows, output compared element-wise. Arms alternate within blocks; the two collections
are different files, which is inherent to the test and is why both are warmed before every window.

Usage:
    bench_subtree_wt_prefix.py --path /000006/000075/000773 --blocks 10 --seconds 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient

import bench_subtree_fused_ab as ab

DB = "bench"
ARMS = (
    ("prefix_on", "layout2_view", "layout2_rootcause_exact_cover"),
    ("prefix_off", "layout2_noprefix", "cover"),
)


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def scan(coll: Any, index: str, lower: str, upper: str) -> list[tuple]:
    cur = (coll.find({"path": {"$gte": lower, "$lt": upper}},
                     {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
           .sort([("path", 1), ("node_id", 1)]).hint(index))
    return [(d.get("node_id"), d.get("title"), d.get("summary")) for d in cur]


def window(coll: Any, index: str, lower: str, upper: str, pid: int, seconds: float,
           tag: str) -> dict[str, Any]:
    scan(coll, index, lower, upper)  # settle: these are different files
    handle = ab.start_perf(pid, seconds, tag)
    time.sleep(0.20)
    ops = 0
    rows: list[tuple] = []
    cpu0 = ab.proc_cpu_us(pid)
    t0 = time.perf_counter()
    while True:
        elapsed = time.perf_counter() - t0
        if ops and elapsed + (elapsed / ops) > seconds:
            break
        if elapsed >= seconds:
            break
        rows = scan(coll, index, lower, upper)
        ops += 1
    wall = (time.perf_counter() - t0) * 1e6
    cpu = ab.proc_cpu_us(pid) - cpu0
    instr = ab.read_perf(handle)
    return {"ops": ops, "rows": rows,
            "wall_us": wall / max(ops, 1), "cpu_us": cpu / max(ops, 1),
            "instructions": (instr / ops) if (instr is not None and ops) else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--path", default="/000006/000075/000773")
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--warmup-blocks", type=int, default=2)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    client = MongoClient(f"mongodb://localhost:{args.port}/?directConnection=true")
    db = client[DB]
    pid = ab.mongod_pid(args.port)
    client.admin.command({"setParameter": 1,
                          "internalQueryEnableFusedCoveredProjection": True})
    lower, upper = args.path + "/", args.path + "0"

    reference = None
    for name, coll, index in ARMS:
        wp = (db[coll].find({"path": {"$gte": lower, "$lt": upper}},
                            {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
              .sort([("path", 1), ("node_id", 1)]).hint(index)).explain()
        wp = wp.get("queryPlanner", {}).get("winningPlan", {})
        fused = ab.ixscan_fused(wp)
        size = db.command("collStats", coll)["indexSizes"][index]
        log(f"{name}: {coll}.{index} {size/1e9:.3f} GB, fused={fused}")
        if not fused:
            raise SystemExit(f"{name} did not take the fused path; probe invalid")
        rows = scan(db[coll], index, lower, upper)
        if reference is None:
            reference = rows
        elif rows != reference:
            raise SystemExit(f"{name} returned different rows; probe invalid")
    log(f"both arms return {len(reference)} identical rows")

    per: dict[str, list[dict[str, Any]]] = {a[0]: [] for a in ARMS}
    mismatches = 0
    for b in range(args.blocks + args.warmup_blocks):
        order = list(ARMS) if b % 2 == 0 else list(reversed(ARMS))
        warm = b < args.warmup_blocks
        for name, coll, index in order:
            m = window(db[coll], index, lower, upper, pid, args.seconds, f"{b}{name}")
            if m.pop("rows") != reference:
                mismatches += 1
            if not warm:
                per[name].append(m)
        if warm:
            log(f"  block {b}: warmup, discarded")
            continue
        on, off = per["prefix_on"][-1], per["prefix_off"][-1]
        log(f"  block {b}: server CPU {(off['cpu_us']/on['cpu_us']-1)*100:+.2f}%  "
            f"instructions {(off['instructions']/on['instructions']-1)*100:+.2f}%")

    out: dict[str, Any] = {"path": args.path, "rows": len(reference),
                           "mismatches": mismatches, "metrics": {}}
    for metric in ("instructions", "cpu_us", "wall_us"):
        deltas = []
        for i in range(len(per["prefix_on"])):
            a, bb = per["prefix_on"][i][metric], per["prefix_off"][i][metric]
            if a and bb:
                deltas.append((bb / a - 1) * 100)
        if deltas:
            out["metrics"][metric] = {"median": statistics.median(deltas),
                                      "min": min(deltas), "max": max(deltas),
                                      "improved": sum(1 for d in deltas if d < 0),
                                      "blocks": len(deltas)}
            log(f"prefix_compression=false vs true, {metric}: "
                f"{statistics.median(deltas):+.2f}% "
                f"[{min(deltas):+.2f}, {max(deltas):+.2f}] "
                f"{sum(1 for d in deltas if d < 0)}/{len(deltas)} blocks lower")
    log(f"output mismatches: {mismatches}")

    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2))
        log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
