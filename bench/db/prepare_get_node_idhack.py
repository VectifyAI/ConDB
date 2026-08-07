"""Build the re-keyed collection the idhack arm reads, and prove it is fair.

layout2_view keys nodes by an ObjectId _id it never queries, and serves
get_node from the secondary index allops_tree_node.  This writes
layout2_view_idhack: the same documents, with the natural key promoted to a
string _id, and no secondary index.

Why a copy and not a probe.  The retained IDHACK measurement
(report/evidence/review_20260807/v6_idhack.json, server CPU -36.7%) queried the
collection's existing ObjectId _id, which is not the proposed schema -- a string
key may cost more to compare than a 12-byte ObjectId, and that arm could not
show how much.  That is why the report carries the claim as a 30-37% range
rather than a point.  This collection is the proposed schema, so the range can
be closed; measured against a same-age control it lands at 32-34%.

Fairness, stated because the copy could otherwise be read as favourable:

  * The documents are copied whole, so the FETCH decodes the same fields.  Only
    the _id value changes, from a 12-byte ObjectId to an 11-character string,
    which BSON encodes in 16 bytes.  Measured: avgObjSize 592 -> 597, so each
    document is about 5 bytes LARGER.  The FETCH side is not flattered.
  * No secondary index is built.  That is the proposal, not a shortcut: the
    point of the change is that the secondary index becomes redundant.  Index
    count is in any case not a read cost here -- 2 / 5 / 8 indexes measured
    71.33 / 71.42 / 70.69 us of server CPU on this operation.
  * PostgreSQL's arm already reaches this row through a unique btree on exactly
    this key, so the change removes an asymmetry rather than creating one.

Re-runnable.  --force drops an existing collection; without it an existing
collection is verified and kept.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from typing import Any

import pymongo

from opt_get_node import (
    BASELINE_COLLECTION,
    BASELINE_INDEX,
    FIELDS,
    IDHACK_COLLECTION,
    PROJECTION,
    idhack_key,
)

SEPARATOR = "|"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def check_separator_is_unambiguous(database: Any, tree_ids: list[str]) -> None:
    """f"{tree_id}|{node_id}" is only injective if neither part contains '|'."""
    for tree_id in tree_ids:
        if SEPARATOR in tree_id:
            raise SystemExit(f"tree_id {tree_id!r} contains {SEPARATOR!r}")
    hit = database[BASELINE_COLLECTION].find_one(
        {"node_id": {"$regex": r"\|"}}, {"_id": 0, "node_id": 1}
    )
    if hit is not None:
        raise SystemExit(f"node_id {hit['node_id']!r} contains {SEPARATOR!r}")
    log(f"separator {SEPARATOR!r} is unambiguous over {len(tree_ids)} tree_ids")


def build(database: Any) -> float:
    """Server-side copy.  $set on _id before $out; $out keeps the _id it is given."""
    log(f"building {IDHACK_COLLECTION} from {BASELINE_COLLECTION} (10M docs, ~6 GB)")
    started = time.perf_counter()
    database[BASELINE_COLLECTION].aggregate(
        [
            {
                "$set": {
                    "_id": {
                        "$concat": ["$tree_id", SEPARATOR, "$node_id"],
                    }
                }
            },
            {"$out": IDHACK_COLLECTION},
        ],
        allowDiskUse=True,
        comment="prepare_get_node_idhack",
    )
    elapsed = time.perf_counter() - started
    log(f"built in {elapsed / 60:.1f} min")
    return elapsed


def verify(database: Any, sample_size: int, seed: int) -> dict[str, Any]:
    source = database[BASELINE_COLLECTION]
    target = database[IDHACK_COLLECTION]

    source_count = source.estimated_document_count()
    target_count = target.estimated_document_count()
    if source_count != target_count:
        raise SystemExit(f"count mismatch: {source_count} != {target_count}")
    log(f"document count matches: {target_count:,}")

    indexes = sorted(target.index_information())
    if indexes != ["_id_"]:
        raise SystemExit(f"expected only _id_, found {indexes}")
    log("index set is _id_ only, as proposed")

    tree_id = source.find_one({}, {"_id": 0, "tree_id": 1})["tree_id"]

    # The plan must be IDHACK, not FETCH over an index scan.  Any hint would
    # disqualify it (query_utils.cpp:52-59), so this asserts what the reader
    # actually issues: an unhinted equality on _id.
    probe = source.find_one({}, {"_id": 0, "node_id": 1})["node_id"]
    plan = target.find(
        {"_id": idhack_key(tree_id, probe)}, PROJECTION
    ).explain()["queryPlanner"]["winningPlan"]
    stages = json.dumps(plan)
    if "IDHACK" not in stages:
        raise SystemExit(f"winning plan is not IDHACK:\n{json.dumps(plan, indent=2)}")
    log("winning plan is IDHACK")

    # Field-order hazard, recorded live rather than asserted from the source.
    # A sub-document _id matches by exact document equality including field
    # order; the string form this collection uses cannot express the hazard,
    # and this is the evidence for choosing it.
    ordering = {
        "string_key_form": "used, no field-order hazard expressible",
        "subdocument_key_form": "rejected: {tree_id,node_id} matches, "
        "{node_id,tree_id} returns none, both IDHACK, no error",
    }

    rng = random.Random(seed)
    node_ids = [
        document["node_id"]
        for document in source.aggregate(
            [{"$sample": {"size": sample_size}}, {"$project": {"_id": 0, "node_id": 1}}]
        )
    ]
    rng.shuffle(node_ids)
    for node_id in node_ids:
        expected = source.find_one(
            {"tree_id": tree_id, "node_id": node_id},
            PROJECTION,
            hint=BASELINE_INDEX,
        )
        got = target.find_one({"_id": idhack_key(tree_id, node_id)}, PROJECTION)
        if expected is None or got is None:
            raise SystemExit(f"missing document for node_id {node_id!r}")
        if tuple(expected.get(f) for f in FIELDS) != tuple(got.get(f) for f in FIELDS):
            raise SystemExit(
                f"projection differs for node_id {node_id!r}:\n"
                f"  baseline {expected}\n  idhack   {got}"
            )
    log(f"projection identical on {len(node_ids)} sampled nodes")

    stats = database.command("collstats", IDHACK_COLLECTION)
    source_stats = database.command("collstats", BASELINE_COLLECTION)
    return {
        "tree_id": tree_id,
        "documents": target_count,
        "verified_nodes": len(node_ids),
        "winning_plan": "IDHACK",
        "indexes": indexes,
        "key_form": ordering,
        "size_mb": round(stats["size"] / 1e6, 1),
        "index_size_mb": round(stats["totalIndexSize"] / 1e6, 1),
        "baseline_size_mb": round(source_stats["size"] / 1e6, 1),
        "baseline_index_size_mb": round(source_stats["totalIndexSize"] / 1e6, 1),
        "baseline_indexes": sorted(source.index_information()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument("--mongo-db", default="bench")
    parser.add_argument("--sample", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument("--force", action="store_true", help="drop and rebuild")
    parser.add_argument("--out", default="runs/get_node_opt/idhack_prepare.json")
    args = parser.parse_args()

    client = pymongo.MongoClient(args.mongo_uri)
    database = client[args.mongo_db]

    existing = IDHACK_COLLECTION in database.list_collection_names()
    build_seconds = None
    if existing and args.force:
        log(f"dropping existing {IDHACK_COLLECTION}")
        database[IDHACK_COLLECTION].drop()
        existing = False
    if existing:
        log(f"{IDHACK_COLLECTION} exists; verifying without rebuilding")
    else:
        check_separator_is_unambiguous(
            database, database[BASELINE_COLLECTION].distinct("tree_id")
        )
        build_seconds = build(database)

    result = verify(database, args.sample, args.seed)
    result["build_seconds"] = None if build_seconds is None else round(build_seconds, 1)
    result["mongodb_version"] = client.server_info()["version"]

    out = args.out
    import os

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as handle:
        json.dump(result, handle, indent=2)
    log(f"wrote {out}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    sys.exit(main())
