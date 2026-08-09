#!/usr/bin/env python3
"""Compare scalar count scans after removing per-row result materialization."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench_layout_2v3_rootcause import (
    LEAN_MONGO_INDEX,
    MONGO_VIEW,
    bounds,
    host_snapshot,
    stats,
    stratified_indices,
    validate_source,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    grouped: defaultdict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[(sample["engine"], sample["source_index"])].append(sample)
    for engine in ("mongo", "postgres"):
        groups = [group for (item_engine, _), group in grouped.items() if item_engine == engine]
        output[engine] = {
            "paths": len(groups),
            "observations": sum(len(group) for group in groups),
            "avg_rows": round(statistics.mean(group[0]["rows"] for group in groups), 3),
            "total_ms": stats(
                [statistics.mean(item["total_ms"] for item in group) for group in groups]
            ),
        }
    mongo = output["mongo"]["total_ms"]["mean"]
    postgres = output["postgres"]["total_ms"]["mean"]
    output["mongo_minus_postgres_ms"] = round(mongo - postgres, 6)
    output["mongo_over_postgres"] = round(mongo / postgres, 6)
    repeat_deltas = []
    for repeat in sorted({sample["repeat"] for sample in samples}):
        by_engine = {
            engine: statistics.mean(
                sample["total_ms"]
                for sample in samples
                if sample["engine"] == engine and sample["repeat"] == repeat
            )
            for engine in ("mongo", "postgres")
        }
        repeat_deltas.append(
            {
                "repeat": repeat,
                "mongo_ms": round(by_engine["mongo"], 6),
                "postgres_ms": round(by_engine["postgres"], 6),
                "delta_ms": round(by_engine["mongo"] - by_engine["postgres"], 6),
                "ratio": round(by_engine["mongo"] / by_engine["postgres"], 6),
            }
        )
    output["by_repeat"] = repeat_deltas
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-result",
        default="bench/db/runs/rootcause_20260718/mongo_seed_10m.json",
    )
    parser.add_argument(
        "--out",
        default="bench/db/runs/rootcause_20260718/count_scan_10m_5x.json",
    )
    parser.add_argument("--paths", type=int, default=40)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--mongo-count-api",
        choices=("documents", "command"),
        default="documents",
        help="documents uses count_documents aggregation; command removes that layer",
    )
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument(
        "--pg-dsn",
        default="host=localhost port=55432 dbname=bench user=postgres password=bench",
    )
    args = parser.parse_args()
    if args.paths < 3 or args.repeats < 2:
        raise SystemExit("requires paths>=3 and repeats>=2")

    source = json.loads(Path(args.source_result).read_text())
    validate_source(source, allow_nonstandard=False)
    indices = stratified_indices(source, args.paths)

    import psycopg
    from pymongo import MongoClient

    mongo = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    db = mongo["bench"]
    collection = db[MONGO_VIEW]
    pg = psycopg.connect(args.pg_dsn, autocommit=True)
    # This is a mechanism-isolation experiment, not a planner-choice benchmark:
    # keep every PostgreSQL path on the same index-only access method as Mongo's
    # forced COUNT_SCAN, including the two million-row-scale ranges.
    pg.execute("SET enable_seqscan=off")
    pg.execute("SET max_parallel_workers_per_gather=0")
    pg.execute("SET jit=off")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output: dict[str, Any] = {
        "status": "running",
        "started_at": utc_now(),
        "source_result": args.source_result,
        "indices": indices,
        "paths": len(indices),
        "repeats": args.repeats,
        "mongo_count_api": args.mongo_count_api,
        "contract": (
            "same path bounds and lean path,node_id indexes; scalar count output; "
            "MongoDB COUNT_SCAN with zero documents examined; PostgreSQL index-only "
            "scan with zero heap fetches; PostgreSQL sequential scan and parallel "
            "gather disabled to hold the access method fixed; mongo_count_api records "
            "whether an aggregation wrapper is present"
        ),
        "environment": {
            "before": host_snapshot(),
            "mongo_server": mongo.server_info().get("version"),
            "postgres_server": pg.info.server_version,
        },
        "samples": [],
    }

    def save() -> None:
        output["summary"] = summarize(output["samples"])
        out_path.write_text(json.dumps(output, indent=2))

    def mongo_count(index: int, repeat: int) -> dict[str, Any]:
        sample = source["samples"][index]
        lower, upper = bounds(sample["path"])
        started = time.perf_counter()
        query = {"path": {"$gte": lower, "$lt": upper}}
        if args.mongo_count_api == "command":
            count = db.command(
                {
                    "count": MONGO_VIEW,
                    "query": query,
                    "hint": LEAN_MONGO_INDEX,
                }
            )["n"]
        else:
            count = collection.count_documents(query, hint=LEAN_MONGO_INDEX)
        total_ms = (time.perf_counter() - started) * 1_000
        if count != sample["rows"]:
            raise RuntimeError(f"Mongo count mismatch at source index {index}")
        return {
            "engine": "mongo",
            "source_index": index,
            "repeat": repeat,
            "rows": sample["rows"],
            "total_ms": round(total_ms, 6),
        }

    def postgres_count(index: int, repeat: int) -> dict[str, Any]:
        sample = source["samples"][index]
        started = time.perf_counter()
        count = pg.execute(
            "SELECT count(*) FROM layout2_pg_view WHERE path >= %s AND path < %s",
            bounds(sample["path"]),
        ).fetchone()[0]
        total_ms = (time.perf_counter() - started) * 1_000
        if count != sample["rows"]:
            raise RuntimeError(f"PostgreSQL count mismatch at source index {index}")
        return {
            "engine": "postgres",
            "source_index": index,
            "repeat": repeat,
            "rows": sample["rows"],
            "total_ms": round(total_ms, 6),
        }

    # Gate the exact physical plans before the timed campaign.
    median_index = sorted(indices, key=lambda i: source["samples"][i]["rows"])[
        len(indices) // 2
    ]
    sample = source["samples"][median_index]
    lower, upper = bounds(sample["path"])
    query = {"path": {"$gte": lower, "$lt": upper}}
    if args.mongo_count_api == "command":
        mongo_explain = db.command(
            "explain",
            {
                "count": MONGO_VIEW,
                "query": query,
                "hint": LEAN_MONGO_INDEX,
            },
            verbosity="executionStats",
        )
        mongo_stats = mongo_explain["executionStats"]
    else:
        aggregate = {
            "aggregate": MONGO_VIEW,
            "pipeline": [
                {"$match": query},
                {"$group": {"_id": 1, "n": {"$sum": 1}}},
            ],
            "cursor": {},
            "hint": LEAN_MONGO_INDEX,
        }
        mongo_explain = db.command("explain", aggregate, verbosity="executionStats")
        mongo_stats = mongo_explain["stages"][0]["$cursor"]["executionStats"]

    mongo_stage = mongo_stats["executionStages"]
    while mongo_stage["stage"] != "COUNT_SCAN" and "inputStage" in mongo_stage:
        mongo_stage = mongo_stage["inputStage"]
    pg_explain = pg.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
        "SELECT count(*) FROM layout2_pg_view WHERE path >= %s AND path < %s",
        (lower, upper),
    ).fetchone()[0][0]
    pg_scan = pg_explain["Plan"]["Plans"][0]
    output["preflight"] = {
        "source_index": median_index,
        "rows": sample["rows"],
        "mongo": {
            "root_stage": mongo_stats["executionStages"]["stage"],
            "scan_stage": mongo_stage["stage"],
            "keys_examined": mongo_stats["totalKeysExamined"],
            "docs_examined": mongo_stats["totalDocsExamined"],
        },
        "postgres": {
            "node_type": pg_scan["Node Type"],
            "actual_rows": pg_scan["Actual Rows"],
            "heap_fetches": pg_scan.get("Heap Fetches", 0),
        },
    }
    if mongo_stage["stage"] != "COUNT_SCAN" or mongo_stats["totalDocsExamined"] != 0:
        raise RuntimeError("MongoDB count did not use a covered COUNT_SCAN")
    if pg_scan["Node Type"] != "Index Only Scan" or pg_scan.get("Heap Fetches", 0) != 0:
        raise RuntimeError("PostgreSQL count did not use a zero-heap index-only scan")
    pg_plan_gate = []
    for index in indices:
        gate_sample = source["samples"][index]
        gate_plan = pg.execute(
            "EXPLAIN (FORMAT JSON) "
            "SELECT count(*) FROM layout2_pg_view WHERE path >= %s AND path < %s",
            bounds(gate_sample["path"]),
        ).fetchone()[0][0]["Plan"]["Plans"][0]
        pg_plan_gate.append(
            {
                "source_index": index,
                "rows": gate_sample["rows"],
                "node_type": gate_plan["Node Type"],
            }
        )
        if gate_plan["Node Type"] != "Index Only Scan":
            raise RuntimeError(f"PostgreSQL path {index} escaped the index-only gate")
    output["postgres_all_path_plan_gate"] = pg_plan_gate

    try:
        print(f"warming {len(indices)} count ranges", flush=True)
        for index in indices:
            mongo_count(index, -1)
            postgres_count(index, -1)

        print(f"measuring {len(indices)} paths x {args.repeats} repeats", flush=True)
        done = 0
        for repeat in range(args.repeats):
            offset = repeat * len(indices) // args.repeats
            order = indices[offset:] + indices[:offset]
            for index in order:
                if (index + repeat) % 2:
                    output["samples"].append(mongo_count(index, repeat))
                    output["samples"].append(postgres_count(index, repeat))
                else:
                    output["samples"].append(postgres_count(index, repeat))
                    output["samples"].append(mongo_count(index, repeat))
                done += 1
                if done % 20 == 0:
                    save()
                    print(f"  measure {done}/{len(indices) * args.repeats}", flush=True)

        output["status"] = "complete"
        output["finished_at"] = utc_now()
        output["environment"]["after"] = host_snapshot()
        save()
        print(json.dumps(output["summary"], indent=2))
    finally:
        pg.close()
        mongo.close()


if __name__ == "__main__":
    main()
