#!/usr/bin/env python3
"""Causal benchmark for MongoDB/PostgreSQL ``get_entity`` point reads.

The benchmark has two independent parts.

Real-store experiment
---------------------
The existing 9M-row text collection/table are read only.  Deterministic hits
are spread across the primary-key range, and guaranteed misses use a
run-specific prefix.  The experiment separates:

* an empty request (MongoDB ping / parameterized PostgreSQL scalar select);
* an index-only lookup;
* a lookup that fetches the document/heap row but returns only the ID;
* the full ``(node_id, text)`` output;
* MongoDB's raw ``find`` command and high-level ``find_one`` wrapper;
* PostgreSQL with preparation disabled and preparation enabled immediately.

MongoDB's normal equality lookup must use IDHACK.  A logically equivalent
closed ``_id`` range is also measured only when explain confirms a covered
``_id_`` index scan with zero examined documents.  PostgreSQL plan gates
separately verify index-only and heap-fetching arms.

Payload-size experiment
-----------------------
A small, run-specific collection/table stores identical deterministic,
incompressible ASCII payloads in both engines.  It measures 0--256 KiB
payloads by default.  The objects are created under unique names and removed
in ``finally``; the existing benchmark data are never modified.

Every output is validated before timing and fingerprinted in the JSON result.
Timed outputs are checked again after the timer stops.

The primary MongoDB/PostgreSQL comparison uses an unmonitored MongoClient.
Command-monitor timings come from a separate MongoDB-only phase and a second
client, so listener overhead cannot contaminate the cross-engine result.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import random
import re
import statistics
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pymongo import monitoring

MONGO_REAL_TEXT = "layout_shared_text"
PG_REAL_TEXT = "layout_shared_pg_text"
DEFAULT_PAYLOAD_SIZES = "0,1024,4096,16384,65536,262144"

Rows = list[tuple[Any, ...]]
Query = Callable[[str], Rows]


def log(message: str) -> None:
    print(message, flush=True)


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
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def percentile(values: Sequence[float], pct: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(
        len(ordered) - 1,
        round(pct / 100 * (len(ordered) - 1)),
    )
    return round(ordered[index], 6)


def metric_summary(values: Sequence[float]) -> dict[str, float]:
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "mean": round(statistics.mean(values), 6) if values else 0.0,
    }


def median(values: Sequence[float]) -> float:
    return float(statistics.median(values))


def save(path: Path, output: dict[str, Any]) -> None:
    path.write_text(json.dumps(output, indent=2, default=str))


def parse_payload_sizes(raw: str) -> list[int]:
    try:
        values = [int(value.strip()) for value in raw.split(",")]
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "payload sizes must be comma-separated integers"
        ) from error
    if not values or any(value < 0 for value in values):
        raise argparse.ArgumentTypeError(
            "payload sizes must contain non-negative integers"
        )
    values = sorted(set(values))
    if values[-1] > 8 * 1024 * 1024:
        raise argparse.ArgumentTypeError(
            "payload probe is capped at 8 MiB per document"
        )
    return values


def deterministic_ascii(size: int, seed: str) -> str:
    """Return deterministic high-entropy ASCII that resists compression."""
    if size == 0:
        return ""
    chunks: list[str] = []
    produced = 0
    counter = 0
    while produced < size:
        chunk = hashlib.sha256(
            f"{seed}:{counter}".encode()
        ).hexdigest()
        chunks.append(chunk)
        produced += len(chunk)
        counter += 1
    return "".join(chunks)[:size]


def safe_generated_name(prefix: str, run_tag: str) -> str:
    name = f"{prefix}_{run_tag}"
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise RuntimeError(f"unsafe generated database object name: {name}")
    return name


def collect_stage_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        stage = value.get("stage")
        if stage is not None:
            names.append(str(stage))
        for child in value.values():
            names.extend(collect_stage_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(collect_stage_names(child))
    return names


def mongo_explain(database: Any, command: dict[str, Any]) -> dict[str, Any]:
    explanation = database.command(
        "explain",
        command,
        verbosity="executionStats",
    )
    stats = explanation["executionStats"]
    stages = collect_stage_names({
        "planner": explanation.get("queryPlanner", {}).get("winningPlan", {}),
        "execution": stats.get("executionStages", {}),
    })
    return {
        "stages": sorted(set(stages)),
        "keys_examined": int(stats.get("totalKeysExamined", 0)),
        "documents_examined": int(stats.get("totalDocsExamined", 0)),
        "n_returned": int(stats.get("nReturned", 0)),
        "execution_time_ms": stats.get("executionTimeMillis"),
        "raw_winning_plan": explanation.get(
            "queryPlanner", {}
        ).get("winningPlan", {}),
    }


def pg_explain(
    connection: Any,
    sql: str,
    params: tuple[Any, ...],
) -> dict[str, Any]:
    result = connection.execute(
        "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, SUMMARY ON, FORMAT JSON) "
        + sql,
        params,
    ).fetchone()[0][0]
    nodes: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        nodes.append({
            "node_type": node.get("Node Type"),
            "index_name": node.get("Index Name"),
            "actual_rows": node.get("Actual Rows"),
            "actual_loops": node.get("Actual Loops"),
            "heap_fetches": node.get("Heap Fetches", 0),
            "shared_hit_blocks": node.get("Shared Hit Blocks", 0),
            "shared_read_blocks": node.get("Shared Read Blocks", 0),
        })
        for child in node.get("Plans", []):
            visit(child)

    visit(result["Plan"])
    return {
        "node_types": [node["node_type"] for node in nodes],
        "heap_fetches": sum(int(node["heap_fetches"] or 0) for node in nodes),
        "actual_rows": result["Plan"].get("Actual Rows"),
        "planning_time_ms": result.get("Planning Time"),
        "execution_time_ms": result.get("Execution Time"),
        "nodes": nodes,
    }


def require_gate(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"execution-plan gate failed: {message}")


def command_first_batch(
    database: Any,
    command: dict[str, Any],
) -> list[dict[str, Any]]:
    return list(database.command(command)["cursor"]["firstBatch"])


class DurationListener(monitoring.CommandListener):
    """Small PyMongo command listener installed lazily in ``main``."""

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


@dataclass(frozen=True)
class Variant:
    name: str
    engine: str
    contract: str
    query: Query
    monitor_commands: bool = False


def measure_variant(
    variant: Variant,
    node_id: str,
    listener: DurationListener | None,
) -> tuple[Rows, float, float | None, float | None, list[str]]:
    if variant.monitor_commands:
        if variant.engine != "mongodb":
            raise RuntimeError(
                f"command monitoring is MongoDB-only: {variant.name}"
            )
        if listener is None:
            raise RuntimeError(
                f"{variant.name} requires a MongoDB command listener"
            )
        listener.reset()
    gc.disable()
    try:
        started = time.perf_counter_ns()
        rows = variant.query(node_id)
        wall_ms = (time.perf_counter_ns() - started) / 1_000_000
    finally:
        gc.enable()
    if not variant.monitor_commands:
        return rows, round(wall_ms, 6), None, None, []
    assert listener is not None
    if len(listener.events) != 1:
        raise RuntimeError(
            f"{variant.name} emitted {len(listener.events)} commands: "
            f"{listener.events}"
        )
    command_ms = listener.events[0][1] / 1_000
    outside_ms = wall_ms - command_ms
    return (
        rows,
        round(wall_ms, 6),
        round(command_ms, 6),
        round(outside_ms, 6),
        [listener.events[0][0]],
    )


def validate_variants(
    items: Sequence[dict[str, Any]],
    variants: Sequence[Variant],
    listener: DurationListener | None,
) -> int:
    checks = 0
    for item in items:
        for variant in variants:
            rows, _, _, _, _ = measure_variant(
                variant,
                item["node_id"],
                listener,
            )
            actual = fingerprint(rows)
            expected = item["fingerprints"][variant.contract]
            if actual != expected:
                raise RuntimeError(
                    f"output mismatch: {variant.name} {item['node_id']} "
                    f"{actual} != {expected}"
                )
            checks += 1
    return checks


def benchmark_variants(
    items: Sequence[dict[str, Any]],
    variants: Sequence[Variant],
    repeats: int,
    seed: int,
    listener: DurationListener | None,
    progress: Callable[[], None],
) -> dict[str, list[dict[str, Any]]]:
    samples = {
        variant.name: [
            {
                "node_id": item["node_id"],
                "wall_ms": [],
                "command_ms": [],
                "outside_command_ms": [],
                "command_names": [],
            }
            for item in items
        ]
        for variant in variants
    }
    for repeat in range(repeats):
        item_order = list(range(len(items)))
        random.Random(seed + repeat).shuffle(item_order)
        for position, item_index in enumerate(item_order):
            rotation = (repeat + position) % len(variants)
            ordered_variants = (
                list(variants[rotation:]) + list(variants[:rotation])
            )
            item = items[item_index]
            for variant in ordered_variants:
                rows, wall, command, outside, names = measure_variant(
                    variant,
                    item["node_id"],
                    listener,
                )
                if (
                    fingerprint(rows)
                    != item["fingerprints"][variant.contract]
                ):
                    raise RuntimeError(
                        f"timed output mismatch: {variant.name} "
                        f"{item['node_id']}"
                    )
                sample = samples[variant.name][item_index]
                sample["wall_ms"].append(wall)
                if command is not None:
                    sample["command_ms"].append(command)
                    sample["outside_command_ms"].append(outside)
                    sample["command_names"].extend(names)
        progress()
    return samples


def summarize_samples(
    samples: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for variant, variant_samples in samples.items():
        output[variant] = {}
        for field in ("wall_ms", "command_ms", "outside_command_ms"):
            per_input = [
                median(sample[field])
                for sample in variant_samples
                if sample[field]
            ]
            if per_input:
                output[variant][field] = {
                    **metric_summary(per_input),
                    "inputs": len(per_input),
                    "repeats": len(
                        next(
                            sample[field]
                            for sample in variant_samples
                            if sample[field]
                        )
                    ),
                }
    return output


def paired_delta(
    samples: dict[str, list[dict[str, Any]]],
    left: str,
    right: str,
) -> dict[str, Any]:
    left_by_id = {
        sample["node_id"]: median(sample["wall_ms"])
        for sample in samples[left]
    }
    right_by_id = {
        sample["node_id"]: median(sample["wall_ms"])
        for sample in samples[right]
    }
    if left_by_id.keys() != right_by_id.keys():
        raise RuntimeError(f"paired sample mismatch: {left} vs {right}")
    deltas_us = [
        (left_by_id[node_id] - right_by_id[node_id]) * 1_000
        for node_id in left_by_id
    ]
    return {
        "left": left,
        "right": right,
        "unit": "us",
        **metric_summary(deltas_us),
    }


def benchmark_floor(
    variants: Sequence[Variant],
    repeats: int,
    seed: int,
    listener: DurationListener | None,
) -> dict[str, Any]:
    items = [
        {
            "node_id": "floor",
            "fingerprints": {
                variant.contract: fingerprint(variant.query("floor"))
                for variant in variants
            },
        }
    ]
    samples = benchmark_variants(
        items,
        variants,
        repeats,
        seed,
        listener,
        progress=lambda: None,
    )
    return {
        "samples": samples,
        "summaries": summarize_samples(samples),
    }


def numeric_stratified_hits(
    pg: Any,
    count: int,
    table: str,
) -> tuple[list[str], dict[str, Any]]:
    minimum, maximum, total = pg.execute(
        f"SELECT min(node_id), max(node_id), count(*) FROM {table}"
    ).fetchone()
    if total < count:
        raise RuntimeError(f"only {total} entity rows, need {count}")
    selected: list[str] = []
    seen: set[str] = set()
    method: dict[str, Any] = {
        "minimum": minimum,
        "maximum": maximum,
        "total": total,
    }
    if (
        isinstance(minimum, str)
        and isinstance(maximum, str)
        and minimum.isdigit()
        and maximum.isdigit()
    ):
        low = int(minimum)
        high = int(maximum)
        width = max(len(minimum), len(maximum))
        anchors = count
        for index in range(anchors):
            fraction = 0.5 if anchors == 1 else index / (anchors - 1)
            value = round(low + (high - low) * fraction)
            anchor = str(value).zfill(width)
            row = pg.execute(
                f"SELECT node_id FROM {table} "
                "WHERE node_id >= %s ORDER BY node_id LIMIT 1",
                (anchor,),
            ).fetchone()
            if row is not None and row[0] not in seen:
                seen.add(row[0])
                selected.append(row[0])
            if len(selected) == count:
                break
        method["method"] = "numeric primary-key anchors"
        method["anchor_count"] = anchors
    if len(selected) < count:
        needed = count - len(selected)
        rows = pg.execute(
            f"SELECT node_id FROM {table} "
            "TABLESAMPLE SYSTEM (1) REPEATABLE (20260724) "
            "ORDER BY md5(node_id) LIMIT %s",
            (max(needed * 4, count),),
        ).fetchall()
        for row in rows:
            if row[0] not in seen:
                seen.add(row[0])
                selected.append(row[0])
            if len(selected) == count:
                break
        method["fallback"] = "1 percent deterministic SYSTEM sample"
    if len(selected) != count:
        raise RuntimeError(
            f"stratified selection produced {len(selected)} IDs, need {count}"
        )
    method["selected"] = len(selected)
    return selected, method


def build_real_queries(
    mongo: Any,
    pg_prepared: Any,
    pg_unprepared: Any,
) -> tuple[dict[str, Query], dict[str, tuple[str, tuple[Any, ...]]]]:
    collection = mongo[MONGO_REAL_TEXT]

    def mongo_idhack_id_raw(node_id: str) -> Rows:
        rows = command_first_batch(mongo, {
            "find": MONGO_REAL_TEXT,
            "filter": {"_id": node_id},
            "projection": {"_id": 1},
            "limit": 1,
            "singleBatch": True,
        })
        return normalize((row.get("_id"),) for row in rows)

    def mongo_covered_id_raw(node_id: str) -> Rows:
        rows = command_first_batch(mongo, {
            "find": MONGO_REAL_TEXT,
            "filter": {"_id": {"$gte": node_id, "$lte": node_id}},
            "projection": {"_id": 1},
            "hint": "_id_",
            "limit": 1,
            "singleBatch": True,
        })
        return normalize((row.get("_id"),) for row in rows)

    def mongo_full_raw(node_id: str) -> Rows:
        rows = command_first_batch(mongo, {
            "find": MONGO_REAL_TEXT,
            "filter": {"_id": node_id},
            "projection": {"_id": 1, "text": 1},
            "limit": 1,
            "singleBatch": True,
        })
        return normalize(
            (row.get("_id"), row.get("text")) for row in rows
        )

    def mongo_full_driver(node_id: str) -> Rows:
        row = collection.find_one(
            {"_id": node_id},
            {"_id": 1, "text": 1},
        )
        return (
            normalize([(row.get("_id"), row.get("text"))])
            if row
            else []
        )

    id_sql = (
        f"SELECT node_id FROM {PG_REAL_TEXT} WHERE node_id=%s"
    )
    fetched_id_sql = (
        f"SELECT node_id FROM {PG_REAL_TEXT} "
        "WHERE node_id=%s AND text IS NOT NULL"
    )
    full_sql = (
        f"SELECT node_id,text FROM {PG_REAL_TEXT} WHERE node_id=%s"
    )

    def pg_query(connection: Any, sql: str) -> Query:
        return lambda node_id: normalize(
            connection.execute(sql, (node_id,)).fetchall()
        )

    queries = {
        "mongo_idhack_id_raw": mongo_idhack_id_raw,
        "mongo_covered_id_raw": mongo_covered_id_raw,
        "mongo_full_raw": mongo_full_raw,
        "mongo_full_driver": mongo_full_driver,
        "pg_prepared_id_index_only": pg_query(pg_prepared, id_sql),
        "pg_unprepared_id_index_only": pg_query(pg_unprepared, id_sql),
        "pg_prepared_id_fetched": pg_query(pg_prepared, fetched_id_sql),
        "pg_unprepared_id_fetched": pg_query(
            pg_unprepared, fetched_id_sql
        ),
        "pg_prepared_full": pg_query(pg_prepared, full_sql),
        "pg_unprepared_full": pg_query(pg_unprepared, full_sql),
    }
    sql = {
        "id_index_only": (id_sql, ("",)),
        "id_fetched": (fetched_id_sql, ("",)),
        "full": (full_sql, ("",)),
    }
    return queries, sql


def collect_real_plans(
    mongo: Any,
    pg: Any,
    hit_id: str,
    miss_id: str,
    pg_sql: dict[str, tuple[str, tuple[Any, ...]]],
) -> tuple[dict[str, Any], bool]:
    mongo_commands = {
        "idhack_id_hit": {
            "find": MONGO_REAL_TEXT,
            "filter": {"_id": hit_id},
            "projection": {"_id": 1},
            "limit": 1,
            "singleBatch": True,
        },
        "idhack_full_hit": {
            "find": MONGO_REAL_TEXT,
            "filter": {"_id": hit_id},
            "projection": {"_id": 1, "text": 1},
            "limit": 1,
            "singleBatch": True,
        },
        "idhack_id_miss": {
            "find": MONGO_REAL_TEXT,
            "filter": {"_id": miss_id},
            "projection": {"_id": 1},
            "limit": 1,
            "singleBatch": True,
        },
        "covered_id_hit": {
            "find": MONGO_REAL_TEXT,
            "filter": {"_id": {"$gte": hit_id, "$lte": hit_id}},
            "projection": {"_id": 1},
            "hint": "_id_",
            "limit": 1,
            "singleBatch": True,
        },
    }
    mongo_plans = {
        name: mongo_explain(mongo, command)
        for name, command in mongo_commands.items()
    }
    for name in ("idhack_id_hit", "idhack_full_hit", "idhack_id_miss"):
        require_gate(
            "IDHACK" in mongo_plans[name]["stages"],
            f"MongoDB {name} must contain IDHACK; "
            f"got {mongo_plans[name]['stages']}",
        )
    require_gate(
        mongo_plans["idhack_id_hit"]["documents_examined"] == 1,
        "MongoDB IDHACK ID hit must examine one document",
    )
    require_gate(
        mongo_plans["idhack_full_hit"]["documents_examined"] == 1,
        "MongoDB IDHACK full hit must examine one document",
    )
    require_gate(
        mongo_plans["idhack_id_miss"]["documents_examined"] == 0,
        "MongoDB IDHACK miss must examine zero documents",
    )
    covered = mongo_plans["covered_id_hit"]
    covered_valid = (
        covered["documents_examined"] == 0
        and covered["keys_examined"] == 1
        and covered["n_returned"] == 1
        and "FETCH" not in covered["stages"]
        and (
            "IXSCAN" in covered["stages"]
            or "EXPRESS_IXSCAN" in covered["stages"]
        )
    )

    pg_plans = {
        name: pg_explain(pg, sql, (hit_id,))
        for name, (sql, _) in pg_sql.items()
    }
    require_gate(
        "Index Only Scan" in pg_plans["id_index_only"]["node_types"],
        "PostgreSQL ID-only hit must use Index Only Scan",
    )
    require_gate(
        pg_plans["id_index_only"]["heap_fetches"] == 0,
        "PostgreSQL ID-only hit must have zero heap fetches",
    )
    require_gate(
        "Index Scan" in pg_plans["id_fetched"]["node_types"]
        and "Index Only Scan" not in pg_plans["id_fetched"]["node_types"],
        "PostgreSQL fetched-ID arm must use Index Scan",
    )
    require_gate(
        "Index Scan" in pg_plans["full"]["node_types"]
        and "Index Only Scan" not in pg_plans["full"]["node_types"],
        "PostgreSQL full arm must use Index Scan",
    )
    return {
        "mongodb": mongo_plans,
        "postgresql": pg_plans,
        "gates": {
            "mongodb_idhack": True,
            "mongodb_hit_fetches_document": True,
            "mongodb_miss_fetches_no_document": True,
            "mongodb_covered_range_valid": covered_valid,
            "postgresql_index_only_zero_heap_fetches": True,
            "postgresql_forced_fetch_uses_index_scan": True,
            "postgresql_full_uses_index_scan": True,
        },
    }, covered_valid


def real_variants(
    queries: dict[str, Query],
    covered_valid: bool,
) -> list[Variant]:
    variants = [
        Variant(
            "mongo_idhack_id_raw",
            "mongodb",
            "id",
            queries["mongo_idhack_id_raw"],
        ),
        Variant(
            "mongo_full_raw",
            "mongodb",
            "full",
            queries["mongo_full_raw"],
        ),
        Variant(
            "mongo_full_driver",
            "mongodb",
            "full",
            queries["mongo_full_driver"],
        ),
        Variant(
            "pg_prepared_id_index_only",
            "postgresql_prepared",
            "id",
            queries["pg_prepared_id_index_only"],
        ),
        Variant(
            "pg_unprepared_id_index_only",
            "postgresql_unprepared",
            "id",
            queries["pg_unprepared_id_index_only"],
        ),
        Variant(
            "pg_prepared_id_fetched",
            "postgresql_prepared",
            "id",
            queries["pg_prepared_id_fetched"],
        ),
        Variant(
            "pg_unprepared_id_fetched",
            "postgresql_unprepared",
            "id",
            queries["pg_unprepared_id_fetched"],
        ),
        Variant(
            "pg_prepared_full",
            "postgresql_prepared",
            "full",
            queries["pg_prepared_full"],
        ),
        Variant(
            "pg_unprepared_full",
            "postgresql_unprepared",
            "full",
            queries["pg_unprepared_full"],
        ),
    ]
    if covered_valid:
        variants.insert(
            1,
            Variant(
                "mongo_covered_id_raw",
                "mongodb",
                "id",
                queries["mongo_covered_id_raw"],
            ),
        )
    return variants


def real_boundary_variants(
    queries: dict[str, Query],
) -> list[Variant]:
    return [
        Variant(
            "mongo_idhack_id_raw",
            "mongodb",
            "id",
            queries["mongo_idhack_id_raw"],
            monitor_commands=True,
        ),
        Variant(
            "mongo_full_raw",
            "mongodb",
            "full",
            queries["mongo_full_raw"],
            monitor_commands=True,
        ),
        Variant(
            "mongo_full_driver",
            "mongodb",
            "full",
            queries["mongo_full_driver"],
            monitor_commands=True,
        ),
    ]


def build_real_items(
    hit_ids: Sequence[str],
    miss_ids: Sequence[str],
    reference_full: Query,
    reference_id: Query,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def item(node_id: str, expected_class: str) -> dict[str, Any]:
        full = reference_full(node_id)
        identifier = reference_id(node_id)
        if expected_class == "hit":
            if len(full) != 1 or len(identifier) != 1:
                raise RuntimeError(f"expected hit for {node_id}")
        elif full or identifier:
            raise RuntimeError(f"expected miss for {node_id}")
        return {
            "node_id": node_id,
            "class": expected_class,
            "rows": {"id": len(identifier), "full": len(full)},
            "fingerprints": {
                "id": fingerprint(identifier),
                "full": fingerprint(full),
            },
            "text_bytes": (
                len(str(full[0][1]).encode("utf-8")) if full else 0
            ),
        }

    hits = [item(node_id, "hit") for node_id in hit_ids]
    misses = [item(node_id, "miss") for node_id in miss_ids]
    return hits, misses


def component_deltas(
    samples: dict[str, list[dict[str, Any]]],
    covered_valid: bool,
) -> dict[str, Any]:
    pairs = [
        ("mongo_full_driver", "mongo_full_raw"),
        ("mongo_full_raw", "mongo_idhack_id_raw"),
        ("pg_prepared_full", "pg_prepared_id_fetched"),
        ("pg_unprepared_full", "pg_unprepared_id_fetched"),
        ("pg_unprepared_full", "pg_prepared_full"),
        (
            "pg_unprepared_id_index_only",
            "pg_prepared_id_index_only",
        ),
    ]
    if covered_valid:
        pairs.append(("mongo_idhack_id_raw", "mongo_covered_id_raw"))
    return {
        f"{left}_minus_{right}": paired_delta(samples, left, right)
        for left, right in pairs
    }


def hit_minus_miss(
    hits: dict[str, Any],
    misses: dict[str, Any],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for variant in hits["summaries"]:
        hit_p50 = hits["summaries"][variant]["wall_ms"]["p50"]
        miss_p50 = misses["summaries"][variant]["wall_ms"]["p50"]
        output[variant] = {
            "unit": "us",
            "method": "hit P50 minus miss P50",
            "delta": round((hit_p50 - miss_p50) * 1_000, 6),
        }
    return output


def mongo_boundary_deltas(
    samples: dict[str, list[dict[str, Any]]],
    id_variant: str,
) -> dict[str, Any]:
    return {
        "driver_minus_raw": paired_delta(
            samples,
            "mongo_full_driver",
            "mongo_full_raw",
        ),
        "full_raw_minus_id": paired_delta(
            samples,
            "mongo_full_raw",
            id_variant,
        ),
    }


def create_payload_probe(
    mongo: Any,
    pg_admin: Any,
    collection_name: str,
    table_name: str,
    sizes: Sequence[int],
    rows_per_size: int,
    run_tag: str,
    created: dict[str, bool],
) -> list[dict[str, Any]]:
    if collection_name in mongo.list_collection_names(
        filter={"name": collection_name}
    ):
        raise RuntimeError(f"MongoDB probe name collision: {collection_name}")
    exists = pg_admin.execute(
        "SELECT to_regclass(%s)",
        (f"public.{table_name}",),
    ).fetchone()[0]
    if exists is not None:
        raise RuntimeError(f"PostgreSQL probe name collision: {table_name}")

    mongo.create_collection(collection_name)
    created["mongodb"] = True
    pg_admin.execute(
        f"CREATE TABLE {table_name} ("
        'node_id TEXT COLLATE "C" PRIMARY KEY, '
        "payload_size INTEGER NOT NULL, text TEXT)"
    )
    created["postgresql"] = True

    descriptors: list[dict[str, Any]] = []
    mongo_batch: list[dict[str, Any]] = []
    pg_batch: list[tuple[str, int, str]] = []

    def insert_pg_batch() -> None:
        with pg_admin.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO {table_name} "
                "(node_id,payload_size,text) VALUES (%s,%s,%s)",
                pg_batch,
            )

    for size in sizes:
        for row_index in range(rows_per_size):
            node_id = (
                f"{run_tag}_{size:08d}_{row_index:04d}"
            )
            text = deterministic_ascii(size, node_id)
            text_hash = hashlib.sha256(text.encode("ascii")).hexdigest()
            descriptors.append({
                "node_id": node_id,
                "payload_size": size,
                "text_sha256": text_hash,
            })
            mongo_batch.append({
                "_id": node_id,
                "payload_size": size,
                "text": text,
            })
            pg_batch.append((node_id, size, text))
            if len(mongo_batch) >= 32:
                mongo[collection_name].insert_many(
                    mongo_batch,
                    ordered=True,
                )
                mongo_batch.clear()
            if len(pg_batch) >= 32:
                insert_pg_batch()
                pg_batch.clear()
    if mongo_batch:
        mongo[collection_name].insert_many(mongo_batch, ordered=True)
    if pg_batch:
        insert_pg_batch()
    pg_admin.execute(f"VACUUM (ANALYZE) {table_name}")

    mongo_count = mongo[collection_name].count_documents({})
    pg_count = pg_admin.execute(
        f"SELECT count(*) FROM {table_name}"
    ).fetchone()[0]
    expected = len(descriptors)
    if mongo_count != expected or pg_count != expected:
        raise RuntimeError(
            f"payload probe count mismatch: "
            f"MongoDB={mongo_count}, PostgreSQL={pg_count}, "
            f"expected={expected}"
        )
    return descriptors


def build_payload_queries(
    mongo: Any,
    pg_prepared: Any,
    pg_unprepared: Any,
    collection_name: str,
    table_name: str,
) -> tuple[dict[str, Query], dict[str, tuple[str, tuple[Any, ...]]]]:
    collection = mongo[collection_name]

    def mongo_id_raw(node_id: str) -> Rows:
        rows = command_first_batch(mongo, {
            "find": collection_name,
            "filter": {"_id": node_id},
            "projection": {"_id": 1},
            "limit": 1,
            "singleBatch": True,
        })
        return normalize((row.get("_id"),) for row in rows)

    def mongo_full_raw(node_id: str) -> Rows:
        rows = command_first_batch(mongo, {
            "find": collection_name,
            "filter": {"_id": node_id},
            "projection": {"_id": 1, "text": 1},
            "limit": 1,
            "singleBatch": True,
        })
        return normalize(
            (row.get("_id"), row.get("text")) for row in rows
        )

    def mongo_full_driver(node_id: str) -> Rows:
        row = collection.find_one(
            {"_id": node_id},
            {"_id": 1, "text": 1},
        )
        return normalize([(row.get("_id"), row.get("text"))])

    id_fetched_sql = (
        f"SELECT node_id FROM {table_name} "
        "WHERE node_id=%s AND text IS NOT NULL"
    )
    full_sql = (
        f"SELECT node_id,text FROM {table_name} WHERE node_id=%s"
    )

    def pg_query(connection: Any, sql: str) -> Query:
        return lambda node_id: normalize(
            connection.execute(sql, (node_id,)).fetchall()
        )

    return {
        "mongo_id_raw": mongo_id_raw,
        "mongo_full_raw": mongo_full_raw,
        "mongo_full_driver": mongo_full_driver,
        "pg_prepared_id_fetched": pg_query(
            pg_prepared, id_fetched_sql
        ),
        "pg_unprepared_id_fetched": pg_query(
            pg_unprepared, id_fetched_sql
        ),
        "pg_prepared_full": pg_query(pg_prepared, full_sql),
        "pg_unprepared_full": pg_query(pg_unprepared, full_sql),
    }, {
        "id_fetched": (id_fetched_sql, ("",)),
        "full": (full_sql, ("",)),
    }


def payload_variants(queries: dict[str, Query]) -> list[Variant]:
    return [
        Variant(
            "mongo_id_raw",
            "mongodb",
            "id",
            queries["mongo_id_raw"],
        ),
        Variant(
            "mongo_full_raw",
            "mongodb",
            "full",
            queries["mongo_full_raw"],
        ),
        Variant(
            "mongo_full_driver",
            "mongodb",
            "full",
            queries["mongo_full_driver"],
        ),
        Variant(
            "pg_prepared_id_fetched",
            "postgresql_prepared",
            "id",
            queries["pg_prepared_id_fetched"],
        ),
        Variant(
            "pg_unprepared_id_fetched",
            "postgresql_unprepared",
            "id",
            queries["pg_unprepared_id_fetched"],
        ),
        Variant(
            "pg_prepared_full",
            "postgresql_prepared",
            "full",
            queries["pg_prepared_full"],
        ),
        Variant(
            "pg_unprepared_full",
            "postgresql_unprepared",
            "full",
            queries["pg_unprepared_full"],
        ),
    ]


def payload_boundary_variants(
    queries: dict[str, Query],
) -> list[Variant]:
    return [
        Variant(
            "mongo_id_raw",
            "mongodb",
            "id",
            queries["mongo_id_raw"],
            monitor_commands=True,
        ),
        Variant(
            "mongo_full_raw",
            "mongodb",
            "full",
            queries["mongo_full_raw"],
            monitor_commands=True,
        ),
        Variant(
            "mongo_full_driver",
            "mongodb",
            "full",
            queries["mongo_full_driver"],
            monitor_commands=True,
        ),
    ]


def build_payload_items(
    descriptors: Sequence[dict[str, Any]],
    reference_full: Query,
    reference_id: Query,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for descriptor in descriptors:
        node_id = descriptor["node_id"]
        full = reference_full(node_id)
        identifier = reference_id(node_id)
        if len(full) != 1 or len(identifier) != 1:
            raise RuntimeError(f"payload row missing: {node_id}")
        text = str(full[0][1])
        actual_size = len(text.encode("ascii"))
        actual_hash = hashlib.sha256(text.encode("ascii")).hexdigest()
        if (
            actual_size != descriptor["payload_size"]
            or actual_hash != descriptor["text_sha256"]
        ):
            raise RuntimeError(f"payload content mismatch: {node_id}")
        items.append({
            **descriptor,
            "rows": {"id": 1, "full": 1},
            "fingerprints": {
                "id": fingerprint(identifier),
                "full": fingerprint(full),
            },
        })
    return items


def collect_payload_plans(
    mongo: Any,
    pg: Any,
    collection_name: str,
    hit_id: str,
    pg_sql: dict[str, tuple[str, tuple[Any, ...]]],
) -> dict[str, Any]:
    mongo_plans = {
        "id": mongo_explain(mongo, {
            "find": collection_name,
            "filter": {"_id": hit_id},
            "projection": {"_id": 1},
            "limit": 1,
            "singleBatch": True,
        }),
        "full": mongo_explain(mongo, {
            "find": collection_name,
            "filter": {"_id": hit_id},
            "projection": {"_id": 1, "text": 1},
            "limit": 1,
            "singleBatch": True,
        }),
    }
    for name, plan in mongo_plans.items():
        require_gate(
            "IDHACK" in plan["stages"],
            f"payload MongoDB {name} must contain IDHACK",
        )
        require_gate(
            plan["documents_examined"] == 1,
            f"payload MongoDB {name} must examine one document",
        )
    pg_plans = {
        name: pg_explain(pg, sql, (hit_id,))
        for name, (sql, _) in pg_sql.items()
    }
    require_gate(
        "Index Scan" in pg_plans["id_fetched"]["node_types"]
        and "Index Only Scan" not in pg_plans["id_fetched"]["node_types"],
        "payload PostgreSQL fetched-ID arm must use Index Scan",
    )
    require_gate(
        "Index Scan" in pg_plans["full"]["node_types"]
        and "Index Only Scan" not in pg_plans["full"]["node_types"],
        "payload PostgreSQL full arm must use Index Scan",
    )
    return {
        "mongodb": mongo_plans,
        "postgresql": pg_plans,
        "gates": {
            "mongodb_idhack_fetches_document": True,
            "postgresql_id_fetched_uses_index_scan": True,
            "postgresql_full_uses_index_scan": True,
        },
    }


def summarize_payload_by_size(
    items: Sequence[dict[str, Any]],
    samples: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    size_by_id = {
        item["node_id"]: item["payload_size"] for item in items
    }
    sizes = sorted(set(size_by_id.values()))
    output: dict[str, Any] = {}
    for size in sizes:
        grouped = {
            variant: [
                sample
                for sample in variant_samples
                if size_by_id[sample["node_id"]] == size
            ]
            for variant, variant_samples in samples.items()
        }
        output[str(size)] = {
            "bytes": size,
            "summaries": summarize_samples(grouped),
            "deltas": {
                "mongo_full_raw_minus_id": paired_delta(
                    grouped,
                    "mongo_full_raw",
                    "mongo_id_raw",
                ),
                "mongo_driver_minus_raw": paired_delta(
                    grouped,
                    "mongo_full_driver",
                    "mongo_full_raw",
                ),
                "pg_prepared_full_minus_fetched_id": paired_delta(
                    grouped,
                    "pg_prepared_full",
                    "pg_prepared_id_fetched",
                ),
                "pg_unprepared_full_minus_fetched_id": paired_delta(
                    grouped,
                    "pg_unprepared_full",
                    "pg_unprepared_id_fetched",
                ),
                "pg_unprepared_minus_prepared_full": paired_delta(
                    grouped,
                    "pg_unprepared_full",
                    "pg_prepared_full",
                ),
            },
        }
    return output


def summarize_payload_boundary_by_size(
    items: Sequence[dict[str, Any]],
    samples: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    size_by_id = {
        item["node_id"]: item["payload_size"] for item in items
    }
    output: dict[str, Any] = {}
    for size in sorted(set(size_by_id.values())):
        grouped = {
            variant: [
                sample
                for sample in variant_samples
                if size_by_id[sample["node_id"]] == size
            ]
            for variant, variant_samples in samples.items()
        }
        output[str(size)] = {
            "bytes": size,
            "summaries": summarize_samples(grouped),
            "deltas": {
                "mongo_full_raw_minus_id": paired_delta(
                    grouped,
                    "mongo_full_raw",
                    "mongo_id_raw",
                ),
                "mongo_driver_minus_raw": paired_delta(
                    grouped,
                    "mongo_full_driver",
                    "mongo_full_raw",
                ),
            },
        }
    return output


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
        "--out",
        default=(
            "bench/db/runs/entity_rootcause_20260724/"
            "entity_rootcause_10m.json"
        ),
    )
    parser.add_argument("--hits", type=int, default=256)
    parser.add_argument("--misses", type=int, default=256)
    parser.add_argument("--real-repeats", type=int, default=30)
    parser.add_argument("--floor-repeats", type=int, default=10000)
    parser.add_argument("--payload-repeats", type=int, default=10)
    parser.add_argument("--payload-rows-per-size", type=int, default=32)
    parser.add_argument(
        "--payload-sizes",
        type=parse_payload_sizes,
        default=parse_payload_sizes(DEFAULT_PAYLOAD_SIZES),
    )
    parser.add_argument("--seed", type=int, default=20260724)
    args = parser.parse_args()
    if min(
        args.hits,
        args.misses,
        args.real_repeats,
        args.floor_repeats,
        args.payload_repeats,
        args.payload_rows_per_size,
    ) <= 0:
        parser.error("input counts and repeats must be positive")

    import psycopg
    from pymongo import MongoClient

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_tag = (
        f"e{os.getpid()}_{uuid.uuid4().hex[:10]}"
    )
    mongo_probe = safe_generated_name("entity_probe_m", run_tag)
    pg_probe = safe_generated_name("entity_probe_p", run_tag)
    listener = DurationListener()

    mongo_client = MongoClient(args.mongo_uri)
    mongo = mongo_client[args.mongo_db]
    mongo_boundary_client = MongoClient(
        args.mongo_uri,
        event_listeners=[listener],
    )
    mongo_boundary = mongo_boundary_client[args.mongo_db]
    pg_admin = psycopg.connect(
        args.pg_dsn,
        autocommit=True,
        prepare_threshold=None,
    )
    pg_prepared = psycopg.connect(
        args.pg_dsn,
        autocommit=True,
        prepare_threshold=0,
    )
    pg_unprepared = psycopg.connect(
        args.pg_dsn,
        autocommit=True,
        prepare_threshold=None,
    )
    for connection in (pg_admin, pg_prepared, pg_unprepared):
        connection.execute("SET jit = off")

    output: dict[str, Any] = {
        "run": {
            "status": "initializing",
            "started_unix_s": time.time(),
            "run_tag": run_tag,
            "seed": args.seed,
            "real_repeats": args.real_repeats,
            "floor_repeats": args.floor_repeats,
            "payload_repeats": args.payload_repeats,
        },
        "contract": {
            "real_stores_are_read_only": True,
            "real_mongodb_collection": MONGO_REAL_TEXT,
            "real_postgresql_table": PG_REAL_TEXT,
            "hit_sampling": "deterministic primary-key stratification",
            "misses": "run-specific keys verified absent in both engines",
            "outputs": {
                "id": ["node_id"],
                "full": ["node_id", "text"],
            },
            "postgresql_modes": {
                "prepared": "psycopg prepare_threshold=0",
                "unprepared": "psycopg prepare_threshold=None",
            },
            "matched_access_paths": {
                "index_only": (
                    "MongoDB covered _id range when valid versus "
                    "PostgreSQL Index Only Scan"
                ),
                "fetched_id": (
                    "MongoDB IDHACK ID projection versus PostgreSQL "
                    "Index Scan with text IS NOT NULL"
                ),
                "full": "one fetched row returning node_id and text",
            },
            "timing": (
                "primary cross-engine wall clock uses an unmonitored "
                "MongoClient and includes full result materialization"
            ),
            "mongo_command_boundary": (
                "a separate MongoClient with a connection-local listener "
                "measures raw command versus find_one; these monitored "
                "numbers are not used for the cross-engine comparison"
            ),
            "summary_unit": (
                "per-input median across repeats, then percentile "
                "across inputs"
            ),
            "payload_probe": {
                "temporary_mongodb_collection": mongo_probe,
                "temporary_postgresql_table": pg_probe,
                "cleanup": "only these run-specific objects are dropped",
                "sizes_bytes": args.payload_sizes,
                "rows_per_size": args.payload_rows_per_size,
                "content": (
                    "deterministic SHA-256 stream encoded as ASCII; "
                    "identical across engines"
                ),
                "postgresql_access_path": (
                    "enable_seqscan=off only during the small payload "
                    "phase so point reads use the primary-key index"
                ),
            },
        },
        "real": {},
        "payload": {},
        "cleanup": {
            "mongodb": "not_created",
            "postgresql": "not_created",
        },
    }
    save(out_path, output)

    created = {"mongodb": False, "postgresql": False}
    try:
        log("selecting stratified real-store hits and guaranteed misses")
        hit_ids, selection = numeric_stratified_hits(
            pg_admin,
            args.hits,
            PG_REAL_TEXT,
        )
        miss_ids = [
            f"__{run_tag}_missing_{index:06d}"
            for index in range(args.misses)
        ]
        queries, pg_sql = build_real_queries(
            mongo,
            pg_prepared,
            pg_unprepared,
        )
        plans, covered_valid = collect_real_plans(
            mongo,
            pg_admin,
            hit_ids[0],
            miss_ids[0],
            pg_sql,
        )
        variants = real_variants(queries, covered_valid)
        hit_items, miss_items = build_real_items(
            hit_ids,
            miss_ids,
            queries["pg_unprepared_full"],
            queries["pg_unprepared_id_index_only"],
        )
        output["real"]["inputs"] = {
            "hits": hit_items,
            "misses": miss_items,
            "selection": selection,
        }
        output["real"]["plans"] = plans
        output["real"]["variants"] = [
            {
                "name": variant.name,
                "engine": variant.engine,
                "output_contract": variant.contract,
            }
            for variant in variants
        ]
        save(out_path, output)

        log("validating and warming real-store variants")
        output["real"]["validation_checks"] = (
            validate_variants(hit_items, variants, None)
            + validate_variants(miss_items, variants, None)
        )

        floor_variants = [
            Variant(
                "mongo_ping",
                "mongodb",
                "mongo_floor",
                lambda ignored: normalize([
                    (int(mongo.command("ping")["ok"]),)
                ]),
            ),
            Variant(
                "pg_prepared_scalar",
                "postgresql_prepared",
                "pg_floor",
                lambda ignored: normalize(
                    pg_prepared.execute(
                        "SELECT %s::int",
                        (1,),
                    ).fetchall()
                ),
            ),
            Variant(
                "pg_unprepared_scalar",
                "postgresql_unprepared",
                "pg_floor",
                lambda ignored: normalize(
                    pg_unprepared.execute(
                        "SELECT %s::int",
                        (1,),
                    ).fetchall()
                ),
            ),
        ]
        log("timing request floors")
        output["real"]["floor"] = benchmark_floor(
            floor_variants,
            args.floor_repeats,
            args.seed,
            None,
        )
        save(out_path, output)

        for class_index, (name, items) in enumerate(
            (("hits", hit_items), ("misses", miss_items))
        ):
            log(
                f"timing real-store {name}: {len(items)} inputs x "
                f"{args.real_repeats} repeats x {len(variants)} variants"
            )
            completed = 0

            def progress(class_name: str = name) -> None:
                nonlocal completed
                completed += 1
                log(
                    f"  real {class_name} repeat "
                    f"{completed}/{args.real_repeats}"
                )

            samples = benchmark_variants(
                items,
                variants,
                args.real_repeats,
                args.seed + class_index * 10_000,
                None,
                progress,
            )
            output["real"][name] = {
                "samples": samples,
                "summaries": summarize_samples(samples),
                "component_deltas_us": component_deltas(
                    samples,
                    covered_valid,
                ),
            }
            save(out_path, output)

        output["real"]["hit_minus_miss_us"] = hit_minus_miss(
            output["real"]["hits"],
            output["real"]["misses"],
        )
        save(out_path, output)

        log("running separate monitored MongoDB command-boundary phase")
        boundary_queries, _ = build_real_queries(
            mongo_boundary,
            pg_prepared,
            pg_unprepared,
        )
        boundary_variants = real_boundary_variants(boundary_queries)
        boundary_checks = (
            validate_variants(hit_items, boundary_variants, listener)
            + validate_variants(miss_items, boundary_variants, listener)
        )
        boundary_floor = benchmark_floor(
            [
                Variant(
                    "mongo_ping",
                    "mongodb",
                    "mongo_floor",
                    lambda ignored: normalize([
                        (int(mongo_boundary.command("ping")["ok"]),)
                    ]),
                    monitor_commands=True,
                )
            ],
            args.floor_repeats,
            args.seed + 30_000,
            listener,
        )
        output["real"]["mongo_command_boundary"] = {
            "contract": (
                "MongoDB-only phase on a second client with a "
                "connection-local command listener"
            ),
            "validation_checks": boundary_checks,
            "floor": boundary_floor,
            "variants": [
                {
                    "name": variant.name,
                    "output_contract": variant.contract,
                }
                for variant in boundary_variants
            ],
        }
        for class_index, (name, items) in enumerate(
            (("hits", hit_items), ("misses", miss_items))
        ):
            log(
                f"  monitored MongoDB {name}: {len(items)} inputs x "
                f"{args.real_repeats} repeats"
            )
            boundary_samples = benchmark_variants(
                items,
                boundary_variants,
                args.real_repeats,
                args.seed + 40_000 + class_index * 10_000,
                listener,
                progress=lambda: None,
            )
            output["real"]["mongo_command_boundary"][name] = {
                "samples": boundary_samples,
                "summaries": summarize_samples(boundary_samples),
                "component_deltas_us": mongo_boundary_deltas(
                    boundary_samples,
                    "mongo_idhack_id_raw",
                ),
            }
            save(out_path, output)

        log("creating run-specific payload probe")
        for connection in (pg_admin, pg_prepared, pg_unprepared):
            connection.execute("SET enable_seqscan = off")
        descriptors = create_payload_probe(
            mongo,
            pg_admin,
            mongo_probe,
            pg_probe,
            args.payload_sizes,
            args.payload_rows_per_size,
            run_tag,
            created,
        )
        output["cleanup"]["mongodb"] = "created"
        output["cleanup"]["postgresql"] = "created"
        payload_queries, payload_sql = build_payload_queries(
            mongo,
            pg_prepared,
            pg_unprepared,
            mongo_probe,
            pg_probe,
        )
        payload_items = build_payload_items(
            descriptors,
            payload_queries["pg_unprepared_full"],
            payload_queries["pg_unprepared_id_fetched"],
        )
        payload_plan_id = next(
            item["node_id"]
            for item in reversed(payload_items)
            if item["payload_size"] > 0
        )
        output["payload"]["plans"] = collect_payload_plans(
            mongo,
            pg_admin,
            mongo_probe,
            payload_plan_id,
            payload_sql,
        )
        output["payload"]["inputs"] = payload_items
        payload_test_variants = payload_variants(payload_queries)
        output["payload"]["variants"] = [
            {
                "name": variant.name,
                "engine": variant.engine,
                "output_contract": variant.contract,
            }
            for variant in payload_test_variants
        ]
        log("validating and warming payload variants")
        output["payload"]["validation_checks"] = validate_variants(
            payload_items,
            payload_test_variants,
            None,
        )
        save(out_path, output)

        log(
            f"timing payload probe: {len(payload_items)} inputs x "
            f"{args.payload_repeats} repeats x "
            f"{len(payload_test_variants)} variants"
        )
        payload_completed = 0

        def payload_progress() -> None:
            nonlocal payload_completed
            payload_completed += 1
            log(
                f"  payload repeat "
                f"{payload_completed}/{args.payload_repeats}"
            )

        payload_samples = benchmark_variants(
            payload_items,
            payload_test_variants,
            args.payload_repeats,
            args.seed + 20_000,
            None,
            payload_progress,
        )
        output["payload"]["samples"] = payload_samples
        output["payload"]["summaries"] = summarize_samples(
            payload_samples
        )
        output["payload"]["by_size"] = summarize_payload_by_size(
            payload_items,
            payload_samples,
        )

        log("running separate monitored MongoDB payload boundary")
        boundary_payload_queries, _ = build_payload_queries(
            mongo_boundary,
            pg_prepared,
            pg_unprepared,
            mongo_probe,
            pg_probe,
        )
        boundary_payload_variants = payload_boundary_variants(
            boundary_payload_queries
        )
        boundary_payload_checks = validate_variants(
            payload_items,
            boundary_payload_variants,
            listener,
        )
        boundary_payload_samples = benchmark_variants(
            payload_items,
            boundary_payload_variants,
            args.payload_repeats,
            args.seed + 50_000,
            listener,
            progress=lambda: None,
        )
        output["payload"]["mongo_command_boundary"] = {
            "contract": (
                "MongoDB-only phase on the separate monitored client"
            ),
            "validation_checks": boundary_payload_checks,
            "samples": boundary_payload_samples,
            "summaries": summarize_samples(
                boundary_payload_samples
            ),
            "by_size": summarize_payload_boundary_by_size(
                payload_items,
                boundary_payload_samples,
            ),
        }
        output["versions"] = {
            "mongodb": mongo_client.server_info()["version"],
            "pymongo": __import__("pymongo").version,
            "postgresql": pg_admin.execute(
                "SHOW server_version"
            ).fetchone()[0],
            "psycopg": psycopg.__version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        }
        output["postgresql_preparation"] = {
            "prepared_connection_threshold": pg_prepared.prepare_threshold,
            "unprepared_connection_threshold": (
                pg_unprepared.prepare_threshold
            ),
            "prepared_statements": [
                {
                    "name": row[0],
                    "statement": row[1],
                    "generic_plans": row[2],
                    "custom_plans": row[3],
                }
                for row in pg_prepared.execute(
                    "SELECT name,statement,generic_plans,custom_plans "
                    "FROM pg_prepared_statements ORDER BY name"
                ).fetchall()
            ],
        }
        output["run"]["status"] = "complete"
        output["run"]["finished_unix_s"] = time.time()
        output["run"]["elapsed_s"] = round(
            output["run"]["finished_unix_s"]
            - output["run"]["started_unix_s"],
            3,
        )
        save(out_path, output)
    except Exception as error:
        output["run"]["status"] = "failed"
        output["run"]["error"] = repr(error)
        output["run"]["failed_unix_s"] = time.time()
        save(out_path, output)
        raise
    finally:
        cleanup_errors: list[str] = []
        if created["mongodb"]:
            try:
                mongo.drop_collection(mongo_probe)
                output["cleanup"]["mongodb"] = "dropped"
            except Exception as error:
                output["cleanup"]["mongodb"] = "drop_failed"
                cleanup_errors.append(f"MongoDB: {error!r}")
        if created["postgresql"]:
            try:
                pg_admin.execute(f"DROP TABLE {pg_probe}")
                output["cleanup"]["postgresql"] = "dropped"
            except Exception as error:
                output["cleanup"]["postgresql"] = "drop_failed"
                cleanup_errors.append(f"PostgreSQL: {error!r}")
        if cleanup_errors:
            output["cleanup"]["errors"] = cleanup_errors
        output["cleanup"]["finished_unix_s"] = time.time()
        save(out_path, output)
        pg_unprepared.close()
        pg_prepared.close()
        pg_admin.close()
        mongo_boundary_client.close()
        mongo_client.close()

    if output["cleanup"].get("errors"):
        output["run"]["status"] = "cleanup_failed"
        save(out_path, output)
        raise RuntimeError(
            "payload-probe cleanup failed: "
            + "; ".join(output["cleanup"]["errors"])
        )

    print(json.dumps({
        "status": output["run"]["status"],
        "out": str(out_path),
        "covered_mongo_id_range": output["real"]["plans"]["gates"][
            "mongodb_covered_range_valid"
        ],
        "real_hit_summaries": output["real"]["hits"]["summaries"],
        "real_miss_summaries": output["real"]["misses"]["summaries"],
        "payload_by_size": output["payload"]["by_size"],
        "cleanup": output["cleanup"],
    }, indent=2))


if __name__ == "__main__":
    main()
