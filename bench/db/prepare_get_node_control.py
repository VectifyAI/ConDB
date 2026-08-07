"""Freshness control for the idhack arm.

layout2_view_idhack is written minutes before it is measured, while
layout2_view has been resident for weeks.  If a freshly written collection is
cheaper to read -- WiredTiger cache state rather than the IDHACK plan -- then
the idhack arm's server-CPU win is partly an artifact of when the collection
was built.

This builds layout2_view_control: the same $out copy at the same moment, but
with the ObjectId _id left alone and allops_tree_node rebuilt, so the baseline
query runs against it unchanged.  Baseline-on-control against baseline-on-
layout2_view isolates freshness with the plan held fixed.

If the two baselines agree, freshness is not the mechanism and the idhack
delta stands.  If they do not, the idhack number has to be re-stated against
the control rather than against layout2_view.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pymongo

from opt_get_node import BASELINE_COLLECTION, BASELINE_INDEX

CONTROL_COLLECTION = "layout2_view_control"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument("--mongo-db", default="bench")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--out", default="runs/get_node_opt/control_prepare.json")
    args = parser.parse_args()

    client = pymongo.MongoClient(args.mongo_uri)
    database = client[args.mongo_db]

    if CONTROL_COLLECTION in database.list_collection_names():
        if not args.force:
            log(f"{CONTROL_COLLECTION} exists; use --force to rebuild")
            return
        log(f"dropping {CONTROL_COLLECTION}")
        database[CONTROL_COLLECTION].drop()

    log(f"copying {BASELINE_COLLECTION} -> {CONTROL_COLLECTION}, _id untouched")
    started = time.perf_counter()
    database[BASELINE_COLLECTION].aggregate(
        [{"$out": CONTROL_COLLECTION}],
        allowDiskUse=True,
        comment="prepare_get_node_control",
    )
    copy_seconds = time.perf_counter() - started
    log(f"copied in {copy_seconds / 60:.1f} min")

    log(f"building {BASELINE_INDEX} (tree_id, node_id) unique")
    started = time.perf_counter()
    database[CONTROL_COLLECTION].create_index(
        [("tree_id", 1), ("node_id", 1)], name=BASELINE_INDEX, unique=True
    )
    index_seconds = time.perf_counter() - started
    log(f"index built in {index_seconds / 60:.1f} min")

    source = database[BASELINE_COLLECTION].estimated_document_count()
    target = database[CONTROL_COLLECTION].estimated_document_count()
    if source != target:
        raise SystemExit(f"count mismatch: {source} != {target}")
    stats = database.command("collstats", CONTROL_COLLECTION)
    result = {
        "collection": CONTROL_COLLECTION,
        "documents": target,
        "indexes": sorted(database[CONTROL_COLLECTION].index_information()),
        "size_mb": round(stats["size"] / 1e6, 1),
        "index_size_mb": round(stats["totalIndexSize"] / 1e6, 1),
        "copy_seconds": round(copy_seconds, 1),
        "index_seconds": round(index_seconds, 1),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))
    log(f"wrote {out_path}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
