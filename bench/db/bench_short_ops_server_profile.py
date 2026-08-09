#!/usr/bin/env python3
"""Profile server-side work for the three short MongoDB/PostgreSQL reads.

MongoDB profile level 2 provides per-command CPU nanoseconds, planning time,
keys/documents examined, response size, and execution-stage counters.
PostgreSQL uses EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) for the matched
queries.  Count, covered ID-only, and full-output arms isolate the work added
by index lookup, row production, and document/heap fetch.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


TREE_ID = "base"
MONGO_NODES = "layout2_view"
MONGO_TEXT = "layout_shared_text"
PG_NODES = "layout2_pg_view"
PG_TEXT = "layout_shared_pg_text"
OPERATIONS = ("get_node", "get_children", "get_entity")
VARIANTS = ("count", "id_only", "full")

Rows = list[tuple[Any, ...]]


def log(message: str) -> None:
    print(message, flush=True)


def percentile(values: Sequence[float], pct: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return round(ordered[index], 6)


def normalize(rows: Iterable[Sequence[Any]]) -> Rows:
    return [
        tuple("" if value is None else value for value in row)
        for row in rows
    ]


def mongo_command(
    operation: str,
    variant: str,
    node_id: str,
    comment: str,
) -> dict[str, Any]:
    if operation == "get_node":
        predicate = {"tree_id": TREE_ID, "node_id": node_id}
        if variant == "count":
            return {
                "count": MONGO_NODES,
                "query": predicate,
                "hint": "allops_tree_node",
                "limit": 1,
                "comment": comment,
            }
        projection = (
            {"_id": 0, "node_id": 1}
            if variant == "id_only"
            else {
                "_id": 0,
                "node_id": 1,
                "parent_id": 1,
                "depth": 1,
                "title": 1,
                "summary": 1,
                "start_index": 1,
                "end_index": 1,
            }
        )
        return {
            "find": MONGO_NODES,
            "filter": predicate,
            "projection": projection,
            "hint": "allops_tree_node",
            "limit": 1,
            "singleBatch": True,
            "comment": comment,
        }

    if operation == "get_children":
        predicate = {"tree_id": TREE_ID, "parent_id": node_id}
        if variant == "count":
            return {
                "count": MONGO_NODES,
                "query": predicate,
                "hint": "allops_tree_parent_path",
                "comment": comment,
            }
        projection = (
            {"_id": 0, "node_id": 1}
            if variant == "id_only"
            else {"_id": 0, "node_id": 1, "title": 1, "summary": 1}
        )
        return {
            "find": MONGO_NODES,
            "filter": predicate,
            "projection": projection,
            "sort": {"path": 1, "node_id": 1},
            "hint": "allops_tree_parent_path",
            "comment": comment,
        }

    predicate = {"_id": node_id}
    if variant == "count":
        return {
            "count": MONGO_TEXT,
            "query": predicate,
            "limit": 1,
            "comment": comment,
        }
    projection = {"_id": 1} if variant == "id_only" else {"_id": 1, "text": 1}
    return {
        "find": MONGO_TEXT,
        "filter": predicate,
        "projection": projection,
        "limit": 1,
        "singleBatch": True,
        "comment": comment,
    }


def run_mongo(database: Any, command: dict[str, Any]) -> Rows:
    if "count" in command:
        result = database.command(command)
        return [(int(result["n"]),)]
    result = database.command(command)
    rows = result["cursor"]["firstBatch"]
    collection = command["find"]
    projection = command["projection"]
    if collection == MONGO_TEXT:
        if "text" in projection:
            return normalize([
                (row.get("_id"), row.get("text")) for row in rows
            ])
        return normalize([(row.get("_id"),) for row in rows])
    if "title" not in projection:
        return normalize([(row.get("node_id"),) for row in rows])
    if "parent_id" in projection:
        return normalize([
            (
                row.get("node_id"),
                row.get("parent_id"),
                row.get("depth"),
                row.get("title"),
                row.get("summary"),
                row.get("start_index"),
                row.get("end_index"),
            )
            for row in rows
        ])
    return normalize([
        (row.get("node_id"), row.get("title"), row.get("summary"))
        for row in rows
    ])


def postgres_query(
    operation: str,
    variant: str,
    node_id: str,
) -> tuple[str, tuple[Any, ...]]:
    if operation == "get_node":
        if variant == "count":
            select = "count(*)"
        elif variant == "id_only":
            select = "node_id"
        else:
            select = (
                "node_id,parent_id,depth,title,summary,start_index,end_index"
            )
        return (
            f"SELECT {select} FROM {PG_NODES} "
            "WHERE tree_id=%s AND node_id=%s",
            (TREE_ID, node_id),
        )
    if operation == "get_children":
        if variant == "count":
            return (
                f"SELECT count(*) FROM {PG_NODES} "
                "WHERE tree_id=%s AND parent_id=%s",
                (TREE_ID, node_id),
            )
        select = "node_id" if variant == "id_only" else "node_id,title,summary"
        return (
            f"SELECT {select} FROM {PG_NODES} "
            "WHERE tree_id=%s AND parent_id=%s ORDER BY path,node_id",
            (TREE_ID, node_id),
        )
    select = (
        "count(*)"
        if variant == "count"
        else "node_id"
        if variant == "id_only"
        else "node_id,text"
    )
    return (
        f"SELECT {select} FROM {PG_TEXT} WHERE node_id=%s",
        (node_id,),
    )


def stage_names(stage: dict[str, Any]) -> list[str]:
    names = [str(stage.get("stage", "unknown"))]
    for key in ("inputStage", "outerStage", "innerStage"):
        child = stage.get(key)
        if isinstance(child, dict):
            names.extend(stage_names(child))
    for child in stage.get("inputStages", []):
        names.extend(stage_names(child))
    return names


def pg_node_types(plan: dict[str, Any]) -> list[str]:
    names = [str(plan.get("Node Type", "unknown"))]
    for child in plan.get("Plans", []):
        names.extend(pg_node_types(child))
    return names


def pg_heap_fetches(plan: dict[str, Any]) -> int:
    return int(plan.get("Heap Fetches", 0)) + sum(
        pg_heap_fetches(child) for child in plan.get("Plans", [])
    )


def metric_summary(
    samples: list[dict[str, Any]],
    field: str,
) -> dict[str, float]:
    per_input = [
        statistics.median(sample[field]) for sample in samples
    ]
    return {
        "p50": percentile(per_input, 50),
        "p95": percentile(per_input, 95),
        "mean": round(statistics.mean(per_input), 6),
    }


def summarize_mongo(samples: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "cpu_us",
        "planning_us",
        "keys_examined",
        "docs_examined",
        "nreturned",
        "response_bytes",
    )
    return {
        field: metric_summary(samples, field) for field in fields
    }


def summarize_pg(samples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        field: metric_summary(samples, field)
        for field in ("planning_us", "execution_us", "total_us")
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mongo-uri",
        default="mongodb://localhost:57017",
    )
    parser.add_argument("--mongo-db", default="bench")
    parser.add_argument(
        "--pg-dsn",
        default=(
            "host=localhost port=55432 dbname=bench "
            "user=postgres password=bench"
        ),
    )
    parser.add_argument(
        "--expected",
        default=(
            "bench/db/runs/report_3eng_20260716/"
            "layout_2v3_postgres_10m_final.json"
        ),
    )
    parser.add_argument(
        "--out",
        default=(
            "bench/db/runs/short_ops_server_profile_20260724/"
            "matched_server_profile_10m.json"
        ),
    )
    parser.add_argument("--point-inputs", type=int, default=500)
    parser.add_argument("--tree-inputs", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260724)
    parser.add_argument(
        "--profile-size-mb",
        type=int,
        default=256,
    )
    args = parser.parse_args()

    from pymongo import MongoClient
    import psycopg

    mongo_client = MongoClient(args.mongo_uri)
    mongo = mongo_client[args.mongo_db]
    pg = psycopg.connect(args.pg_dsn, autocommit=True)
    pg.execute("SET jit = off")

    point_ids = [
        row[0]
        for row in pg.execute(
            f"SELECT node_id FROM {PG_TEXT} ORDER BY node_id LIMIT %s",
            (args.point_inputs,),
        ).fetchall()
    ]
    expected = json.loads(Path(args.expected).read_text())
    tree_ids = [
        sample["path"].rsplit("/", 1)[-1]
        for sample in expected["samples"][:args.tree_inputs]
    ]
    inputs = {
        "get_node": point_ids,
        "get_children": tree_ids,
        "get_entity": point_ids,
    }

    log("validating outputs and warming")
    checks = 0
    for operation in OPERATIONS:
        for node_id in inputs[operation]:
            for variant in VARIANTS:
                command = mongo_command(
                    operation, variant, node_id, "warm"
                )
                mongo_rows = run_mongo(mongo, command)
                sql, params = postgres_query(operation, variant, node_id)
                pg_rows = normalize(pg.execute(sql, params).fetchall())
                if mongo_rows != pg_rows:
                    raise RuntimeError(
                        f"output mismatch: {operation} {variant} {node_id}"
                    )
                checks += 1

    samples: dict[str, dict[str, list[dict[str, Any]]]] = {
        operation: {
            variant: [
                {
                    "node_id": node_id,
                    "cpu_us": [],
                    "planning_us": [],
                    "keys_examined": [],
                    "docs_examined": [],
                    "nreturned": [],
                    "response_bytes": [],
                }
                for node_id in inputs[operation]
            ]
            for variant in VARIANTS
        }
        for operation in OPERATIONS
    }
    pg_samples: dict[str, dict[str, list[dict[str, Any]]]] = {
        operation: {
            variant: [
                {
                    "node_id": node_id,
                    "planning_us": [],
                    "execution_us": [],
                    "total_us": [],
                }
                for node_id in inputs[operation]
            ]
            for variant in VARIANTS
        }
        for operation in OPERATIONS
    }

    before = mongo.command({"profile": -1})
    if before["was"] != 0:
        raise RuntimeError("refusing to replace an enabled MongoDB profiler")
    profile_existed = "system.profile" in mongo.list_collection_names()
    if profile_existed:
        raise RuntimeError("refusing to replace existing system.profile")
    tag_prefix = f"condb-short-profile-{uuid.uuid4()}"
    representative_mongo: dict[str, dict[str, Any]] = {
        operation: {} for operation in OPERATIONS
    }
    try:
        mongo.create_collection(
            "system.profile",
            capped=True,
            size=args.profile_size_mb * 1024 * 1024,
        )
        mongo.command("profile", 2, slowms=0, sampleRate=1.0)
        log("running MongoDB profiled commands")
        for operation_index, operation in enumerate(OPERATIONS):
            for repeat in range(args.repeats):
                order = list(range(len(inputs[operation])))
                random.Random(
                    args.seed + operation_index * 1000 + repeat
                ).shuffle(order)
                for position, input_index in enumerate(order):
                    rotation = (repeat + position) % len(VARIANTS)
                    variant_order = VARIANTS[rotation:] + VARIANTS[:rotation]
                    for variant in variant_order:
                        tag = (
                            f"{tag_prefix}|{operation}|{variant}|"
                            f"{input_index}|{repeat}"
                        )
                        command = mongo_command(
                            operation,
                            variant,
                            inputs[operation][input_index],
                            tag,
                        )
                        run_mongo(mongo, command)
                log(
                    f"  MongoDB {operation} repeat "
                    f"{repeat + 1}/{args.repeats}"
                )
        mongo.command(
            "profile",
            before["was"],
            slowms=before.get("slowms", 100),
            sampleRate=before.get("sampleRate", 1.0),
        )

        records = list(mongo["system.profile"].find({
            "command.comment": {"$regex": f"^{tag_prefix}"}
        }))
        expected_records = sum(
            len(inputs[operation]) * len(VARIANTS) * args.repeats
            for operation in OPERATIONS
        )
        if len(records) != expected_records:
            raise RuntimeError(
                f"profile record mismatch: {len(records)} "
                f"!= {expected_records}"
            )
        seen: set[str] = set()
        for record in records:
            tag = record["command"]["comment"]
            if tag in seen:
                raise RuntimeError(f"duplicate profile tag: {tag}")
            seen.add(tag)
            _, operation, variant, input_text, _ = tag.split("|")
            input_index = int(input_text)
            sample = samples[operation][variant][input_index]
            sample["cpu_us"].append(record.get("cpuNanos", 0) / 1_000)
            sample["planning_us"].append(
                record.get("planningTimeMicros", 0)
            )
            sample["keys_examined"].append(record.get("keysExamined", 0))
            sample["docs_examined"].append(record.get("docsExamined", 0))
            sample["nreturned"].append(record.get("nreturned", 0))
            sample["response_bytes"].append(record.get("responseLength", 0))
            if variant not in representative_mongo[operation]:
                representative_mongo[operation][variant] = {
                    "plan_summary": record.get("planSummary"),
                    "query_framework": record.get("queryFramework"),
                    "stages": stage_names(record.get("execStats", {})),
                    "exec_stats": record.get("execStats"),
                }
    finally:
        status = mongo.command({"profile": -1})
        if status["was"] != before["was"]:
            mongo.command(
                "profile",
                before["was"],
                slowms=before.get("slowms", 100),
                sampleRate=before.get("sampleRate", 1.0),
            )
        if not profile_existed and "system.profile" in mongo.list_collection_names():
            mongo.drop_collection("system.profile")

    log("running PostgreSQL server-side plans")
    representative_pg: dict[str, dict[str, Any]] = {
        operation: {} for operation in OPERATIONS
    }
    for operation_index, operation in enumerate(OPERATIONS):
        for repeat in range(args.repeats):
            order = list(range(len(inputs[operation])))
            random.Random(
                args.seed + operation_index * 1000 + repeat
            ).shuffle(order)
            for position, input_index in enumerate(order):
                rotation = (repeat + position) % len(VARIANTS)
                variant_order = VARIANTS[rotation:] + VARIANTS[:rotation]
                for variant in variant_order:
                    sql, params = postgres_query(
                        operation,
                        variant,
                        inputs[operation][input_index],
                    )
                    plan = pg.execute(
                        "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, "
                        "SUMMARY ON, FORMAT JSON) " + sql,
                        params,
                    ).fetchone()[0][0]
                    planning_us = float(plan["Planning Time"]) * 1_000
                    execution_us = float(plan["Execution Time"]) * 1_000
                    sample = pg_samples[operation][variant][input_index]
                    sample["planning_us"].append(planning_us)
                    sample["execution_us"].append(execution_us)
                    sample["total_us"].append(planning_us + execution_us)
                    if variant not in representative_pg[operation]:
                        representative_pg[operation][variant] = {
                            "node_types": pg_node_types(plan["Plan"]),
                            "heap_fetches": pg_heap_fetches(plan["Plan"]),
                            "plan": plan,
                        }
            log(
                f"  PostgreSQL {operation} repeat "
                f"{repeat + 1}/{args.repeats}"
            )

    summaries = {
        operation: {
            variant: {
                "mongodb": summarize_mongo(samples[operation][variant]),
                "postgresql": summarize_pg(pg_samples[operation][variant]),
            }
            for variant in VARIANTS
        }
        for operation in OPERATIONS
    }
    derived: dict[str, Any] = {}
    for operation in OPERATIONS:
        mongo_count = summaries[operation]["count"]["mongodb"]["cpu_us"]["p50"]
        mongo_id = summaries[operation]["id_only"]["mongodb"]["cpu_us"]["p50"]
        mongo_full = summaries[operation]["full"]["mongodb"]["cpu_us"]["p50"]
        pg_count = summaries[operation]["count"]["postgresql"]["total_us"]["p50"]
        pg_id = summaries[operation]["id_only"]["postgresql"]["total_us"]["p50"]
        pg_full = summaries[operation]["full"]["postgresql"]["total_us"]["p50"]
        derived[operation] = {
            "mongodb_cpu_us": {
                "count": mongo_count,
                "id_only": mongo_id,
                "full": mongo_full,
                "id_minus_count": round(mongo_id - mongo_count, 6),
                "full_minus_id": round(mongo_full - mongo_id, 6),
            },
            "postgresql_plan_plus_execution_us": {
                "count": pg_count,
                "id_only": pg_id,
                "full": pg_full,
                "id_minus_count": round(pg_id - pg_count, 6),
                "full_minus_id": round(pg_full - pg_id, 6),
            },
        }

    output = {
        "run": {
            "status": "complete",
            "generated_unix_s": time.time(),
            "repeats": args.repeats,
            "point_inputs": args.point_inputs,
            "tree_inputs": args.tree_inputs,
            "seed": args.seed,
        },
        "contract": {
            "mongodb": (
                "profile level 2 cpuNanos and execution statistics; "
                "temporary capped system.profile removed after run"
            ),
            "postgresql": (
                "EXPLAIN ANALYZE with TIMING OFF and matched SQL/index paths"
            ),
            "warning": (
                "MongoDB cpu_us is server CPU; PostgreSQL total_us is "
                "planning plus execution wall time and is not a direct CPU ratio"
            ),
        },
        "validation": {
            "all_outputs_match": True,
            "checks": checks,
        },
        "summaries": summaries,
        "derived": derived,
        "representative_plans": {
            "mongodb": representative_mongo,
            "postgresql": representative_pg,
        },
        "samples": {
            "mongodb": samples,
            "postgresql": pg_samples,
        },
        "versions": {
            "mongodb": mongo_client.server_info()["version"],
            "postgresql": pg.execute("SHOW server_version").fetchone()[0],
        },
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(json.dumps({
        "summaries": summaries,
        "derived": derived,
    }, indent=2))
    pg.close()
    mongo_client.close()


if __name__ == "__main__":
    main()
