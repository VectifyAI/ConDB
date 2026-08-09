#!/usr/bin/env python3
"""Capture MongoDB profiler evidence for representative subtree scans.

The script temporarily enables the database profiler, tags only its own finds,
copies the relevant find/getMore records into a JSON result, restores the prior
profiler level, and removes ``system.profile`` when it did not exist before.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench_layout_2v3_rootcause import (
    LEAN_MONGO_INDEX,
    MONGO_COVER_INDEX,
    MONGO_VIEW,
    bounds,
    host_snapshot,
    validate_source,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def nested(document: dict[str, Any], *keys: str, default: Any = 0) -> Any:
    value: Any = document
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def pick_indices(source: dict[str, Any], targets: list[int]) -> list[int]:
    return [
        min(
            range(len(source["samples"])),
            key=lambda i: abs(source["samples"][i]["rows"] - target),
        )
        for target in targets
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-result",
        default="bench/db/runs/rootcause_20260718/mongo_seed_10m.json",
    )
    parser.add_argument(
        "--out",
        default="bench/db/runs/rootcause_20260718/mongo_profile_10m.json",
    )
    parser.add_argument("--target-rows", default="5000,12000,100000")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    args = parser.parse_args()

    source = json.loads(Path(args.source_result).read_text())
    validate_source(source, allow_nonstandard=False)
    targets = [int(value) for value in args.target_rows.split(",")]
    indices = pick_indices(source, targets)

    from pymongo import MongoClient

    mongo = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    db = mongo["bench"]
    collection = db[MONGO_VIEW]
    before = db.command({"profile": -1})
    if before["was"] != 0:
        raise SystemExit("refusing to replace an already-enabled MongoDB profiler")
    profile_existed = "system.profile" in db.list_collection_names()
    tag_prefix = f"condb-rootcause-{uuid.uuid4()}"
    arms = {
        "baseline": (
            {"node_id": 1, "title": 1, "summary": 1, "_id": 0},
            LEAN_MONGO_INDEX,
        ),
        "covered": (
            {"node_id": 1, "title": 1, "summary": 1, "_id": 0},
            MONGO_COVER_INDEX,
        ),
        "id_only": ({"node_id": 1, "_id": 0}, LEAN_MONGO_INDEX),
    }
    output: dict[str, Any] = {
        "status": "running",
        "started_at": utc_now(),
        "source_result": args.source_result,
        "indices": indices,
        "target_rows": targets,
        "repeats": args.repeats,
        "batch_size": 1_000_000,
        "contract": (
            "warm retained 10M data; profiler sums tagged find/getMore records; "
            "batch_size=1000000 minimizes avoidable cursor round trips"
        ),
        "environment": {
            "before": host_snapshot(),
            "mongo_server": mongo.server_info().get("version"),
            "profiler_before": before,
        },
        "samples": [],
    }

    try:
        db.command("profile", 2, slowms=0, sampleRate=1.0)
        for repeat in range(args.repeats):
            for position, index in enumerate(indices):
                arm_order = list(arms)
                rotation = (repeat + position) % len(arm_order)
                arm_order = arm_order[rotation:] + arm_order[:rotation]
                sample = source["samples"][index]
                lower, upper = bounds(sample["path"])
                for arm in arm_order:
                    projection, hint = arms[arm]
                    tag = f"{tag_prefix}:{repeat}:{index}:{arm}"
                    started = time.perf_counter()
                    rows = list(
                        collection.find(
                            {"path": {"$gte": lower, "$lt": upper}},
                            projection,
                            sort=[("path", 1), ("node_id", 1)],
                            hint=hint,
                            batch_size=1_000_000,
                            comment=tag,
                        )
                    )
                    client_ms = (time.perf_counter() - started) * 1_000
                    if len(rows) != sample["rows"]:
                        raise RuntimeError(f"row mismatch at source index {index}")
                    del rows

                    records = list(
                        db["system.profile"].find(
                            {
                                "$or": [
                                    {"command.comment": tag},
                                    {"originatingCommand.comment": tag},
                                ]
                            }
                        )
                    )
                    if not records:
                        raise RuntimeError(f"no profiler records for {tag}")
                    output["samples"].append(
                        {
                            "source_index": index,
                            "repeat": repeat,
                            "arm": arm,
                            "rows": sample["rows"],
                            "client_ms": round(client_ms, 6),
                            "profile_records": len(records),
                            "profile_millis": sum(record.get("millis", 0) for record in records),
                            "cpu_ms": round(
                                sum(record.get("cpuNanos", 0) for record in records)
                                / 1_000_000,
                                6,
                            ),
                            "response_bytes": sum(
                                record.get("responseLength", 0) for record in records
                            ),
                            "keys_examined": sum(
                                record.get("keysExamined", 0) for record in records
                            ),
                            "docs_examined": sum(
                                record.get("docsExamined", 0) for record in records
                            ),
                            "nreturned": sum(
                                record.get("nreturned", 0) for record in records
                            ),
                            "num_yield": sum(
                                record.get("numYield", 0) for record in records
                            ),
                            "bytes_read": sum(
                                nested(record, "storage", "data", "bytesRead")
                                for record in records
                            ),
                            "time_reading_ms": round(
                                sum(
                                    nested(
                                        record,
                                        "storage",
                                        "data",
                                        "timeReadingMicros",
                                    )
                                    for record in records
                                )
                                / 1_000,
                                6,
                            ),
                            "operations": [record.get("op") for record in records],
                        }
                    )

        grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for sample in output["samples"]:
            grouped[sample["arm"]].append(sample)
        output["summary"] = {
            arm: {
                "observations": len(samples),
                "avg_rows": round(statistics.mean(s["rows"] for s in samples), 3),
                **{
                    key: round(statistics.mean(s[key] for s in samples), 6)
                    for key in (
                        "client_ms",
                        "profile_records",
                        "profile_millis",
                        "cpu_ms",
                        "response_bytes",
                        "keys_examined",
                        "docs_examined",
                        "nreturned",
                        "num_yield",
                        "bytes_read",
                        "time_reading_ms",
                    )
                },
            }
            for arm, samples in grouped.items()
        }
        output["status"] = "complete"
        output["finished_at"] = utc_now()
        output["environment"]["after"] = host_snapshot()
    finally:
        db.command(
            "profile",
            before["was"],
            slowms=before.get("slowms", 100),
            sampleRate=before.get("sampleRate", 1.0),
        )
        if not profile_existed:
            db.drop_collection("system.profile")
        mongo.close()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(json.dumps(output["summary"], indent=2))


if __name__ == "__main__":
    main()
