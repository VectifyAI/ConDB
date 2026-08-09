#!/usr/bin/env python3
"""Reproduce the original get_subtree multi-second tail by measuring on a COLD cache.

All prior re-measurements were warm (collection freshly written and read, or
warmed up), and got ~0.4s P95. The original benchmark recorded ~2.5s. The leading
remaining hypothesis: the original measured get_subtree on a COLD WiredTiger cache,
so each of the ~36k matched nodes was a random DISK read, not a cache hit.

This ingests the 10M tree (text inline, matching the original schema), restarts
mongod to empty the WiredTiger cache, then measures get_subtree on the FIRST touch
(no warmup) versus warm. If cold P95 reaches seconds, the 2.5s is a cold-cache /
disk-read artifact, not a steady-state cost.

One client throughout. The collection is dropped at the end.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time

from bench_databases import flatten, time_calls

DB, COL = "bench", "cold_baseline"
URI = "mongodb://localhost:57017"
PROJ_VIEW = {"node_id": 1, "title": 1, "summary": 1, "_id": 0}


def log(m):
    print(m, file=sys.stderr, flush=True)


def free_gb():
    return shutil.disk_usage("/").free / 1e9


def connect(retries=40, delay=2.0):
    from pymongo import MongoClient
    last = None
    for i in range(retries):
        try:
            c = MongoClient(URI, serverSelectionTimeoutMS=2000)
            c.admin.command("ping")
            return c
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(delay)
    raise SystemExit(f"could not connect after restart: {last!r}")


def subtree_fn(col):
    def q(path):
        return len(list(col.find(
            {"path": {"$gte": path + "/", "$lt": path + "0"}}, PROJ_VIEW)))
    return q


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="data/large.json")
    ap.add_argument("--out", default="runs/subset_coldcache_large.json")
    ap.add_argument("--container", default="condb_mongo")
    ap.add_argument("--min-free-gb", type=float, default=30.0)
    args = ap.parse_args()

    from pymongo import ASCENDING

    if free_gb() < args.min_free_gb:
        raise SystemExit(f"ABORT: free disk {free_gb():.1f}GB < {args.min_free_gb}GB")
    log(f"free disk {free_gb():.1f}GB; loading {args.doc} ...")
    t0 = time.time()
    doc = json.load(open(args.doc))
    recs = flatten(doc, tree_id="base", seed=7)
    del doc
    rows, paths = recs.rows, recs.subtree_paths
    log(f"flattened {len(rows):,} nodes in {time.time()-t0:.0f}s; {len(paths)} paths")

    out = {"doc": args.doc, "nodes": len(rows), "paths": len(paths)}

    # --- ingest (text inline), then drop the in-RAM rows ---------------------
    c = connect()
    db = c[DB]
    db.drop_collection(COL)
    col = db[COL]
    t0 = time.time()
    buf, n = [], 0
    for r in rows:
        buf.append(dict(r))
        if len(buf) >= 10000:
            col.insert_many(buf, ordered=False); n += len(buf); buf = []
            if n % 2_000_000 == 0:
                log(f"      ... {n:,}")
    if buf:
        col.insert_many(buf, ordered=False)
    col.create_index([("path", ASCENDING)])
    out["ingest_s"] = round(time.time() - t0, 1)
    log(f"  ingested + indexed in {out['ingest_s']:.0f}s; free {free_gb():.1f}GB")
    c.close()
    del rows  # free RAM so it cannot mask a cold disk read

    # --- restart mongod to empty the WiredTiger cache ------------------------
    log(f"  restarting container {args.container} to clear WiredTiger cache ...")
    t0 = time.time()
    subprocess.run(["docker", "restart", args.container], check=True,
                   capture_output=True, timeout=120)
    log(f"  restart issued in {time.time()-t0:.0f}s; reconnecting ...")
    c = connect()
    db = c[DB]
    col = db[COL]
    # confirm cache is cold
    wt = c.admin.command("serverStatus").get("wiredTiger", {}).get("cache", {})
    out["cache_in_bytes_after_restart_gb"] = round(wt.get("bytes currently in the cache", 0) / 1e9, 3)
    log(f"  WT cache after restart: {out['cache_in_bytes_after_restart_gb']} GB (should be tiny)")

    try:
        q = subtree_fn(col)
        log("  measuring COLD (first touch, no warmup) ...")
        out["cold"] = time_calls(q, paths, warmup=0)
        log("  measuring WARM (same paths again) ...")
        out["warm"] = time_calls(q, paths, warmup=3)
    finally:
        db.drop_collection(COL)
        log(f"  dropped {COL}; free {free_gb():.1f}GB")
        c.close()

    from pathlib import Path
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2))

    cold, warm = out["cold"], out["warm"]
    print("\n=========== get_subtree COLD vs WARM cache, 10M tree, single client ===========")
    print(f"  {'state':6s} | {'P50':>9s} | {'P95':>10s} | {'P99':>10s} | {'mean':>9s} | {'max':>9s}")
    for tag, st in (("cold", cold), ("warm", warm)):
        print(f"  {tag:6s} | {st['p50_ms']:9.1f} | {st['p95_ms']:10.1f} | "
              f"{st['p99_ms']:10.1f} | {st['mean_ms']:9.1f} |")
    if warm["p95_ms"]:
        print(f"\n  cold P95 is {cold['p95_ms']/warm['p95_ms']:.1f}x the warm P95 "
              f"({cold['p95_ms']:.0f} vs {warm['p95_ms']:.0f} ms); original run recorded 2478 ms")
    log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
