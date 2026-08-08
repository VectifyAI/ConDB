#!/usr/bin/env python3
"""Clone bench.layout2_view from the 7.0.34 server onto a locally-built mongod.

The measured baseline lives in the condb_mongo container (7.0.34, port 57017).  A
master build cannot open that dbpath, so any A/B on a self-built binary needs its
own copy of the data.  This copies the collection whole -- 10M documents -- rather
than a subtree slice, so the covering index has the same 4.66 GB / 10M-key shape
and the range scan descends the same B-tree.

Documents move as raw BSON (`RawBSONDocument` on the read side), so neither side
decodes or re-encodes them: the bytes that come off the source socket are the bytes
handed to the destination.  Indexes are created after the load, not before.

Usage:
    subtree_clone_local.py --dest-port 57018 [--collection layout2_view] [--workers 8]
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from bson.raw_bson import RawBSONDocument
from pymongo import MongoClient

SRC_URI = "mongodb://localhost:57017/?directConnection=true"
DB = "bench"
NODES = "layout2_view"

# Same five index definitions as the source collection, same order.
INDEXES: list[tuple[list[tuple[str, int]], str, bool]] = [
    ([("tree_id", 1), ("node_id", 1)], "allops_tree_node", True),
    ([("path", 1), ("node_id", 1)], "path_1_node_id_1", False),
    ([("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)],
     "layout2_rootcause_exact_cover", False),
    ([("tree_id", 1), ("parent_id", 1), ("path", 1), ("node_id", 1)],
     "allops_tree_parent_path", False),
]


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def split_points(src: MongoClient, collection: str, workers: int) -> list[Any]:
    """Boundaries that cut the collection into `workers` roughly equal _id ranges."""
    if workers <= 1:
        return []
    coll = src[DB][collection]
    total = coll.estimated_document_count()
    step = max(1, total // workers)
    bounds = []
    # A covered walk of _id is cheap; take every `step`-th key.
    for i in range(1, workers):
        doc = next(coll.find({}, {"_id": 1}).sort("_id", 1).skip(i * step).limit(1), None)
        if doc is not None:
            bounds.append(doc["_id"])
    return bounds


def copy_range(dest_uri: str, collection: str, lo: Any, hi: Any, batch: int) -> int:
    src = MongoClient(SRC_URI, document_class=RawBSONDocument)
    dst = MongoClient(dest_uri, document_class=RawBSONDocument)
    query: dict[str, Any] = {}
    if lo is not None or hi is not None:
        bounds: dict[str, Any] = {}
        if lo is not None:
            bounds["$gte"] = lo
        if hi is not None:
            bounds["$lt"] = hi
        query["_id"] = bounds

    out = dst[DB][collection]
    copied = 0
    buf: list[RawBSONDocument] = []
    for doc in src[DB][collection].find(query, batch_size=4000):
        buf.append(doc)
        if len(buf) >= batch:
            out.insert_many(buf, ordered=False)
            copied += len(buf)
            buf = []
    if buf:
        out.insert_many(buf, ordered=False)
        copied += len(buf)
    src.close()
    dst.close()
    return copied


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest-port", type=int, default=57018)
    ap.add_argument("--collection", default=NODES)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--drop", action="store_true", help="drop the destination collection first")
    ap.add_argument("--indexes-only", action="store_true")
    args = ap.parse_args()

    dest_uri = f"mongodb://localhost:{args.dest_port}/?directConnection=true"
    src = MongoClient(SRC_URI)
    dst = MongoClient(dest_uri)
    log(f"source  {src.server_info()['version']}  ->  dest {dst.server_info()['version']}")

    expected = src[DB][args.collection].estimated_document_count()

    if not args.indexes_only:
        if args.drop:
            dst[DB][args.collection].drop()
            log(f"dropped dest {DB}.{args.collection}")

        bounds = split_points(src, args.collection, args.workers)
        edges: list[Any] = [None, *bounds, None]
        ranges = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
        log(f"copying {expected} docs in {len(ranges)} _id ranges")

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
            futures = [
                pool.submit(copy_range, dest_uri, args.collection, lo, hi, args.batch)
                for lo, hi in ranges
            ]
            copied = sum(f.result() for f in futures)
        dt = time.time() - t0
        log(f"copied {copied} docs in {dt:.1f}s ({copied / max(dt, 1e-9):,.0f}/s)")

    got = dst[DB][args.collection].count_documents({})
    log(f"dest count {got} (source {expected})")
    if got != expected:
        log("WARNING: destination count does not match source")

    for keys, name, uniq in INDEXES:
        t0 = time.time()
        dst[DB][args.collection].create_index(keys, name=name, unique=uniq)
        log(f"index {name} built in {time.time() - t0:.1f}s")

    stats = dst.get_database(DB).command("collStats", args.collection)
    log(f"dest storageSize {stats['storageSize'] / 1e9:.2f} GB  "
        f"totalIndexSize {stats['totalIndexSize'] / 1e9:.2f} GB")
    for k, v in stats["indexSizes"].items():
        log(f"  idx {k} {v / 1e9:.3f} GB")
    return 0 if got == expected else 1


if __name__ == "__main__":
    sys.exit(main())
