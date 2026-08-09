#!/usr/bin/env python3
"""Id/meta decoupling (k:v) experiment for get_subtree on MongoDB.

The optimization report measured the Subset Pattern (text split out; title and
summary stayed inline) and a covered-id-scan + chunked $in that re-fetched the
view fields from the SAME fat collection. It did not measure the schema where
structure and metadata are fully decoupled:

  struct   topology only: {node_id, path, parent_id, depth}; the range scan
           touches ~100-byte documents (or none at all, with the lean
           {path,node_id} covering index)
  meta     a k:v collection keyed by _id=node_id holding {title, summary};
           the view is resolved per node against this store
  meta_clu the same k:v store as a clustered collection (record lives in the
           _id index itself, no index->record hop)

Variants measured on the 10M tree, same 200 seeded subtree paths as the report:

  ref_view        no-text collection, view via FETCH        (anchor: ~419 ms P95)
  ref_id_cov      no-text collection, id covered            (anchor: ~191 ms P95)
  struct_id_fetch topology-only docs, id via FETCH          (does tiny-doc FETCH help?)
  struct_id_cov   topology-only docs, id covered
  kv_in           covered id scan + chunked $in on meta._id (client-side join)
  kv_in_clu       same, meta clustered
  kv_lookup       server-side $lookup struct->meta          (one round trip)
  kv_lookup_clu   same, meta clustered

Single-client, disk-guarded, checkpointed after every phase, collections
dropped on exit unless --keep.
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
REF, STRUCT, META, META_CLU = "kv_ref", "kv_struct", "kv_meta", "kv_meta_clu"
PROJ_ID = {"node_id": 1, "_id": 0}
PROJ_VIEW = {"node_id": 1, "title": 1, "summary": 1, "_id": 0}
LEAN = [("path", 1), ("node_id", 1)]
STRUCT_FIELDS = ("tree_id", "node_id", "parent_id", "depth", "path")


def log(m):
    print(m, file=sys.stderr, flush=True)


def free_gb():
    return shutil.disk_usage("/").free / 1e9


def guard(floor):
    f = free_gb()
    if f < floor:
        raise SystemExit(f"ABORT: free disk {f:.1f}GB < {floor}GB floor")
    return f


def ingest(col, docs_iter, batch=10000):
    t0 = time.time()
    buf, n = [], 0
    for d in docs_iter:
        buf.append(d)
        if len(buf) >= batch:
            col.insert_many(buf, ordered=False); n += len(buf); buf = []
            if n % 2_000_000 == 0:
                log(f"      ... {n:,}")
    if buf:
        col.insert_many(buf, ordered=False); n += len(buf)
    return time.time() - t0, n


def storage(db, name):
    st = db.command("collStats", name)
    return {"data_gb": round(st.get("storageSize", 0) / 1e9, 3),
            "index_gb": round(st.get("totalIndexSize", 0) / 1e9, 3),
            "logical_gb": round(st.get("size", 0) / 1e9, 3),
            "count": st.get("count", 0)}


def explain(col, path, projection, hint=None):
    c = col.find({"path": {"$gte": path + "/", "$lt": path + "0"}}, projection)
    if hint:
        c = c.hint(hint)
    e = c.explain()
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


def measure_scan(col, paths, projection, hint=None, label=""):
    def q(path):
        c = col.find({"path": {"$gte": path + "/", "$lt": path + "0"}}, projection)
        if hint:
            c = c.hint(hint)
        return len(list(c))
    log(f"      {label} ...")
    return time_calls(q, paths)


def measure_kv_in(struct, meta, paths, chunk, label):
    def q(path):
        ids = [d["node_id"] for d in struct.find(
            {"path": {"$gte": path + "/", "$lt": path + "0"}}, PROJ_ID).hint(LEAN)]
        got = 0
        for i in range(0, len(ids), chunk):
            got += len(list(meta.find({"_id": {"$in": ids[i:i + chunk]}},
                                      {"title": 1, "summary": 1})))
        return got
    log(f"      {label} (chunk={chunk}) ...")
    return time_calls(q, paths)


def measure_kv_lookup(struct, meta_name, paths, label):
    def q(path):
        pipe = [
            {"$match": {"path": {"$gte": path + "/", "$lt": path + "0"}}},
            {"$project": {"node_id": 1, "_id": 0}},
            {"$lookup": {"from": meta_name, "localField": "node_id",
                         "foreignField": "_id", "as": "m"}},
            {"$project": {"node_id": 1,
                          "title": {"$first": "$m.title"},
                          "summary": {"$first": "$m.summary"}}},
        ]
        return len(list(struct.aggregate(pipe, hint="path_1_node_id_1")))
    log(f"      {label} ...")
    return time_calls(q, paths)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="data/large.json")
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--out", default="runs/subset_kv_large.json")
    ap.add_argument("--in-chunk", type=int, default=1000)
    ap.add_argument("--min-free-gb", type=float, default=30.0)
    ap.add_argument(
        "--stop-after",
        choices=["kv_in"],
        help="stop after the standard metadata-batch phase, before clustered and lookup variants",
    )
    ap.add_argument("--keep", action="store_true")
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

    uniq = len({r["node_id"] for r in rows})
    if uniq != len(rows):
        raise SystemExit(f"ABORT: node_id not unique ({uniq:,} of {len(rows):,}); "
                         f"_id-keyed meta store needs unique keys")

    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5000)
    db = client[DB]
    out = {"doc": args.doc, "nodes": len(rows), "paths": len(paths),
           "in_chunk": args.in_chunk, "stop_after": args.stop_after,
           "status": "running", "phases": {}}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    def save():
        Path(args.out).write_text(json.dumps(out, indent=2))

    def phase(name, fn):
        log(f"\n=== {name} ===")
        try:
            out["phases"][name] = fn()
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            out["phases"][name] = {"error": repr(e)}
        save()

    def drop(*names):
        for nm in names:
            db.drop_collection(nm)

    try:
        # ---- ingest all four collections -------------------------------------
        def do_ingest():
            guard(args.min_free_gb)
            drop(REF, STRUCT, META, META_CLU)
            r = {}

            col = db[REF]
            s, n = ingest(col, ({k: v for k, v in row.items() if k != "text"}
                                for row in rows))
            col.create_index([("path", ASCENDING)])
            col.create_index(LEAN)
            r["ref"] = {"ingest_s": round(s, 1), **storage(db, REF)}
            log(f"  ref: {n:,} in {s:.0f}s; free {free_gb():.1f}GB")

            col = db[STRUCT]
            s, n = ingest(col, ({k: row[k] for k in STRUCT_FIELDS} for row in rows))
            col.create_index([("path", ASCENDING)])
            col.create_index(LEAN)
            r["struct"] = {"ingest_s": round(s, 1), **storage(db, STRUCT)}
            log(f"  struct: {n:,} in {s:.0f}s; free {free_gb():.1f}GB")

            col = db[META]
            s, n = ingest(col, ({"_id": row["node_id"], "title": row["title"],
                                 "summary": row["summary"]} for row in rows))
            r["meta"] = {"ingest_s": round(s, 1), **storage(db, META)}
            log(f"  meta: {n:,} in {s:.0f}s; free {free_gb():.1f}GB")

            if args.stop_after != "kv_in":
                db.create_collection(
                    META_CLU,
                    clusteredIndex={"key": {"_id": 1}, "unique": True},
                )
                col = db[META_CLU]
                s, n = ingest(col, (
                    {"_id": row["node_id"], "title": row["title"],
                     "summary": row["summary"]}
                    for row in rows
                ))
                r["meta_clu"] = {"ingest_s": round(s, 1), **storage(db, META_CLU)}
                log(f"  meta_clu: {n:,} in {s:.0f}s; free {free_gb():.1f}GB")
            return r
        phase("ingest", do_ingest)

        # rows are only needed for ingest; drop them (~20 GB on the 10M tree)
        # so the measurement phases run with a slim client.
        rows = None
        recs.rows.clear()

        ref, struct = db[REF], db[STRUCT]
        meta, meta_clu = db[META], db[META_CLU]

        # ---- anchors against the report's Table 4 ----------------------------
        phase("ref_view", lambda: measure_scan(
            ref, paths, PROJ_VIEW, label="ref view (FETCH)"))
        phase("ref_id_cov", lambda: {
            "explain": explain(ref, paths[0], PROJ_ID, LEAN),
            "stats": measure_scan(ref, paths, PROJ_ID, LEAN, "ref id covered")})

        # ---- topology-only scans ----------------------------------------------
        phase("struct_id_fetch", lambda: measure_scan(
            struct, paths, PROJ_ID, [("path", 1)], "struct id (FETCH, tiny docs)"))
        phase("struct_id_cov", lambda: {
            "explain": explain(struct, paths[0], PROJ_ID, LEAN),
            "stats": measure_scan(struct, paths, PROJ_ID, LEAN, "struct id covered")})

        # ---- the k:v view: struct scan + meta resolution ----------------------
        phase("kv_in", lambda: measure_kv_in(
            struct, meta, paths, args.in_chunk, "kv view: covered ids + $in meta"))
        if args.stop_after == "kv_in":
            out["status"] = "complete"
            out["completed_through"] = "kv_in"
            save()
            log("stopping after kv_in as requested")
            return
        phase("kv_in_clu", lambda: measure_kv_in(
            struct, meta_clu, paths, args.in_chunk,
            "kv view: covered ids + $in meta (clustered)"))
        phase("kv_lookup", lambda: measure_kv_lookup(
            struct, META, paths, "kv view: $lookup struct->meta"))
        phase("kv_lookup_clu", lambda: measure_kv_lookup(
            struct, META_CLU, paths, "kv view: $lookup struct->meta (clustered)"))
        out["status"] = "complete"
        out["completed_through"] = "kv_lookup_clu"
        save()
    finally:
        if not args.keep:
            drop(REF, STRUCT, META, META_CLU)
            log(f"\ncleaned up; free {free_gb():.1f}GB")
        client.close()
        save()

    # ---- summary -------------------------------------------------------------
    def line(tag, st):
        if not st or "p50_ms" not in st:
            return f"  {tag:34s} (n/a)"
        return (f"  {tag:34s} p50={st['p50_ms']:8.2f}  p95={st['p95_ms']:9.2f}  "
                f"p99={st['p99_ms']:9.2f}  mean={st['mean_ms']:8.2f}  rows~{st['avg_rows']:.0f}")

    ph = out["phases"]
    print("\n========== get_subtree id/meta k:v decoupling, 10M tree, single client ==========")
    print(line("ref view (FETCH, no text)", ph.get("ref_view")))
    print(line("ref id covered", ph.get("ref_id_cov", {}).get("stats")))
    print(line("struct id (FETCH, tiny docs)", ph.get("struct_id_fetch")))
    print(line("struct id covered", ph.get("struct_id_cov", {}).get("stats")))
    print(line("kv view: ids + $in meta", ph.get("kv_in")))
    print(line("kv view: ids + $in meta (clu)", ph.get("kv_in_clu")))
    print(line("kv view: $lookup meta", ph.get("kv_lookup")))
    print(line("kv view: $lookup meta (clu)", ph.get("kv_lookup_clu")))
    ing = ph.get("ingest", {})
    for nm in ("ref", "struct", "meta", "meta_clu"):
        st = ing.get(nm)
        if st:
            print(f"  storage {nm:9s} data={st['data_gb']}GB idx={st['index_gb']}GB "
                  f"logical={st['logical_gb']}GB n={st['count']:,}")
    log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
