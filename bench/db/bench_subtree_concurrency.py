#!/usr/bin/env python3
"""Does the exhaust+batch pipelining win survive concurrency?

An exhaust cursor holds its connection, and its mongod worker thread, for the
whole cursor rather than only for each getMore.  That is the one way this
change could be a regression: it could cost throughput when many clients run
get_subtree at once.  This measures throughput and latency at several client
counts, baseline against pipelined, in interleaved blocks.

Worker processes, not threads, so that the client-side BSON decode is not
serialized by the GIL and the measurement reflects the server.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import statistics
import time
from pathlib import Path
from typing import Any

import pymongo
from pymongo import CursorType

URI = "mongodb://localhost:57017"
NODES = "layout2_view"
COVER_INDEX = "layout2_rootcause_exact_cover"
NODE_INDEX = "allops_tree_node"
TREE_ID = "base"
PROJECTION = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}
COHORT = "bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json"


def log(m: str) -> None:
    print(m, flush=True)


def cgroup_cpu_us() -> int:
    cid = os.popen("docker inspect -f '{{.Id}}' condb_mongo").read().strip()
    p = Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat")
    for line in p.read_text().splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    return 0


def worker(paths: list[str], pipelined: bool, batch: int, seconds: float,
           start_at: float, offset: int, q) -> None:
    client = pymongo.MongoClient(URI)
    nodes = client["bench"][NODES]
    sort_spec = [("path", 1), ("node_id", 1)]
    lat: list[float] = []
    rows_total = 0
    while time.time() < start_at:
        time.sleep(0.001)
    deadline = start_at + seconds
    i = offset
    while time.time() < deadline:
        p = paths[i % len(paths)]
        i += 1
        lower, upper = p + "/", p + "0"
        t0 = time.perf_counter()
        kw = {"cursor_type": CursorType.EXHAUST} if pipelined else {}
        cur = (nodes.find({"path": {"$gte": lower, "$lt": upper}}, PROJECTION, **kw)
               .sort(sort_spec).hint(COVER_INDEX))
        if pipelined:
            cur = cur.batch_size(batch)
        n = 0
        for _ in cur:
            n += 1
        lat.append((time.perf_counter() - t0) * 1e3)
        rows_total += n
    q.put((lat, rows_total))


def run_arm(paths, pipelined, batch, clients, seconds):
    q = mp.Queue()
    start_at = time.time() + 1.0
    procs = [mp.Process(target=worker,
                        args=(paths, pipelined, batch, seconds, start_at, k, q))
             for k in range(clients)]
    for p in procs:
        p.start()
    while time.time() < start_at:
        time.sleep(0.01)
    c0 = cgroup_cpu_us()
    t0 = time.perf_counter()
    lats: list[float] = []
    rows = 0
    for _ in procs:
        l, r = q.get()
        lats.extend(l)
        rows += r
    elapsed = time.perf_counter() - t0
    cpu = cgroup_cpu_us() - c0
    for p in procs:
        p.join()
    lats.sort()
    return {"ops": len(lats), "rows": rows, "elapsed_s": round(elapsed, 3),
            "ops_per_s": round(len(lats) / elapsed, 2),
            "rows_per_s": round(rows / elapsed, 0),
            "p50_ms": round(lats[len(lats) // 2], 2),
            "p95_ms": round(lats[int(0.95 * (len(lats) - 1))], 2),
            "mongod_cpu_cores": round(cpu / 1e6 / elapsed, 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", default="1,4,16,32")
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--blocks", type=int, default=3)
    ap.add_argument("--min-rows", type=int, default=5000)
    ap.add_argument("--max-rows", type=int, default=30000)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    samples = json.loads(Path(COHORT).read_text())["samples"][:200]
    pool = [s["path"] for s in samples
            if args.min_rows <= s["rows"] <= args.max_rows]
    sizes = [s["rows"] for s in samples if args.min_rows <= s["rows"] <= args.max_rows]
    log(f"{len(pool)} subtrees, rows {min(sizes)}..{max(sizes)} "
        f"median {statistics.median(sizes)}")

    out: dict[str, Any] = {"pool_size": len(pool), "seconds": args.seconds,
                           "batch": args.batch, "blocks": args.blocks,
                           "row_window": [args.min_rows, args.max_rows],
                           "levels": {}}
    for clients in [int(x) for x in args.clients.split(",")]:
        res = {"baseline": [], "pipelined": []}
        for b in range(args.blocks):
            order = ["baseline", "pipelined"] if b % 2 == 0 else ["pipelined", "baseline"]
            for arm in order:
                r = run_arm(pool, arm == "pipelined", args.batch, clients, args.seconds)
                res[arm].append(r)
                log(f"  clients={clients:3d} block={b} {arm:9s} "
                    f"{r['ops_per_s']:8.2f} ops/s  p50 {r['p50_ms']:8.2f} ms  "
                    f"p95 {r['p95_ms']:8.2f} ms  mongod {r['mongod_cpu_cores']:5.2f} cores")
        paired = [round(100 * (res["pipelined"][i]["ops_per_s"] -
                               res["baseline"][i]["ops_per_s"]) /
                        res["baseline"][i]["ops_per_s"], 2)
                  for i in range(args.blocks)]
        out["levels"][str(clients)] = {"baseline": res["baseline"],
                                       "pipelined": res["pipelined"],
                                       "paired_throughput_pct": paired}
        log(f"  clients={clients:3d} throughput change, paired per block: "
            f"median {statistics.median(paired):+6.2f}%  {paired}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=1))
        log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
