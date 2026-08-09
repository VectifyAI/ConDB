#!/usr/bin/env python3
"""Does cursor pipelining compose with the report's bucketed layout?

Arms, all returning the same ordered (node_id,title,summary) list:

  scan            the report's covered path scan
  scan_pipe       same, exhaust cursor + explicit batch size
  bucket<N>       the report's bucket read against subtree_buckets_v2_<N>
  bucket<N>_pipe  same, exhaust cursor + explicit batch size

Paired and interleaved; outputs compared element-wise before timing.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
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
BUCKET_PROJECTION = {"_id": 0, "seq": 1, "count": 1, "paths": 1,
                     "node_ids": 1, "titles": 1, "summaries": 1}
COHORT = "bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json"


def log(m: str) -> None:
    print(m, flush=True)


def fingerprint(rows) -> str:
    d = hashlib.sha256()
    for r in rows:
        for v in r:
            e = str(v).encode()
            d.update(len(e).to_bytes(4, "big"))
            d.update(e)
    return d.hexdigest()


def cgroup_cpu_us() -> int:
    cid = os.popen("docker inspect -f '{{.Id}}' condb_mongo").read().strip()
    p = Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat")
    for line in p.read_text().splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=11686)
    ap.add_argument("--scan-batch", type=int, default=1000)
    ap.add_argument("--bucket-sizes", default="256,2048,8192")
    ap.add_argument("--bucket-batch", type=int, default=8)
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--iters", type=int, default=2)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    s = min(json.loads(Path(COHORT).read_text())["samples"][:200],
            key=lambda x: abs(x["rows"] - args.rows))
    path, lower, upper = s["path"], s["path"] + "/", s["path"] + "0"
    node_id = path.rsplit("/", 1)[-1]
    log(f"input path={path} rows={s['rows']}")

    client = pymongo.MongoClient(URI)
    db = client["bench"]
    nodes = db[NODES]
    flt = {"path": {"$gte": lower, "$lt": upper}}
    sort_spec = [("path", 1), ("node_id", 1)]

    def scan(pipe: bool):
        def run():
            nodes.find_one({"tree_id": TREE_ID, "node_id": node_id},
                           {"_id": 0, "path": 1}, hint=NODE_INDEX)
            kw = {"cursor_type": CursorType.EXHAUST} if pipe else {}
            cur = nodes.find(flt, PROJECTION, **kw).sort(sort_spec).hint(COVER_INDEX)
            if pipe:
                cur = cur.batch_size(args.scan_batch)
            return [(r["node_id"], r["title"], r["summary"]) for r in cur]
        return run

    def bucket(size: int, pipe: bool):
        coll = db[f"subtree_buckets_v2_{size}"]

        def run():
            nodes.find_one({"tree_id": TREE_ID, "node_id": node_id},
                           {"_id": 0, "path": 1}, hint=NODE_INDEX)
            first = coll.find_one(
                {"kind": "bucket", "tree_id": TREE_ID, "last_path": {"$gte": lower}},
                {"_id": 0, "seq": 1}, sort=[("last_path", 1), ("seq", 1)],
                hint="bucket_last_path")
            last = coll.find_one(
                {"kind": "bucket", "tree_id": TREE_ID, "first_path": {"$lt": upper}},
                {"_id": 0, "seq": 1}, sort=[("first_path", -1), ("seq", -1)],
                hint="bucket_first_path")
            if first is None or last is None or first["seq"] > last["seq"]:
                return []
            kw = {"cursor_type": CursorType.EXHAUST} if pipe else {}
            cur = coll.find(
                {"kind": "bucket", "tree_id": TREE_ID,
                 "seq": {"$gte": first["seq"], "$lte": last["seq"]}},
                BUCKET_PROJECTION, **kw).sort("seq", 1).hint("bucket_sequence")
            if pipe:
                cur = cur.batch_size(args.bucket_batch)
            rows = []
            for d in cur:
                for p, n, t, su in zip(d["paths"], d["node_ids"],
                                       d["titles"], d["summaries"]):
                    if lower <= p < upper:
                        rows.append((n, t, su))
            return rows
        return run

    arms: dict[str, Any] = {"scan": scan(False), "scan_pipe": scan(True)}
    for sz in [int(x) for x in args.bucket_sizes.split(",")]:
        arms[f"bucket{sz}"] = bucket(sz, False)
        arms[f"bucket{sz}_pipe"] = bucket(sz, True)

    fps = {}
    for n, fn in arms.items():
        r = fn()
        fps[n] = (len(r), fingerprint(r))
        log(f"  {n:16s} rows={len(r)} fp={fps[n][1][:16]}")
    assert len(set(fps.values())) == 1, f"arms disagree: {fps}"
    log("  element-wise output identical across all arms")

    names = list(arms)
    wall = {n: [] for n in names}
    cpu = {n: [] for n in names}
    for b in range(args.blocks):
        order = names if b % 2 == 0 else list(reversed(names))
        for n in order:
            arms[n]()
            gc.collect()
            gc.disable()
            c0 = cgroup_cpu_us()
            t0 = time.perf_counter()
            for _ in range(args.iters):
                r = arms[n]()
                del r
            dt = (time.perf_counter() - t0) * 1e6 / args.iters
            c1 = cgroup_cpu_us()
            gc.enable()
            wall[n].append(dt)
            cpu[n].append((c1 - c0) / args.iters)
        log("  block %d: " % b + "  ".join(f"{n}={wall[n][-1]/1000:.2f}" for n in names))

    base = "scan"
    out = {"input": {"path": path, "rows": s["rows"]},
           "scan_batch": args.scan_batch, "bucket_batch": args.bucket_batch,
           "blocks": args.blocks, "iters": args.iters, "arms": {}}
    log("\nsummary (wall p50, and paired vs the report's plain scan):")
    for n in names:
        pp = [round(100 * (wall[n][i] - wall[base][i]) / wall[base][i], 2)
              for i in range(args.blocks)]
        out["arms"][n] = {"wall_us": [round(x, 1) for x in wall[n]],
                          "mongod_cgroup_cpu_us": [round(x, 1) for x in cpu[n]],
                          "wall_p50_us": round(statistics.median(wall[n]), 1),
                          "paired_pct_vs_scan": pp}
        log(f"  {n:16s} {statistics.median(wall[n])/1000:8.2f} ms   "
            f"speedup {statistics.median(wall[base])/statistics.median(wall[n]):5.2f}x   "
            f"paired median {statistics.median(pp):+7.2f}%  range [{min(pp):+.1f},{max(pp):+.1f}]")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=1))
        log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
