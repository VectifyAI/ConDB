#!/usr/bin/env python3
"""Measure where MongoDB spends time in the three short read operations.

For each operation, the benchmark compares direct raw commands for count,
ID-only, and full output with the high-level PyMongo helper used by the main
benchmark.  PyMongo command monitoring separates time inside the command
round-trip boundary from Python work before and after that boundary.
"""

from __future__ import annotations

import argparse
import gc
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from pymongo import MongoClient, monitoring


TREE_ID = "base"
MONGO_NODES = "layout2_view"
MONGO_TEXT = "layout_shared_text"
OPERATIONS = ("get_children", "get_node", "get_entity")
VARIANTS = ("count", "id_only", "raw_full", "driver_full")

Query = Callable[[str], list[tuple[Any, ...]]]


def log(message: str) -> None:
    print(message, flush=True)


def percentile(values: Sequence[float], pct: int) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return round(ordered[index], 6)


def normalize(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [
        tuple("" if value is None else value for value in row)
        for row in rows
    ]


class DurationListener(monitoring.CommandListener):
    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []

    def started(self, event: Any) -> None:
        del event

    def succeeded(self, event: Any) -> None:
        self.events.append((event.command_name, event.duration_micros))

    def failed(self, event: Any) -> None:
        raise RuntimeError(f"MongoDB command failed: {event.failure}")

    def reset(self) -> None:
        self.events.clear()


def command_rows(database: Any, command: dict[str, Any]) -> list[dict[str, Any]]:
    return database.command(command)["cursor"]["firstBatch"]


def build_queries(database: Any) -> dict[str, dict[str, Query]]:
    nodes = database[MONGO_NODES]
    text = database[MONGO_TEXT]
    node_full_projection = {
        "_id": 0,
        "node_id": 1,
        "parent_id": 1,
        "depth": 1,
        "title": 1,
        "summary": 1,
        "start_index": 1,
        "end_index": 1,
    }
    child_full_projection = {
        "_id": 0,
        "node_id": 1,
        "title": 1,
        "summary": 1,
    }

    def node_count(node_id: str) -> list[tuple[Any, ...]]:
        result = database.command({
            "count": MONGO_NODES,
            "query": {"tree_id": TREE_ID, "node_id": node_id},
            "hint": "allops_tree_node",
            "limit": 1,
        })
        return [(int(result["n"]),)]

    def node_id_only(node_id: str) -> list[tuple[Any, ...]]:
        rows = command_rows(database, {
            "find": MONGO_NODES,
            "filter": {"tree_id": TREE_ID, "node_id": node_id},
            "projection": {"_id": 0, "node_id": 1},
            "hint": "allops_tree_node",
            "limit": 1,
            "singleBatch": True,
        })
        return normalize([(row.get("node_id"),) for row in rows])

    def node_raw_full(node_id: str) -> list[tuple[Any, ...]]:
        rows = command_rows(database, {
            "find": MONGO_NODES,
            "filter": {"tree_id": TREE_ID, "node_id": node_id},
            "projection": node_full_projection,
            "hint": "allops_tree_node",
            "limit": 1,
            "singleBatch": True,
        })
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

    def node_driver_full(node_id: str) -> list[tuple[Any, ...]]:
        row = nodes.find_one(
            {"tree_id": TREE_ID, "node_id": node_id},
            node_full_projection,
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

    def children_count(node_id: str) -> list[tuple[Any, ...]]:
        result = database.command({
            "count": MONGO_NODES,
            "query": {"tree_id": TREE_ID, "parent_id": node_id},
            "hint": "allops_tree_parent_path",
        })
        return [(int(result["n"]),)]

    def children_id_only(node_id: str) -> list[tuple[Any, ...]]:
        rows = command_rows(database, {
            "find": MONGO_NODES,
            "filter": {"tree_id": TREE_ID, "parent_id": node_id},
            "projection": {"_id": 0, "node_id": 1},
            "sort": {"path": 1, "node_id": 1},
            "hint": "allops_tree_parent_path",
        })
        return normalize([(row.get("node_id"),) for row in rows])

    def children_raw_full(node_id: str) -> list[tuple[Any, ...]]:
        rows = command_rows(database, {
            "find": MONGO_NODES,
            "filter": {"tree_id": TREE_ID, "parent_id": node_id},
            "projection": child_full_projection,
            "sort": {"path": 1, "node_id": 1},
            "hint": "allops_tree_parent_path",
        })
        return normalize([
            (row.get("node_id"), row.get("title"), row.get("summary"))
            for row in rows
        ])

    def children_driver_full(node_id: str) -> list[tuple[Any, ...]]:
        cursor = (
            nodes.find(
                {"tree_id": TREE_ID, "parent_id": node_id},
                child_full_projection,
            )
            .sort([("path", 1), ("node_id", 1)])
            .hint("allops_tree_parent_path")
        )
        return normalize([
            (row.get("node_id"), row.get("title"), row.get("summary"))
            for row in cursor
        ])

    def entity_count(node_id: str) -> list[tuple[Any, ...]]:
        result = database.command({
            "count": MONGO_TEXT,
            "query": {"_id": node_id},
            "limit": 1,
        })
        return [(int(result["n"]),)]

    def entity_id_only(node_id: str) -> list[tuple[Any, ...]]:
        rows = command_rows(database, {
            "find": MONGO_TEXT,
            "filter": {"_id": node_id},
            "projection": {"_id": 1},
            "limit": 1,
            "singleBatch": True,
        })
        return normalize([(row.get("_id"),) for row in rows])

    def entity_raw_full(node_id: str) -> list[tuple[Any, ...]]:
        rows = command_rows(database, {
            "find": MONGO_TEXT,
            "filter": {"_id": node_id},
            "projection": {"_id": 1, "text": 1},
            "limit": 1,
            "singleBatch": True,
        })
        return normalize([
            (row.get("_id"), row.get("text")) for row in rows
        ])

    def entity_driver_full(node_id: str) -> list[tuple[Any, ...]]:
        row = text.find_one(
            {"_id": node_id},
            {"_id": 1, "text": 1},
        )
        return normalize([(row.get("_id"), row.get("text"))]) if row else []

    return {
        "get_node": {
            "count": node_count,
            "id_only": node_id_only,
            "raw_full": node_raw_full,
            "driver_full": node_driver_full,
        },
        "get_children": {
            "count": children_count,
            "id_only": children_id_only,
            "raw_full": children_raw_full,
            "driver_full": children_driver_full,
        },
        "get_entity": {
            "count": entity_count,
            "id_only": entity_id_only,
            "raw_full": entity_raw_full,
            "driver_full": entity_driver_full,
        },
    }


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in ("wall_ms", "command_ms", "outside_command_ms"):
        medians = [
            statistics.median(sample[field]) for sample in samples
        ]
        result[field] = {
            "p50": percentile(medians, 50),
            "p95": percentile(medians, 95),
            "mean": round(statistics.mean(medians), 6),
        }
    return result


def measure(
    listener: DurationListener,
    query: Query,
    node_id: str,
) -> tuple[float, float, float, list[tuple[Any, ...]], list[str]]:
    listener.reset()
    gc.disable()
    try:
        started = time.perf_counter_ns()
        rows = query(node_id)
        wall_ms = (time.perf_counter_ns() - started) / 1_000_000
    finally:
        gc.enable()
    command_ms = sum(duration for _, duration in listener.events) / 1_000
    outside_ms = wall_ms - command_ms
    return (
        round(wall_ms, 6),
        round(command_ms, 6),
        round(outside_ms, 6),
        rows,
        [name for name, _ in listener.events],
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
            "mongo_short_command_boundary_10m.json"
        ),
    )
    parser.add_argument("--point-inputs", type=int, default=500)
    parser.add_argument("--tree-inputs", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--ping-repeats", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260724)
    args = parser.parse_args()

    import psycopg

    listener = DurationListener()
    client = MongoClient(
        args.mongo_uri,
        event_listeners=[listener],
    )
    database = client[args.mongo_db]
    pg = psycopg.connect(args.pg_dsn, autocommit=True)
    point_ids = [
        row[0]
        for row in pg.execute(
            "SELECT node_id FROM layout_shared_pg_text "
            "ORDER BY node_id LIMIT %s",
            (args.point_inputs,),
        ).fetchall()
    ]
    expected = json.loads(Path(args.expected).read_text())
    tree_ids = [
        sample["path"].rsplit("/", 1)[-1]
        for sample in expected["samples"][:args.tree_inputs]
    ]
    inputs = {
        "get_children": tree_ids,
        "get_node": point_ids,
        "get_entity": point_ids,
    }
    queries = build_queries(database)

    log("validating and warming")
    validation_checks = 0
    for operation in OPERATIONS:
        for node_id in inputs[operation]:
            outputs = {
                variant: queries[operation][variant](node_id)
                for variant in VARIANTS
            }
            if outputs["raw_full"] != outputs["driver_full"]:
                raise RuntimeError(
                    f"raw/driver mismatch: {operation} {node_id}"
                )
            if int(outputs["count"][0][0]) != len(outputs["id_only"]):
                raise RuntimeError(
                    f"count/ID mismatch: {operation} {node_id}"
                )
            validation_checks += 2

    output: dict[str, Any] = {
        "run": {
            "status": "timing",
            "repeats": args.repeats,
            "ping_repeats": args.ping_repeats,
            "seed": args.seed,
        },
        "contract": {
            "command_ms": (
                "sum of PyMongo CommandSucceededEvent duration_micros"
            ),
            "outside_command_ms": "wall_ms minus command_ms",
            "raw_full": "direct find command with full output",
            "driver_full": "high-level PyMongo helper used by main benchmark",
        },
        "validation": {
            "raw_and_driver_outputs_match": True,
            "counts_match_id_rows": True,
            "checks": validation_checks,
        },
        "samples": {},
    }

    for operation_index, operation in enumerate(OPERATIONS):
        log(f"timing {operation}")
        by_variant = {
            variant: [
                {
                    "node_id": node_id,
                    "wall_ms": [],
                    "command_ms": [],
                    "outside_command_ms": [],
                    "command_names": [],
                }
                for node_id in inputs[operation]
            ]
            for variant in VARIANTS
        }
        output["samples"][operation] = by_variant
        for repeat in range(args.repeats):
            order = list(range(len(inputs[operation])))
            random.Random(
                args.seed + operation_index * 1000 + repeat
            ).shuffle(order)
            for position, input_index in enumerate(order):
                rotation = (repeat + position) % len(VARIANTS)
                variant_order = VARIANTS[rotation:] + VARIANTS[:rotation]
                for variant in variant_order:
                    sample = by_variant[variant][input_index]
                    wall, command, outside, _, names = measure(
                        listener,
                        queries[operation][variant],
                        sample["node_id"],
                    )
                    if len(names) != 1:
                        raise RuntimeError(
                            f"expected one command, got {names}"
                        )
                    sample["wall_ms"].append(wall)
                    sample["command_ms"].append(command)
                    sample["outside_command_ms"].append(outside)
                    sample["command_names"].append(names[0])
            log(f"  repeat {repeat + 1}/{args.repeats}")

    log("timing ping")
    ping_samples = {
        "wall_ms": [],
        "command_ms": [],
        "outside_command_ms": [],
    }
    for _ in range(args.ping_repeats):
        wall, command, outside, _, names = measure(
            listener,
            lambda ignored: [tuple(database.command("ping").items())],
            "",
        )
        if names != ["ping"]:
            raise RuntimeError(f"unexpected ping commands: {names}")
        ping_samples["wall_ms"].append(wall)
        ping_samples["command_ms"].append(command)
        ping_samples["outside_command_ms"].append(outside)

    output["summaries"] = {
        operation: {
            variant: summarize(samples)
            for variant, samples in by_variant.items()
        }
        for operation, by_variant in output["samples"].items()
    }
    output["ping"] = {
        field: {
            "p50": percentile(values, 50),
            "p95": percentile(values, 95),
            "mean": round(statistics.mean(values), 6),
        }
        for field, values in ping_samples.items()
    }
    output["derived"] = {}
    for operation in OPERATIONS:
        summary = output["summaries"][operation]
        raw_wall = summary["raw_full"]["wall_ms"]["p50"]
        driver_wall = summary["driver_full"]["wall_ms"]["p50"]
        raw_command = summary["raw_full"]["command_ms"]["p50"]
        output["derived"][operation] = {
            "driver_wrapper_ms": round(driver_wall - raw_wall, 6),
            "raw_full_inside_command_fraction": round(
                raw_command / raw_wall, 6
            ),
            "raw_full_minus_id_wall_ms": round(
                raw_wall - summary["id_only"]["wall_ms"]["p50"], 6
            ),
            "id_minus_count_wall_ms": round(
                summary["id_only"]["wall_ms"]["p50"]
                - summary["count"]["wall_ms"]["p50"],
                6,
            ),
            "count_minus_ping_wall_ms": round(
                summary["count"]["wall_ms"]["p50"]
                - output["ping"]["wall_ms"]["p50"],
                6,
            ),
        }
    output["versions"] = {
        "mongodb": client.server_info()["version"],
        "pymongo": __import__("pymongo").version,
    }
    output["run"]["status"] = "complete"
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(json.dumps({
        "ping": output["ping"],
        "summaries": output["summaries"],
        "derived": output["derived"],
    }, indent=2))
    pg.close()
    client.close()


if __name__ == "__main__":
    main()
