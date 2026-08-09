#!/usr/bin/env python3
"""Does get_subtree's 2.5s tail reproduce under CONCURRENT load?

Runs 1-2 (bench_subset_opt*.py) found get_subtree P95 ~250-400ms in isolation on
the 10M tree, not the report's 2478ms. The WiredTiger cache is 540GB vs a 5.8GB
collection (0 app-thread evictions), so eviction is ruled out -- the only
remaining explanation for the report's tail is CPU contention from the concurrent
multi-engine / concurrency benchmarks that ran alongside the original measurement.

This measures get_subtree P50/P95 on the baseline (text-inline) collection while N
background processes hammer indexed point-lookups against the same mongod, sweeping
N in {0,4,8,15}. The foreground measurement client keeps total concurrent clients
at or below 16. If P95 climbs toward seconds as N rises, the 2.5s is contention,
not a steady-state structural cost.

Single mongod, all processes nice'd; one collection; disk-guarded; dropped on exit.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import random
import shutil
import sys
import time
from pathlib import Path

from bench_databases import flatten, time_calls

DB, COL = "bench", "ctn_baseline"
URI = "mongodb://localhost:57017"
MAX_CONCURRENT_CLIENTS = 16
FOREGROUND_MEASUREMENT_CLIENTS = 1
MAX_BACKGROUND_WORKERS = (
    MAX_CONCURRENT_CLIENTS - FOREGROUND_MEASUREMENT_CLIENTS
)


def log(m):
    print(m, file=sys.stderr, flush=True)


def free_gb():
    return shutil.disk_usage("/").free / 1e9


def load_worker(uri, ids, stop):
    from pymongo import MongoClient
    c = MongoClient(uri)
    col = c[DB][COL]
    rng = random.Random(os.getpid())
    while not stop.is_set():
        nid = ids[rng.randrange(len(ids))]
        list(col.find({"tree_id": "base", "node_id": nid}, {"title": 1, "summary": 1}).limit(1))
    c.close()


def wt_stats(client):
    wt = client.admin.command("serverStatus").get("wiredTiger", {}).get("cache", {})
    return {
        "in_cache_gb": round(wt.get("bytes currently in the cache", 0) / 1e9, 2),
        "app_evictions": wt.get("pages evicted by application threads", 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="data/large.json")
    ap.add_argument("--out", default="runs/subset_contention_large.json")
    ap.add_argument(
        "--levels",
        nargs="+",
        type=int,
        default=[0, 4, 8, MAX_BACKGROUND_WORKERS],
        help=(
            f"background worker processes (maximum {MAX_BACKGROUND_WORKERS}; "
            f"plus {FOREGROUND_MEASUREMENT_CLIENTS} measurement client keeps "
            f"the total at {MAX_CONCURRENT_CLIENTS})"
        ),
    )
    ap.add_argument("--paths", type=int, default=200, help="subtree queries measured per level")
    ap.add_argument("--ramp", type=float, default=3.0, help="seconds to let load ramp before measuring")
    ap.add_argument("--min-free-gb", type=float, default=30.0)
    args = ap.parse_args()
    if not args.levels or any(
        level < 0 or level > MAX_BACKGROUND_WORKERS
        for level in args.levels
    ):
        raise SystemExit(
            f"contention levels must be between 0 and "
            f"{MAX_BACKGROUND_WORKERS}"
        )

    from pymongo import ASCENDING, MongoClient

    if free_gb() < args.min_free_gb:
        raise SystemExit(f"ABORT: free disk {free_gb():.1f}GB < {args.min_free_gb}GB")
    log(f"free disk {free_gb():.1f}GB; loading {args.doc} ...")
    t0 = time.time()
    doc = json.load(open(args.doc))
    recs = flatten(doc, tree_id="base", seed=7)
    del doc
    rows = recs.rows
    paths = recs.subtree_paths[:args.paths]
    point_ids = recs.point_ids
    log(f"flattened {len(rows):,} nodes in {time.time()-t0:.0f}s; "
        f"{len(paths)} subtree paths, {len(point_ids)} load ids")

    client = MongoClient(URI, serverSelectionTimeoutMS=5000)
    db = client[DB]
    out = {"doc": args.doc, "nodes": len(rows), "paths": len(paths),
           "levels": args.levels, "cores": os.cpu_count(), "results": []}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    # Clean-stop machinery: write our PID so a controller can `kill -TERM` us by
    # PID (never by name-pattern, which self-matched and botched the last stop).
    # SIGTERM -> sys.exit -> the finally block drops the collection, and
    # interpreter shutdown terminates the daemon load-workers automatically.
    import signal
    Path("runs/contention.pid").write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    def save():
        Path(args.out).write_text(json.dumps(out, indent=2))

    def subtree(path):
        return len(list(db[COL].find(
            {"path": {"$gte": path + "/", "$lt": path + "0"}}, {"node_id": 1, "_id": 0})))

    try:
        db.drop_collection(COL)
        col = db[COL]
        t0 = time.time()
        buf = []
        for r in rows:
            buf.append(dict(r))
            if len(buf) >= 10000:
                col.insert_many(buf, ordered=False); buf = []
        if buf:
            col.insert_many(buf, ordered=False)
        col.create_index([("path", ASCENDING)])
        col.create_index([("tree_id", ASCENDING), ("node_id", ASCENDING)])
        out["ingest_s"] = round(time.time() - t0, 1); save()
        log(f"  ingested + indexed in {out['ingest_s']:.0f}s; free {free_gb():.1f}GB")

        for c in args.levels:
            stop = mp.Event()
            workers = [mp.Process(target=load_worker, args=(URI, point_ids, stop), daemon=True)
                       for _ in range(c)]
            for w in workers:
                w.start()
            if c:
                time.sleep(args.ramp)  # let load ramp
            la1 = os.getloadavg()[0]
            wt0 = wt_stats(client)
            log(f"\n=== concurrency={c} (loadavg~{la1:.0f}) measuring {len(paths)} get_subtree ...")
            t0 = time.time()
            st = time_calls(subtree, paths)
            dur = time.time() - t0
            wt1 = wt_stats(client)
            stop.set()
            for w in workers:
                w.join(timeout=10)
                if w.is_alive():
                    w.terminate()
            rec = {"concurrency": c, "loadavg_at_start": round(la1, 1),
                   "measure_seconds": round(dur, 1),
                   "p50_ms": st["p50_ms"], "p95_ms": st["p95_ms"],
                   "p99_ms": st["p99_ms"], "mean_ms": st["mean_ms"],
                   "avg_rows": st["avg_rows"],
                   "wt_in_cache_gb": wt1["in_cache_gb"],
                   "wt_app_evictions_delta": wt1["app_evictions"] - wt0["app_evictions"]}
            out["results"].append(rec); save()
            log(f"    c={c}: p50={rec['p50_ms']:.1f} p95={rec['p95_ms']:.1f} "
                f"p99={rec['p99_ms']:.1f} mean={rec['mean_ms']:.1f}ms | "
                f"WT cache {rec['wt_in_cache_gb']}GB, app-evict +{rec['wt_app_evictions_delta']}")
            if free_gb() < args.min_free_gb:
                raise SystemExit(f"ABORT mid-sweep: free disk {free_gb():.1f}GB")
    finally:
        db.drop_collection(COL)
        log(f"\ncleaned up; free {free_gb():.1f}GB")
        client.close()
        save()

    print("\n========= get_subtree under concurrent point-lookup load, 10M tree =========")
    print(f"  {'concurrency':>11s} | {'p50':>8s} | {'p95':>9s} | {'p99':>9s} | {'mean':>8s} | WT-evict")
    for r in out["results"]:
        print(f"  {r['concurrency']:>11d} | {r['p50_ms']:8.1f} | {r['p95_ms']:9.1f} | "
              f"{r['p99_ms']:9.1f} | {r['mean_ms']:8.1f} | +{r['wt_app_evictions_delta']}")
    base = out["results"][0]["p95_ms"] if out["results"] else 0
    top = out["results"][-1]["p95_ms"] if out["results"] else 0
    if base:
        print(f"\n  P95 grew {top/base:.1f}x from c={out['results'][0]['concurrency']} "
              f"to c={out['results'][-1]['concurrency']} ({base:.0f} -> {top:.0f} ms); "
              f"report's isolated->loaded claim was 2478 ms")
    log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
