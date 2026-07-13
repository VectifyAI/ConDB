#!/usr/bin/env python3
"""Resume the two missing phases (kv_lookup, kv_lookup_clu) of the large kv run.

The prior run was killed mid-kv_lookup by a session restart; its first 7 phases
are checkpointed in runs/subset_kv_large.json and the four collections survived
(finally-cleanup never ran). This driver re-derives the same seeded paths,
runs ONLY the two $lookup phases with the original measurement code, merges
them into the existing JSON, then drops the collections like the original
finally block would.
"""
import json
import sys
import time
from pathlib import Path

from bench_databases import flatten
from bench_subset_kv import DB, META, META_CLU, REF, STRUCT, log, measure_kv_lookup

OUT = Path("runs/subset_kv_large.json")

out = json.loads(OUT.read_text())
have = set(out["phases"])
assert {"ingest", "struct_id_cov", "kv_in"} <= have, f"unexpected checkpoint state: {have}"

log("loading data/large.json (paths only; rows freed after flatten) ...")
t0 = time.time()
doc = json.load(open("data/large.json"))
recs = flatten(doc, tree_id="base", seed=7)
del doc
paths = recs.subtree_paths
n_rows = len(recs.rows)
recs.rows.clear()
log(f"flattened {n_rows:,} nodes in {time.time()-t0:.0f}s; {len(paths)} paths")
assert n_rows == out["nodes"] and len(paths) == out["paths"], "path derivation mismatch"

from pymongo import MongoClient

client = MongoClient("mongodb://localhost:57017", serverSelectionTimeoutMS=5000)
db = client[DB]
for name in (REF, STRUCT, META, META_CLU):
    cnt = db[name].estimated_document_count()
    assert cnt == out["nodes"], f"{name} has {cnt:,} docs, expected {out['nodes']:,}"
struct = db[STRUCT]


def phase(name, fn):
    log(f"\n=== {name} ===")
    try:
        out["phases"][name] = fn()
    except Exception as e:  # noqa: BLE001
        import traceback; traceback.print_exc()
        out["phases"][name] = {"error": repr(e)}
    OUT.write_text(json.dumps(out, indent=2))


try:
    phase("kv_lookup", lambda: measure_kv_lookup(
        struct, META, paths, "kv view: $lookup struct->meta"))
    phase("kv_lookup_clu", lambda: measure_kv_lookup(
        struct, META_CLU, paths, "kv view: $lookup struct->meta (clustered)"))
finally:
    for name in (REF, STRUCT, META, META_CLU):
        db.drop_collection(name)
    log("cleaned up collections")
    client.close()
    OUT.write_text(json.dumps(out, indent=2))

for name in ("kv_lookup", "kv_lookup_clu"):
    st = out["phases"].get(name, {})
    if "p50_ms" in st:
        print(f"{name}: p50={st['p50_ms']} p95={st['p95_ms']} rows~{st['avg_rows']}")
    else:
        print(f"{name}: {st}")
