#!/usr/bin/env python3
"""Verify MongoDB/PostgreSQL relative slowdowns and isolate their source.

The comparison uses the same materialized-path node layout and the same logical
outputs as bench_all_ops_layouts.py.  All four reads are timed to establish the
ranking.  The three short reads additionally use two controls:

* id_only: keep the lookup/range and ordering, but return only node IDs;
* count: keep the lookup/range, but return only a scalar count.

The controls distinguish fixed request/index overhead from document/heap fetch
and payload decoding.  Every timed result is fully consumed and checked across
the two engines before measurement.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import random
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


MONGO_NODES = "layout2_view"
MONGO_TEXT = "layout_shared_text"
MONGO_SUBTREE_INDEX = "layout2_rootcause_exact_cover"
PG_NODES = "layout2_pg_view"
PG_TEXT = "layout_shared_pg_text"
TREE_ID = "base"

OPERATIONS = ("get_node", "get_children", "get_subtree", "get_entity")
SHORT_OPERATIONS = ("get_node", "get_children", "get_entity")
VARIANTS = ("full", "id_only", "count")
ENGINES = ("mongodb", "postgresql")

Rows = list[tuple[Any, ...]]
Query = Callable[[str], Rows]


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


def fingerprint(rows: Sequence[Sequence[Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        for value in row:
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def load_tree_inputs(path: Path, limit: int) -> list[str]:
    document = json.loads(path.read_text())
    samples = document["samples"][:limit]
    return [sample["path"].rsplit("/", 1)[-1] for sample in samples]


def mongo_queries(database: Any) -> dict[str, dict[str, Query]]:
    nodes = database[MONGO_NODES]
    text = database[MONGO_TEXT]

    def node_full(node_id: str) -> Rows:
        row = nodes.find_one(
            {"tree_id": TREE_ID, "node_id": node_id},
            {
                "_id": 0,
                "node_id": 1,
                "parent_id": 1,
                "depth": 1,
                "title": 1,
                "summary": 1,
                "start_index": 1,
                "end_index": 1,
            },
            hint="allops_tree_node",
        )
        if row is None:
            return []
        return normalize([(
            row.get("node_id"),
            row.get("parent_id"),
            row.get("depth"),
            row.get("title"),
            row.get("summary"),
            row.get("start_index"),
            row.get("end_index"),
        )])

    def node_id_only(node_id: str) -> Rows:
        row = nodes.find_one(
            {"tree_id": TREE_ID, "node_id": node_id},
            {"_id": 0, "node_id": 1},
            hint="allops_tree_node",
        )
        return normalize([(row.get("node_id"),)]) if row else []

    def node_count(node_id: str) -> Rows:
        result = database.command(
            "count",
            MONGO_NODES,
            query={"tree_id": TREE_ID, "node_id": node_id},
            hint="allops_tree_node",
            limit=1,
        )
        return [(int(result["n"]),)]

    def children_full(node_id: str) -> Rows:
        cursor = (
            nodes.find(
                {"tree_id": TREE_ID, "parent_id": node_id},
                {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
            )
            .sort([("path", 1), ("node_id", 1)])
            .hint("allops_tree_parent_path")
        )
        return normalize(
            (row.get("node_id"), row.get("title"), row.get("summary"))
            for row in cursor
        )

    def children_id_only(node_id: str) -> Rows:
        cursor = (
            nodes.find(
                {"tree_id": TREE_ID, "parent_id": node_id},
                {"_id": 0, "node_id": 1},
            )
            .sort([("path", 1), ("node_id", 1)])
            .hint("allops_tree_parent_path")
        )
        return normalize((row.get("node_id"),) for row in cursor)

    def children_count(node_id: str) -> Rows:
        result = database.command(
            "count",
            MONGO_NODES,
            query={"tree_id": TREE_ID, "parent_id": node_id},
            hint="allops_tree_parent_path",
        )
        return [(int(result["n"]),)]

    def subtree_full(node_id: str) -> Rows:
        root = nodes.find_one(
            {"tree_id": TREE_ID, "node_id": node_id},
            {"_id": 0, "path": 1},
            hint="allops_tree_node",
        )
        if root is None:
            return []
        lower, upper = root["path"] + "/", root["path"] + "0"
        cursor = (
            nodes.find(
                {"path": {"$gte": lower, "$lt": upper}},
                {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
            )
            .sort([("path", 1), ("node_id", 1)])
            .hint(MONGO_SUBTREE_INDEX)
        )
        return normalize(
            (row.get("node_id"), row.get("title"), row.get("summary"))
            for row in cursor
        )

    def entity_full(node_id: str) -> Rows:
        row = text.find_one(
            {"_id": node_id},
            {"_id": 1, "text": 1},
        )
        return normalize([(row.get("_id"), row.get("text"))]) if row else []

    def entity_id_only(node_id: str) -> Rows:
        row = text.find_one(
            {"_id": node_id},
            {"_id": 1},
        )
        return normalize([(row.get("_id"),)]) if row else []

    def entity_count(node_id: str) -> Rows:
        result = database.command(
            "count",
            MONGO_TEXT,
            query={"_id": node_id},
            limit=1,
        )
        return [(int(result["n"]),)]

    return {
        "get_node": {
            "full": node_full,
            "id_only": node_id_only,
            "count": node_count,
        },
        "get_children": {
            "full": children_full,
            "id_only": children_id_only,
            "count": children_count,
        },
        "get_subtree": {"full": subtree_full},
        "get_entity": {
            "full": entity_full,
            "id_only": entity_id_only,
            "count": entity_count,
        },
    }


def postgres_queries(connection: Any) -> dict[str, dict[str, Query]]:
    def node_full(node_id: str) -> Rows:
        return normalize(connection.execute(
            f"""
            SELECT node_id,parent_id,depth,title,summary,start_index,end_index
            FROM {PG_NODES}
            WHERE tree_id=%s AND node_id=%s
            """,
            (TREE_ID, node_id),
        ).fetchall())

    def node_id_only(node_id: str) -> Rows:
        return normalize(connection.execute(
            f"""
            SELECT node_id
            FROM {PG_NODES}
            WHERE tree_id=%s AND node_id=%s
            """,
            (TREE_ID, node_id),
        ).fetchall())

    def node_count(node_id: str) -> Rows:
        return normalize(connection.execute(
            f"""
            SELECT count(*)
            FROM {PG_NODES}
            WHERE tree_id=%s AND node_id=%s
            """,
            (TREE_ID, node_id),
        ).fetchall())

    def children_full(node_id: str) -> Rows:
        return normalize(connection.execute(
            f"""
            SELECT node_id,title,summary
            FROM {PG_NODES}
            WHERE tree_id=%s AND parent_id=%s
            ORDER BY path,node_id
            """,
            (TREE_ID, node_id),
        ).fetchall())

    def children_id_only(node_id: str) -> Rows:
        return normalize(connection.execute(
            f"""
            SELECT node_id
            FROM {PG_NODES}
            WHERE tree_id=%s AND parent_id=%s
            ORDER BY path,node_id
            """,
            (TREE_ID, node_id),
        ).fetchall())

    def children_count(node_id: str) -> Rows:
        return normalize(connection.execute(
            f"""
            SELECT count(*)
            FROM {PG_NODES}
            WHERE tree_id=%s AND parent_id=%s
            """,
            (TREE_ID, node_id),
        ).fetchall())

    def subtree_full(node_id: str) -> Rows:
        root = connection.execute(
            f"""
            SELECT path
            FROM {PG_NODES}
            WHERE tree_id=%s AND node_id=%s
            """,
            (TREE_ID, node_id),
        ).fetchone()
        if root is None:
            return []
        lower, upper = root[0] + "/", root[0] + "0"
        return normalize(connection.execute(
            f"""
            SELECT node_id,title,summary
            FROM {PG_NODES}
            WHERE tree_id=%s AND path>=%s AND path<%s
            ORDER BY path,node_id
            """,
            (TREE_ID, lower, upper),
        ).fetchall())

    def entity_full(node_id: str) -> Rows:
        return normalize(connection.execute(
            f"SELECT node_id,text FROM {PG_TEXT} WHERE node_id=%s",
            (node_id,),
        ).fetchall())

    def entity_id_only(node_id: str) -> Rows:
        return normalize(connection.execute(
            f"SELECT node_id FROM {PG_TEXT} WHERE node_id=%s",
            (node_id,),
        ).fetchall())

    def entity_count(node_id: str) -> Rows:
        return normalize(connection.execute(
            f"SELECT count(*) FROM {PG_TEXT} WHERE node_id=%s",
            (node_id,),
        ).fetchall())

    return {
        "get_node": {
            "full": node_full,
            "id_only": node_id_only,
            "count": node_count,
        },
        "get_children": {
            "full": children_full,
            "id_only": children_id_only,
            "count": children_count,
        },
        "get_subtree": {"full": subtree_full},
        "get_entity": {
            "full": entity_full,
            "id_only": entity_id_only,
            "count": entity_count,
        },
    }


def summarize_samples(samples: list[dict[str, Any]], engine: str) -> dict[str, Any]:
    per_input = [
        statistics.median(sample["times_ms"][engine])
        for sample in samples
    ]
    return {
        "inputs": len(samples),
        "repeats": len(samples[0]["times_ms"][engine]) if samples else 0,
        "p50_ms": percentile(per_input, 50),
        "p95_ms": percentile(per_input, 95),
        "p99_ms": percentile(per_input, 99),
        "mean_ms": round(statistics.mean(per_input), 6) if per_input else 0.0,
    }


def paired_ratio(
    samples: list[dict[str, Any]],
    numerator: str,
    denominator: str,
) -> dict[str, float]:
    ratios = []
    deltas = []
    for sample in samples:
        left = statistics.median(sample["times_ms"][numerator])
        right = statistics.median(sample["times_ms"][denominator])
        if right > 0:
            ratios.append(left / right)
        deltas.append(left - right)
    return {
        "median_ratio": percentile(ratios, 50),
        "p95_ratio": percentile(ratios, 95),
        "median_delta_ms": percentile(deltas, 50),
        "p95_delta_ms": percentile(deltas, 95),
    }


def save(path: Path, output: dict[str, Any]) -> None:
    path.write_text(json.dumps(output, indent=2))


def validate_and_warm(
    queries: dict[str, dict[str, dict[str, Query]]],
    inputs: dict[str, list[str]],
) -> dict[str, Any]:
    validation: dict[str, Any] = {
        "all_cross_engine_outputs_match": True,
        "count_matches_id_only_rows": True,
        "checks": 0,
    }
    for operation in OPERATIONS:
        variants = VARIANTS if operation in SHORT_OPERATIONS else ("full",)
        log(f"validating and warming {operation}")
        for input_index, node_id in enumerate(inputs[operation]):
            results: dict[str, dict[str, Rows]] = {}
            for engine in ENGINES:
                results[engine] = {
                    variant: queries[engine][operation][variant](node_id)
                    for variant in variants
                }
            for variant in variants:
                left = results["mongodb"][variant]
                right = results["postgresql"][variant]
                if fingerprint(left) != fingerprint(right):
                    raise RuntimeError(
                        f"output mismatch: {operation} {variant} {node_id}"
                    )
                validation["checks"] += 1
            if operation in SHORT_OPERATIONS:
                expected_count = len(results["postgresql"]["id_only"])
                for engine in ENGINES:
                    count = int(results[engine]["count"][0][0])
                    if count != expected_count:
                        raise RuntimeError(
                            f"count mismatch: {operation} {engine} {node_id}"
                        )
            if (input_index + 1) % 100 == 0:
                log(f"  warmed {input_index + 1}/{len(inputs[operation])}")
    return validation


def benchmark_operation(
    operation: str,
    variants: tuple[str, ...],
    node_ids: list[str],
    repeats: int,
    queries: dict[str, dict[str, dict[str, Query]]],
    seed: int,
    output: dict[str, Any],
    out_path: Path,
) -> None:
    operation_output: dict[str, list[dict[str, Any]]] = {
        variant: [
            {
                "node_id": node_id,
                "times_ms": {engine: [] for engine in ENGINES},
            }
            for node_id in node_ids
        ]
        for variant in variants
    }
    output["samples"][operation] = operation_output
    log(
        f"timing {operation}: {len(node_ids)} inputs x {repeats} repeats x "
        f"{len(variants)} variants x 2 engines"
    )
    for repeat in range(repeats):
        order = list(range(len(node_ids)))
        random.Random(seed + repeat).shuffle(order)
        for position, input_index in enumerate(order):
            rotation = (repeat + position) % len(variants)
            variant_order = variants[rotation:] + variants[:rotation]
            for variant_index, variant in enumerate(variant_order):
                engine_order = (
                    ENGINES
                    if (repeat + position + variant_index) % 2 == 0
                    else tuple(reversed(ENGINES))
                )
                sample = operation_output[variant][input_index]
                for engine in engine_order:
                    gc.disable()
                    try:
                        started = time.perf_counter_ns()
                        rows = queries[engine][operation][variant](
                            sample["node_id"]
                        )
                        elapsed_ms = (
                            time.perf_counter_ns() - started
                        ) / 1_000_000
                    finally:
                        gc.enable()
                    sample["times_ms"][engine].append(
                        round(elapsed_ms, 6)
                    )
                    del rows
        log(f"  completed repeat {repeat + 1}/{repeats}")
        save(out_path, output)


def request_floor(
    mongo: Any,
    pg: Any,
    repeats: int,
) -> dict[str, Any]:
    samples = {engine: [] for engine in ENGINES}
    for index in range(repeats):
        engine_order = ENGINES if index % 2 == 0 else tuple(reversed(ENGINES))
        for engine in engine_order:
            gc.disable()
            try:
                started = time.perf_counter_ns()
                if engine == "mongodb":
                    mongo.command("ping")
                else:
                    pg.execute("SELECT 1").fetchone()
                elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            finally:
                gc.enable()
            samples[engine].append(round(elapsed_ms, 6))
    summaries = {
        engine: {
            "repeats": repeats,
            "p50_ms": percentile(samples[engine], 50),
            "p95_ms": percentile(samples[engine], 95),
            "mean_ms": round(statistics.mean(samples[engine]), 6),
        }
        for engine in ENGINES
    }
    summaries["comparison"] = {
        "p50_ratio": round(
            summaries["mongodb"]["p50_ms"]
            / summaries["postgresql"]["p50_ms"],
            6,
        ),
        "p95_ratio": round(
            summaries["mongodb"]["p95_ms"]
            / summaries["postgresql"]["p95_ms"],
            6,
        ),
    }
    return {"summaries": summaries, "samples_ms": samples}


def mongo_plan_metrics(database: Any, command: dict[str, Any]) -> dict[str, Any]:
    explain = database.command("explain", command, verbosity="executionStats")
    stats = explain["executionStats"]
    return {
        "execution_time_ms": stats.get("executionTimeMillis"),
        "keys_examined": stats.get("totalKeysExamined"),
        "documents_examined": stats.get("totalDocsExamined"),
        "n_returned": stats.get("nReturned"),
        "winning_plan": explain.get("queryPlanner", {}).get(
            "winningPlan", {}
        ).get("stage"),
    }


def postgres_plan_metrics(
    connection: Any,
    sql: str,
    params: tuple[Any, ...],
) -> dict[str, Any]:
    result = connection.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
        params,
    ).fetchone()[0][0]
    nodes: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        nodes.append(node)
        for child in node.get("Plans", []):
            visit(child)

    visit(result["Plan"])
    return {
        "planning_time_ms": result.get("Planning Time"),
        "execution_time_ms": result.get("Execution Time"),
        "node_types": [node.get("Node Type") for node in nodes],
        "actual_rows": result["Plan"].get("Actual Rows"),
        "heap_fetches": sum(node.get("Heap Fetches", 0) for node in nodes),
        "shared_hit_blocks": sum(
            node.get("Shared Hit Blocks", 0) for node in nodes
        ),
        "shared_read_blocks": sum(
            node.get("Shared Read Blocks", 0) for node in nodes
        ),
    }


def collect_plans(
    mongo: Any,
    pg: Any,
    point_id: str,
    child_id: str,
) -> dict[str, Any]:
    mongo_commands = {
        "get_node": {
            "full": {
                "find": MONGO_NODES,
                "filter": {"tree_id": TREE_ID, "node_id": point_id},
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
                "hint": "allops_tree_node",
                "limit": 1,
                "singleBatch": True,
            },
            "id_only": {
                "find": MONGO_NODES,
                "filter": {"tree_id": TREE_ID, "node_id": point_id},
                "projection": {"_id": 0, "node_id": 1},
                "hint": "allops_tree_node",
                "limit": 1,
                "singleBatch": True,
            },
            "count": {
                "count": MONGO_NODES,
                "query": {"tree_id": TREE_ID, "node_id": point_id},
                "hint": "allops_tree_node",
                "limit": 1,
            },
        },
        "get_children": {
            "full": {
                "find": MONGO_NODES,
                "filter": {"tree_id": TREE_ID, "parent_id": child_id},
                "projection": {
                    "_id": 0,
                    "node_id": 1,
                    "title": 1,
                    "summary": 1,
                },
                "sort": {"path": 1, "node_id": 1},
                "hint": "allops_tree_parent_path",
            },
            "id_only": {
                "find": MONGO_NODES,
                "filter": {"tree_id": TREE_ID, "parent_id": child_id},
                "projection": {"_id": 0, "node_id": 1},
                "sort": {"path": 1, "node_id": 1},
                "hint": "allops_tree_parent_path",
            },
            "count": {
                "count": MONGO_NODES,
                "query": {"tree_id": TREE_ID, "parent_id": child_id},
                "hint": "allops_tree_parent_path",
            },
        },
        "get_entity": {
            "full": {
                "find": MONGO_TEXT,
                "filter": {"_id": point_id},
                "projection": {"_id": 1, "text": 1},
                "limit": 1,
                "singleBatch": True,
            },
            "id_only": {
                "find": MONGO_TEXT,
                "filter": {"_id": point_id},
                "projection": {"_id": 1},
                "limit": 1,
                "singleBatch": True,
            },
            "count": {
                "count": MONGO_TEXT,
                "query": {"_id": point_id},
                "limit": 1,
            },
        },
    }
    pg_queries = {
        "get_node": {
            "full": (
                f"""
                SELECT node_id,parent_id,depth,title,summary,start_index,end_index
                FROM {PG_NODES}
                WHERE tree_id=%s AND node_id=%s
                """,
                (TREE_ID, point_id),
            ),
            "id_only": (
                f"""
                SELECT node_id FROM {PG_NODES}
                WHERE tree_id=%s AND node_id=%s
                """,
                (TREE_ID, point_id),
            ),
            "count": (
                f"""
                SELECT count(*) FROM {PG_NODES}
                WHERE tree_id=%s AND node_id=%s
                """,
                (TREE_ID, point_id),
            ),
        },
        "get_children": {
            "full": (
                f"""
                SELECT node_id,title,summary FROM {PG_NODES}
                WHERE tree_id=%s AND parent_id=%s
                ORDER BY path,node_id
                """,
                (TREE_ID, child_id),
            ),
            "id_only": (
                f"""
                SELECT node_id FROM {PG_NODES}
                WHERE tree_id=%s AND parent_id=%s
                ORDER BY path,node_id
                """,
                (TREE_ID, child_id),
            ),
            "count": (
                f"""
                SELECT count(*) FROM {PG_NODES}
                WHERE tree_id=%s AND parent_id=%s
                """,
                (TREE_ID, child_id),
            ),
        },
        "get_entity": {
            "full": (
                f"SELECT node_id,text FROM {PG_TEXT} WHERE node_id=%s",
                (point_id,),
            ),
            "id_only": (
                f"SELECT node_id FROM {PG_TEXT} WHERE node_id=%s",
                (point_id,),
            ),
            "count": (
                f"SELECT count(*) FROM {PG_TEXT} WHERE node_id=%s",
                (point_id,),
            ),
        },
    }
    plans: dict[str, Any] = {}
    for operation in SHORT_OPERATIONS:
        plans[operation] = {"mongodb": {}, "postgresql": {}}
        for variant in VARIANTS:
            plans[operation]["mongodb"][variant] = mongo_plan_metrics(
                mongo,
                mongo_commands[operation][variant],
            )
            sql, params = pg_queries[operation][variant]
            plans[operation]["postgresql"][variant] = (
                postgres_plan_metrics(pg, sql, params)
            )
    return plans


def derive_results(output: dict[str, Any]) -> None:
    summaries: dict[str, Any] = {}
    comparisons: dict[str, Any] = {}
    for operation, by_variant in output["samples"].items():
        summaries[operation] = {}
        comparisons[operation] = {}
        for variant, samples in by_variant.items():
            summaries[operation][variant] = {
                engine: summarize_samples(samples, engine)
                for engine in ENGINES
            }
            mongo_summary = summaries[operation][variant]["mongodb"]
            pg_summary = summaries[operation][variant]["postgresql"]
            comparisons[operation][variant] = {
                "p50_ratio": round(
                    mongo_summary["p50_ms"] / pg_summary["p50_ms"], 6
                ),
                "p95_ratio": round(
                    mongo_summary["p95_ms"] / pg_summary["p95_ms"], 6
                ),
                "p50_delta_ms": round(
                    mongo_summary["p50_ms"] - pg_summary["p50_ms"], 6
                ),
                "p95_delta_ms": round(
                    mongo_summary["p95_ms"] - pg_summary["p95_ms"], 6
                ),
                "paired": paired_ratio(
                    samples, "mongodb", "postgresql"
                ),
            }
    output["summaries"] = summaries
    output["comparisons"] = comparisons
    output["full_output_relative_ranking_p50"] = sorted(
        [
            {
                "operation": operation,
                **comparisons[operation]["full"],
            }
            for operation in OPERATIONS
        ],
        key=lambda item: item["p50_ratio"],
        reverse=True,
    )
    output["full_output_relative_ranking_p95"] = sorted(
        [
            {
                "operation": operation,
                **comparisons[operation]["full"],
            }
            for operation in OPERATIONS
        ],
        key=lambda item: item["p95_ratio"],
        reverse=True,
    )


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
            "bench/db/runs/relative_slowdown_20260724/"
            "mongo_postgres_rootcause_10m.json"
        ),
    )
    parser.add_argument("--point-inputs", type=int, default=500)
    parser.add_argument("--tree-inputs", type=int, default=200)
    parser.add_argument("--short-repeats", type=int, default=15)
    parser.add_argument("--subtree-repeats", type=int, default=5)
    parser.add_argument("--floor-repeats", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260723)
    args = parser.parse_args()
    if min(
        args.point_inputs,
        args.tree_inputs,
        args.short_repeats,
        args.subtree_repeats,
        args.floor_repeats,
    ) <= 0:
        parser.error("input and repeat counts must be positive")

    from pymongo import MongoClient
    import psycopg

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mongo_client = MongoClient(args.mongo_uri)
    mongo = mongo_client[args.mongo_db]
    pg = psycopg.connect(args.pg_dsn, autocommit=True)

    point_ids = [
        row[0]
        for row in pg.execute(
            f"SELECT node_id FROM {PG_TEXT} ORDER BY node_id LIMIT %s",
            (args.point_inputs,),
        ).fetchall()
    ]
    tree_ids = load_tree_inputs(Path(args.expected), args.tree_inputs)
    inputs = {
        "get_node": point_ids,
        "get_children": tree_ids,
        "get_subtree": tree_ids,
        "get_entity": point_ids,
    }
    if len(point_ids) != args.point_inputs:
        raise RuntimeError(f"only found {len(point_ids)} point inputs")
    if len(tree_ids) != args.tree_inputs:
        raise RuntimeError(f"only found {len(tree_ids)} tree inputs")

    queries = {
        "mongodb": mongo_queries(mongo),
        "postgresql": postgres_queries(pg),
    }
    output: dict[str, Any] = {
        "run": {
            "status": "validating",
            "started_unix_s": time.time(),
            "short_repeats": args.short_repeats,
            "subtree_repeats": args.subtree_repeats,
            "floor_repeats": args.floor_repeats,
            "seed": args.seed,
        },
        "contract": {
            "comparison": (
                "MongoDB versus PostgreSQL using the matched "
                "materialized-path layout"
            ),
            "tree_id": TREE_ID,
            "operations": list(OPERATIONS),
            "controls": {
                "full": "original logical output, fully materialized",
                "id_only": "same lookup/range and order, IDs only",
                "count": "same lookup/range, scalar count only",
            },
            "summary_unit": (
                "per-input median across repeats, then percentile "
                "across inputs"
            ),
            "engine_order": "alternated within each input and variant",
            "input_order": "deterministically shuffled per repeat",
        },
        "sources": {
            "expected": args.expected,
            "mongodb_nodes": MONGO_NODES,
            "mongodb_text": MONGO_TEXT,
            "postgresql_nodes": PG_NODES,
            "postgresql_text": PG_TEXT,
        },
        "inputs": {
            operation: len(values) for operation, values in inputs.items()
        },
        "samples": {},
    }
    save(out_path, output)

    output["validation"] = validate_and_warm(queries, inputs)
    output["run"]["status"] = "timing"
    save(out_path, output)
    for operation in SHORT_OPERATIONS:
        benchmark_operation(
            operation,
            VARIANTS,
            inputs[operation],
            args.short_repeats,
            queries,
            args.seed + OPERATIONS.index(operation) * 1000,
            output,
            out_path,
        )
    benchmark_operation(
        "get_subtree",
        ("full",),
        inputs["get_subtree"],
        args.subtree_repeats,
        queries,
        args.seed + 3000,
        output,
        out_path,
    )

    log("timing request-floor controls")
    output["request_floor"] = request_floor(
        mongo, pg, args.floor_repeats
    )
    log("collecting representative execution plans")
    output["plans"] = collect_plans(
        mongo,
        pg,
        point_ids[0],
        tree_ids[0],
    )
    derive_results(output)
    output["versions"] = {
        "mongodb": mongo_client.server_info()["version"],
        "postgresql": pg.execute("SHOW server_version").fetchone()[0],
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
    }
    output["run"]["status"] = "complete"
    output["run"]["finished_unix_s"] = time.time()
    output["run"]["elapsed_s"] = round(
        output["run"]["finished_unix_s"]
        - output["run"]["started_unix_s"],
        3,
    )
    save(out_path, output)

    print(json.dumps({
        "ranking_p50": output["full_output_relative_ranking_p50"],
        "ranking_p95": output["full_output_relative_ranking_p95"],
        "request_floor": output["request_floor"]["summaries"],
        "controls": {
            operation: output["comparisons"][operation]
            for operation in SHORT_OPERATIONS
        },
    }, indent=2))
    pg.close()
    mongo_client.close()


if __name__ == "__main__":
    main()
