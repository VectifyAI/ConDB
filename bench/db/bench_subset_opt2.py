#!/usr/bin/env python3
"""Follow-up validation for the Subset-Pattern experiment (bench_subset_opt.py).

Run 1 showed: the 2.5s tail did not reproduce in isolation (~300-420ms P95),
splitting text barely helped, and a lean {path,node_id} covering index was the
only measurable win (id-only). This run answers the open questions, all on ONE
baseline ingest (text inline, the report's schema) to avoid re-ingesting:

  variance     repeat the 200-path subtree measurement R times -> is the tail
               stable at ~300-420ms, or does it spike toward the report's 2.5s?
  lean         {path,node_id} covering index for the id-only query (re-confirm)
  lean+$in     covered node_id scan, then batched $in (chunked, since 36k ids in
               one $in exceeds the 16MB command limit) to resolve title+summary
  fat-cover    {path,node_id,title,summary} covering index for the FULL view --
               does it run covered, beat the ~419ms FETCH, and how large does the
               multi-kB summary key make the index?

Single-client, one collection, disk-guarded, dropped on exit. Results are
checkpointed to --out after every phase so one phase failing cannot lose the rest.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from bench_databases import flatten, time_calls

DB = "bench"
COL = "opt2_baseline"
PROJ_ID = {"node_id": 1, "_id": 0}
PROJ_VIEW = {"node_id": 1, "title": 1, "summary": 1, "_id": 0}
LEAN = [("path", 1), ("node_id", 1)]
FAT = [("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)]


def log(m):
    print(m, file=sys.stderr, flush=True)


def free_gb():
    return shutil.disk_usage("/").free / 1e9


def guard(floor):
    f = free_gb()
    if f < floor:
        raise SystemExit(f"ABORT: free disk {f:.1f}GB < {floor}GB floor")
    return f


def ingest(col, rows, batch=10000):
    t0 = time.time()
    buf, n = [], 0
    for r in rows:
        buf.append(dict(r))
        if len(buf) >= batch:
            col.insert_many(buf, ordered=False); n += len(buf); buf = []
            if n % 2_000_000 == 0:
                log(f"      ... {n:,}")
    if buf:
        col.insert_many(buf, ordered=False); n += len(buf)
    return time.time() - t0, n


def index_sizes(db, name):
    st = db.command("collStats", name)
    return {"total_index_gb": round(st.get("totalIndexSize", 0) / 1e9, 3),
            "data_gb": round(st.get("storageSize", 0) / 1e9, 3),
            "per_index_mb": {k: round(v / 1e6, 1) for k, v in st.get("indexSizes", {}).items()}}


def explain(col, path, projection, hint):
    e = col.find({"path": {"$gte": path + "/", "$lt": path + "0"}}, projection).hint(hint).explain()
    plan = e.get("queryPlanner", {}).get("winningPlan", {})
    stats = e.get("executionStats", {})
    names = []

    def walk(s):
        if isinstance(s, dict):
            if "stage" in s:
                names.append(s["stage"])
            if "inputStage" in s:
                walk(s["inputStage"])
    walk(plan)
    return {"stages": names, "covered": "FETCH" not in names,
            "docsExamined": stats.get("totalDocsExamined"),
            "keysExamined": stats.get("totalKeysExamined"),
            "nReturned": stats.get("nReturned")}


def measure(col, paths, projection, hint=None, label=""):
    def q(path):
        c = col.find({"path": {"$gte": path + "/", "$lt": path + "0"}}, projection)
        if hint:
            c = c.hint(hint)
        return len(list(c))
    log(f"      {label} ...")
    return time_calls(q, paths)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="data/large.json")
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--out", default="runs/subset_opt2_large.json")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--in-chunk", type=int, default=1000)
    ap.add_argument("--min-free-gb", type=float, default=30.0)
    args = ap.parse_args()

    from pymongo import ASCENDING, MongoClient

    log(f"free disk: {free_gb():.1f}GB"); guard(args.min_free_gb)
    log(f"loading {args.doc} ...")
    t0 = time.time()
    doc = json.load(open(args.doc))
    recs = flatten(doc, tree_id="base", seed=7)
    del doc
    rows, paths = recs.rows, recs.subtree_paths
    log(f"flattened {len(rows):,} nodes in {time.time()-t0:.0f}s; {len(paths)} paths")

    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5000)
    db = client[DB]
    out = {"doc": args.doc, "nodes": len(rows), "paths": len(paths), "repeats": args.repeats}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    def save():
        Path(args.out).write_text(json.dumps(out, indent=2))

    def phase(name, fn):
        log(f"\n=== {name} ===")
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            out[name] = {"error": repr(e)}
        save()

    try:
        guard(args.min_free_gb)
        db.drop_collection(COL)
        col = db[COL]
        ing_s, n = ingest(col, rows)
        col.create_index([("path", ASCENDING)])
        out["ingest_s"] = round(ing_s, 1); save()
        log(f"  ingested {n:,} in {ing_s:.0f}s; free {free_gb():.1f}GB")

        def do_variance():
            return {
                "id": [measure(col, paths, PROJ_ID, label=f"id rep{i+1}") for i in range(args.repeats)],
                "view": [measure(col, paths, PROJ_VIEW, label=f"view rep{i+1}") for i in range(args.repeats)],
            }
        phase("variance", do_variance)

        def do_lean():
            guard(args.min_free_gb)
            t = time.time(); col.create_index(LEAN); b = time.time() - t
            return {"build_s": round(b, 1),
                    "explain": explain(col, paths[0], PROJ_ID, LEAN),
                    "id_covered": measure(col, paths, PROJ_ID, LEAN, "lean id covered"),
                    "index_sizes": index_sizes(db, COL)}
        phase("lean", do_lean)

        def do_lean_in():
            col.create_index([("node_id", ASCENDING)])

            def lean_in(path):
                ids = [d["node_id"] for d in col.find(
                    {"path": {"$gte": path + "/", "$lt": path + "0"}}, PROJ_ID).hint(LEAN)]
                if not ids:
                    return 0
                got = 0
                for i in range(0, len(ids), args.in_chunk):
                    got += len(list(col.find(
                        {"node_id": {"$in": ids[i:i + args.in_chunk]}}, PROJ_VIEW)))
                return got
            log(f"      lean+$in view (chunk={args.in_chunk}) ...")
            return time_calls(lean_in, paths)
        phase("lean_plus_in_view", do_lean_in)

        def do_fat():
            guard(args.min_free_gb)
            t = time.time(); col.create_index(FAT); b = time.time() - t
            log(f"  fat index built in {b:.0f}s; free {free_gb():.1f}GB")
            return {"build_s": round(b, 1),
                    "explain": explain(col, paths[0], PROJ_VIEW, FAT),
                    "view_covered": measure(col, paths, PROJ_VIEW, FAT, "fat view covered"),
                    "index_sizes": index_sizes(db, COL)}
        phase("fat", do_fat)
    finally:
        db.drop_collection(COL)
        log(f"\ncleaned up; free {free_gb():.1f}GB")
        client.close()
        save()

    # ---- summary (robust to any phase having errored) -----------------------
    def line(tag, st):
        if not st or "p50_ms" not in st:
            return f"  {tag:26s} (n/a)"
        return (f"  {tag:26s} p50={st['p50_ms']:8.2f}  p95={st['p95_ms']:9.2f}  "
                f"p99={st['p99_ms']:9.2f}  mean={st['mean_ms']:8.2f}")

    print("\n=============== get_subtree validation, 10M tree, single client ===============")
    v = out.get("variance", {})
    if "view" in v:
        print(f"  variance id-only P95 x{args.repeats}: {[r['p95_ms'] for r in v['id']]}")
        print(f"  variance view    P95 x{args.repeats}: {[r['p95_ms'] for r in v['view']]}")
        print(line("baseline view (FETCH)", v["view"][-1]))
    print(line("lean id-only (covered)", out.get("lean", {}).get("id_covered")))
    print(line("lean+$in view (chunked)", out.get("lean_plus_in_view")))
    print(line("fat view (covered)", out.get("fat", {}).get("view_covered")))
    if "explain" in out.get("lean", {}):
        print(f"\n  lean explain: {out['lean']['explain']}")
    if "explain" in out.get("fat", {}):
        print(f"  fat  explain: {out['fat']['explain']}")
        print(f"  fat index sizes (MB): {out['fat']['index_sizes']['per_index_mb']}")
    log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
