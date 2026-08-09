#!/usr/bin/env python3
"""Causal benchmark for MongoDB/PostgreSQL ``get_children`` overhead.

The benchmark creates a small, uniquely named synthetic dataset in both
engines.  It does not read, alter, or drop any existing benchmark collection
or table.  Exact fanouts and four query arms separate:

* ``empty``: fixed request, planning, executor, and B-tree seek cost;
* ``covered_id``: covered index traversal plus one returned ID per child;
* ``covered_full``: covered traversal plus title/summary projection and output;
* ``noncovered_full``: the same full output with document/heap access.

PostgreSQL is measured through one immediately prepared connection and one
connection with automatic preparation disabled.  MongoDB command monitoring
records the driver command boundary.  Separate, untimed profiler/EXPLAIN
passes collect server evidence without contaminating the primary latency run.

All temporary object names begin with ``children_rc_`` and include a random
run ID.  Pre-existing objects are never reused or removed.  Temporary objects
are dropped in ``finally`` unless ``--keep-temporary`` is explicitly supplied.
"""

from __future__ import annotations

import argparse
import base64
import gc
import hashlib
import json
import math
import os
import platform
import random
import re
import statistics
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

TREE_ID = "children_rc"
ARMS = ("empty", "covered_id", "covered_full", "noncovered_full")
CONFIGURATIONS = ("mongodb", "postgres_prepared", "postgres_unprepared")
RUN_ID_RE = re.compile(r"^[a-z0-9]{4,20}$")

Rows = list[tuple[Any, ...]]


def log(message: str) -> None:
    print(message, flush=True)


def percentile(values: Sequence[float], pct: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(
        len(ordered) - 1,
        round(pct / 100 * (len(ordered) - 1)),
    )
    return round(float(ordered[index]), 6)


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


def deterministic_text(label: str, length: int) -> str:
    """Return deterministic printable ASCII with exactly ``length`` bytes."""
    if length == 0:
        return ""
    output = bytearray()
    counter = 0
    while len(output) < length:
        block = hashlib.sha256(
            f"{label}|{counter}".encode()
        ).digest()
        output.extend(base64.urlsafe_b64encode(block).rstrip(b"="))
        counter += 1
    return bytes(output[:length]).decode("ascii")


def parse_fanouts(text: str) -> tuple[int, ...]:
    try:
        values = tuple(sorted({int(value) for value in text.split(",")}))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "fanouts must be comma-separated integers"
        ) from exc
    if not values or values[0] != 0:
        raise argparse.ArgumentTypeError("fanouts must include 0")
    if values[0] < 0 or values[-1] > 10_000:
        raise argparse.ArgumentTypeError(
            "fanouts must be between 0 and 10000"
        )
    return values


def quote_pg_identifier(identifier: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", identifier):
        raise ValueError(f"unsafe PostgreSQL identifier: {identifier!r}")
    return f'"{identifier}"'


def build_dataset(
    fanouts: tuple[int, ...],
    parents_per_fanout: int,
    title_bytes: int,
    summary_bytes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Rows]]:
    documents: list[dict[str, Any]] = []
    parents: list[dict[str, Any]] = []
    expected: dict[str, Rows] = {}
    parent_counter = 0
    for fanout in fanouts:
        for group_index in range(parents_per_fanout):
            parent_id = f"p{parent_counter:012d}"
            parent_counter += 1
            rows: Rows = []
            for child_index in range(fanout):
                node_id = f"n{parent_counter:012d}{child_index:06d}"
                path = f"/{parent_id}/{child_index:06d}"
                title = deterministic_text(
                    f"title|{parent_id}|{child_index}",
                    title_bytes,
                )
                summary = deterministic_text(
                    f"summary|{parent_id}|{child_index}",
                    summary_bytes,
                )
                documents.append({
                    "_id": node_id,
                    "tree_id": TREE_ID,
                    "parent_id": parent_id,
                    "path": path,
                    "node_id": node_id,
                    "title": title,
                    "summary": summary,
                    "cover_tag": True,
                })
                rows.append((node_id, title, summary))
            rows.sort(key=lambda row: row[0])
            expected[parent_id] = rows
            parents.append({
                "parent_id": parent_id,
                "fanout": fanout,
                "group_index": group_index,
            })
    return documents, parents, expected


def make_duration_listener(monitoring: Any) -> Any:
    class DurationListener(monitoring.CommandListener):
        def __init__(self) -> None:
            self.events: list[tuple[str, int]] = []

        def started(self, event: Any) -> None:
            del event

        def succeeded(self, event: Any) -> None:
            self.events.append(
                (event.command_name, event.duration_micros)
            )

        def failed(self, event: Any) -> None:
            raise RuntimeError(
                f"MongoDB command failed: {event.failure}"
            )

        def reset(self) -> None:
            self.events.clear()

    return DurationListener()


def mongo_command(
    collection: str,
    narrow_index: str,
    cover_index: str,
    arm: str,
    parent_id: str,
    batch_size: int,
    comment: str | None = None,
) -> dict[str, Any]:
    if arm in ("empty", "covered_id"):
        projection = {"_id": 0, "node_id": 1}
        filter_document = {
            "tree_id": TREE_ID,
            "parent_id": parent_id,
        }
        hint = narrow_index
    elif arm == "covered_full":
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
        hint = cover_index
    elif arm == "noncovered_full":
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
        hint = narrow_index
    else:
        raise ValueError(f"unknown arm: {arm}")

    command: dict[str, Any] = {
        "find": collection,
        "filter": filter_document,
        "projection": projection,
        "sort": {"path": 1, "node_id": 1},
        "hint": hint,
        "batchSize": batch_size,
        "singleBatch": True,
    }
    if comment is not None:
        command["comment"] = comment
    return command


def mongo_rows(database: Any, command: dict[str, Any], arm: str) -> Rows:
    result = database.command(command)
    batch = result["cursor"]["firstBatch"]
    if arm in ("empty", "covered_id"):
        return normalize((row.get("node_id"),) for row in batch)
    return normalize(
        (row.get("node_id"), row.get("title"), row.get("summary"))
        for row in batch
    )


def postgres_query(
    table_qualified: str,
    arm: str,
) -> str:
    if arm in ("empty", "covered_id"):
        return (
            f"SELECT node_id FROM {table_qualified} "
            "WHERE tree_id=%s AND parent_id=%s "
            "ORDER BY path,node_id"
        )
    if arm == "covered_full":
        return (
            f"SELECT node_id,title,summary FROM {table_qualified} "
            "WHERE tree_id=%s AND parent_id=%s AND cover_tag "
            "ORDER BY path,node_id"
        )
    if arm == "noncovered_full":
        return (
            f"SELECT node_id,title,summary FROM {table_qualified} "
            "WHERE tree_id=%s AND parent_id=%s "
            "ORDER BY path,node_id"
        )
    raise ValueError(f"unknown arm: {arm}")


def run_postgres(
    connection: Any,
    sql: str,
    parent_id: str,
) -> Rows:
    return normalize(
        connection.execute(sql, (TREE_ID, parent_id)).fetchall()
    )


def recursive_values(
    value: Any,
    key: str,
) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        if key in value:
            found.append(value[key])
        for child in value.values():
            found.extend(recursive_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(recursive_values(child, key))
    return found


def mongo_plan_metrics(
    database: Any,
    command: dict[str, Any],
) -> dict[str, Any]:
    explanation = database.command(
        "explain",
        command,
        verbosity="executionStats",
    )
    stats = explanation["executionStats"]
    winning = explanation.get("queryPlanner", {}).get("winningPlan", {})
    execution_stages = stats.get("executionStages", {})
    return {
        "stages": sorted(
            {
                str(stage)
                for stage in (
                    recursive_values(winning, "stage")
                    + recursive_values(execution_stages, "stage")
                )
            }
        ),
        "index_names": sorted(
            {
                str(name)
                for name in (
                    recursive_values(winning, "indexName")
                    + recursive_values(execution_stages, "indexName")
                )
            }
        ),
        "keys_examined": int(stats.get("totalKeysExamined", 0)),
        "documents_examined": int(
            stats.get("totalDocsExamined", 0)
        ),
        "n_returned": int(stats.get("nReturned", 0)),
        "execution_time_ms": stats.get("executionTimeMillis"),
    }


def visit_pg_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [plan]
    for child in plan.get("Plans", []):
        nodes.extend(visit_pg_plan(child))
    return nodes


def postgres_plan_metrics(
    connection: Any,
    sql: str,
    parent_id: str,
) -> dict[str, Any]:
    result = connection.execute(
        "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, "
        "SUMMARY ON, FORMAT JSON) " + sql,
        (TREE_ID, parent_id),
    ).fetchone()[0][0]
    nodes = visit_pg_plan(result["Plan"])
    return {
        "node_types": [str(node.get("Node Type")) for node in nodes],
        "index_names": sorted({
            str(node["Index Name"])
            for node in nodes
            if "Index Name" in node
        }),
        "actual_rows": int(result["Plan"].get("Actual Rows", 0)),
        "heap_fetches": sum(
            int(node.get("Heap Fetches", 0)) for node in nodes
        ),
        "shared_hit_blocks": sum(
            int(node.get("Shared Hit Blocks", 0)) for node in nodes
        ),
        "shared_read_blocks": sum(
            int(node.get("Shared Read Blocks", 0)) for node in nodes
        ),
        "planning_us": round(
            float(result.get("Planning Time", 0.0)) * 1_000,
            6,
        ),
        "execution_us": round(
            float(result.get("Execution Time", 0.0)) * 1_000,
            6,
        ),
    }


def gate_mongo_plan(
    arm: str,
    fanout: int,
    metrics: dict[str, Any],
    narrow_index: str,
    cover_index: str,
) -> None:
    expected_index = (
        cover_index if arm == "covered_full" else narrow_index
    )
    if expected_index not in metrics["index_names"]:
        raise RuntimeError(
            f"MongoDB plan gate failed for {arm}/{fanout}: "
            f"expected {expected_index}, got {metrics['index_names']}"
        )
    stages = set(metrics["stages"])
    if "IXSCAN" not in stages:
        raise RuntimeError(
            f"MongoDB plan gate failed for {arm}/{fanout}: no IXSCAN"
        )
    if "SORT" in stages:
        raise RuntimeError(
            f"MongoDB plan gate failed for {arm}/{fanout}: blocking SORT"
        )
    if metrics["n_returned"] != fanout:
        raise RuntimeError(
            f"MongoDB plan gate failed for {arm}/{fanout}: "
            f"returned {metrics['n_returned']}"
        )
    if metrics["keys_examined"] != fanout:
        raise RuntimeError(
            f"MongoDB plan gate failed for {arm}/{fanout}: "
            f"keys {metrics['keys_examined']} != {fanout}"
        )
    if arm == "noncovered_full":
        if "FETCH" not in stages:
            raise RuntimeError(
                f"MongoDB plan gate failed for {arm}/{fanout}: no FETCH"
            )
        if metrics["documents_examined"] != fanout:
            raise RuntimeError(
                f"MongoDB plan gate failed for {arm}/{fanout}: "
                f"documents {metrics['documents_examined']} != {fanout}"
            )
    else:
        if "FETCH" in stages or metrics["documents_examined"] != 0:
            raise RuntimeError(
                f"MongoDB covered plan gate failed for {arm}/{fanout}"
            )


def gate_postgres_plan(
    arm: str,
    fanout: int,
    metrics: dict[str, Any],
    narrow_index: str,
    cover_index: str,
) -> None:
    expected_index = (
        cover_index if arm == "covered_full" else narrow_index
    )
    if expected_index not in metrics["index_names"]:
        raise RuntimeError(
            f"PostgreSQL plan gate failed for {arm}/{fanout}: "
            f"expected {expected_index}, got {metrics['index_names']}"
        )
    node_types = set(metrics["node_types"])
    if "Sort" in node_types:
        raise RuntimeError(
            f"PostgreSQL plan gate failed for {arm}/{fanout}: blocking Sort"
        )
    if arm == "noncovered_full":
        if "Index Scan" not in node_types or "Index Only Scan" in node_types:
            raise RuntimeError(
                f"PostgreSQL plan gate failed for {arm}/{fanout}: "
                f"{metrics['node_types']}"
            )
    else:
        if "Index Only Scan" not in node_types:
            raise RuntimeError(
                f"PostgreSQL covered plan gate failed for {arm}/{fanout}: "
                f"{metrics['node_types']}"
            )
        if metrics["heap_fetches"] != 0:
            raise RuntimeError(
                f"PostgreSQL covered plan gate failed for {arm}/{fanout}: "
                f"heap fetches {metrics['heap_fetches']}"
            )
    if metrics["actual_rows"] != fanout:
        raise RuntimeError(
            f"PostgreSQL plan gate failed for {arm}/{fanout}: "
            f"returned {metrics['actual_rows']}"
        )


def expected_rows_for_arm(
    arm: str,
    parent_id: str,
    expected_full: dict[str, Rows],
) -> Rows:
    rows = expected_full[parent_id]
    if arm in ("empty", "covered_id"):
        return [(row[0],) for row in rows]
    return rows


def arm_inputs(
    arm: str,
    parents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if arm == "empty":
        return [parent for parent in parents if parent["fanout"] == 0]
    return parents


def summarize_samples(
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for arm in ARMS:
        arm_samples = [
            sample for sample in samples if sample["arm"] == arm
        ]
        output[arm] = {}
        fanouts = sorted({sample["fanout"] for sample in arm_samples})
        for configuration in CONFIGURATIONS:
            output[arm][configuration] = {}
            for fanout in fanouts:
                group = [
                    sample
                    for sample in arm_samples
                    if sample["fanout"] == fanout
                ]
                per_parent = [
                    statistics.median(
                        sample["times_us"][configuration]
                    )
                    for sample in group
                ]
                output[arm][configuration][str(fanout)] = {
                    "parents": len(group),
                    "repeats": (
                        len(group[0]["times_us"][configuration])
                        if group
                        else 0
                    ),
                    "p50_us": percentile(per_parent, 50),
                    "p95_us": percentile(per_parent, 95),
                    "mean_us": (
                        round(statistics.mean(per_parent), 6)
                        if per_parent
                        else 0.0
                    ),
                }
    return output


def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> dict[str, Any]:
    if len(xs) < 3 or len(set(xs)) < 2:
        return {"available": False}
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(ys)
    ss_x = sum((value - x_mean) ** 2 for value in xs)
    slope = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in zip(xs, ys)
    ) / ss_x
    intercept = y_mean - slope * x_mean
    residuals = [
        y - (intercept + slope * x)
        for x, y in zip(xs, ys)
    ]
    ss_residual = sum(value * value for value in residuals)
    ss_total = sum((value - y_mean) ** 2 for value in ys)
    residual_variance = ss_residual / (len(xs) - 2)
    slope_se = math.sqrt(residual_variance / ss_x)
    intercept_se = math.sqrt(
        residual_variance
        * (1 / len(xs) + x_mean * x_mean / ss_x)
    )
    return {
        "available": True,
        "observations": len(xs),
        "intercept_us": round(intercept, 6),
        "intercept_se_us": round(intercept_se, 6),
        "slope_us_per_child": round(slope, 6),
        "slope_se_us_per_child": round(slope_se, 6),
        "r_squared": round(
            1.0 - ss_residual / ss_total if ss_total else 0.0,
            6,
        ),
    }


def derive_fits(samples: list[dict[str, Any]]) -> dict[str, Any]:
    fits: dict[str, Any] = {}
    for arm in ARMS:
        arm_samples = [
            sample for sample in samples if sample["arm"] == arm
        ]
        fits[arm] = {}
        for configuration in CONFIGURATIONS:
            xs = [float(sample["fanout"]) for sample in arm_samples]
            ys = [
                float(statistics.median(
                    sample["times_us"][configuration]
                ))
                for sample in arm_samples
            ]
            fits[arm][configuration] = linear_fit(xs, ys)
    return fits


def summarize_boundary_samples(
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for arm in ARMS:
        arm_samples = [
            sample for sample in samples if sample["arm"] == arm
        ]
        output[arm] = {}
        fanouts = sorted({sample["fanout"] for sample in arm_samples})
        for fanout in fanouts:
            group = [
                sample
                for sample in arm_samples
                if sample["fanout"] == fanout
            ]
            output[arm][str(fanout)] = {}
            for field in ("wall_us", "command_us", "outside_command_us"):
                per_parent = [
                    statistics.median(sample[field])
                    for sample in group
                ]
                output[arm][str(fanout)][field] = {
                    "parents": len(group),
                    "repeats": (
                        len(group[0][field]) if group else 0
                    ),
                    "p50_us": percentile(per_parent, 50),
                    "p95_us": percentile(per_parent, 95),
                    "mean_us": (
                        round(statistics.mean(per_parent), 6)
                        if per_parent
                        else 0.0
                    ),
                }
    return output


def derive_boundary_fits(
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    fits: dict[str, Any] = {}
    for arm in ARMS:
        arm_samples = [
            sample for sample in samples if sample["arm"] == arm
        ]
        fits[arm] = {}
        for field in ("wall_us", "command_us", "outside_command_us"):
            xs = [float(sample["fanout"]) for sample in arm_samples]
            ys = [
                float(statistics.median(sample[field]))
                for sample in arm_samples
            ]
            fits[arm][field] = linear_fit(xs, ys)
    return fits


def summarize_values(values: list[float]) -> dict[str, Any]:
    return {
        "observations": len(values),
        "p50_us": percentile(values, 50),
        "p95_us": percentile(values, 95),
        "mean_us": (
            round(statistics.mean(values), 6) if values else 0.0
        ),
    }


def derive_contrasts(samples: list[dict[str, Any]]) -> dict[str, Any]:
    by_key = {
        (sample["arm"], sample["parent_id"]): sample
        for sample in samples
    }
    fanouts = sorted({
        int(sample["fanout"])
        for sample in samples
        if sample["arm"] != "empty"
    })
    contrasts: dict[str, Any] = {
        "within_configuration": {},
        "cross_engine": {},
        "postgres_preparation": {},
    }

    for configuration in CONFIGURATIONS:
        contrasts["within_configuration"][configuration] = {}
        for fanout in fanouts:
            matching = [
                sample
                for sample in samples
                if sample["arm"] == "covered_id"
                and int(sample["fanout"]) == fanout
            ]
            payload_output: list[float] = []
            base_record_bundle: list[float] = []
            for id_sample in matching:
                parent_id = id_sample["parent_id"]
                covered_full = by_key[("covered_full", parent_id)]
                noncovered_full = by_key[
                    ("noncovered_full", parent_id)
                ]
                id_median = statistics.median(
                    id_sample["times_us"][configuration]
                )
                covered_median = statistics.median(
                    covered_full["times_us"][configuration]
                )
                noncovered_median = statistics.median(
                    noncovered_full["times_us"][configuration]
                )
                payload_output.append(covered_median - id_median)
                base_record_bundle.append(
                    noncovered_median - covered_median
                )
            contrasts["within_configuration"][configuration][
                str(fanout)
            ] = {
                "covered_full_minus_covered_id": summarize_values(
                    payload_output
                ),
                "noncovered_full_minus_covered_full": summarize_values(
                    base_record_bundle
                ),
            }

    for pg_configuration in (
        "postgres_prepared",
        "postgres_unprepared",
    ):
        comparison_name = f"mongodb_minus_{pg_configuration}"
        contrasts["cross_engine"][comparison_name] = {}
        for arm in ARMS:
            contrasts["cross_engine"][comparison_name][arm] = {}
            arm_fanouts = sorted({
                int(sample["fanout"])
                for sample in samples
                if sample["arm"] == arm
            })
            for fanout in arm_fanouts:
                deltas = []
                for sample in samples:
                    if (
                        sample["arm"] != arm
                        or int(sample["fanout"]) != fanout
                    ):
                        continue
                    deltas.append(
                        statistics.median(
                            sample["times_us"]["mongodb"]
                        )
                        - statistics.median(
                            sample["times_us"][pg_configuration]
                        )
                    )
                contrasts["cross_engine"][comparison_name][arm][
                    str(fanout)
                ] = summarize_values(deltas)

    for arm in ARMS:
        contrasts["postgres_preparation"][arm] = {}
        arm_fanouts = sorted({
            int(sample["fanout"])
            for sample in samples
            if sample["arm"] == arm
        })
        for fanout in arm_fanouts:
            deltas = []
            for sample in samples:
                if (
                    sample["arm"] != arm
                    or int(sample["fanout"]) != fanout
                ):
                    continue
                deltas.append(
                    statistics.median(
                        sample["times_us"]["postgres_unprepared"]
                    )
                    - statistics.median(
                        sample["times_us"]["postgres_prepared"]
                    )
                )
            contrasts["postgres_preparation"][arm][str(fanout)] = (
                summarize_values(deltas)
            )
    return contrasts


def summarize_flat_metrics(
    records: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    keys = sorted({
        (record["arm"], int(record["fanout"]))
        for record in records
    })
    for arm, fanout in keys:
        group = [
            record
            for record in records
            if record["arm"] == arm
            and int(record["fanout"]) == fanout
        ]
        output.setdefault(arm, {})[str(fanout)] = {}
        for field in fields:
            values = [float(record[field]) for record in group]
            output[arm][str(fanout)][field] = {
                "observations": len(values),
                "p50": percentile(values, 50),
                "p95": percentile(values, 95),
                "mean": round(statistics.mean(values), 6),
            }
    return output


def profile_mongodb(
    database: Any,
    collection: str,
    narrow_index: str,
    cover_index: str,
    parents: list[dict[str, Any]],
    batch_size: int,
    repeats: int,
    inputs_per_fanout: int,
    profile_size_mb: int,
    tag_prefix: str,
) -> dict[str, Any]:
    before = database.command({"profile": -1})
    if before["was"] != 0:
        raise RuntimeError(
            "refusing to replace an enabled MongoDB profiler"
        )
    if "system.profile" in database.list_collection_names():
        raise RuntimeError(
            "refusing to replace an existing system.profile collection"
        )

    selected: dict[int, list[dict[str, Any]]] = {}
    for parent in parents:
        selected.setdefault(parent["fanout"], [])
        if len(selected[parent["fanout"]]) < inputs_per_fanout:
            selected[parent["fanout"]].append(parent)

    expected_records = 0
    profile_created = False
    records: list[dict[str, Any]] = []
    try:
        database.create_collection(
            "system.profile",
            capped=True,
            size=profile_size_mb * 1024 * 1024,
        )
        profile_created = True
        database.command("profile", 2, slowms=0, sampleRate=1.0)
        for arm in ARMS:
            candidates = (
                selected[0]
                if arm == "empty"
                else [
                    parent
                    for fanout in sorted(selected)
                    for parent in selected[fanout]
                ]
            )
            for repeat in range(repeats):
                for input_index, parent in enumerate(candidates):
                    comment = (
                        f"{tag_prefix}|{arm}|{parent['fanout']}|"
                        f"{input_index}|{repeat}"
                    )
                    command = mongo_command(
                        collection,
                        narrow_index,
                        cover_index,
                        arm,
                        parent["parent_id"],
                        batch_size,
                        comment=comment,
                    )
                    mongo_rows(database, command, arm)
                    expected_records += 1
        database.command(
            "profile",
            before["was"],
            slowms=before.get("slowms", 100),
            sampleRate=before.get("sampleRate", 1.0),
        )

        raw_records = list(database["system.profile"].find({
            "command.comment": {"$regex": f"^{re.escape(tag_prefix)}"}
        }))
        if len(raw_records) != expected_records:
            raise RuntimeError(
                f"MongoDB profile record mismatch: "
                f"{len(raw_records)} != {expected_records}"
            )
        seen: set[str] = set()
        for record in raw_records:
            comment = record["command"]["comment"]
            if comment in seen:
                raise RuntimeError(
                    f"duplicate MongoDB profile tag: {comment}"
                )
            seen.add(comment)
            _, arm, fanout_text, input_text, repeat_text = (
                comment.split("|")
            )
            execution = record.get("execStats", {})
            records.append({
                "arm": arm,
                "fanout": int(fanout_text),
                "input_index": int(input_text),
                "repeat": int(repeat_text),
                "cpu_us": round(
                    float(record.get("cpuNanos", 0)) / 1_000,
                    6,
                ),
                "planning_us": float(
                    record.get("planningTimeMicros", 0)
                ),
                "keys_examined": int(record.get("keysExamined", 0)),
                "documents_examined": int(
                    record.get("docsExamined", 0)
                ),
                "n_returned": int(record.get("nreturned", 0)),
                "response_bytes": int(
                    record.get("responseLength", 0)
                ),
                "stages": sorted({
                    str(stage)
                    for stage in recursive_values(execution, "stage")
                }),
            })
    finally:
        status = database.command({"profile": -1})
        if status["was"] != before["was"]:
            database.command(
                "profile",
                before["was"],
                slowms=before.get("slowms", 100),
                sampleRate=before.get("sampleRate", 1.0),
            )
        if (
            profile_created
            and "system.profile" in database.list_collection_names()
        ):
            database.drop_collection("system.profile")

    return {
        "records": records,
        "summary": summarize_flat_metrics(
            records,
            (
                "cpu_us",
                "planning_us",
                "keys_examined",
                "documents_examined",
                "n_returned",
                "response_bytes",
            ),
        ),
    }


def profile_postgres(
    connection: Any,
    sql_by_arm: dict[str, str],
    parents: list[dict[str, Any]],
    repeats: int,
    inputs_per_fanout: int,
) -> dict[str, Any]:
    selected: dict[int, list[dict[str, Any]]] = {}
    for parent in parents:
        selected.setdefault(parent["fanout"], [])
        if len(selected[parent["fanout"]]) < inputs_per_fanout:
            selected[parent["fanout"]].append(parent)

    records: list[dict[str, Any]] = []
    for arm in ARMS:
        candidates = (
            selected[0]
            if arm == "empty"
            else [
                parent
                for fanout in sorted(selected)
                for parent in selected[fanout]
            ]
        )
        for repeat in range(repeats):
            for input_index, parent in enumerate(candidates):
                metrics = postgres_plan_metrics(
                    connection,
                    sql_by_arm[arm],
                    parent["parent_id"],
                )
                records.append({
                    "arm": arm,
                    "fanout": parent["fanout"],
                    "input_index": input_index,
                    "repeat": repeat,
                    "planning_us": metrics["planning_us"],
                    "execution_us": metrics["execution_us"],
                    "heap_fetches": metrics["heap_fetches"],
                    "shared_hit_blocks": metrics["shared_hit_blocks"],
                    "shared_read_blocks": metrics["shared_read_blocks"],
                    "actual_rows": metrics["actual_rows"],
                    "node_types": metrics["node_types"],
                    "index_names": metrics["index_names"],
                })
    return {
        "records": records,
        "summary": summarize_flat_metrics(
            records,
            (
                "planning_us",
                "execution_us",
                "heap_fetches",
                "shared_hit_blocks",
                "shared_read_blocks",
                "actual_rows",
            ),
        ),
    }


def prepared_statement_state(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT name,statement,parameter_types::text,
               generic_plans,custom_plans
        FROM pg_prepared_statements
        ORDER BY name
        """
    ).fetchall()
    return [
        {
            "name": row[0],
            "statement": row[1],
            "parameter_types": row[2],
            "generic_plans": int(row[3]),
            "custom_plans": int(row[4]),
        }
        for row in rows
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run an isolated matched get_children causal benchmark. "
            "No existing benchmark collection or table is modified."
        )
    )
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
        "--fanouts",
        type=parse_fanouts,
        default=parse_fanouts("0,1,2,4,8,16,32,64,128"),
    )
    parser.add_argument("--parents-per-fanout", type=int, default=30)
    parser.add_argument("--repeats", type=int, default=50)
    parser.add_argument("--boundary-repeats", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--server-repeats", type=int, default=3)
    parser.add_argument(
        "--server-inputs-per-fanout",
        type=int,
        default=5,
    )
    parser.add_argument("--profile-size-mb", type=int, default=128)
    parser.add_argument("--title-bytes", type=int, default=32)
    parser.add_argument("--summary-bytes", type=int, default=256)
    parser.add_argument("--seed", type=int, default=20260724)
    parser.add_argument(
        "--run-id",
        help="4-20 lowercase letters/digits; random when omitted",
    )
    parser.add_argument(
        "--out",
        help="JSON output path; a unique path is chosen when omitted",
    )
    parser.add_argument(
        "--skip-server-profile",
        action="store_true",
        help="skip MongoDB profiler and PostgreSQL EXPLAIN sample passes",
    )
    parser.add_argument(
        "--keep-temporary",
        action="store_true",
        help="keep the uniquely named temporary collection and table",
    )
    args = parser.parse_args()

    positive = (
        args.parents_per_fanout,
        args.repeats,
        args.boundary_repeats,
        args.warmups,
        args.server_repeats,
        args.server_inputs_per_fanout,
        args.profile_size_mb,
    )
    if min(positive) <= 0:
        parser.error("counts, repeats, warmups, and profile size must be positive")
    if args.title_bytes < 0 or args.summary_bytes < 0:
        parser.error("payload byte lengths must be non-negative")
    if args.title_bytes + args.summary_bytes > 600:
        parser.error(
            "title-bytes + summary-bytes must not exceed 600 "
            "because MongoDB stores them in the covering index"
        )
    if args.run_id is not None and not RUN_ID_RE.fullmatch(args.run_id):
        parser.error(
            "run-id must contain 4-20 lowercase letters or digits"
        )
    return args


def main() -> None:
    args = parse_args()

    import psycopg
    from pymongo import MongoClient, monitoring

    run_id = args.run_id or uuid.uuid4().hex[:12]
    object_name = f"children_rc_{run_id}"
    narrow_index = f"{object_name}_narrow"
    cover_index = f"{object_name}_cover"
    pg_table = object_name
    pg_narrow_index = narrow_index
    pg_cover_index = cover_index

    if len(pg_cover_index) > 63:
        raise RuntimeError("generated PostgreSQL object name is too long")

    if args.out:
        out_path = Path(args.out)
    else:
        out_path = Path(
            "bench/db/runs/children_rootcause_20260724/"
        ) / f"{object_name}.json"
    if out_path.exists():
        raise RuntimeError(f"refusing to overwrite {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    documents, parents, expected_full = build_dataset(
        args.fanouts,
        args.parents_per_fanout,
        args.title_bytes,
        args.summary_bytes,
    )
    max_fanout = max(args.fanouts)
    batch_size = max_fanout + 1

    table_q = (
        f'{quote_pg_identifier("public")}.'
        f"{quote_pg_identifier(pg_table)}"
    )
    pg_narrow_q = quote_pg_identifier(pg_narrow_index)
    pg_cover_q = quote_pg_identifier(pg_cover_index)
    sql_by_arm = {
        arm: postgres_query(table_q, arm) for arm in ARMS
    }

    output: dict[str, Any] = {
        "run": {
            "status": "initializing",
            "run_id": run_id,
            "started_unix_s": time.time(),
            "repeats": args.repeats,
            "boundary_repeats": args.boundary_repeats,
            "warmups": args.warmups,
            "seed": args.seed,
        },
        "contract": {
            "operation": "get_children",
            "arms": {
                "empty": (
                    "same covered ID find/select shape with exactly zero rows"
                ),
                "covered_id": (
                    "narrow covering index; ordered node_id output"
                ),
                "covered_full": (
                    "wide covering index; ordered node_id,title,summary output"
                ),
                "noncovered_full": (
                    "narrow index plus document/heap access; same full output"
                ),
            },
            "configurations": list(CONFIGURATIONS),
            "postgres_prepared": "psycopg prepare_threshold=0",
            "postgres_unprepared": "psycopg prepare_threshold=None",
            "postgres_access_path": (
                "enable_seqscan=off and enable_bitmapscan=off; exact index "
                "names and covered/non-covered plans are gated before timing"
            ),
            "mongo_command_us": (
                "measured only in a separate listener-instrumented MongoDB "
                "phase; includes request encoding, network, server, response "
                "receive, and BSON decode"
            ),
            "primary_instrumentation": (
                "plain MongoClient without command listeners; PostgreSQL "
                "connections likewise have no event listener"
            ),
            "summary_unit": (
                "per-parent median across repeats, then percentile "
                "across parents"
            ),
            "output_validation": (
                "all arms and configurations checked against deterministic "
                "row fingerprints before timing"
            ),
            "contrast_scope": (
                "noncovered_full minus covered_full localizes the original "
                "document/heap-access bundle, but also changes from the wide "
                "covering index to the narrow original index"
            ),
        },
        "synthetic_dataset": {
            "tree_id": TREE_ID,
            "fanouts": list(args.fanouts),
            "parents_per_fanout": args.parents_per_fanout,
            "parents": len(parents),
            "child_rows": len(documents),
            "title_bytes": args.title_bytes,
            "summary_bytes": args.summary_bytes,
        },
        "temporary_objects": {
            "mongodb_collection": object_name,
            "postgresql_table": f"public.{pg_table}",
            "narrow_index": narrow_index,
            "cover_index": cover_index,
            "keep_requested": args.keep_temporary,
        },
        "validation": {},
        "plan_gates": {},
        "samples": [],
        "mongodb_boundary": {},
        "server_profiles": {},
        "cleanup": {},
    }

    def save() -> None:
        out_path.write_text(json.dumps(output, indent=2, default=str))

    save()

    mongo_client: Any = None
    mongo: Any = None
    mongo_boundary_client: Any = None
    mongo_boundary: Any = None
    pg_admin: Any = None
    pg_prepared: Any = None
    pg_unprepared: Any = None
    mongo_created = False
    pg_created = False
    failure: BaseException | None = None

    try:
        log(f"connecting; temporary object prefix: {object_name}")
        mongo_client = MongoClient(
            args.mongo_uri,
            serverSelectionTimeoutMS=5_000,
        )
        mongo = mongo_client[args.mongo_db]
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
            connection.execute("SET enable_seqscan = off")
            connection.execute("SET enable_bitmapscan = off")

        if object_name in mongo.list_collection_names():
            raise RuntimeError(
                f"refusing to reuse MongoDB collection {object_name}"
            )
        existing_pg = pg_admin.execute(
            "SELECT to_regclass(%s)",
            (f"public.{pg_table}",),
        ).fetchone()[0]
        if existing_pg is not None:
            raise RuntimeError(
                f"refusing to reuse PostgreSQL table public.{pg_table}"
            )

        output["run"]["status"] = "loading"
        save()
        log(
            f"loading {len(documents)} matched child rows "
            f"for {len(parents)} parents"
        )
        mongo.create_collection(object_name)
        mongo_created = True
        if documents:
            mongo[object_name].insert_many(documents, ordered=False)
        mongo[object_name].create_index(
            [
                ("tree_id", 1),
                ("parent_id", 1),
                ("path", 1),
                ("node_id", 1),
            ],
            name=narrow_index,
            unique=True,
        )
        mongo[object_name].create_index(
            [
                ("tree_id", 1),
                ("parent_id", 1),
                ("cover_tag", 1),
                ("path", 1),
                ("node_id", 1),
                ("title", 1),
                ("summary", 1),
            ],
            name=cover_index,
        )

        pg_admin.execute(f"""
            CREATE TABLE {table_q} (
                tree_id TEXT COLLATE "C" NOT NULL,
                parent_id TEXT COLLATE "C" NOT NULL,
                path TEXT COLLATE "C" NOT NULL,
                node_id TEXT COLLATE "C" NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                cover_tag BOOLEAN NOT NULL
            )
        """)
        pg_created = True
        if documents:
            with pg_admin.cursor().copy(
                f"COPY {table_q} "
                "(tree_id,parent_id,path,node_id,title,summary,cover_tag) "
                "FROM STDIN"
            ) as copy:
                for document in documents:
                    copy.write_row((
                        document["tree_id"],
                        document["parent_id"],
                        document["path"],
                        document["node_id"],
                        document["title"],
                        document["summary"],
                        document["cover_tag"],
                    ))
        pg_admin.execute(
            f"CREATE UNIQUE INDEX {pg_narrow_q} ON {table_q} "
            "(tree_id,parent_id,path,node_id)"
        )
        pg_admin.execute(
            f"CREATE INDEX {pg_cover_q} ON {table_q} "
            "(tree_id,parent_id,cover_tag,path,node_id) "
            "INCLUDE(title,summary) WHERE cover_tag"
        )
        pg_admin.execute(
            f"VACUUM (ANALYZE, PARALLEL 0) {table_q}"
        )

        mongo_count = mongo[object_name].count_documents({})
        pg_count = pg_admin.execute(
            f"SELECT count(*) FROM {table_q}"
        ).fetchone()[0]
        if mongo_count != len(documents) or pg_count != len(documents):
            raise RuntimeError(
                f"load count mismatch: MongoDB={mongo_count}, "
                f"PostgreSQL={pg_count}, expected={len(documents)}"
            )
        output["validation"]["loaded_rows_match"] = True

        log("checking access paths for every fanout")
        plan_gates: dict[str, Any] = {
            "mongodb": {},
            "postgresql": {},
        }
        first_by_fanout = {
            fanout: next(
                parent for parent in parents
                if parent["fanout"] == fanout
            )
            for fanout in args.fanouts
        }
        for arm in ARMS:
            candidates = (
                [first_by_fanout[0]]
                if arm == "empty"
                else [
                    first_by_fanout[fanout]
                    for fanout in args.fanouts
                ]
            )
            plan_gates["mongodb"][arm] = []
            plan_gates["postgresql"][arm] = []
            for parent in candidates:
                fanout = parent["fanout"]
                mongo_metrics = mongo_plan_metrics(
                    mongo,
                    mongo_command(
                        object_name,
                        narrow_index,
                        cover_index,
                        arm,
                        parent["parent_id"],
                        batch_size,
                    ),
                )
                gate_mongo_plan(
                    arm,
                    fanout,
                    mongo_metrics,
                    narrow_index,
                    cover_index,
                )
                plan_gates["mongodb"][arm].append({
                    "fanout": fanout,
                    **mongo_metrics,
                })

                pg_metrics = postgres_plan_metrics(
                    pg_admin,
                    sql_by_arm[arm],
                    parent["parent_id"],
                )
                gate_postgres_plan(
                    arm,
                    fanout,
                    pg_metrics,
                    pg_narrow_index,
                    pg_cover_index,
                )
                plan_gates["postgresql"][arm].append({
                    "fanout": fanout,
                    **pg_metrics,
                })
        output["plan_gates"] = plan_gates
        output["validation"]["all_plan_gates_passed"] = True
        save()

        log("validating every logical output and warming connections")
        query_functions: dict[str, Callable[[str, str], Rows]] = {
            "mongodb": lambda arm, parent_id: mongo_rows(
                mongo,
                mongo_command(
                    object_name,
                    narrow_index,
                    cover_index,
                    arm,
                    parent_id,
                    batch_size,
                ),
                arm,
            ),
            "postgres_prepared": lambda arm, parent_id: run_postgres(
                pg_prepared,
                sql_by_arm[arm],
                parent_id,
            ),
            "postgres_unprepared": lambda arm, parent_id: run_postgres(
                pg_unprepared,
                sql_by_arm[arm],
                parent_id,
            ),
        }
        validation_checks = 0
        for warmup in range(args.warmups):
            for arm in ARMS:
                for parent in arm_inputs(arm, parents):
                    expected = expected_rows_for_arm(
                        arm,
                        parent["parent_id"],
                        expected_full,
                    )
                    expected_fingerprint = fingerprint(expected)
                    for configuration in CONFIGURATIONS:
                        actual = query_functions[configuration](
                            arm,
                            parent["parent_id"],
                        )
                        if (
                            len(actual) != parent["fanout"]
                            or fingerprint(actual) != expected_fingerprint
                        ):
                            raise RuntimeError(
                                f"output mismatch: {arm} {configuration} "
                                f"{parent['parent_id']}"
                            )
                        validation_checks += 1
            log(f"  validation/warmup {warmup + 1}/{args.warmups}")
        output["validation"].update({
            "all_output_fingerprints_match": True,
            "output_checks": validation_checks,
        })
        output["postgres_prepared_statements_after_warmup"] = (
            prepared_statement_state(pg_prepared)
        )

        log("running primary interleaved latency measurements")
        samples: list[dict[str, Any]] = []
        sample_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for arm in ARMS:
            for parent in arm_inputs(arm, parents):
                sample = {
                    "arm": arm,
                    "parent_id": parent["parent_id"],
                    "fanout": parent["fanout"],
                    "rows": parent["fanout"],
                    "expected_fingerprint": fingerprint(
                        expected_rows_for_arm(
                            arm,
                            parent["parent_id"],
                            expected_full,
                        )
                    ),
                    "times_us": {
                        configuration: []
                        for configuration in CONFIGURATIONS
                    },
                }
                samples.append(sample)
                sample_by_key[(arm, parent["parent_id"])] = sample
        output["samples"] = samples
        output["run"]["status"] = "timing"
        save()

        work_units = [
            (sample["arm"], sample["parent_id"])
            for sample in samples
        ]
        for repeat in range(args.repeats):
            order = list(range(len(work_units)))
            random.Random(args.seed + repeat).shuffle(order)
            for position, work_index in enumerate(order):
                arm, parent_id = work_units[work_index]
                sample = sample_by_key[(arm, parent_id)]
                rotation = (repeat + position) % len(CONFIGURATIONS)
                config_order = (
                    CONFIGURATIONS[rotation:]
                    + CONFIGURATIONS[:rotation]
                )
                for configuration in config_order:
                    gc.disable()
                    try:
                        started = time.perf_counter_ns()
                        rows = query_functions[configuration](
                            arm,
                            parent_id,
                        )
                        elapsed_us = (
                            time.perf_counter_ns() - started
                        ) / 1_000
                    finally:
                        gc.enable()
                    if len(rows) != sample["rows"]:
                        raise RuntimeError(
                            f"timed row mismatch: {arm} {configuration} "
                            f"{parent_id}"
                        )
                    sample["times_us"][configuration].append(
                        round(elapsed_us, 6)
                    )
            log(f"  primary repeat {repeat + 1}/{args.repeats}")
            output["run"]["completed_repeats"] = repeat + 1
            save()

        output["summaries"] = summarize_samples(samples)
        output["fanout_fits"] = derive_fits(samples)
        output["contrasts"] = derive_contrasts(samples)
        output["postgres_prepared_statements_after_timing"] = (
            prepared_statement_state(pg_prepared)
        )
        save()

        log("running separate listener-instrumented MongoDB boundary pass")
        listener = make_duration_listener(monitoring)
        mongo_boundary_client = MongoClient(
            args.mongo_uri,
            serverSelectionTimeoutMS=5_000,
            event_listeners=[listener],
        )
        mongo_boundary = mongo_boundary_client[args.mongo_db]
        boundary_samples: list[dict[str, Any]] = []
        boundary_by_key: dict[
            tuple[str, str], dict[str, Any]
        ] = {}
        for arm in ARMS:
            for parent in arm_inputs(arm, parents):
                sample = {
                    "arm": arm,
                    "parent_id": parent["parent_id"],
                    "fanout": parent["fanout"],
                    "rows": parent["fanout"],
                    "wall_us": [],
                    "command_us": [],
                    "outside_command_us": [],
                }
                boundary_samples.append(sample)
                boundary_by_key[(arm, parent["parent_id"])] = sample

        # Establish the monitored connection and warm each query shape/input
        # before recording the boundary phase.
        mongo_boundary.command("ping")
        for sample in boundary_samples:
            rows = mongo_rows(
                mongo_boundary,
                mongo_command(
                    object_name,
                    narrow_index,
                    cover_index,
                    sample["arm"],
                    sample["parent_id"],
                    batch_size,
                ),
                sample["arm"],
            )
            if len(rows) != sample["rows"]:
                raise RuntimeError(
                    "MongoDB boundary warmup row mismatch: "
                    f"{sample['arm']} {sample['parent_id']}"
                )

        boundary_work = [
            (sample["arm"], sample["parent_id"])
            for sample in boundary_samples
        ]
        for repeat in range(args.boundary_repeats):
            order = list(range(len(boundary_work)))
            random.Random(
                args.seed + 100_000 + repeat
            ).shuffle(order)
            for work_index in order:
                arm, parent_id = boundary_work[work_index]
                sample = boundary_by_key[(arm, parent_id)]
                listener.reset()
                gc.disable()
                try:
                    started = time.perf_counter_ns()
                    rows = mongo_rows(
                        mongo_boundary,
                        mongo_command(
                            object_name,
                            narrow_index,
                            cover_index,
                            arm,
                            parent_id,
                            batch_size,
                        ),
                        arm,
                    )
                    elapsed_us = (
                        time.perf_counter_ns() - started
                    ) / 1_000
                finally:
                    gc.enable()
                if len(rows) != sample["rows"]:
                    raise RuntimeError(
                        "MongoDB boundary row mismatch: "
                        f"{arm} {parent_id}"
                    )
                if (
                    len(listener.events) != 1
                    or listener.events[0][0] != "find"
                ):
                    raise RuntimeError(
                        "expected exactly one monitored MongoDB find "
                        f"command, got {listener.events}"
                    )
                command_us = float(listener.events[0][1])
                sample["wall_us"].append(round(elapsed_us, 6))
                sample["command_us"].append(command_us)
                sample["outside_command_us"].append(
                    round(elapsed_us - command_us, 6)
                )
            log(
                f"  boundary repeat "
                f"{repeat + 1}/{args.boundary_repeats}"
            )
        output["mongodb_boundary"] = {
            "contract": (
                "separate MongoClient with a CommandListener; these wall "
                "times are not used in cross-engine primary comparisons"
            ),
            "samples": boundary_samples,
            "summaries": summarize_boundary_samples(boundary_samples),
            "fanout_fits": derive_boundary_fits(boundary_samples),
        }
        mongo_boundary_client.close()
        mongo_boundary_client = None
        mongo_boundary = None
        save()

        if not args.skip_server_profile:
            log("running separate MongoDB profiler pass")
            output["server_profiles"]["mongodb"] = profile_mongodb(
                mongo,
                object_name,
                narrow_index,
                cover_index,
                parents,
                batch_size,
                args.server_repeats,
                args.server_inputs_per_fanout,
                args.profile_size_mb,
                f"childrenrc{run_id}",
            )
            save()
            log("running separate PostgreSQL EXPLAIN pass")
            output["server_profiles"]["postgresql"] = profile_postgres(
                pg_admin,
                sql_by_arm,
                parents,
                args.server_repeats,
                args.server_inputs_per_fanout,
            )
        else:
            output["server_profiles"] = {
                "skipped": True,
                "reason": "--skip-server-profile",
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
        output["run"]["status"] = "complete"
        output["run"]["finished_unix_s"] = time.time()
        output["run"]["elapsed_s"] = round(
            output["run"]["finished_unix_s"]
            - output["run"]["started_unix_s"],
            3,
        )
    except BaseException as exc:
        failure = exc
        output["run"]["status"] = "failed"
        output["run"]["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for connection in (pg_prepared, pg_unprepared):
            if connection is not None:
                try:
                    connection.close()
                except Exception as exc:
                    output["cleanup"].setdefault(
                        "connection_errors", []
                    ).append(f"{type(exc).__name__}: {exc}")

        if args.keep_temporary:
            output["cleanup"].update({
                "temporary_objects_removed": False,
                "reason": "--keep-temporary",
            })
        else:
            if mongo_created and mongo is not None:
                try:
                    mongo.drop_collection(object_name)
                    output["cleanup"]["mongodb_collection_removed"] = True
                except Exception as exc:
                    output["cleanup"]["mongodb_collection_removed"] = False
                    output["cleanup"]["mongodb_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
            if pg_created and pg_admin is not None:
                try:
                    pg_admin.execute(f"DROP TABLE {table_q}")
                    output["cleanup"]["postgresql_table_removed"] = True
                except Exception as exc:
                    output["cleanup"]["postgresql_table_removed"] = False
                    output["cleanup"]["postgresql_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
            output["cleanup"]["temporary_objects_removed"] = bool(
                output["cleanup"].get(
                    "mongodb_collection_removed",
                    not mongo_created,
                )
                and output["cleanup"].get(
                    "postgresql_table_removed",
                    not pg_created,
                )
            )
            if (
                not output["cleanup"]["temporary_objects_removed"]
                and failure is None
            ):
                failure = RuntimeError(
                    "one or more temporary benchmark objects could not "
                    "be removed"
                )
                output["run"]["status"] = "failed"
                output["run"]["error"] = (
                    f"{type(failure).__name__}: {failure}"
                )

        if pg_admin is not None:
            try:
                pg_admin.close()
            except Exception as exc:
                output["cleanup"].setdefault(
                    "connection_errors", []
                ).append(f"{type(exc).__name__}: {exc}")
        if mongo_client is not None:
            try:
                mongo_client.close()
            except Exception as exc:
                output["cleanup"].setdefault(
                    "connection_errors", []
                ).append(f"{type(exc).__name__}: {exc}")
        if mongo_boundary_client is not None:
            try:
                mongo_boundary_client.close()
            except Exception as exc:
                output["cleanup"].setdefault(
                    "connection_errors", []
                ).append(f"{type(exc).__name__}: {exc}")
        save()

    print(json.dumps({
        "status": output["run"]["status"],
        "output": str(out_path),
        "temporary_objects": output["temporary_objects"],
        "cleanup": output["cleanup"],
        "fanout_fits": output.get("fanout_fits"),
    }, indent=2))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    main()
