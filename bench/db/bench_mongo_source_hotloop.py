#!/usr/bin/env python3
"""Single-arm MongoDB hot loop for source-level CPU sampling.

The script keeps one server query shape hot for a fixed duration so an
external sampler can attribute CPU to MongoDB and WiredTiger functions.
It uses the real 10M stores for point reads and a temporary, validated
128-child collection for the child-scan arms.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient


TREE_ID = "base"
NODES = "layout2_view"
TEXT = "layout_shared_text"
NODE_INDEX = "allops_tree_node"
CHILD_COLLECTION = "source_profile_children"
CHILD_NARROW = "source_profile_children_narrow"
CHILD_COVER = "source_profile_children_cover"
ARMS = (
    "node_miss",
    "node_hit",
    "entity_miss",
    "entity_hit",
    "children_empty",
    "children_covered128",
    "children_noncovered128",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("arm", choices=ARMS)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--start-delay", type=float, default=2.0)
    parser.add_argument("--ready-file")
    parser.add_argument("--go-file")
    parser.add_argument(
        "--mongo-uri",
        default=os.environ.get(
            "MONGO_URI",
            "mongodb://localhost:57017/?directConnection=true",
        ),
    )
    parser.add_argument(
        "--mongo-db",
        default=os.environ.get("MONGO_DB", "bench"),
    )
    return parser.parse_args()


def load_real_ids() -> dict[str, str]:
    run_root = Path(__file__).resolve().parent / "runs"
    node = json.loads(
        (
            run_root
            / "node_rootcause_20260724"
            / "get_node_10m.json"
        ).read_text()
    )
    entity = json.loads(
        (
            run_root
            / "entity_rootcause_20260724"
            / "entity_rootcause_10m.json"
        ).read_text()
    )
    return {
        "node_hit": str(node["inputs"]["hit_ids"][0]),
        "node_miss": str(node["inputs"]["miss_ids"][0]),
        "entity_hit": str(
            entity["real"]["inputs"]["hits"][0]["node_id"]
        ),
        "entity_miss": str(
            entity["real"]["inputs"]["misses"][0]["node_id"]
        ),
    }


def setup_children(database: Any) -> None:
    database[CHILD_COLLECTION].drop()
    documents: list[dict[str, Any]] = []
    for child in range(128):
        node_id = f"child-{child:06d}"
        documents.append(
            {
                "_id": node_id,
                "tree_id": TREE_ID,
                "parent_id": "parent-128",
                "path": f"/parent-128/{child:06d}",
                "node_id": node_id,
                "title": f"title-{child:06d}".ljust(32, "x"),
                "summary": f"summary-{child:06d}".ljust(256, "y"),
                "cover_tag": True,
            }
        )
    database[CHILD_COLLECTION].insert_many(documents, ordered=True)
    database[CHILD_COLLECTION].create_index(
        [
            ("tree_id", 1),
            ("parent_id", 1),
            ("path", 1),
            ("node_id", 1),
        ],
        name=CHILD_NARROW,
        unique=True,
    )
    database[CHILD_COLLECTION].create_index(
        [
            ("tree_id", 1),
            ("parent_id", 1),
            ("cover_tag", 1),
            ("path", 1),
            ("node_id", 1),
            ("title", 1),
            ("summary", 1),
        ],
        name=CHILD_COVER,
    )


def point_command(arm: str, ids: dict[str, str]) -> tuple[str, dict[str, Any], int]:
    if arm.startswith("node_"):
        return (
            NODES,
            {
                "find": NODES,
                "filter": {
                    "tree_id": TREE_ID,
                    "node_id": ids[arm],
                },
                "projection": {
                    "_id": 0,
                    "node_id": 1,
                    "parent_id": 1,
                    "depth": 1,
                    "title": 1,
                    "summary": 1,
                    "start_index": 1,
                    "end_index": 1,
                },
                "hint": NODE_INDEX,
                "limit": 1,
                "batchSize": 1,
                "singleBatch": True,
            },
            0 if arm.endswith("miss") else 1,
        )
    return (
        TEXT,
        {
            "find": TEXT,
            "filter": {"_id": ids[arm]},
            "projection": {"_id": 1, "text": 1},
            "limit": 1,
            "batchSize": 1,
            "singleBatch": True,
        },
        0 if arm.endswith("miss") else 1,
    )


def child_command(arm: str) -> tuple[str, dict[str, Any], int]:
    if arm == "children_empty":
        parent_id = "parent-empty"
        projection = {"_id": 0, "node_id": 1}
        filter_document = {
            "tree_id": TREE_ID,
            "parent_id": parent_id,
        }
        hint = CHILD_NARROW
        expected = 0
    elif arm == "children_covered128":
        parent_id = "parent-128"
        projection = {
            "_id": 0,
            "node_id": 1,
            "title": 1,
            "summary": 1,
        }
        filter_document = {
            "tree_id": TREE_ID,
            "parent_id": parent_id,
            "cover_tag": True,
        }
        hint = CHILD_COVER
        expected = 128
    else:
        parent_id = "parent-128"
        projection = {
            "_id": 0,
            "node_id": 1,
            "title": 1,
            "summary": 1,
        }
        filter_document = {
            "tree_id": TREE_ID,
            "parent_id": parent_id,
        }
        hint = CHILD_NARROW
        expected = 128
    return (
        CHILD_COLLECTION,
        {
            "find": CHILD_COLLECTION,
            "filter": filter_document,
            "projection": projection,
            "sort": {"path": 1, "node_id": 1},
            "hint": hint,
            "batchSize": 128,
            "singleBatch": True,
        },
        expected,
    )


def stage_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        if "stage" in value:
            names.append(str(value["stage"]))
        for child in value.values():
            names.extend(stage_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(stage_names(child))
    return names


def main() -> None:
    args = parse_args()
    client = MongoClient(args.mongo_uri)
    database = client[args.mongo_db]
    is_children = args.arm.startswith("children_")
    try:
        ids = load_real_ids()
        if is_children:
            setup_children(database)
            _, command, expected = child_command(args.arm)
        else:
            _, command, expected = point_command(args.arm, ids)

        explanation = database.command(
            "explain",
            command,
            verbosity="executionStats",
        )
        stats = explanation["executionStats"]
        plan = {
            "stages": sorted(set(stage_names(explanation))),
            "keys_examined": int(stats["totalKeysExamined"]),
            "documents_examined": int(stats["totalDocsExamined"]),
            "n_returned": int(stats["nReturned"]),
        }
        if plan["n_returned"] != expected:
            raise RuntimeError(
                f"{args.arm}: expected {expected} rows, got {plan}"
            )

        for _ in range(200):
            response = database.command(command)
            if len(response["cursor"]["firstBatch"]) != expected:
                raise RuntimeError("warmup result mismatch")

        if args.ready_file and args.go_file:
            ready_file = Path(args.ready_file)
            go_file = Path(args.go_file)
            ready_file.write_text("ready\n")
            wait_deadline = time.monotonic() + 60
            while not go_file.exists():
                if time.monotonic() >= wait_deadline:
                    raise RuntimeError("timed out waiting for sampler")
                time.sleep(0.01)
        else:
            time.sleep(args.start_delay)
        deadline = time.monotonic() + args.duration
        iterations = 0
        rows = 0
        while time.monotonic() < deadline:
            response = database.command(command)
            batch = response["cursor"]["firstBatch"]
            if len(batch) != expected:
                raise RuntimeError("timed result mismatch")
            iterations += 1
            rows += len(batch)

        print(
            json.dumps(
                {
                    "arm": args.arm,
                    "duration_s": args.duration,
                    "iterations": iterations,
                    "rows": rows,
                    "plan": plan,
                },
                sort_keys=True,
            )
        )
    finally:
        if is_children:
            database[CHILD_COLLECTION].drop()
        client.close()


if __name__ == "__main__":
    main()
