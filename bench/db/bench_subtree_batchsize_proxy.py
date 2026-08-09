#!/usr/bin/env python3
"""L1c proxy: does the size of a reply batch matter, with no exhaust and no overlap?

`find_common.cpp:39` pins kMaxBytesToReturnToClientAtOnce to BSONObjMaxUserSize (16 MB), so a
large covered scan comes back in a handful of very large batches. Nothing about that is a client
choice, and nothing about changing it requires a protocol or driver change.

I previously dismissed this by reasoning: without an exhaust cursor the client sends a getMore and
blocks, so smaller batches add round trips and overlap nothing. That reasoning does not explain the
prior observation on this workload that batchSize 2000 was -11.2% over 10/10 blocks at 96,238 rows.
When an argument and a measurement disagree, the measurement wins, so this probes it directly.

The likely mechanism is cache residency rather than overlap: a 16 MB reply buffer is written by the
server and then read by the client, and at that size neither side's working set stays in L2/L3,
while a ~1 MB batch does. If that is what is happening, the server can capture it alone by
lowering its own cap -- clients see the same bytes in more messages.

Client-side batchSize is used here only as a *proxy* for the server-side cap, to find out whether
the effect reproduces before touching mongod. Arms alternate within blocks, deltas are per block,
and every arm's output is compared element-wise against a reference.

Usage:
    bench_subtree_batchsize_proxy.py --path /000004/000046 --batch-sizes 0 1000 2000 8000
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
NODES = "layout2_view"
COVER_INDEX = "layout2_rootcause_exact_cover"


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def scan(coll: Any, lower: str, upper: str, batch_size: int) -> list[tuple]:
    cur = (coll.find({"path": {"$gte": lower, "$lt": upper}},
                     {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
           .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX))
    if batch_size:
        cur = cur.batch_size(batch_size)
    return [(d.get("node_id"), d.get("title"), d.get("summary")) for d in cur]


def window(coll: Any, lower: str, upper: str, bs: int, pid: int, seconds: float,
           tag: str) -> dict[str, Any]:
    # Settle this arm before the window opens. Without it the arm that happens to run first in a
    # block pays for whatever the previous arm left in cache, and since the arm order rotates the
    # damage lands unevenly -- it showed up as a spurious -35% to -53% in the first two blocks.
    scan(coll, lower, upper, bs)
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
        rows = scan(coll, lower, upper, bs)
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
    ap.add_argument("--path", default="/000004/000046")
    ap.add_argument("--batch-sizes", type=int, nargs="+", default=[0, 1000, 2000, 8000])
    ap.add_argument("--blocks", type=int, default=8)
    ap.add_argument("--warmup-blocks", type=int, default=2,
                    help="leading blocks run but discarded, so cold cache cannot reach the medians")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    uri = f"mongodb://localhost:{args.port}/?directConnection=true"
    client = MongoClient(uri)
    coll = client[DB][NODES]
    pid = ab.mongod_pid(args.port)
    lower, upper = args.path + "/", args.path + "0"

    # Measure on the shipped configuration: fusion on.
    ab.set_arm(client, True)
    reference = scan(coll, lower, upper, 0)
    log(f"{args.path}: {len(reference)} rows; baseline batchSize=0 (server default, 16 MB cap)")

    results: dict[str, Any] = {"path": args.path, "rows": len(reference), "arms": {}}
    mismatches = 0
    per_bs: dict[int, list[dict[str, Any]]] = {bs: [] for bs in args.batch_sizes}

    total_blocks = args.blocks + args.warmup_blocks
    for b in range(total_blocks):
        order = list(args.batch_sizes)
        if b % 2:
            order.reverse()
        warm = b < args.warmup_blocks
        for bs in order:
            m = window(coll, lower, upper, bs, pid, args.seconds, f"{b}_{bs}")
            if m.pop("rows") != reference:
                mismatches += 1
            if not warm:
                per_bs[bs].append(m)
        if warm:
            log(f"  block {b}: warmup, discarded")
            continue
        base = per_bs[args.batch_sizes[0]][-1]
        parts = []
        for bs in args.batch_sizes[1:]:
            cur = per_bs[bs][-1]
            parts.append(f"bs{bs} wall {(cur['wall_us']/base['wall_us']-1)*100:+.1f}%")
        log(f"  block {b}: " + "  ".join(parts))

    baseline_key = args.batch_sizes[0]
    for bs in args.batch_sizes:
        entry: dict[str, Any] = {}
        for metric in ("wall_us", "cpu_us", "instructions"):
            deltas = []
            for i in range(len(per_bs[baseline_key])):
                b0 = per_bs[baseline_key][i][metric]
                bx = per_bs[bs][i][metric]
                if b0 and bx:
                    deltas.append((bx / b0 - 1) * 100)
            if deltas:
                entry[metric] = {"median": statistics.median(deltas),
                                 "min": min(deltas), "max": max(deltas),
                                 "improved": sum(1 for d in deltas if d < 0),
                                 "blocks": len(deltas)}
        entry["median_ops_per_window"] = statistics.median(
            m["ops"] for m in per_bs[bs])
        results["arms"][str(bs)] = entry
        if bs != baseline_key:
            w, c = entry.get("wall_us"), entry.get("cpu_us")
            i_ = entry.get("instructions")
            log(f"batchSize {bs}: wall {w['median']:+.2f}% [{w['min']:+.2f}, {w['max']:+.2f}] "
                f"{w['improved']}/{w['blocks']}; server CPU {c['median']:+.2f}%; "
                f"instructions {i_['median']:+.2f}%" if w and c and i_ else f"batchSize {bs}: n/a")
    results["mismatches"] = mismatches
    log(f"output mismatches: {mismatches}")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
