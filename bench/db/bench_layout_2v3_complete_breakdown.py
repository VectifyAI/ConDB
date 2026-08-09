#!/usr/bin/env python3
"""Complete MongoDB/PostgreSQL subtree-layout breakdown.

This diagnostic intentionally excludes SQLite.  It gives the two server engines
the same logical workload and the same raw-materialize stage boundaries:

* two-store fetch, tuple normalization, and raw-object cleanup;
* three-store Structure fetch, ID extraction, Structure cleanup, Metadata
  fetch, Metadata-map construction, Metadata cleanup, and ordered merge.

The formal benchmark is run first to rebuild and validate the 10M layouts.  The
breakdown then uses a complete untimed warm sweep, alternating layout order,
cyclically rotated path order, exact ordered-output checks, and result release
outside the next timer.  It is an internal diagnostic and does not edit the
report.
"""

from __future__ import annotations

import argparse
import fcntl
import gc
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench_layout_2v3_mongo_breakdown import (
    fingerprint,
    output_bytes,
    parse_indices,
)
from bench_layout_2v3_postgres import explain as pg_explain


MONGO_TABLES = (
    "layout2_view",
    "layout3_struct",
    "layout3_meta",
    "layout_shared_text",
)
PG_TABLES = (
    "layout2_pg_view",
    "layout3_pg_struct",
    "layout3_pg_meta",
    "layout_shared_pg_text",
)

TWO_COMPONENTS = (
    "two_fetch_ms",
    "two_normalize_ms",
    "two_raw_cleanup_ms",
    "two_unattributed_ms",
)
THREE_COMPONENTS = (
    "structure_fetch_ms",
    "structure_id_extract_ms",
    "structure_raw_cleanup_ms",
    "metadata_request_build_ms",
    "metadata_fetch_ms",
    "metadata_map_ms",
    "metadata_batch_cleanup_ms",
    "ordered_merge_ms",
    "three_unattributed_ms",
)
MEASURE_KEYS = (
    "two_total_ms",
    *TWO_COMPONENTS,
    "three_total_ms",
    "structure_ms",
    *THREE_COMPONENTS,
    "two_release_ms",
    "three_release_ms",
    "gc_collect_ms",
)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def host_snapshot() -> dict[str, Any]:
    meminfo: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass

    return {
        "captured_at": utc_now(),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "loadavg_1m_5m_15m": [round(value, 6) for value in os.getloadavg()],
        "memory_total_bytes": meminfo.get("MemTotal"),
        "memory_available_bytes": meminfo.get("MemAvailable"),
        "cpu_affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
    }


def percentile(values: list[float], p: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return ordered[index]


def stats(values: list[float]) -> dict[str, float | int]:
    mean = statistics.mean(values) if values else 0.0
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    median = statistics.median(values) if values else 0.0
    mad = statistics.median([abs(value - median) for value in values]) if values else 0.0
    ordered = sorted(values)
    return {
        "n": len(values),
        "p50_ms": round(percentile(values, 50), 6),
        "p95_ms": round(percentile(values, 95), 6),
        "p99_ms": round(percentile(values, 99), 6),
        "min_ms": round(min(values), 6) if values else 0.0,
        "max_ms": round(max(values), 6) if values else 0.0,
        "mean_ms": round(mean, 6),
        "median_ms": round(median, 6),
        "mad_ms": round(mad, 6),
        "iqr_ms": round(percentile(ordered, 75) - percentile(ordered, 25), 6),
        "stdev_ms": round(stdev, 6),
        "se_ms": round(stdev / math.sqrt(len(values)), 6) if values else 0.0,
        "cv": round(stdev / mean, 6) if mean else 0.0,
        "sum_ms": round(sum(values), 6),
    }


def representative_indices(source_samples: list[dict]) -> dict[str, int]:
    ordered = sorted(enumerate(source_samples), key=lambda item: item[1]["rows"])

    def at(p: int) -> int:
        position = min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))
        return ordered[position][0]

    return {
        "row_p50": at(50),
        "row_p95": at(95),
        "row_p99": at(99),
        "row_max": ordered[-1][0],
    }


def aggregate(samples: list[dict]) -> dict:
    result: dict[str, Any] = {
        key: stats([sample[key] for sample in samples]) for key in MEASURE_KEYS
    }

    two_total = sum(sample["two_total_ms"] for sample in samples)
    three_total = sum(sample["three_total_ms"] for sample in samples)
    result["two_stage_share_of_total"] = {
        key: round(sum(sample[key] for sample in samples) / two_total, 6)
        for key in TWO_COMPONENTS
    }
    result["three_stage_share_of_total"] = {
        key: round(sum(sample[key] for sample in samples) / three_total, 6)
        for key in THREE_COMPONENTS
    }

    fetch_calls = [
        latency
        for sample in samples
        for latency in sample["metadata_fetch_calls_ms"]
    ]
    result["metadata_fetch_calls"] = stats(fetch_calls)
    batches = [batch for sample in samples for batch in sample["metadata_batches"]]
    result["metadata_batches"] = {
        "all": {
            key: stats([batch[key] for batch in batches])
            for key in (
                "request_build_ms",
                "raw_fetch_ms",
                "map_ms",
                "raw_cleanup_ms",
            )
        },
        "full_1000": {
            key: stats([batch[key] for batch in batches if batch["size"] == 1_000])
            for key in (
                "request_build_ms",
                "raw_fetch_ms",
                "map_ms",
                "raw_cleanup_ms",
            )
        },
        "partial_tail": {
            key: stats([batch[key] for batch in batches if batch["size"] < 1_000])
            for key in (
                "request_build_ms",
                "raw_fetch_ms",
                "map_ms",
                "raw_cleanup_ms",
            )
        },
        "count": len(batches),
        "full_1000_count": sum(batch["size"] == 1_000 for batch in batches),
        "partial_tail_count": sum(batch["size"] < 1_000 for batch in batches),
    }
    result["logical_metadata_calls"] = {
        "sum": sum(sample["metadata_calls"] for sample in samples),
        "p50": int(percentile([sample["metadata_calls"] for sample in samples], 50)),
        "p95": int(percentile([sample["metadata_calls"] for sample in samples], 95)),
        "p99": int(percentile([sample["metadata_calls"] for sample in samples], 99)),
        "max": max(sample["metadata_calls"] for sample in samples),
    }

    two_errors = [
        sample["two_total_ms"] - sum(sample[key] for key in TWO_COMPONENTS)
        for sample in samples
    ]
    three_errors = [
        sample["three_total_ms"] - sum(sample[key] for key in THREE_COMPONENTS)
        for sample in samples
    ]
    result["stage_additivity_max_abs_error_ms"] = {
        "two": round(max(abs(value) for value in two_errors), 9),
        "three": round(max(abs(value) for value in three_errors), 9),
    }

    repeats: dict[int, list[dict]] = defaultdict(list)
    paths: dict[int, list[dict]] = defaultdict(list)
    for sample in samples:
        repeats[sample["repeat"]].append(sample)
        paths[sample["source_index"]].append(sample)

    by_order = {
        order: [sample for sample in samples if sample["order"] == order]
        for order in ("two_first", "three_first")
    }
    result["layout_order_effect"] = {
        order: {
            "pairs": len(group),
            "two_total_ms": stats([sample["two_total_ms"] for sample in group]),
            "three_total_ms": stats([sample["three_total_ms"] for sample in group]),
        }
        for order, group in by_order.items()
    }

    result["per_repeat"] = {
        str(repeat): {
            key: stats([sample[key] for sample in group])
            for key in MEASURE_KEYS
        }
        for repeat, group in sorted(repeats.items())
    }
    result["per_path"] = {
        str(index): {
            "path": group[0]["path"],
            "rows": group[0]["rows"],
            "metadata_calls": group[0]["metadata_calls"],
            "two_first_count": sum(sample["order"] == "two_first" for sample in group),
            "three_first_count": sum(sample["order"] == "three_first" for sample in group),
            "stages": {
                key: stats([sample[key] for sample in group])
                for key in MEASURE_KEYS
            },
        }
        for index, group in sorted(paths.items())
    }
    return result


def validate_samples(
    samples: list[dict], source: dict, indices: list[int], repeats: int
) -> dict[str, Any]:
    expected_pairs = {(repeat, index) for repeat in range(repeats) for index in indices}
    actual_pairs = {(sample["repeat"], sample["source_index"]) for sample in samples}
    if len(samples) != len(expected_pairs) or actual_pairs != expected_pairs:
        raise RuntimeError("sample coverage is not exactly repeats x selected paths")

    max_two_error = 0.0
    max_three_error = 0.0
    max_fetch_batch_error = 0.0
    max_request_batch_error = 0.0
    max_map_batch_error = 0.0
    max_cleanup_batch_error = 0.0
    max_fetch_call_error = 0.0
    max_structure_error = 0.0
    for sample in samples:
        reference = source["samples"][sample["source_index"]]
        if (
            sample["path"] != reference["path"]
            or sample["rows"] != reference["rows"]
            or sample["fingerprint"] != reference["fingerprint"]
        ):
            raise RuntimeError("sample differs from the formal source signature")
        expected_calls = math.ceil(reference["rows"] / source["chunk"])
        if sample["metadata_calls"] != expected_calls:
            raise RuntimeError("sample has the wrong Metadata call count")
        if len(sample["metadata_batches"]) != expected_calls:
            raise RuntimeError("sample has the wrong number of raw Metadata batches")
        if len(sample["metadata_fetch_calls_ms"]) != expected_calls:
            raise RuntimeError("sample has the wrong number of Metadata call latencies")
        if sum(batch["size"] for batch in sample["metadata_batches"]) != reference["rows"]:
            raise RuntimeError("Metadata batch sizes do not sum to the subtree rows")

        two_error = sample["two_total_ms"] - sum(
            sample[key] for key in TWO_COMPONENTS
        )
        three_error = sample["three_total_ms"] - sum(
            sample[key] for key in THREE_COMPONENTS
        )
        fetch_batch_error = sample["metadata_fetch_ms"] - sum(
            batch["raw_fetch_ms"] for batch in sample["metadata_batches"]
        )
        request_batch_error = sample["metadata_request_build_ms"] - sum(
            batch["request_build_ms"] for batch in sample["metadata_batches"]
        )
        map_batch_error = sample["metadata_map_ms"] - sum(
            batch["map_ms"] for batch in sample["metadata_batches"]
        )
        cleanup_batch_error = sample["metadata_batch_cleanup_ms"] - sum(
            batch["raw_cleanup_ms"] for batch in sample["metadata_batches"]
        )
        fetch_call_error = max(
            (
                abs(call - batch["raw_fetch_ms"])
                for call, batch in zip(
                    sample["metadata_fetch_calls_ms"], sample["metadata_batches"]
                )
            ),
            default=0.0,
        )
        structure_error = sample["structure_ms"] - sum(
            sample[key]
            for key in (
                "structure_fetch_ms",
                "structure_id_extract_ms",
                "structure_raw_cleanup_ms",
            )
        )
        max_two_error = max(max_two_error, abs(two_error))
        max_three_error = max(max_three_error, abs(three_error))
        max_fetch_batch_error = max(max_fetch_batch_error, abs(fetch_batch_error))
        max_request_batch_error = max(max_request_batch_error, abs(request_batch_error))
        max_map_batch_error = max(max_map_batch_error, abs(map_batch_error))
        max_cleanup_batch_error = max(
            max_cleanup_batch_error, abs(cleanup_batch_error)
        )
        max_fetch_call_error = max(max_fetch_call_error, fetch_call_error)
        max_structure_error = max(max_structure_error, abs(structure_error))
        if any(
            not math.isfinite(sample[key]) or sample[key] < 0
            for key in MEASURE_KEYS
        ):
            raise RuntimeError("a measured stage is non-finite or negative")

    if max(max_two_error, max_three_error) > 0.001:
        raise RuntimeError("stage additivity error exceeds 0.001 ms")
    if max(
        max_fetch_batch_error,
        max_request_batch_error,
        max_map_batch_error,
        max_cleanup_batch_error,
        max_structure_error,
    ) > 0.001:
        raise RuntimeError("raw Metadata batch sums differ from their parent stage")
    if max_fetch_call_error > 0.000001:
        raise RuntimeError("Metadata call list differs from raw batch timings")
    return {
        "sample_count": len(samples),
        "coverage": "exact repeats x selected paths",
        "source_signature": "exact path/rows/fingerprint match",
        "max_two_additivity_error_ms": round(max_two_error, 9),
        "max_three_additivity_error_ms": round(max_three_error, 9),
        "max_metadata_fetch_batch_error_ms": round(max_fetch_batch_error, 9),
        "max_metadata_request_batch_error_ms": round(max_request_batch_error, 9),
        "max_metadata_map_batch_error_ms": round(max_map_batch_error, 9),
        "max_metadata_cleanup_batch_error_ms": round(max_cleanup_batch_error, 9),
        "max_metadata_fetch_call_error_ms": round(max_fetch_call_error, 9),
        "max_structure_additivity_error_ms": round(max_structure_error, 9),
    }


def mongo_explain_summary(explanation: dict) -> dict:
    stages: list[str] = []
    indexes: list[str] = []
    plan = explanation.get("queryPlanner", {}).get("winningPlan", {})

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "stage" in value:
                stages.append(value["stage"])
            if "indexName" in value:
                indexes.append(value["indexName"])
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(plan)
    execution = explanation.get("executionStats", {})
    return {
        "stages": list(dict.fromkeys(stages)),
        "indexes": list(dict.fromkeys(indexes)),
        "execution_time_ms": execution.get("executionTimeMillis"),
        "execution_success": execution.get("executionSuccess"),
        "n_returned": execution.get("nReturned"),
        "docs_examined": execution.get("totalDocsExamined"),
        "keys_examined": execution.get("totalKeysExamined"),
    }


def emergency_cleanup(engine: str, args: argparse.Namespace) -> None:
    if engine == "mongo":
        from pymongo import MongoClient

        client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
        try:
            database = client["bench"]
            for name in MONGO_TABLES:
                database.drop_collection(name)
        finally:
            client.close()
    else:
        import psycopg

        conn = psycopg.connect(args.pg_dsn, autocommit=True)
        try:
            for name in PG_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {name}")
        finally:
            conn.close()


def rebuild(engine: str, args: argparse.Namespace, source: dict) -> dict:
    if engine == "mongo":
        benchmark = Path(__file__).with_name("bench_layout_2v3.py")
        connection_args = ["--mongo-uri", args.mongo_uri]
    else:
        benchmark = Path(__file__).with_name("bench_layout_2v3_postgres.py")
        connection_args = ["--pg-dsn", args.pg_dsn]

    command = [
        sys.executable,
        str(benchmark),
        "--doc",
        source["doc"],
        *connection_args,
        "--out",
        args.rebuild_result,
        "--chunk",
        str(source["chunk"]),
        "--max-paths",
        str(source["paths"]),
        "--keep",
    ]
    log(f"rebuilding {engine} layouts and running the complete seed ...")
    subprocess.run(command, check=True)
    return json.loads(Path(args.rebuild_result).read_text())


def setup_mongo(
    args: argparse.Namespace,
    source: dict,
    seed: dict | None,
    reps: dict[str, int],
) -> dict[str, Any]:
    import pymongo
    from pymongo import MongoClient

    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    database = client["bench"]
    existing = set(database.list_collection_names())
    missing = sorted(set(MONGO_TABLES) - existing)
    if missing:
        client.close()
        raise SystemExit("missing MongoDB collections: " + ", ".join(missing))

    if seed is not None:
        source_counts = seed["collections"]
        counts = {
            MONGO_TABLES[0]: source_counts["two_view"]["count"],
            MONGO_TABLES[1]: source_counts["three_struct"]["count"],
            MONGO_TABLES[2]: source_counts["three_meta"]["count"],
            MONGO_TABLES[3]: source_counts["shared_text"]["count"],
        }
    else:
        counts = {
            name: database[name].estimated_document_count() for name in MONGO_TABLES
        }

    expected = source["nodes"]
    for name in MONGO_TABLES[:3]:
        if counts[name] != expected:
            client.close()
            raise SystemExit(f"collection {name} count mismatch: {counts[name]}")

    view = database[MONGO_TABLES[0]]
    struct = database[MONGO_TABLES[1]]
    meta = database[MONGO_TABLES[2]]
    lean = [("path", 1), ("node_id", 1)]
    proj_view = {"node_id": 1, "title": 1, "summary": 1, "_id": 0}
    proj_id = {"node_id": 1, "_id": 0}
    proj_meta = {"title": 1, "summary": 1}

    def bounds(path: str) -> dict:
        return {"path": {"$gte": path + "/", "$lt": path + "0"}}

    def two(path: str) -> tuple[list[tuple[str, str, str]], dict]:
        started = time.perf_counter()
        raw = list(view.find(bounds(path), proj_view).sort(lean).hint(lean))
        fetch_ms = (time.perf_counter() - started) * 1_000

        started = time.perf_counter()
        result = [
            (doc["node_id"], doc.get("title", ""), doc.get("summary", ""))
            for doc in raw
        ]
        normalize_ms = (time.perf_counter() - started) * 1_000

        started = time.perf_counter()
        del raw
        cleanup_ms = (time.perf_counter() - started) * 1_000
        return result, {
            "two_fetch_ms": fetch_ms,
            "two_normalize_ms": normalize_ms,
            "two_raw_cleanup_ms": cleanup_ms,
        }

    def three(path: str) -> tuple[list[tuple[str, str, str]], dict]:
        started = time.perf_counter()
        raw_structure = list(
            struct.find(bounds(path), proj_id).sort(lean).hint(lean)
        )
        structure_fetch_ms = (time.perf_counter() - started) * 1_000

        started = time.perf_counter()
        ids = [doc["node_id"] for doc in raw_structure]
        structure_id_extract_ms = (time.perf_counter() - started) * 1_000

        started = time.perf_counter()
        del raw_structure
        structure_raw_cleanup_ms = (time.perf_counter() - started) * 1_000

        metadata: dict[str, tuple[str, str]] = {}
        metadata_request_build_ms = 0.0
        metadata_fetch_calls_ms: list[float] = []
        metadata_map_ms = 0.0
        metadata_batch_cleanup_ms = 0.0
        metadata_batches: list[dict[str, float | int]] = []
        for offset in range(0, len(ids), source["chunk"]):
            started = time.perf_counter()
            chunk = ids[offset : offset + source["chunk"]]
            query = {"_id": {"$in": chunk}}
            request_build_ms = (time.perf_counter() - started) * 1_000
            metadata_request_build_ms += request_build_ms

            started = time.perf_counter()
            raw_metadata = list(meta.find(query, proj_meta))
            fetch_ms = (time.perf_counter() - started) * 1_000
            metadata_fetch_calls_ms.append(fetch_ms)

            started = time.perf_counter()
            for document in raw_metadata:
                metadata[document["_id"]] = (
                    document.get("title", ""),
                    document.get("summary", ""),
                )
            map_ms = (time.perf_counter() - started) * 1_000
            metadata_map_ms += map_ms

            started = time.perf_counter()
            del raw_metadata
            cleanup_ms = (time.perf_counter() - started) * 1_000
            metadata_batch_cleanup_ms += cleanup_ms
            metadata_batches.append({
                "size": len(chunk),
                "request_build_ms": request_build_ms,
                "raw_fetch_ms": fetch_ms,
                "map_ms": map_ms,
                "raw_cleanup_ms": cleanup_ms,
            })

        if len(metadata) != len(ids):
            raise RuntimeError(f"metadata mismatch for {path}")

        started = time.perf_counter()
        result = [(node_id, *metadata[node_id]) for node_id in ids]
        ordered_merge_ms = (time.perf_counter() - started) * 1_000
        structure_ms = (
            structure_fetch_ms
            + structure_id_extract_ms
            + structure_raw_cleanup_ms
        )
        return result, {
            "structure_fetch_ms": structure_fetch_ms,
            "structure_id_extract_ms": structure_id_extract_ms,
            "structure_raw_cleanup_ms": structure_raw_cleanup_ms,
            "structure_ms": structure_ms,
            "metadata_request_build_ms": metadata_request_build_ms,
            "metadata_fetch_ms": sum(metadata_fetch_calls_ms),
            "metadata_fetch_calls_ms": metadata_fetch_calls_ms,
            "metadata_batches": metadata_batches,
            "metadata_map_ms": metadata_map_ms,
            "metadata_batch_cleanup_ms": metadata_batch_cleanup_ms,
            "ordered_merge_ms": ordered_merge_ms,
            "metadata_calls": len(metadata_fetch_calls_ms),
        }

    def plans() -> dict:
        output = {}
        for label, index in reps.items():
            path = source["samples"][index]["path"]
            ids = [
                doc["node_id"]
                for doc in struct.find(bounds(path), proj_id).sort(lean).hint(lean)
            ]
            first_ids = ids[: source["chunk"]]
            tail_size = len(ids) % source["chunk"] or min(source["chunk"], len(ids))
            tail_ids = ids[-tail_size:]
            output[label] = {
                "source_index": index,
                "rows": source["samples"][index]["rows"],
                "two": mongo_explain_summary(
                    view.find(bounds(path), proj_view).sort(lean).hint(lean).explain()
                ),
                "structure": mongo_explain_summary(
                    struct.find(bounds(path), proj_id).sort(lean).hint(lean).explain()
                ),
                "metadata_first_batch": {
                    "batch_size": len(first_ids),
                    "plan": mongo_explain_summary(
                        meta.find({"_id": {"$in": first_ids}}, proj_meta).explain()
                    ),
                },
                "metadata_tail_batch": {
                    "batch_size": len(tail_ids),
                    "plan": mongo_explain_summary(
                        meta.find({"_id": {"$in": tail_ids}}, proj_meta).explain()
                    ),
                },
            }
        return output

    def metrics() -> dict:
        status = database.command("serverStatus")
        cache = status.get("wiredTiger", {}).get("cache", {})
        return {
            "captured_at": utc_now(),
            "opcounters": status.get("opcounters", {}),
            "wiredtiger_cache": {
                key: cache.get(key)
                for key in (
                    "bytes currently in the cache",
                    "maximum bytes configured",
                    "pages read into cache",
                    "pages written from cache",
                    "tracked dirty bytes in the cache",
                )
            },
        }

    def post_warm_state() -> dict:
        return {
            "ping": database.command("ping").get("ok"),
            "warm_contract": "all selected paths, both layouts, same connection",
        }

    def cleanup() -> None:
        for name in MONGO_TABLES:
            database.drop_collection(name)

    return {
        "two": two,
        "three": three,
        "plans": plans,
        "metrics": metrics,
        "post_warm_state": post_warm_state,
        "cleanup": cleanup,
        "close": client.close,
        "counts": counts,
        "environment": {
            "server_version": client.server_info().get("version"),
            "storage_engine": database.command("serverStatus")
            .get("storageEngine", {})
            .get("name"),
            "driver": "pymongo",
            "driver_version": pymongo.version,
            "read_preference": view.read_preference.name,
            "read_concern": view.read_concern.document,
            "write_concern": view.write_concern.document,
            "transport": "localhost Docker",
            "raw_result_type": "BSON-decoded dict",
        },
    }


def setup_postgres(
    args: argparse.Namespace,
    source: dict,
    seed: dict | None,
    reps: dict[str, int],
) -> dict[str, Any]:
    import psycopg

    conn = psycopg.connect(args.pg_dsn, autocommit=True)
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    }
    missing = sorted(set(PG_TABLES) - existing)
    if missing:
        conn.close()
        raise SystemExit("missing PostgreSQL tables: " + ", ".join(missing))

    if seed is not None:
        source_counts = seed["tables"]
        counts = {
            PG_TABLES[0]: source_counts["two_view"]["count"],
            PG_TABLES[1]: source_counts["three_struct"]["count"],
            PG_TABLES[2]: source_counts["three_meta"]["count"],
            PG_TABLES[3]: source_counts["shared_text"]["count"],
        }
    else:
        counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in PG_TABLES
        }

    expected = source["nodes"]
    for name in PG_TABLES[:3]:
        if counts[name] != expected:
            conn.close()
            raise SystemExit(f"table {name} count mismatch: {counts[name]}")

    range_sql = (
        "SELECT node_id, title, summary "
        f"FROM {PG_TABLES[0]} WHERE path >= %s AND path < %s "
        "ORDER BY path, node_id"
    )
    structure_sql = (
        f"SELECT node_id FROM {PG_TABLES[1]} "
        "WHERE path >= %s AND path < %s ORDER BY path, node_id"
    )
    metadata_sql = (
        f"SELECT node_id, title, summary FROM {PG_TABLES[2]} "
        "WHERE node_id = ANY(%s::text[])"
    )

    def bounds(path: str) -> tuple[str, str]:
        return path + "/", path + "0"

    def two(path: str) -> tuple[list[tuple[str, str, str]], dict]:
        started = time.perf_counter()
        raw = conn.execute(range_sql, bounds(path)).fetchall()
        fetch_ms = (time.perf_counter() - started) * 1_000

        started = time.perf_counter()
        result = [
            (node_id, title or "", summary_text or "")
            for node_id, title, summary_text in raw
        ]
        normalize_ms = (time.perf_counter() - started) * 1_000

        started = time.perf_counter()
        del raw
        cleanup_ms = (time.perf_counter() - started) * 1_000
        return result, {
            "two_fetch_ms": fetch_ms,
            "two_normalize_ms": normalize_ms,
            "two_raw_cleanup_ms": cleanup_ms,
        }

    def three(path: str) -> tuple[list[tuple[str, str, str]], dict]:
        started = time.perf_counter()
        raw_structure = conn.execute(structure_sql, bounds(path)).fetchall()
        structure_fetch_ms = (time.perf_counter() - started) * 1_000

        started = time.perf_counter()
        ids = [row[0] for row in raw_structure]
        structure_id_extract_ms = (time.perf_counter() - started) * 1_000

        started = time.perf_counter()
        del raw_structure
        structure_raw_cleanup_ms = (time.perf_counter() - started) * 1_000

        metadata: dict[str, tuple[str, str]] = {}
        metadata_request_build_ms = 0.0
        metadata_fetch_calls_ms: list[float] = []
        metadata_map_ms = 0.0
        metadata_batch_cleanup_ms = 0.0
        metadata_batches: list[dict[str, float | int]] = []
        for offset in range(0, len(ids), source["chunk"]):
            started = time.perf_counter()
            chunk = ids[offset : offset + source["chunk"]]
            params = (chunk,)
            request_build_ms = (time.perf_counter() - started) * 1_000
            metadata_request_build_ms += request_build_ms

            started = time.perf_counter()
            raw_metadata = conn.execute(metadata_sql, params).fetchall()
            fetch_ms = (time.perf_counter() - started) * 1_000
            metadata_fetch_calls_ms.append(fetch_ms)

            started = time.perf_counter()
            for node_id, title, summary_text in raw_metadata:
                metadata[node_id] = (title or "", summary_text or "")
            map_ms = (time.perf_counter() - started) * 1_000
            metadata_map_ms += map_ms

            started = time.perf_counter()
            del raw_metadata
            cleanup_ms = (time.perf_counter() - started) * 1_000
            metadata_batch_cleanup_ms += cleanup_ms
            metadata_batches.append({
                "size": len(chunk),
                "request_build_ms": request_build_ms,
                "raw_fetch_ms": fetch_ms,
                "map_ms": map_ms,
                "raw_cleanup_ms": cleanup_ms,
            })

        if len(metadata) != len(ids):
            raise RuntimeError(f"metadata mismatch for {path}")

        started = time.perf_counter()
        result = [(node_id, *metadata[node_id]) for node_id in ids]
        ordered_merge_ms = (time.perf_counter() - started) * 1_000
        structure_ms = (
            structure_fetch_ms
            + structure_id_extract_ms
            + structure_raw_cleanup_ms
        )
        return result, {
            "structure_fetch_ms": structure_fetch_ms,
            "structure_id_extract_ms": structure_id_extract_ms,
            "structure_raw_cleanup_ms": structure_raw_cleanup_ms,
            "structure_ms": structure_ms,
            "metadata_request_build_ms": metadata_request_build_ms,
            "metadata_fetch_ms": sum(metadata_fetch_calls_ms),
            "metadata_fetch_calls_ms": metadata_fetch_calls_ms,
            "metadata_batches": metadata_batches,
            "metadata_map_ms": metadata_map_ms,
            "metadata_batch_cleanup_ms": metadata_batch_cleanup_ms,
            "ordered_merge_ms": ordered_merge_ms,
            "metadata_calls": len(metadata_fetch_calls_ms),
        }

    def plans() -> dict:
        output = {}
        for label, index in reps.items():
            path = source["samples"][index]["path"]
            ids = [
                row[0]
                for row in conn.execute(structure_sql, bounds(path)).fetchall()
            ]
            first_ids = ids[: source["chunk"]]
            tail_size = len(ids) % source["chunk"] or min(source["chunk"], len(ids))
            tail_ids = ids[-tail_size:]
            output[label] = {
                "source_index": index,
                "rows": source["samples"][index]["rows"],
                "two": pg_explain(conn, range_sql, bounds(path)),
                "structure": pg_explain(conn, structure_sql, bounds(path)),
                "metadata_first_batch": {
                    "batch_size": len(first_ids),
                    "plan": pg_explain(conn, metadata_sql, (first_ids,)),
                },
                "metadata_tail_batch": {
                    "batch_size": len(tail_ids),
                    "plan": pg_explain(conn, metadata_sql, (tail_ids,)),
                },
            }
        return output

    def metrics() -> dict:
        row = conn.execute(
            "SELECT blks_read, blks_hit, tup_returned, tup_fetched, "
            "temp_files, temp_bytes FROM pg_stat_database "
            "WHERE datname = current_database()"
        ).fetchone()
        names = (
            "blks_read",
            "blks_hit",
            "tup_returned",
            "tup_fetched",
            "temp_files",
            "temp_bytes",
        )
        return {"captured_at": utc_now(), **dict(zip(names, row))}

    def post_warm_state() -> dict:
        prepared = conn.execute(
            "SELECT name, statement, parameter_types::text, from_sql "
            "FROM pg_prepared_statements ORDER BY name"
        ).fetchall()
        return {
            "prepare_threshold": conn.prepare_threshold,
            "prepared_statement_count": len(prepared),
            "prepared_statements": [
                {
                    "name": name,
                    "statement": statement,
                    "parameter_types": parameter_types,
                    "from_sql": from_sql,
                }
                for name, statement, parameter_types, from_sql in prepared
            ],
            "warm_contract": "all selected paths, both layouts, same connection",
        }

    def cleanup() -> None:
        for name in PG_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {name}")

    return {
        "two": two,
        "three": three,
        "plans": plans,
        "metrics": metrics,
        "post_warm_state": post_warm_state,
        "cleanup": cleanup,
        "close": conn.close,
        "counts": counts,
        "environment": {
            "server_version": conn.info.server_version,
            "driver": "psycopg",
            "driver_version": psycopg.__version__,
            "autocommit": conn.autocommit,
            "prepare_threshold": conn.prepare_threshold,
            "client_encoding": conn.execute("SHOW client_encoding").fetchone()[0],
            "transaction_isolation": conn.execute(
                "SHOW default_transaction_isolation"
            ).fetchone()[0],
            "database_collation": conn.execute(
                "SELECT datcollate FROM pg_database WHERE datname = current_database()"
            ).fetchone()[0],
            "settings": {
                name: conn.execute(f"SHOW {name}").fetchone()[0]
                for name in (
                    "shared_buffers",
                    "effective_cache_size",
                    "work_mem",
                    "jit",
                    "max_parallel_workers_per_gather",
                    "effective_io_concurrency",
                    "plan_cache_mode",
                    "track_io_timing",
                    "synchronous_commit",
                )
            },
            "transport": "localhost Docker",
            "raw_result_type": "decoded tuple",
        },
    }


def validate_source(source: dict, peer: dict, allow_nonstandard: bool) -> None:
    if source.get("status") != "complete":
        raise SystemExit("source result is not complete")
    if not allow_nonstandard and source.get("nodes") != 10_000_000:
        raise SystemExit("source result is not the 10M dataset")
    if source.get("paths") != 200 or len(source.get("samples", [])) != 200:
        raise SystemExit("source result does not contain 200 paths/samples")
    if source.get("chunk") != 1_000:
        raise SystemExit("source Metadata chunk is not 1000")
    if peer.get("status") != "complete":
        raise SystemExit("peer result is not complete")
    if (
        peer.get("nodes") != source.get("nodes")
        or peer.get("paths") != source.get("paths")
        or peer.get("chunk") != source.get("chunk")
    ):
        raise SystemExit("source and peer dataset parameters differ")

    signature = [
        (sample["path"], sample["rows"], sample["fingerprint"])
        for sample in source["samples"]
    ]
    peer_signature = [
        (sample["path"], sample["rows"], sample["fingerprint"])
        for sample in peer.get("samples", [])
    ]
    if signature != peer_signature:
        raise SystemExit("MongoDB and PostgreSQL source signatures differ")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, choices=("mongo", "postgres"))
    parser.add_argument("--source-result")
    parser.add_argument("--peer-result")
    parser.add_argument("--out", required=True)
    parser.add_argument("--rebuild-result", required=True)
    parser.add_argument("--indices", default="all")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warm-rounds", type=int, default=1)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--allow-nonstandard-source", action="store_true")
    parser.add_argument("--keep-engine-data", action="store_true")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument(
        "--pg-dsn",
        default="host=localhost port=55432 dbname=bench user=postgres password=bench",
    )
    args = parser.parse_args()

    if args.repeats < 2:
        raise SystemExit("complete breakdown requires at least two repeats")
    if args.warm_rounds < 1:
        raise SystemExit("complete breakdown requires at least one warm round")

    defaults = {
        "mongo": (
            "bench/db/runs/report_3eng_20260716/layout_2v3_mongo_10m_final.json",
            "bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json",
        ),
        "postgres": (
            "bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json",
            "bench/db/runs/report_3eng_20260716/layout_2v3_mongo_10m_final.json",
        ),
    }
    source_path = args.source_result or defaults[args.engine][0]
    peer_path = args.peer_result or defaults[args.engine][1]
    source = json.loads(Path(source_path).read_text())
    peer = json.loads(Path(peer_path).read_text())
    validate_source(source, peer, args.allow_nonstandard_source)
    indices = parse_indices(args.indices, len(source["samples"]))
    reps = representative_indices(source["samples"])

    lock_path = Path("/tmp/condb-layout-complete.lock")
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another complete breakdown holds {lock_path}")

    owned = args.rebuild
    seed: dict | None = None
    adapter: dict[str, Any] | None = None
    run_started_at = utc_now()
    host_before_rebuild = host_snapshot()
    try:
        if args.rebuild:
            try:
                seed = rebuild(args.engine, args, source)
            except BaseException:
                emergency_cleanup(args.engine, args)
                raise
            if seed.get("status") != "complete":
                raise SystemExit("rebuild seed did not complete")
            seed_signature = [
                (sample["path"], sample["rows"], sample["fingerprint"])
                for sample in seed.get("samples", [])
            ]
            source_signature = [
                (sample["path"], sample["rows"], sample["fingerprint"])
                for sample in source["samples"]
            ]
            if seed_signature != source_signature:
                raise SystemExit("rebuild seed does not reproduce the source output")

        if args.engine == "mongo":
            adapter = setup_mongo(args, source, seed, reps)
        else:
            adapter = setup_postgres(args, source, seed, reps)

        log(
            f"warming {args.engine}: {args.warm_rounds} complete untimed sweep(s) "
            f"x {len(indices)} paths x both layouts ..."
        )
        warm_done = 0
        for _ in range(args.warm_rounds):
            for index in indices:
                sample = source["samples"][index]
                left, _ = adapter["two"](sample["path"])
                right, _ = adapter["three"](sample["path"])
                if left != right:
                    raise RuntimeError(f"warm-up output mismatch for {sample['path']}")
                if len(left) != sample["rows"]:
                    raise RuntimeError(f"warm-up row-count mismatch for {sample['path']}")
                del left, right
                warm_done += 1
                if warm_done % 50 == 0:
                    log(f"      warm ... {warm_done}/{args.warm_rounds * len(indices)}")
        gc.collect()
        post_warm_state = adapter["post_warm_state"]()
        if (
            args.engine == "postgres"
            and post_warm_state["prepare_threshold"] is not None
            and post_warm_state["prepared_statement_count"] < 3
        ):
            raise RuntimeError("PostgreSQL timed run did not reach stable prepared state")

        output = {
            "engine": args.engine,
            "source_result": source_path,
            "peer_result": peer_path,
            "nodes": source["nodes"],
            "source_paths": source["paths"],
            "chunk": source["chunk"],
            "indices": indices,
            "repeats": args.repeats,
            "warm_rounds": args.warm_rounds,
            "path_order": "fixed cyclic rotation by floor(repeat * paths / repeats)",
            "layout_order": "alternating by source_index + repeat",
            "timing_contract": {
                "cache": "hot steady-state after complete untimed sweep; no cache flush",
                "two_fetch": "server + local transport + driver decode + raw materialization",
                "three_structure_fetch": "server + local transport + driver decode + raw materialization",
                "metadata_fetch": "per-batch server + local transport + driver decode + raw materialization",
                "validation_release_gc": "outside query totals",
                "text_store": "present but not read by this subtree diagnostic",
            },
            "representative_indices": reps,
            "counts": adapter["counts"],
            "environment": adapter["environment"],
            "run_started_at": run_started_at,
            "host_before_rebuild": host_before_rebuild,
            "host_before_timing": host_snapshot(),
            "engine_metrics_before_timing": adapter["metrics"](),
            "post_warm_state": post_warm_state,
            "status": "running",
            "samples": [],
            "plans": {},
        }
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        total = args.repeats * len(indices)
        done = 0
        log(f"measuring {args.engine}: {len(indices)} paths x {args.repeats} repeats ...")
        for repeat in range(args.repeats):
            offset = (repeat * len(indices)) // args.repeats
            repeat_indices = indices[offset:] + indices[:offset]
            for sequence_position, index in enumerate(repeat_indices):
                source_sample = source["samples"][index]
                path = source_sample["path"]
                two_first = (index + repeat) % 2 == 0

                gc.disable()
                try:
                    if two_first:
                        outer = time.perf_counter()
                        two_rows, two_stages = adapter["two"](path)
                        two_total_ms = (time.perf_counter() - outer) * 1_000

                        outer = time.perf_counter()
                        three_rows, three_stages = adapter["three"](path)
                        three_total_ms = (time.perf_counter() - outer) * 1_000
                    else:
                        outer = time.perf_counter()
                        three_rows, three_stages = adapter["three"](path)
                        three_total_ms = (time.perf_counter() - outer) * 1_000

                        outer = time.perf_counter()
                        two_rows, two_stages = adapter["two"](path)
                        two_total_ms = (time.perf_counter() - outer) * 1_000
                finally:
                    gc.enable()

                two_stages["two_unattributed_ms"] = two_total_ms - sum(
                    two_stages[key]
                    for key in (
                        "two_fetch_ms",
                        "two_normalize_ms",
                        "two_raw_cleanup_ms",
                    )
                )
                three_stages["three_unattributed_ms"] = three_total_ms - sum(
                    three_stages[key]
                    for key in THREE_COMPONENTS
                    if key != "three_unattributed_ms"
                )

                if two_rows != three_rows:
                    raise RuntimeError(f"timed output mismatch for {path}")
                if len(two_rows) != source_sample["rows"]:
                    raise RuntimeError(f"row-count mismatch for {path}")
                digest = fingerprint(two_rows)
                if digest != source_sample["fingerprint"]:
                    raise RuntimeError(f"fingerprint mismatch for {path}")
                expected_calls = (source_sample["rows"] + source["chunk"] - 1) // source[
                    "chunk"
                ]
                if three_stages["metadata_calls"] != expected_calls:
                    raise RuntimeError(f"Metadata-call mismatch for {path}")

                bytes_returned = output_bytes(two_rows)
                started = time.perf_counter()
                del two_rows
                two_release_ms = (time.perf_counter() - started) * 1_000
                started = time.perf_counter()
                del three_rows
                three_release_ms = (time.perf_counter() - started) * 1_000
                started = time.perf_counter()
                gc.collect()
                gc_collect_ms = (time.perf_counter() - started) * 1_000

                sample = {
                    "repeat": repeat,
                    "sequence_position": sequence_position,
                    "source_index": index,
                    "path": path,
                    "order": "two_first" if two_first else "three_first",
                    "rows": source_sample["rows"],
                    "metadata_calls": expected_calls,
                    "output_utf8_bytes": bytes_returned,
                    "bytes_per_row": round(bytes_returned / source_sample["rows"], 6),
                    "fingerprint": digest,
                    "two_total_ms": round(two_total_ms, 6),
                    **{
                        key: round(two_stages[key], 6)
                        for key in TWO_COMPONENTS
                    },
                    "three_total_ms": round(three_total_ms, 6),
                    "structure_ms": round(three_stages["structure_ms"], 6),
                    **{
                        key: round(three_stages[key], 6)
                        for key in THREE_COMPONENTS
                    },
                    "metadata_fetch_calls_ms": [
                        round(value, 6)
                        for value in three_stages["metadata_fetch_calls_ms"]
                    ],
                    "metadata_batches": [
                        {
                            "size": batch["size"],
                            "request_build_ms": round(batch["request_build_ms"], 9),
                            "raw_fetch_ms": round(batch["raw_fetch_ms"], 9),
                            "map_ms": round(batch["map_ms"], 9),
                            "raw_cleanup_ms": round(batch["raw_cleanup_ms"], 9),
                        }
                        for batch in three_stages["metadata_batches"]
                    ],
                    "two_release_ms": round(two_release_ms, 6),
                    "three_release_ms": round(three_release_ms, 6),
                    "gc_collect_ms": round(gc_collect_ms, 6),
                }
                output["samples"].append(sample)

                done += 1
                if done % 20 == 0 or done == total:
                    log(f"      ... {done}/{total}")
                    output_path.write_text(json.dumps(output, indent=2))

        output["engine_metrics_after_timing"] = adapter["metrics"]()
        output["host_after_timing"] = host_snapshot()
        log("collecting representative execution plans after timing ...")
        output["plans"] = adapter["plans"]()
        output["validation"] = validate_samples(
            output["samples"], source, indices, args.repeats
        )
        output["aggregate"] = aggregate(output["samples"])
        output["run_finished_at"] = utc_now()
        output["status"] = "complete"
        output_path.write_text(json.dumps(output, indent=2))
        log(f"wrote {args.out}")

        print(f"{args.engine} complete subtree breakdown")
        print(
            f"  samples={len(output['samples'])} paths={len(indices)} "
            f"repeats={args.repeats}"
        )
        print(
            "  two p50/p95="
            f"{output['aggregate']['two_total_ms']['p50_ms']:.3f}/"
            f"{output['aggregate']['two_total_ms']['p95_ms']:.3f} ms"
        )
        print(
            "  three p50/p95="
            f"{output['aggregate']['three_total_ms']['p50_ms']:.3f}/"
            f"{output['aggregate']['three_total_ms']['p95_ms']:.3f} ms"
        )
    finally:
        if owned and not args.keep_engine_data:
            try:
                if adapter is not None:
                    adapter["cleanup"]()
                else:
                    emergency_cleanup(args.engine, args)
                log(f"cleaned up {args.engine} complete-breakdown data")
            finally:
                if adapter is not None:
                    adapter["close"]()
        elif adapter is not None:
            adapter["close"]()
        lock_handle.close()


if __name__ == "__main__":
    main()
