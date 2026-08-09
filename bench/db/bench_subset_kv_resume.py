#!/usr/bin/env python3
"""Resume the interrupted bench_subset_kv.py run.

The first run completed ingest and the phases through kv_in, then was stopped to
free the host; the four collections (kv_ref/kv_struct/kv_meta/kv_meta_clu) were
left in place so nothing needs re-ingesting. This runs only the remaining
phases -- kv_in_clu, kv_lookup, kv_lookup_clu -- and merges them into the same
results file. Collections are dropped at the end unless --keep.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from bench_databases import flatten
from bench_subset_kv import (DB, META, META_CLU, REF, STRUCT, log,
                             measure_kv_in, measure_kv_lookup)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="data/large.json")
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--out", default="runs/subset_kv_large.json")
    ap.add_argument("--in-chunk", type=int, default=1000)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    from pymongo import MongoClient

    out = json.loads(Path(args.out).read_text())
    log(f"resuming; phases already saved: {', '.join(out['phases'])}")

    log(f"loading {args.doc} ...")
    t0 = time.time()
    doc = json.load(open(args.doc))
    recs = flatten(doc, tree_id="base", seed=7)
    del doc
    paths = recs.subtree_paths
    n_rows = len(recs.rows)
    log(f"flattened {n_rows:,} nodes in {time.time()-t0:.0f}s; {len(paths)} paths")

    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5000)
    db = client[DB]
    for name in (REF, STRUCT, META, META_CLU):
        cnt = db[name].estimated_document_count()
        if cnt != n_rows:
            raise SystemExit(f"ABORT: {name} has {cnt:,} docs, expected {n_rows:,}; "
                             f"re-run bench_subset_kv.py from scratch instead")

    def save():
        Path(args.out).write_text(json.dumps(out, indent=2))

    def phase(name, fn):
        if name in out["phases"] and "error" not in out["phases"][name]:
            log(f"=== {name} === (already done, skipping)")
            return
        log(f"\n=== {name} ===")
        try:
            out["phases"][name] = fn()
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            out["phases"][name] = {"error": repr(e)}
        save()

    struct = db[STRUCT]
    try:
        phase("kv_in_clu", lambda: measure_kv_in(
            struct, db[META_CLU], paths, args.in_chunk,
            "kv view: covered ids + $in meta (clustered)"))
        phase("kv_lookup", lambda: measure_kv_lookup(
            struct, META, paths, "kv view: $lookup struct->meta"))
        phase("kv_lookup_clu", lambda: measure_kv_lookup(
            struct, META_CLU, paths, "kv view: $lookup struct->meta (clustered)"))
    finally:
        if not args.keep:
            for name in (REF, STRUCT, META, META_CLU):
                db.drop_collection(name)
            log("\ncleaned up collections")
        client.close()
        save()

    def line(tag, st):
        if not st or "p50_ms" not in st:
            return f"  {tag:34s} (n/a)"
        return (f"  {tag:34s} p50={st['p50_ms']:8.2f}  p95={st['p95_ms']:9.2f}  "
                f"p99={st['p99_ms']:9.2f}  mean={st['mean_ms']:8.2f}")

    ph = out["phases"]
    print("\n========== resumed phases ==========")
    for name in ("kv_in_clu", "kv_lookup", "kv_lookup_clu"):
        print(line(name, ph.get(name)))
    log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
