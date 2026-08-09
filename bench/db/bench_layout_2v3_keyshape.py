#!/usr/bin/env python3
"""Isolate whether key representation causes MongoDB COUNT_SCAN overhead.

The probe builds the same synthetic keys in MongoDB and PostgreSQL, then
compares scalar counts over integer, short-string, and materialized-path-like
long-string indexes.  MongoDB uses the direct count command (COUNT over
COUNT_SCAN), while PostgreSQL is held to a zero-heap Index Only Scan.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench_layout_2v3_rootcause import host_snapshot, stats


MONGO_COLLECTION = "layout2_keyshape_probe"
PG_TABLE = "layout2_keyshape_probe"
LONG_PREFIX = "/000000/000007/000084/"
ARMS = {
    "int": ("k_int", "keyshape_int_idx", "layout2_keyshape_int_idx"),
    "short": ("k_short", "keyshape_short_idx", "layout2_keyshape_short_idx"),
    "long": ("k_long", "keyshape_long_idx", "layout2_keyshape_long_idx"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def key(arm: str, value: int) -> int | str:
    if arm == "int":
        return value
    if arm == "short":
        return f"{value:08d}"
    return f"{LONG_PREFIX}{value:08d}"


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: defaultdict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[(sample["arm"], sample["engine"], sample["range_id"])].append(sample)
    output: dict[str, Any] = {}
    for arm in ARMS:
        output[arm] = {}
        engine_points: dict[str, list[tuple[int, float]]] = {}
        for engine in ("mongo", "postgres"):
            groups = [
                group
                for (item_arm, item_engine, _), group in grouped.items()
                if item_arm == arm and item_engine == engine
            ]
            points = [
                (group[0]["rows"], statistics.mean(item["total_ms"] for item in group))
                for group in groups
            ]
            engine_points[engine] = points
            x_mean = statistics.mean(x for x, _ in points)
            y_mean = statistics.mean(y for _, y in points)
            slope = sum((x - x_mean) * (y - y_mean) for x, y in points) / sum(
                (x - x_mean) ** 2 for x, _ in points
            )
            intercept = y_mean - slope * x_mean
            output[arm][engine] = {
                "paths": len(groups),
                "observations": sum(len(group) for group in groups),
                "avg_rows": round(statistics.mean(x for x, _ in points), 3),
                "total_ms": stats([y for _, y in points]),
                "ols_intercept_ms": round(intercept, 6),
                "ols_us_per_key": round(slope * 1_000, 6),
            }
        mongo_mean = output[arm]["mongo"]["total_ms"]["mean"]
        postgres_mean = output[arm]["postgres"]["total_ms"]["mean"]
        mongo_slope = output[arm]["mongo"]["ols_us_per_key"]
        postgres_slope = output[arm]["postgres"]["ols_us_per_key"]
        output[arm]["mongo_minus_postgres_ms"] = round(mongo_mean - postgres_mean, 6)
        output[arm]["mongo_over_postgres"] = round(mongo_mean / postgres_mean, 6)
        output[arm]["per_key_slope_ratio"] = round(mongo_slope / postgres_slope, 6)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, default=1_000_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--starts-per-size", type=int, default=10)
    parser.add_argument("--sizes", default="5000,12000,100000")
    parser.add_argument(
        "--out",
        default="bench/db/runs/rootcause_20260718/keyshape_1m_5x.json",
    )
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument(
        "--pg-dsn",
        default="host=localhost port=55432 dbname=bench user=postgres password=bench",
    )
    args = parser.parse_args()
    sizes = [int(value) for value in args.sizes.split(",")]
    if args.nodes < max(sizes) or args.repeats < 2 or args.starts_per_size < 2:
        raise SystemExit("invalid nodes, sizes, starts, or repeats")

    import psycopg
    from pymongo import ASCENDING, MongoClient

    mongo = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    db = mongo["bench"]
    collection = db[MONGO_COLLECTION]
    pg = psycopg.connect(args.pg_dsn, autocommit=True)
    pg.execute("SET enable_seqscan=off")
    pg.execute("SET max_parallel_workers_per_gather=0")
    pg.execute("SET jit=off")

    ranges = []
    for size in sizes:
        starts = {
            round(position * (args.nodes - size) / (args.starts_per_size - 1))
            for position in range(args.starts_per_size)
        }
        for start in sorted(starts):
            ranges.append({"range_id": len(ranges), "start": start, "rows": size})

    output: dict[str, Any] = {
        "status": "running",
        "started_at": utc_now(),
        "nodes": args.nodes,
        "repeats": args.repeats,
        "sizes": sizes,
        "starts_per_size": args.starts_per_size,
        "ranges": ranges,
        "contract": (
            "same synthetic ordinal values and range cardinalities; MongoDB direct "
            "count command with COUNT_SCAN; PostgreSQL count with zero-heap Index "
            "Only Scan; PostgreSQL strings use COLLATE C like layout2_pg_view; only "
            "indexed key representation changes across arms"
        ),
        "environment": {
            "before_prepare": host_snapshot(),
            "mongo_server": mongo.server_info().get("version"),
            "postgres_server": pg.info.server_version,
        },
        "samples": [],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        if output["samples"]:
            output["summary"] = summarize(output["samples"])
        out_path.write_text(json.dumps(output, indent=2))

    def cleanup() -> None:
        collection.drop()
        pg.execute(f"DROP TABLE IF EXISTS {PG_TABLE}")

    def prepare() -> None:
        print(f"preparing {args.nodes:,} synthetic keys", flush=True)
        cleanup()
        batch_size = 10_000
        for start in range(0, args.nodes, batch_size):
            stop = min(args.nodes, start + batch_size)
            collection.insert_many(
                [
                    {
                        "k_int": value,
                        "k_short": f"{value:08d}",
                        "k_long": f"{LONG_PREFIX}{value:08d}",
                    }
                    for value in range(start, stop)
                ],
                ordered=False,
            )
            if stop % 100_000 == 0:
                print(f"  mongo rows {stop:,}/{args.nodes:,}", flush=True)
        for arm, (field, mongo_index, _) in ARMS.items():
            collection.create_index([(field, ASCENDING)], name=mongo_index)

        pg.execute(
            f"CREATE UNLOGGED TABLE {PG_TABLE} ("
            "k_int INTEGER, k_short TEXT COLLATE \"C\", k_long TEXT COLLATE \"C\")"
        )
        pg.execute(
            f"INSERT INTO {PG_TABLE} "
            "SELECT value, lpad(value::text, 8, '0'), "
            f"'{LONG_PREFIX}' || lpad(value::text, 8, '0') "
            f"FROM generate_series(0, {args.nodes - 1}) AS value"
        )
        for _, (field, _, pg_index) in ARMS.items():
            pg.execute(f"CREATE INDEX {pg_index} ON {PG_TABLE} ({field})")
        pg.execute(f"VACUUM (ANALYZE) {PG_TABLE}")

        mongo_stats = db.command("collStats", MONGO_COLLECTION, scale=1)
        output["index_bytes"] = {
            arm: {
                "mongo": mongo_stats["indexSizes"][mongo_index],
                "postgres": pg.execute(
                    "SELECT pg_relation_size(%s::regclass)", (pg_index,)
                ).fetchone()[0],
            }
            for arm, (_, mongo_index, pg_index) in ARMS.items()
        }

    def mongo_count(arm: str, item: dict[str, int], repeat: int) -> dict[str, Any]:
        field, mongo_index, _ = ARMS[arm]
        query = {
            field: {
                "$gte": key(arm, item["start"]),
                "$lt": key(arm, item["start"] + item["rows"]),
            }
        }
        started = time.perf_counter()
        count = db.command(
            {
                "count": MONGO_COLLECTION,
                "query": query,
                "hint": mongo_index,
            }
        )["n"]
        total_ms = (time.perf_counter() - started) * 1_000
        if count != item["rows"]:
            raise RuntimeError(f"Mongo count mismatch for {arm}, range {item['range_id']}")
        return {
            "engine": "mongo",
            "arm": arm,
            "range_id": item["range_id"],
            "repeat": repeat,
            "rows": item["rows"],
            "total_ms": round(total_ms, 6),
        }

    def pg_count(arm: str, item: dict[str, int], repeat: int) -> dict[str, Any]:
        field, _, _ = ARMS[arm]
        started = time.perf_counter()
        count = pg.execute(
            f"SELECT count(*) FROM {PG_TABLE} WHERE {field} >= %s AND {field} < %s",
            (key(arm, item["start"]), key(arm, item["start"] + item["rows"])),
        ).fetchone()[0]
        total_ms = (time.perf_counter() - started) * 1_000
        if count != item["rows"]:
            raise RuntimeError(f"PostgreSQL count mismatch for {arm}, range {item['range_id']}")
        return {
            "engine": "postgres",
            "arm": arm,
            "range_id": item["range_id"],
            "repeat": repeat,
            "rows": item["rows"],
            "total_ms": round(total_ms, 6),
        }

    def plan_gate() -> None:
        gate = ranges[len(ranges) // 2]
        output["preflight"] = {}
        for arm, (field, mongo_index, pg_index) in ARMS.items():
            query = {
                field: {
                    "$gte": key(arm, gate["start"]),
                    "$lt": key(arm, gate["start"] + gate["rows"]),
                }
            }
            explanation = db.command(
                "explain",
                {
                    "count": MONGO_COLLECTION,
                    "query": query,
                    "hint": mongo_index,
                },
                verbosity="executionStats",
            )
            mongo_stats = explanation["executionStats"]
            stage = mongo_stats["executionStages"]
            while stage["stage"] != "COUNT_SCAN" and "inputStage" in stage:
                stage = stage["inputStage"]
            pg_plan = pg.execute(
                "EXPLAIN (ANALYZE, FORMAT JSON) "
                f"SELECT count(*) FROM {PG_TABLE} WHERE {field} >= %s AND {field} < %s",
                (key(arm, gate["start"]), key(arm, gate["start"] + gate["rows"])),
            ).fetchone()[0][0]["Plan"]["Plans"][0]
            output["preflight"][arm] = {
                "mongo_stage": stage["stage"],
                "mongo_docs_examined": mongo_stats["totalDocsExamined"],
                "postgres_node_type": pg_plan["Node Type"],
                "postgres_index": pg_plan.get("Index Name"),
                "postgres_heap_fetches": pg_plan.get("Heap Fetches", 0),
            }
            if stage["stage"] != "COUNT_SCAN" or mongo_stats["totalDocsExamined"] != 0:
                raise RuntimeError(f"Mongo {arm} failed COUNT_SCAN gate")
            if (
                pg_plan["Node Type"] != "Index Only Scan"
                or pg_plan.get("Index Name") != pg_index
                or pg_plan.get("Heap Fetches", 0) != 0
            ):
                raise RuntimeError(f"PostgreSQL {arm} failed index-only gate")

    try:
        prepare()
        plan_gate()
        save()
        print(f"warming {len(ranges)} ranges x {len(ARMS)} key shapes", flush=True)
        for item in ranges:
            for arm in ARMS:
                mongo_count(arm, item, -1)
                pg_count(arm, item, -1)

        total = len(ranges) * args.repeats
        print(f"measuring {total} range repetitions", flush=True)
        done = 0
        for repeat in range(args.repeats):
            offset = repeat * len(ranges) // args.repeats
            order = ranges[offset:] + ranges[:offset]
            for item in order:
                arm_order = list(ARMS)
                rotation = (item["range_id"] + repeat) % len(arm_order)
                arm_order = arm_order[rotation:] + arm_order[:rotation]
                for arm_number, arm in enumerate(arm_order):
                    if (item["range_id"] + repeat + arm_number) % 2:
                        output["samples"].append(mongo_count(arm, item, repeat))
                        output["samples"].append(pg_count(arm, item, repeat))
                    else:
                        output["samples"].append(pg_count(arm, item, repeat))
                        output["samples"].append(mongo_count(arm, item, repeat))
                done += 1
                if done % 15 == 0:
                    save()
                    print(f"  measure {done}/{total}", flush=True)

        output["summary"] = summarize(output["samples"])
        output["status"] = "complete"
        output["finished_at"] = utc_now()
        output["environment"]["after"] = host_snapshot()
        save()
        print(json.dumps(output["summary"], indent=2))
    finally:
        if not args.keep:
            cleanup()
        pg.close()
        mongo.close()


if __name__ == "__main__":
    main()
