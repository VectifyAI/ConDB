#!/usr/bin/env python3
"""Subset-Pattern optimization experiment for get_subtree on MongoDB.

The read benchmark (bench_databases.py) found get_subtree's P95 on the 10M-node
tree runs to seconds on MongoDB because a non-covering path-range scan FETCHes the
full BSON of every matched node (the large `text` payload included) just to project
out fields the tree view needs. This measures whether the documented fix actually
works on this exact dataset/host:

  baseline   one collection, text inline (the current schema)
  struct     text split into a separate collection (Subset Pattern); the hot
             collection holds only structure+summary, so the range scan
             materializes small documents
  lean-cover a {path, node_id} covering index on the struct collection, to test
             whether the id-only subtree query runs index-only (IXSCAN, no FETCH,
             totalDocsExamined 0) -- the "lean covering index" the report flags

Single-client throughout (no concurrency: the box is shared). One collection is
resident at a time to cap disk use, and every collection is dropped on exit.
Results are directly comparable to Table 4 / Figure 1 of the report because the
subtree paths are sampled with the same seed and depth<=3 rule as flatten().
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time

from bench_databases import flatten, human, time_calls

DB = "bench"
PROJ_ID = {"node_id": 1, "_id": 0}
PROJ_VIEW = {"node_id": 1, "title": 1, "summary": 1, "_id": 0}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def free_gb(path="/"):
    return shutil.disk_usage(path).free / 1e9


def disk_guard(min_free_gb):
    f = free_gb()
    if f < min_free_gb:
        raise SystemExit(f"ABORT: free disk {f:.1f} GB < {min_free_gb} GB floor; "
                         f"refusing to risk other sessions.")
    return f


def ingest(col, rows, drop_text=False, batch=10000):
    t0 = time.time()
    buf = []
    n = 0
    for r in rows:
        d = {k: v for k, v in r.items() if k != "text"} if drop_text else dict(r)
        buf.append(d)
        if len(buf) >= batch:
            col.insert_many(buf, ordered=False)
            n += len(buf)
            buf = []
            if n % 1_000_000 == 0:
                log(f"      ... {n:,} docs")
    if buf:
        col.insert_many(buf, ordered=False)
        n += len(buf)
    return time.time() - t0, n


def coll_storage(db, name):
    st = db.command("collStats", name)
    return {
        "data_gb": round(st.get("storageSize", 0) / 1e9, 3),
        "index_gb": round(st.get("totalIndexSize", 0) / 1e9, 3),
        "logical_gb": round(st.get("size", 0) / 1e9, 3),
        "count": st.get("count", 0),
    }


def measure_subtree(col, paths, projection, label):
    def q(path):
        return len(list(col.find(
            {"path": {"$gte": path + "/", "$lt": path + "0"}}, projection)))
    log(f"      measuring {label} over {len(paths)} subtree paths...")
    return time_calls(q, paths)


def explain_subtree(col, path, projection):
    exp = col.find({"path": {"$gte": path + "/", "$lt": path + "0"}},
                   projection).explain()
    win = exp.get("executionStats", {})
    stage = exp.get("queryPlanner", {}).get("winningPlan", {})

    def stage_names(s, acc):
        if not isinstance(s, dict):
            return
        if "stage" in s:
            acc.append(s["stage"])
        for k in ("inputStage", "innerStage", "outerStage"):
            if k in s:
                stage_names(s[k], acc)
        for c in s.get("inputStages", []) or []:
            stage_names(c, acc)
    names = []
    stage_names(stage, names)
    return {
        "stages": names,
        "covered": "FETCH" not in names,
        "totalDocsExamined": win.get("totalDocsExamined"),
        "totalKeysExamined": win.get("totalKeysExamined"),
        "nReturned": win.get("nReturned"),
        "execTimeMillis": win.get("executionTimeMillis"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="data/large.json")
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--out", default="runs/subset_opt_large.json")
    ap.add_argument("--min-free-gb", type=float, default=30.0,
                    help="abort if free disk drops below this (protect other sessions)")
    ap.add_argument("--keep", action="store_true", help="do not drop collections at end")
    args = ap.parse_args()

    from pymongo import ASCENDING, MongoClient

    log(f"free disk at start: {free_gb():.1f} GB (floor {args.min_free_gb} GB)")
    disk_guard(args.min_free_gb)

    log(f"loading {args.doc} ...")
    t0 = time.time()
    doc = json.load(open(args.doc))
    recs = flatten(doc, tree_id="base", seed=7)
    del doc
    rows = recs.rows
    paths = recs.subtree_paths
    log(f"flattened {len(rows):,} nodes in {time.time() - t0:.0f}s; "
        f"{len(paths)} subtree paths (depth<=3)")

    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5000)
    db = client[DB]
    BASE, STRUCT = "opt_baseline", "opt_struct"
    results = {"doc": args.doc, "nodes": len(rows), "subtree_paths": len(paths),
               "phases": {}}

    def drop(*names):
        for nm in names:
            db.drop_collection(nm)

    try:
        # ---- Phase 1: baseline (text inline) ---------------------------------
        log("\n=== baseline: text inline ===")
        disk_guard(args.min_free_gb)
        drop(BASE)
        col = db[BASE]
        ing_s, n = ingest(col, rows, drop_text=False)
        col.create_index([("path", ASCENDING)])
        store = coll_storage(db, BASE)
        log(f"  ingested {n:,} in {ing_s:.0f}s; {human(store['data_gb']*1e9)} data, "
            f"{human(store['index_gb']*1e9)} index; free disk {free_gb():.1f} GB")
        results["phases"]["baseline"] = {
            "storage": store, "ingest_s": round(ing_s, 1),
            "subtree_id": measure_subtree(col, paths, PROJ_ID, "baseline id-only"),
            "subtree_view": measure_subtree(col, paths, PROJ_VIEW, "baseline id+title+summary"),
        }
        drop(BASE)  # free disk before building the next collection
        log(f"  dropped baseline; free disk {free_gb():.1f} GB")

        # ---- Phase 2: Subset Pattern (text split out) ------------------------
        log("\n=== struct: Subset Pattern (text removed) ===")
        disk_guard(args.min_free_gb)
        drop(STRUCT)
        col = db[STRUCT]
        ing_s, n = ingest(col, rows, drop_text=True)
        col.create_index([("path", ASCENDING)])
        store = coll_storage(db, STRUCT)
        log(f"  ingested {n:,} in {ing_s:.0f}s; {human(store['data_gb']*1e9)} data, "
            f"{human(store['index_gb']*1e9)} index; free disk {free_gb():.1f} GB")
        results["phases"]["struct"] = {
            "storage": store, "ingest_s": round(ing_s, 1),
            "subtree_id": measure_subtree(col, paths, PROJ_ID, "struct id-only"),
            "subtree_view": measure_subtree(col, paths, PROJ_VIEW, "struct id+title+summary"),
        }

        # ---- Phase 3: lean covering index {path, node_id} --------------------
        log("\n=== lean covering index {path, node_id} on struct ===")
        col.create_index([("path", ASCENDING), ("node_id", ASCENDING)])
        store_cov = coll_storage(db, STRUCT)
        sample_path = max(paths, key=lambda p: p.count("/") * -1) if paths else None
        results["phases"]["lean_covering"] = {
            "storage_with_cover_index": store_cov,
            "explain_id_only": explain_subtree(col, paths[0], PROJ_ID) if paths else {},
            "subtree_id_covered": measure_subtree(col, paths, PROJ_ID, "lean-cover id-only"),
        }
    finally:
        if not args.keep:
            drop(BASE, STRUCT)
            log(f"\ncleaned up collections; free disk {free_gb():.1f} GB")
        client.close()

    # ---- summary ------------------------------------------------------------
    def row(tag, st):
        return (f"  {tag:28s} p50={st['p50_ms']:8.2f}  p95={st['p95_ms']:9.2f}  "
                f"p99={st['p99_ms']:9.2f}  mean={st['mean_ms']:8.2f}  rows~{st['avg_rows']:.0f}")

    b, s, c = results["phases"]["baseline"], results["phases"]["struct"], results["phases"]["lean_covering"]
    print("\n================ get_subtree P50/P95 (ms), 10M tree, single client ================")
    print(row("baseline  id-only", b["subtree_id"]))
    print(row("baseline  id+title+summary", b["subtree_view"]))
    print(row("subset    id-only", s["subtree_id"]))
    print(row("subset    id+title+summary", s["subtree_view"]))
    print(row("lean-cover id-only", c["subtree_id_covered"]))
    bp95, sp95 = b["subtree_view"]["p95_ms"], s["subtree_view"]["p95_ms"]
    if sp95:
        print(f"\n  view P95: baseline {bp95:.0f} ms -> subset {sp95:.0f} ms  "
              f"({bp95/sp95:.1f}x faster)")
    ex = c["explain_id_only"]
    print(f"  lean covering explain: stages={ex.get('stages')} covered={ex.get('covered')} "
          f"docsExamined={ex.get('totalDocsExamined')} keysExamined={ex.get('totalKeysExamined')}")
    print(f"  storage: baseline {b['storage']['data_gb']}GB+{b['storage']['index_gb']}GB idx | "
          f"struct {s['storage']['data_gb']}GB+{s['storage']['index_gb']}GB idx")

    from pathlib import Path
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2))
    log(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
