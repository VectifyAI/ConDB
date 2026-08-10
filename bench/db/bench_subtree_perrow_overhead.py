#!/usr/bin/env python3
"""How much of this scan is per-row overhead, i.e. what could batching actually amortise?

A vectorised or batched index scan -- pull N keys from WiredTiger in a tight loop while the page is
hot, decode them together, then hand them out -- amortises everything that costs the same regardless
of how big a row is: the virtual dispatch chain (`getNextBatch` -> `PlanStage::work` ->
`ProjectionStage::doWork` -> `IndexScan::doWork`), the WT cursor call, the per-row allocation, the
WorkingSetMember transitions. It cannot amortise anything proportional to the bytes: the KeyString
decode, the copy into the output object, the copy into the reply buffer, the transmission.

So the ceiling on batching is the per-row fixed cost, and that can be measured without building
anything: scan the **same total payload bytes** twice, once as many small rows and once as few large
rows. Whatever differs is per-row, not per-byte.

    cost(op) ~= rows * fixed + bytes * variable

With bytes held equal, the difference between a many-row arm and a few-row arm isolates `fixed`,
and `fixed * 11686` is what batching could recover on the real P50 subtree.

Both collections carry the same four-component covering index and are scanned by the same fused
covered projection, so the shape of the work is identical -- only the row/byte ratio differs.

Usage:
    bench_subtree_perrow_overhead.py --blocks 10 --seconds 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from pymongo import ASCENDING, MongoClient

import bench_subtree_fused_ab as ab

DB = "perrow_probe"
PREFIX = "/p"
TOTAL_PAYLOAD = 8_000_000  # bytes, held equal across arms


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def build(client: MongoClient, name: str, rows: int) -> tuple[int, int]:
    """`rows` documents whose summaries sum to TOTAL_PAYLOAD bytes."""
    db = client[DB]
    db[name].drop()
    per = TOTAL_PAYLOAD // rows
    batch = []
    total = 0
    for i in range(rows):
        summary = ("s%08d" % i) * (per // 9) + "x" * (per % 9)
        total += len(summary)
        batch.append({"path": f"{PREFIX}/{i:09d}", "node_id": f"n{i}",
                      "title": f"t{i}", "summary": summary})
        if len(batch) >= 1000:
            db[name].insert_many(batch, ordered=False)
            batch = []
    if batch:
        db[name].insert_many(batch, ordered=False)
    db[name].create_index([("path", ASCENDING), ("node_id", ASCENDING),
                           ("title", ASCENDING), ("summary", ASCENDING)], name="cover")
    return rows, total


def scan(coll: Any, lower: str, upper: str) -> tuple[int, int]:
    cur = (coll.find({"path": {"$gte": lower, "$lt": upper}},
                     {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
           .sort([("path", 1), ("node_id", 1)]).hint("cover"))
    n = nbytes = 0
    for d in cur:
        nbytes += len(d.get("summary", "")) + len(d.get("title", ""))
        n += 1
    return n, nbytes


def window(coll: Any, lower: str, upper: str, pid: int, seconds: float,
           tag: str) -> dict[str, Any]:
    scan(coll, lower, upper)
    handle = ab.start_perf(pid, seconds, tag)
    time.sleep(0.20)
    ops = 0
    res = (0, 0)
    cpu0 = ab.proc_cpu_us(pid)
    t0 = time.perf_counter()
    while True:
        elapsed = time.perf_counter() - t0
        if ops and elapsed + (elapsed / ops) > seconds:
            break
        if elapsed >= seconds:
            break
        res = scan(coll, lower, upper)
        ops += 1
    cpu = ab.proc_cpu_us(pid) - cpu0
    instr = ab.read_perf(handle)
    return {"ops": ops, "rows": res[0], "bytes": res[1],
            "cpu_us": cpu / max(ops, 1),
            "instructions": (instr / ops) if (instr is not None and ops) else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--many-rows", type=int, default=20000)
    ap.add_argument("--few-rows", type=int, default=2000)
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--warmup-blocks", type=int, default=2)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--real-rows", type=int, default=11686,
                    help="row count of the real P50 subtree, for scaling the answer")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    client = MongoClient(f"mongodb://localhost:{args.port}/?directConnection=true")
    client.admin.command({"setParameter": 1,
                          "internalQueryEnableFusedCoveredProjection": True})
    pid = ab.mongod_pid(args.port)
    db = client[DB]

    arms = (("many", args.many_rows), ("few", args.few_rows))
    built = {}
    for name, rows in arms:
        r, total = build(client, name, rows)
        built[name] = (r, total)
        log(f"{name}: {r} rows, {total:,} payload bytes ({total//r} B/row)")
    if abs(built["many"][1] - built["few"][1]) > built["many"][1] * 0.01:
        raise SystemExit("payload byte totals differ by more than 1%; probe invalid")

    lower, upper = PREFIX + "/", PREFIX + "0"
    for name, _ in arms:
        wp = (db[name].find({"path": {"$gte": lower, "$lt": upper}},
                            {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
              .sort([("path", 1), ("node_id", 1)]).hint("cover")).explain()
        if not ab.ixscan_fused(wp.get("queryPlanner", {}).get("winningPlan", {})):
            raise SystemExit(f"{name} did not take the fused path; probe invalid")

    per: dict[str, list[dict[str, Any]]] = {a[0]: [] for a in arms}
    for b in range(args.blocks + args.warmup_blocks):
        order = list(arms) if b % 2 == 0 else list(reversed(arms))
        warm = b < args.warmup_blocks
        for name, _ in order:
            m = window(db[name], lower, upper, pid, args.seconds, f"{b}{name}")
            if not warm:
                per[name].append(m)
        if not warm:
            log(f"  block {b}: many {per['many'][-1]['instructions']/1e6:.1f} Minstr, "
                f"few {per['few'][-1]['instructions']/1e6:.1f} Minstr")
        else:
            log(f"  block {b}: warmup, discarded")

    out: dict[str, Any] = {"many_rows": built["many"][0], "few_rows": built["few"][0],
                           "payload_bytes": built["many"][1], "per_block": [], "metrics": {}}
    for metric in ("instructions", "cpu_us"):
        fixed = []
        for i in range(len(per["many"])):
            m, f = per["many"][i][metric], per["few"][i][metric]
            if not m or not f:
                continue
            # cost = rows*fixed + bytes*variable, bytes equal, so:
            #   m - f = (many_rows - few_rows) * fixed
            fixed.append((m - f) / (built["many"][0] - built["few"][0]))
        if fixed:
            med = statistics.median(fixed)
            out["metrics"][metric] = {
                "per_row_fixed": med, "min": min(fixed), "max": max(fixed),
                "blocks": len(fixed),
                "share_of_real_op": None}
            unit = "instructions" if metric == "instructions" else "us"
            log(f"per-row fixed cost, {metric}: {med:,.1f} {unit}/row "
                f"[{min(fixed):,.1f}, {max(fixed):,.1f}] over {len(fixed)} blocks")
            log(f"  -> on the real {args.real_rows}-row subtree that is "
                f"{med*args.real_rows:,.0f} {unit}")

    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2))
        log(f"wrote {args.out}")
    client.drop_database(DB)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
