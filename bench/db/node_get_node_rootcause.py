#!/usr/bin/env python3
"""Matched causal experiment for MongoDB/PostgreSQL get_node.

The experiment keeps the logical point-read contract fixed and intervenes on
four factors:

* MongoDB compound-index lookup with and without an explicit hint;
* PostgreSQL execution with server prepared statements enabled or disabled;
* an existing key versus a nearby missing key;
* covered ID-only output versus the complete get_node row.

End-to-end timings are collected with MongoDB profiling disabled.  A separate,
smaller MongoDB profiler phase records server CPU, planning, and execution
statistics under identical instrumentation for all MongoDB arms.
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
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

from pymongo import MongoClient, monitoring


TREE_ID = "base"
MONGO_NODES = "layout2_view"
MONGO_INDEX = "allops_tree_node"
PG_NODES = "layout2_pg_view"

CONFIGS = (
    "mongo_hinted",
    "mongo_unhinted",
    "pg_prepared",
    "pg_unprepared",
)
MONGO_CONFIGS = ("mongo_hinted", "mongo_unhinted")
PG_CONFIGS = ("pg_prepared", "pg_unprepared")
PROJECTIONS = ("id", "full")
OUTCOMES = ("hit", "miss")

FULL_MONGO_PROJECTION = {
    "_id": 0,
    "node_id": 1,
    "parent_id": 1,
    "depth": 1,
    "title": 1,
    "summary": 1,
    "start_index": 1,
    "end_index": 1,
}
ID_MONGO_PROJECTION = {"_id": 0, "node_id": 1}

FULL_COLUMNS = (
    "node_id,parent_id,depth,title,summary,start_index,end_index"
)
PG_SQL = {
    "id": (
        f"/* node_probe:id */ SELECT node_id FROM {PG_NODES} "
        "WHERE tree_id=%s AND node_id=%s"
    ),
    "full": (
        f"/* node_probe:full */ SELECT {FULL_COLUMNS} FROM {PG_NODES} "
        "WHERE tree_id=%s AND node_id=%s"
    ),
}


def log(message: str) -> None:
    print(message, flush=True)


def normalize(rows: Iterable[Sequence[Any]]) -> list[tuple[Any, ...]]:
    return [
        tuple("" if value is None else value for value in row)
        for row in rows
    ]


def percentile(values: Sequence[float], pct: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return round(float(ordered[index]), 6)


def metric_summary(
    samples: list[dict[str, Any]],
    field: str,
) -> dict[str, float]:
    per_input = [
        statistics.median(sample[field])
        for sample in samples
        if sample[field]
    ]
    return {
        "inputs": len(per_input),
        "p50": percentile(per_input, 50),
        "p95": percentile(per_input, 95),
        "mean": round(statistics.mean(per_input), 6) if per_input else 0.0,
    }


def paired_delta_summary(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    field: str,
) -> dict[str, float]:
    if len(left) != len(right):
        raise RuntimeError("paired sample lengths differ")
    deltas = [
        statistics.median(left_sample[field])
        - statistics.median(right_sample[field])
        for left_sample, right_sample in zip(left, right)
    ]
    return {
        "inputs": len(deltas),
        "p50": percentile(deltas, 50),
        "p95": percentile(deltas, 95),
        "mean": round(statistics.mean(deltas), 6) if deltas else 0.0,
    }


def fingerprint_update(
    digest: Any,
    input_id: str,
    rows: Sequence[Sequence[Any]],
) -> None:
    for value in (input_id, len(rows)):
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    for row in rows:
        for value in row:
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)


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


def mongo_rows(
    database: Any,
    mode: str,
    projection: str,
    node_id: str,
    comment: str | None = None,
) -> list[tuple[Any, ...]]:
    kwargs: dict[str, Any] = {}
    if mode == "hinted":
        kwargs["hint"] = MONGO_INDEX
    if comment is not None:
        kwargs["comment"] = comment
    row = database[MONGO_NODES].find_one(
        {"tree_id": TREE_ID, "node_id": node_id},
        ID_MONGO_PROJECTION
        if projection == "id"
        else FULL_MONGO_PROJECTION,
        **kwargs,
    )
    if row is None:
        return []
    if projection == "id":
        return normalize([(row.get("node_id"),)])
    return normalize([(
        row.get("node_id"),
        row.get("parent_id"),
        row.get("depth"),
        row.get("title"),
        row.get("summary"),
        row.get("start_index"),
        row.get("end_index"),
    )])


def pg_rows(
    connection: Any,
    projection: str,
    node_id: str,
) -> list[tuple[Any, ...]]:
    return normalize(
        connection.execute(
            PG_SQL[projection],
            (TREE_ID, node_id),
        ).fetchall()
    )


def query_rows(
    config: str,
    projection: str,
    node_id: str,
    mongo: Any,
    pg_prepared: Any,
    pg_unprepared: Any,
    comment: str | None = None,
) -> list[tuple[Any, ...]]:
    if config == "mongo_hinted":
        return mongo_rows(mongo, "hinted", projection, node_id, comment)
    if config == "mongo_unhinted":
        return mongo_rows(mongo, "unhinted", projection, node_id, comment)
    if config == "pg_prepared":
        return pg_rows(pg_prepared, projection, node_id)
    if config == "pg_unprepared":
        return pg_rows(pg_unprepared, projection, node_id)
    raise ValueError(config)


def load_hit_ids(connection: Any, count: int, seed: int) -> list[str]:
    percentages = (0.05, 0.1, 0.2, 0.5, 1.0)
    rows: list[tuple[str]] = []
    for percentage in percentages:
        rows = connection.execute(
            f"""
            SELECT node_id
            FROM {PG_NODES}
            TABLESAMPLE SYSTEM ({percentage}) REPEATABLE ({seed})
            WHERE tree_id=%s
            LIMIT %s
            """,
            (TREE_ID, count),
        ).fetchall()
        if len(rows) >= count:
            break
    ids = [row[0] for row in rows]
    if len(ids) != count:
        raise RuntimeError(f"sampled {len(ids)} point IDs, expected {count}")
    if len(set(ids)) != count:
        raise RuntimeError("sampled point IDs are not unique")
    random.Random(seed).shuffle(ids)
    return ids


def prepare_state(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT name,statement,parameter_types::text,from_sql,
               generic_plans,custom_plans
        FROM pg_prepared_statements
        WHERE statement LIKE '/* node_probe:%'
        ORDER BY name
        """,
        prepare=False,
    ).fetchall()
    return [
        {
            "name": row[0],
            "statement": row[1],
            "parameter_types": row[2],
            "from_sql": row[3],
            "generic_plans": row[4],
            "custom_plans": row[5],
        }
        for row in rows
    ]


def validate_and_warm(
    hit_ids: list[str],
    miss_ids: list[str],
    mongo: Any,
    pg_prepared: Any,
    pg_unprepared: Any,
) -> dict[str, Any]:
    digests = {
        config: {
            projection: {
                outcome: hashlib.sha256()
                for outcome in OUTCOMES
            }
            for projection in PROJECTIONS
        }
        for config in CONFIGS
    }
    checks = 0
    for input_index, (hit_id, miss_id) in enumerate(zip(hit_ids, miss_ids)):
        ids = {"hit": hit_id, "miss": miss_id}
        reference = {
            projection: pg_rows(pg_prepared, projection, hit_id)
            for projection in PROJECTIONS
        }
        if any(len(rows) != 1 for rows in reference.values()):
            raise RuntimeError(f"reference hit missing: {hit_id}")
        for projection in PROJECTIONS:
            for outcome in OUTCOMES:
                expected = reference[projection] if outcome == "hit" else []
                for config in CONFIGS:
                    rows = query_rows(
                        config,
                        projection,
                        ids[outcome],
                        mongo,
                        pg_prepared,
                        pg_unprepared,
                    )
                    if rows != expected:
                        raise RuntimeError(
                            f"output mismatch: {config} {projection} "
                            f"{outcome} {hit_id}"
                        )
                    fingerprint_update(
                        digests[config][projection][outcome],
                        hit_id,
                        rows,
                    )
                    checks += 1
        if (input_index + 1) % 250 == 0:
            log(f"  validated {input_index + 1}/{len(hit_ids)}")
    fingerprints = {
        config: {
            projection: {
                outcome: digests[config][projection][outcome].hexdigest()
                for outcome in OUTCOMES
            }
            for projection in PROJECTIONS
        }
        for config in CONFIGS
    }
    reference_fingerprints = fingerprints["pg_prepared"]
    if any(
        fingerprints[config] != reference_fingerprints
        for config in CONFIGS
    ):
        raise RuntimeError("aggregate validation fingerprints differ")
    return {
        "all_outputs_match": True,
        "checks": checks,
        "fingerprints": fingerprints,
        "miss_key_rule": "append ASCII tilde to each existing node_id",
    }


def empty_samples(hit_ids: list[str]) -> dict[str, Any]:
    return {
        config: {
            projection: {
                outcome: [
                    {
                        "node_id": node_id,
                        "wall_us": [],
                        "command_us": [],
                        "outside_command_us": [],
                    }
                    for node_id in hit_ids
                ]
                for outcome in OUTCOMES
            }
            for projection in PROJECTIONS
        }
        for config in CONFIGS
    }


def measure_one(
    listener: DurationListener | None,
    config: str,
    projection: str,
    node_id: str,
    mongo: Any,
    pg_prepared: Any,
    pg_unprepared: Any,
) -> tuple[float, float | None, float | None, int]:
    command_us: float | None = None
    outside_us: float | None = None
    if listener is not None:
        listener.reset()
    gc.disable()
    try:
        started = time.perf_counter_ns()
        rows = query_rows(
            config,
            projection,
            node_id,
            mongo,
            pg_prepared,
            pg_unprepared,
        )
        wall_us = (time.perf_counter_ns() - started) / 1_000
    finally:
        gc.enable()
    if config in MONGO_CONFIGS and listener is not None:
        if len(listener.events) != 1 or listener.events[0][0] != "find":
            raise RuntimeError(
                f"unexpected MongoDB command events: {listener.events}"
            )
        command_us = float(listener.events[0][1])
        outside_us = wall_us - command_us
    elif listener is not None:
        if listener.events:
            raise RuntimeError(
                f"unexpected MongoDB event during {config}: {listener.events}"
            )
        command_us = None
        outside_us = None
    return wall_us, command_us, outside_us, len(rows)


def time_end_to_end(
    hit_ids: list[str],
    miss_ids: list[str],
    repeats: int,
    seed: int,
    mongo: Any,
    pg_prepared: Any,
    pg_unprepared: Any,
    output: dict[str, Any],
    out_path: Path,
) -> dict[str, Any]:
    samples = empty_samples(hit_ids)
    base_arms = [
        (config, projection, outcome)
        for config in CONFIGS
        for projection in PROJECTIONS
        for outcome in OUTCOMES
    ]
    for repeat in range(repeats):
        input_order = list(range(len(hit_ids)))
        random.Random(seed + repeat * 10_000).shuffle(input_order)
        for position, input_index in enumerate(input_order):
            arms = list(base_arms)
            random.Random(
                seed + repeat * 1_000_000 + position
            ).shuffle(arms)
            ids = {
                "hit": hit_ids[input_index],
                "miss": miss_ids[input_index],
            }
            for config, projection, outcome in arms:
                wall_us, command_us, outside_us, row_count = measure_one(
                    None,
                    config,
                    projection,
                    ids[outcome],
                    mongo,
                    pg_prepared,
                    pg_unprepared,
                )
                expected_rows = 1 if outcome == "hit" else 0
                if row_count != expected_rows:
                    raise RuntimeError(
                        f"timed row mismatch: {config} {projection} "
                        f"{outcome} {hit_ids[input_index]}"
                    )
                sample = samples[config][projection][outcome][input_index]
                sample["wall_us"].append(round(wall_us, 6))
        output["run"]["completed_repeats"] = repeat + 1
        output["end_to_end_samples"] = samples
        out_path.write_text(json.dumps(output, indent=2))
        log(f"  completed timing repeat {repeat + 1}/{repeats}")
    return samples


def summarize_end_to_end(samples: dict[str, Any]) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for config in CONFIGS:
        summaries[config] = {}
        for projection in PROJECTIONS:
            summaries[config][projection] = {}
            for outcome in OUTCOMES:
                arm = samples[config][projection][outcome]
                summary = {"wall_us": metric_summary(arm, "wall_us")}
                summaries[config][projection][outcome] = summary
    return summaries


def time_mongo_command_boundary(
    hit_ids: list[str],
    miss_ids: list[str],
    repeats: int,
    seed: int,
    listener: DurationListener,
    mongo: Any,
) -> dict[str, Any]:
    samples = {
        config: {
            projection: {
                outcome: [
                    {
                        "node_id": node_id,
                        "wall_us": [],
                        "command_us": [],
                        "outside_command_us": [],
                    }
                    for node_id in hit_ids
                ]
                for outcome in OUTCOMES
            }
            for projection in PROJECTIONS
        }
        for config in MONGO_CONFIGS
    }
    base_arms = [
        (config, projection, outcome)
        for config in MONGO_CONFIGS
        for projection in PROJECTIONS
        for outcome in OUTCOMES
    ]
    for repeat in range(repeats):
        input_order = list(range(len(hit_ids)))
        random.Random(seed + repeat * 10_000).shuffle(input_order)
        for position, input_index in enumerate(input_order):
            arms = list(base_arms)
            random.Random(
                seed + repeat * 1_000_000 + position
            ).shuffle(arms)
            ids = {
                "hit": hit_ids[input_index],
                "miss": miss_ids[input_index],
            }
            for config, projection, outcome in arms:
                wall_us, command_us, outside_us, row_count = measure_one(
                    listener,
                    config,
                    projection,
                    ids[outcome],
                    mongo,
                    None,
                    None,
                )
                expected_rows = 1 if outcome == "hit" else 0
                if row_count != expected_rows:
                    raise RuntimeError(
                        f"boundary row mismatch: {config} {projection} "
                        f"{outcome} {hit_ids[input_index]}"
                    )
                sample = samples[config][projection][outcome][input_index]
                sample["wall_us"].append(round(wall_us, 6))
                sample["command_us"].append(round(command_us, 6))
                sample["outside_command_us"].append(
                    round(outside_us, 6)
                )
        log(
            f"  completed command-boundary repeat "
            f"{repeat + 1}/{repeats}"
        )
    summaries = {
        config: {
            projection: {
                outcome: {
                    field: metric_summary(
                        samples[config][projection][outcome],
                        field,
                    )
                    for field in (
                        "wall_us",
                        "command_us",
                        "outside_command_us",
                    )
                }
                for outcome in OUTCOMES
            }
            for projection in PROJECTIONS
        }
        for config in MONGO_CONFIGS
    }
    return {
        "contract": {
            "inputs": len(hit_ids),
            "repeats": repeats,
            "command_us": (
                "PyMongo CommandSucceededEvent duration; includes the "
                "command round trip and response decode boundary"
            ),
            "warning": (
                "This separately instrumented phase is not used for the "
                "cross-engine wall-time comparison"
            ),
        },
        "summaries": summaries,
        "samples": samples,
    }


def derive_end_to_end(samples: dict[str, Any]) -> dict[str, Any]:
    hit_minus_miss = {
        config: {
            projection: paired_delta_summary(
                samples[config][projection]["hit"],
                samples[config][projection]["miss"],
                "wall_us",
            )
            for projection in PROJECTIONS
        }
        for config in CONFIGS
    }
    full_minus_id_hit = {
        config: paired_delta_summary(
            samples[config]["full"]["hit"],
            samples[config]["id"]["hit"],
            "wall_us",
        )
        for config in CONFIGS
    }
    mongo_hint_effect = {
        projection: {
            outcome: paired_delta_summary(
                samples["mongo_hinted"][projection][outcome],
                samples["mongo_unhinted"][projection][outcome],
                "wall_us",
            )
            for outcome in OUTCOMES
        }
        for projection in PROJECTIONS
    }
    pg_prepare_effect = {
        projection: {
            outcome: paired_delta_summary(
                samples["pg_unprepared"][projection][outcome],
                samples["pg_prepared"][projection][outcome],
                "wall_us",
            )
            for outcome in OUTCOMES
        }
        for projection in PROJECTIONS
    }
    cross_engine_gap = {
        pg_mode: {
            mongo_mode: {
                projection: {
                    outcome: paired_delta_summary(
                        samples[mongo_mode][projection][outcome],
                        samples[pg_mode][projection][outcome],
                        "wall_us",
                    )
                    for outcome in OUTCOMES
                }
                for projection in PROJECTIONS
            }
            for mongo_mode in MONGO_CONFIGS
        }
        for pg_mode in PG_CONFIGS
    }
    return {
        "hit_minus_miss_us": hit_minus_miss,
        "full_minus_id_for_hit_us": full_minus_id_hit,
        "mongo_hinted_minus_unhinted_us": mongo_hint_effect,
        "pg_unprepared_minus_prepared_us": pg_prepare_effect,
        "mongo_minus_postgres_us": cross_engine_gap,
    }


def stage_names(stage: dict[str, Any]) -> list[str]:
    names = [str(stage.get("stage", "unknown"))]
    for key in ("inputStage", "outerStage", "innerStage"):
        child = stage.get(key)
        if isinstance(child, dict):
            names.extend(stage_names(child))
    for child in stage.get("inputStages", []):
        names.extend(stage_names(child))
    return names


def mongo_find_command(
    mode: str,
    projection: str,
    node_id: str,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "find": MONGO_NODES,
        "filter": {"tree_id": TREE_ID, "node_id": node_id},
        "projection": (
            ID_MONGO_PROJECTION
            if projection == "id"
            else FULL_MONGO_PROJECTION
        ),
        "limit": 1,
        "singleBatch": True,
    }
    if mode == "hinted":
        command["hint"] = MONGO_INDEX
    return command


def collect_plans(
    mongo: Any,
    pg_prepared: Any,
    pg_unprepared: Any,
    hit_id: str,
    miss_id: str,
) -> dict[str, Any]:
    mongo_plans: dict[str, Any] = {}
    ids = {"hit": hit_id, "miss": miss_id}
    for mode in ("hinted", "unhinted"):
        mongo_plans[mode] = {}
        for projection in PROJECTIONS:
            mongo_plans[mode][projection] = {}
            for outcome in OUTCOMES:
                explain = mongo.command(
                    {
                        "explain": mongo_find_command(
                            mode, projection, ids[outcome]
                        ),
                        "verbosity": "executionStats",
                    }
                )
                planner = explain.get("queryPlanner", {})
                stats = explain.get("executionStats", {})
                execution_stages = stats.get("executionStages", {})
                mongo_plans[mode][projection][outcome] = {
                    "query_hash": planner.get("queryHash"),
                    "plan_cache_key": planner.get("planCacheKey"),
                    "winning_plan": planner.get("winningPlan"),
                    "execution": {
                        "stages": stage_names(execution_stages),
                        "n_returned": stats.get("nReturned"),
                        "keys_examined": stats.get("totalKeysExamined"),
                        "docs_examined": stats.get("totalDocsExamined"),
                        "execution_time_ms": stats.get(
                            "executionTimeMillis"
                        ),
                    },
                }

    pg_plans: dict[str, Any] = {}
    for mode, connection in (
        ("prepared_connection", pg_prepared),
        ("unprepared_connection", pg_unprepared),
    ):
        pg_plans[mode] = {}
        for projection in PROJECTIONS:
            pg_plans[mode][projection] = {}
            for outcome in OUTCOMES:
                plan = connection.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, "
                    "SUMMARY ON, FORMAT JSON) "
                    + PG_SQL[projection],
                    (TREE_ID, ids[outcome]),
                    prepare=False,
                ).fetchone()[0][0]
                pg_plans[mode][projection][outcome] = plan
    return {"mongodb": mongo_plans, "postgresql": pg_plans}


def empty_profile_samples(
    hit_ids: list[str],
) -> dict[str, Any]:
    return {
        config: {
            projection: {
                outcome: [
                    {
                        "node_id": node_id,
                        "cpu_us": [],
                        "planning_us": [],
                        "keys_examined": [],
                        "docs_examined": [],
                        "nreturned": [],
                        "response_bytes": [],
                    }
                    for node_id in hit_ids
                ]
                for outcome in OUTCOMES
            }
            for projection in PROJECTIONS
        }
        for config in MONGO_CONFIGS
    }


def collect_mongo_profile(
    mongo: Any,
    hit_ids: list[str],
    miss_ids: list[str],
    repeats: int,
    seed: int,
    profile_size_mb: int,
) -> dict[str, Any]:
    samples = empty_profile_samples(hit_ids)
    before = mongo.command({"profile": -1})
    if before["was"] != 0:
        raise RuntimeError("refusing to replace an enabled MongoDB profiler")
    profile_existed = "system.profile" in mongo.list_collection_names()
    if profile_existed:
        raise RuntimeError("refusing to replace existing system.profile")

    tag_prefix = f"condb-node-profile-{uuid.uuid4()}"
    expected_records = (
        len(hit_ids)
        * repeats
        * len(MONGO_CONFIGS)
        * len(PROJECTIONS)
        * len(OUTCOMES)
    )
    base_arms = [
        (config, projection, outcome)
        for config in MONGO_CONFIGS
        for projection in PROJECTIONS
        for outcome in OUTCOMES
    ]
    representative: dict[str, Any] = {
        config: {
            projection: {} for projection in PROJECTIONS
        }
        for config in MONGO_CONFIGS
    }
    try:
        mongo.create_collection(
            "system.profile",
            capped=True,
            size=profile_size_mb * 1024 * 1024,
        )
        mongo.command("profile", 2, slowms=0, sampleRate=1.0)
        for repeat in range(repeats):
            input_order = list(range(len(hit_ids)))
            random.Random(seed + repeat * 10_000).shuffle(input_order)
            for position, input_index in enumerate(input_order):
                arms = list(base_arms)
                random.Random(
                    seed + repeat * 1_000_000 + position
                ).shuffle(arms)
                ids = {
                    "hit": hit_ids[input_index],
                    "miss": miss_ids[input_index],
                }
                for config, projection, outcome in arms:
                    mode = config.removeprefix("mongo_")
                    tag = (
                        f"{tag_prefix}|{config}|{projection}|{outcome}|"
                        f"{input_index}|{repeat}"
                    )
                    rows = mongo_rows(
                        mongo,
                        mode,
                        projection,
                        ids[outcome],
                        tag,
                    )
                    expected_rows = 1 if outcome == "hit" else 0
                    if len(rows) != expected_rows:
                        raise RuntimeError(
                            f"profile row mismatch: {tag}"
                        )
            log(f"  completed profile repeat {repeat + 1}/{repeats}")

        mongo.command(
            "profile",
            before["was"],
            slowms=before.get("slowms", 100),
            sampleRate=before.get("sampleRate", 1.0),
        )
        records = list(mongo["system.profile"].find({
            "command.comment": {"$regex": f"^{tag_prefix}"}
        }))
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
            (
                _,
                config,
                projection,
                outcome,
                input_text,
                _,
            ) = tag.split("|")
            input_index = int(input_text)
            sample = samples[config][projection][outcome][input_index]
            sample["cpu_us"].append(record.get("cpuNanos", 0) / 1_000)
            sample["planning_us"].append(
                record.get("planningTimeMicros", 0)
            )
            sample["keys_examined"].append(record.get("keysExamined", 0))
            sample["docs_examined"].append(record.get("docsExamined", 0))
            sample["nreturned"].append(record.get("nreturned", 0))
            sample["response_bytes"].append(record.get("responseLength", 0))
            if outcome not in representative[config][projection]:
                exec_stats = record.get("execStats", {})
                representative[config][projection][outcome] = {
                    "plan_summary": record.get("planSummary"),
                    "query_framework": record.get("queryFramework"),
                    "stages": stage_names(exec_stats),
                    "exec_stats": exec_stats,
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
        if (
            not profile_existed
            and "system.profile" in mongo.list_collection_names()
        ):
            mongo.drop_collection("system.profile")

    summaries = {
        config: {
            projection: {
                outcome: {
                    field: metric_summary(
                        samples[config][projection][outcome],
                        field,
                    )
                    for field in (
                        "cpu_us",
                        "planning_us",
                        "keys_examined",
                        "docs_examined",
                        "nreturned",
                        "response_bytes",
                    )
                }
                for outcome in OUTCOMES
            }
            for projection in PROJECTIONS
        }
        for config in MONGO_CONFIGS
    }
    derived = {
        "hit_minus_miss_cpu_us": {
            config: {
                projection: paired_delta_summary(
                    samples[config][projection]["hit"],
                    samples[config][projection]["miss"],
                    "cpu_us",
                )
                for projection in PROJECTIONS
            }
            for config in MONGO_CONFIGS
        },
        "full_minus_id_hit_cpu_us": {
            config: paired_delta_summary(
                samples[config]["full"]["hit"],
                samples[config]["id"]["hit"],
                "cpu_us",
            )
            for config in MONGO_CONFIGS
        },
        "hinted_minus_unhinted_cpu_us": {
            projection: {
                outcome: paired_delta_summary(
                    samples["mongo_hinted"][projection][outcome],
                    samples["mongo_unhinted"][projection][outcome],
                    "cpu_us",
                )
                for outcome in OUTCOMES
            }
            for projection in PROJECTIONS
        },
        "hinted_minus_unhinted_planning_us": {
            projection: {
                outcome: paired_delta_summary(
                    samples["mongo_hinted"][projection][outcome],
                    samples["mongo_unhinted"][projection][outcome],
                    "planning_us",
                )
                for outcome in OUTCOMES
            }
            for projection in PROJECTIONS
        },
    }
    return {
        "contract": {
            "instrumentation": (
                "MongoDB profile level 2; same instrumentation for every "
                "MongoDB profile arm"
            ),
            "records": expected_records,
            "inputs": len(hit_ids),
            "repeats": repeats,
        },
        "summaries": summaries,
        "derived": derived,
        "representative_plans": representative,
        "samples": samples,
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
        "--out",
        default=(
            "bench/db/runs/node_rootcause_20260724/"
            "get_node_10m.json"
        ),
    )
    parser.add_argument("--inputs", type=int, default=1_000)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--boundary-inputs", type=int, default=500)
    parser.add_argument("--boundary-repeats", type=int, default=10)
    parser.add_argument("--profile-inputs", type=int, default=250)
    parser.add_argument("--profile-repeats", type=int, default=3)
    parser.add_argument("--profile-size-mb", type=int, default=256)
    parser.add_argument("--seed", type=int, default=20260724)
    args = parser.parse_args()
    if min(
        args.inputs,
        args.repeats,
        args.boundary_inputs,
        args.boundary_repeats,
        args.profile_inputs,
        args.profile_repeats,
        args.profile_size_mb,
    ) <= 0:
        parser.error("counts, repeats, and profile size must be positive")
    if args.profile_inputs > args.inputs:
        parser.error("profile-inputs cannot exceed inputs")
    if args.boundary_inputs > args.inputs:
        parser.error("boundary-inputs cannot exceed inputs")

    import psycopg
    import pymongo

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    listener = DurationListener()
    mongo_client = MongoClient(
        args.mongo_uri,
        serverSelectionTimeoutMS=5_000,
    )
    monitored_mongo_client = MongoClient(
        args.mongo_uri,
        event_listeners=[listener],
        serverSelectionTimeoutMS=5_000,
    )
    mongo = mongo_client[args.mongo_db]
    monitored_mongo = monitored_mongo_client[args.mongo_db]
    pg_admin = psycopg.connect(args.pg_dsn, autocommit=True)
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
        connection.execute("SET jit = off", prepare=False)

    hit_ids = load_hit_ids(pg_admin, args.inputs, args.seed)
    miss_ids = [node_id + "~" for node_id in hit_ids]
    output: dict[str, Any] = {
        "run": {
            "status": "validating",
            "started_unix_s": time.time(),
            "inputs": args.inputs,
            "repeats": args.repeats,
            "boundary_inputs": args.boundary_inputs,
            "boundary_repeats": args.boundary_repeats,
            "profile_inputs": args.profile_inputs,
            "profile_repeats": args.profile_repeats,
            "seed": args.seed,
            "completed_repeats": 0,
        },
        "contract": {
            "operation": "get_node",
            "input": ["tree_id", "node_id"],
            "tree_id": TREE_ID,
            "full_output": [
                "node_id",
                "parent_id",
                "depth",
                "title",
                "summary",
                "start_index",
                "end_index",
            ],
            "id_output": ["node_id"],
            "configs": list(CONFIGS),
            "projections": list(PROJECTIONS),
            "outcomes": list(OUTCOMES),
            "timing_order": (
                "inputs and the 16 config/projection/outcome arms are "
                "deterministically shuffled per repeat"
            ),
            "summary_unit": (
                "per-input median across repeats, then distribution "
                "across inputs"
            ),
        },
        "sources": {
            "mongodb_collection": MONGO_NODES,
            "mongodb_index": MONGO_INDEX,
            "postgresql_table": PG_NODES,
        },
        "versions": {
            "mongodb": mongo_client.server_info()["version"],
            "pymongo": pymongo.version,
            "postgresql": pg_admin.execute(
                "SHOW server_version", prepare=False
            ).fetchone()[0],
            "psycopg": psycopg.__version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "connection_modes": {
            "pg_prepared_threshold": pg_prepared.prepare_threshold,
            "pg_unprepared_threshold": pg_unprepared.prepare_threshold,
        },
        "inputs": {
            "hit_ids": hit_ids,
            "miss_ids": miss_ids,
        },
    }
    out_path.write_text(json.dumps(output, indent=2))

    log("validating outputs and warming every arm")
    output["validation"] = validate_and_warm(
        hit_ids,
        miss_ids,
        mongo,
        pg_prepared,
        pg_unprepared,
    )
    output["prepared_statements_after_warm"] = {
        "prepared_connection": prepare_state(pg_prepared),
        "unprepared_connection": prepare_state(pg_unprepared),
    }
    output["run"]["status"] = "timing"
    out_path.write_text(json.dumps(output, indent=2))

    log("timing interleaved end-to-end arms")
    samples = time_end_to_end(
        hit_ids,
        miss_ids,
        args.repeats,
        args.seed,
        mongo,
        pg_prepared,
        pg_unprepared,
        output,
        out_path,
    )
    output["end_to_end_summaries"] = summarize_end_to_end(samples)
    output["end_to_end_derived"] = derive_end_to_end(samples)
    output["prepared_statements_after_timing"] = {
        "prepared_connection": prepare_state(pg_prepared),
        "unprepared_connection": prepare_state(pg_unprepared),
    }

    log("collecting separately instrumented MongoDB command boundaries")
    output["mongo_command_boundary"] = time_mongo_command_boundary(
        hit_ids[: args.boundary_inputs],
        miss_ids[: args.boundary_inputs],
        args.boundary_repeats,
        args.seed + 50_000,
        listener,
        monitored_mongo,
    )

    log("collecting representative access plans")
    output["plans"] = collect_plans(
        mongo,
        pg_prepared,
        pg_unprepared,
        hit_ids[0],
        miss_ids[0],
    )

    log("collecting separately instrumented MongoDB server profiles")
    output["mongo_server_profile"] = collect_mongo_profile(
        mongo,
        hit_ids[: args.profile_inputs],
        miss_ids[: args.profile_inputs],
        args.profile_repeats,
        args.seed + 90_000,
        args.profile_size_mb,
    )
    output["run"]["status"] = "complete"
    output["run"]["finished_unix_s"] = time.time()
    output["run"]["elapsed_s"] = round(
        output["run"]["finished_unix_s"]
        - output["run"]["started_unix_s"],
        3,
    )
    out_path.write_text(json.dumps(output, indent=2))

    print(json.dumps({
        "summaries": output["end_to_end_summaries"],
        "derived": output["end_to_end_derived"],
        "mongo_server_profile": {
            "summaries": output["mongo_server_profile"]["summaries"],
            "derived": output["mongo_server_profile"]["derived"],
        },
        "prepared_statements": output[
            "prepared_statements_after_timing"
        ],
    }, indent=2))

    pg_unprepared.close()
    pg_prepared.close()
    pg_admin.close()
    monitored_mongo_client.close()
    mongo_client.close()


if __name__ == "__main__":
    main()
