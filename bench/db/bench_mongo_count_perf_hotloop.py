#!/usr/bin/env python3
"""Build and drive the MongoDB half of the scalar-count CPU profile."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from pymongo import ASCENDING, MongoClient


def find_stage(stage: dict, name: str) -> dict | None:
    if stage.get("stage") == name:
        return stage
    for key in ("inputStage", "inputStages", "outerStage", "innerStage"):
        child = stage.get(key)
        if isinstance(child, dict):
            match = find_stage(child, name)
            if match is not None:
                return match
        elif isinstance(child, list):
            for item in child:
                match = find_stage(item, name)
                if match is not None:
                    return match
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="mongodb://localhost:57018")
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--count-rows", type=int, default=500_000)
    parser.add_argument("--queries", type=int, default=150)
    parser.add_argument("--out")
    parser.add_argument("--skip-seed", action="store_true")
    args = parser.parse_args()

    client = MongoClient(args.uri, serverSelectionTimeoutMS=10_000)
    db = client["bench"]
    coll = db["perfprobe"]
    if not args.skip_seed:
        print(f"seeding {args.rows:,} long path-like keys ...", flush=True)
        coll.drop()
        batch = []
        for i in range(args.rows):
            batch.append({"k": f"/000000/000007/000084/{i:08d}"})
            if len(batch) == 10_000:
                coll.insert_many(batch, ordered=False)
                batch.clear()
        if batch:
            coll.insert_many(batch, ordered=False)
        coll.create_index([("k", ASCENDING)], name="k_1")

    lower = "/000000/000007/000084/00000000"
    upper = f"/000000/000007/000084/{args.count_rows:08d}"
    predicate = {"k": {"$gte": lower, "$lt": upper}}
    command = {"count": coll.name, "query": predicate, "hint": "k_1"}
    explain = db.command("explain", command, verbosity="executionStats")
    count_scan = find_stage(explain["queryPlanner"]["winningPlan"], "COUNT_SCAN")
    stats = explain["executionStats"]
    if count_scan is None or stats["totalDocsExamined"] != 0:
        raise RuntimeError(f"expected zero-document COUNT_SCAN, got {explain}")
    warm = db.command(command)
    if warm["n"] != args.count_rows:
        raise RuntimeError(f"warm count mismatch: {warm['n']}")

    version = db.command("buildInfo")["version"]
    print(
        f"READY mongod_pid=1 rows={args.rows} count_rows={args.count_rows}",
        flush=True,
    )
    input("Attach perf, then press Enter to start: ")

    latencies_ms: list[float] = []
    for _ in range(args.queries):
        started = time.perf_counter()
        result = db.command(command)
        latencies_ms.append((time.perf_counter() - started) * 1_000)
        if result["n"] != args.count_rows:
            raise RuntimeError(f"count mismatch: {result['n']}")

    output = {
        "status": "complete",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": "mongodb",
        "version": version,
        "workload": {
            "rows": args.rows,
            "count_rows": args.count_rows,
            "key_shape": "materialized-path-like long string",
            "plan_node": count_scan["stage"],
            "documents_examined": stats["totalDocsExamined"],
        },
        "hot_loop": {
            "requested_queries": args.queries,
            "queries": len(latencies_ms),
            "mean_ms": statistics.mean(latencies_ms),
            "median_ms": statistics.median(latencies_ms),
            "min_ms": min(latencies_ms),
            "max_ms": max(latencies_ms),
        },
        "explain": explain,
    }
    client.close()
    rendered = json.dumps(output, indent=2)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n")
    print(rendered, flush=True)


if __name__ == "__main__":
    main()
