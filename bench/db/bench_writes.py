#!/usr/bin/env python3
"""Write-path benchmark for the same engines as bench_databases.py.

Measures what the read-only benchmark misses:
  1. single field-update latency (MongoDB $set vs PostgreSQL JSONB rewrite, etc.)
  2. write amplification: on-disk growth after many updates, before vacuum/compact
  3. incremental insert throughput with indexes live (vs the bulk "load then index"
     path measured in bench_databases.py)

Reuses the backend classes and the flatten() helper from bench_databases.py.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

from bench_databases import COLS, build_backend, flatten, human


def percentiles(lat_ms):
    lat_ms = sorted(lat_ms)

    def pct(p):
        if not lat_ms:
            return 0.0
        idx = min(len(lat_ms) - 1, int(round(p / 100 * (len(lat_ms) - 1))))
        return round(lat_ms[idx], 3)

    return {"p50_ms": pct(50), "p95_ms": pct(95), "p99_ms": pct(99),
            "mean_ms": round(statistics.mean(lat_ms), 3) if lat_ms else 0.0}


def do_update(be, node_id, new_summary):
    if be.name == "sqlite":
        be.conn.execute("UPDATE nodes SET summary=? WHERE tree_id=? AND node_id=?",
                        (new_summary, be.tree_id, node_id))
        be.conn.commit()
    elif be.name == "duckdb":
        be.conn.execute("UPDATE nodes SET summary=? WHERE tree_id=? AND node_id=?",
                        [new_summary, be.tree_id, node_id])
    elif be.name == "postgres":
        be.conn.execute(
            "UPDATE nodes SET doc = jsonb_set(doc, '{summary}', to_jsonb(%s::text)) "
            "WHERE tree_id=%s AND node_id=%s",
            (new_summary, be.tree_id, node_id))
    elif be.name == "mongo":
        be.col.update_one({"tree_id": be.tree_id, "node_id": node_id},
                          {"$set": {"summary": new_summary}})


def do_insert(be, row):
    if be.name == "sqlite":
        be.conn.execute(
            f"INSERT INTO nodes ({','.join(COLS)}) VALUES ({','.join(['?'] * len(COLS))})",
            tuple(row[c] for c in COLS))
        be.conn.commit()
    elif be.name == "duckdb":
        be.conn.execute(
            f"INSERT INTO nodes ({','.join(COLS)}) VALUES ({','.join(['?'] * len(COLS))})",
            [row[c] for c in COLS])
    elif be.name == "postgres":
        doc = {"title": row["title"], "summary": row["summary"],
               "start_index": row["start_index"], "end_index": row["end_index"],
               "text": row["text"]}
        be.conn.execute(
            "INSERT INTO nodes (tree_id,node_id,parent_id,depth,path,start_index,doc) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (row["tree_id"], row["node_id"], row["parent_id"], row["depth"],
             row["path"], row["start_index"], json.dumps(doc)))
    elif be.name == "mongo":
        be.col.insert_one(dict(row))


def run_backend(be, recs, n_updates, n_inserts, seed):
    rng = random.Random(seed)
    print(f"  [{be.name}] setup + ingest...", file=sys.stderr)
    be.tree_id = recs.rows[0]["tree_id"]
    be.setup()
    be.ingest(recs.rows)
    be.build_indexes()
    size_before = be.storage_bytes()["total"]

    # 1. update latency
    print(f"  [{be.name}] {n_updates} updates...", file=sys.stderr)
    ids = [recs.rows[rng.randrange(len(recs.rows))]["node_id"] for _ in range(n_updates)]
    for nid in ids[:3]:
        do_update(be, nid, "warmup")
    lat = []
    for i, nid in enumerate(ids):
        new = f"updated summary revision {i} " + recs.rows[0]["summary"]
        t0 = time.perf_counter()
        do_update(be, nid, new)
        lat.append((time.perf_counter() - t0) * 1000.0)
    upd = percentiles(lat)

    # 2. write amplification: on-disk growth from the updates (pre-reclaim)
    size_after_updates = be.storage_bytes()["total"]

    # 3. incremental insert with indexes live
    print(f"  [{be.name}] {n_inserts} incremental inserts...", file=sys.stderr)
    template = recs.rows[0]
    t0 = time.perf_counter()
    for i in range(n_inserts):
        row = dict(template)
        row["node_id"] = f"ins_{i:08d}"
        row["path"] = f"/ins/{i:08d}"
        do_insert(be, row)
    insert_s = time.perf_counter() - t0

    size_after_inserts = be.storage_bytes()["total"]
    be.teardown()

    return {
        "engine": be.name,
        "update_latency": upd,
        "update_throughput_ops_s": int(n_updates / (sum(lat) / 1000.0)) if lat else 0,
        "incremental_insert_ops_s": int(n_inserts / insert_s) if insert_s > 0 else 0,
        "storage_before": size_before,
        "bloat_after_updates_bytes": size_after_updates - size_before,
        "bloat_after_inserts_bytes": size_after_inserts - size_before,
    }


def main():
    ap = argparse.ArgumentParser(description="Write-path benchmark (update / bloat / incremental insert).")
    ap.add_argument("--doc", required=True)
    ap.add_argument("--engines", nargs="+", default=["sqlite", "duckdb", "postgres", "mongo"])
    ap.add_argument("--updates", type=int, default=5000)
    ap.add_argument("--inserts", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out")
    ap.add_argument("--pg-dsn", default="host=localhost port=55432 dbname=bench user=postgres password=bench")
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--sqlite-path", default="bench/db/runs/_sqlite_w.db")
    ap.add_argument("--duckdb-path", default="bench/db/runs/_duck_w.db")
    args = ap.parse_args()

    doc = json.load(open(args.doc))
    recs = flatten(doc, tree_id=Path(args.doc).stem)
    print(f"Loaded {len(recs.rows):,} nodes from {args.doc}", file=sys.stderr)

    results = {"doc": args.doc, "node_count": len(recs.rows),
               "updates": args.updates, "inserts": args.inserts, "engines": {}}
    for eng in args.engines:
        print(f"\n=== {eng} ===", file=sys.stderr)
        try:
            results["engines"][eng] = run_backend(build_backend(eng, args), recs,
                                                   args.updates, args.inserts, args.seed)
        except Exception as e:
            import traceback
            traceback.print_exc()
            results["engines"][eng] = {"engine": eng, "error": str(e)}

    # compact human-readable summary
    print("\nengine     | upd p50 | upd p95 | upd ops/s | incr-insert ops/s | bloat(updates) | bloat(inserts)")
    for eng, r in results["engines"].items():
        if "error" in r:
            print(f"{eng:10s} | ERROR: {r['error'][:50]}")
            continue
        u = r["update_latency"]
        print(f"{eng:10s} | {u['p50_ms']:7.3f} | {u['p95_ms']:7.3f} | "
              f"{r['update_throughput_ops_s']:9,} | {r['incremental_insert_ops_s']:17,} | "
              f"{human(r['bloat_after_updates_bytes']):>14s} | {human(r['bloat_after_inserts_bytes']):>14s}")

    out = json.dumps(results, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out)
        print(f"\nWrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
