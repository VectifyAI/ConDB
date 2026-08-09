#!/usr/bin/env python3
"""Causal decomposition of the MongoDB/PostgreSQL two-store latency gap.

This experiment keeps the logical output fixed as
``(node_id, title, summary)`` and adds an exact-output covering intervention:

* baseline: ``(path, node_id)`` index followed by document/heap fetches;
* covered: the same rows and output, with title/summary carried by the index;
* id_only: a lean covered lower bound returning only node_id.

It also splits client work at driver-native boundaries.  Psycopg ``execute``
and ``fetchall`` are timed separately.  PyMongo command-monitor durations are
recorded inside cursor consumption, and a RawBSONDocument arm moves BSON field
inflation out of cursor fetch and into an explicitly timed decode/normalize
stage.  Server-side EXPLAIN measurements are collected on row-stratified paths
after the client campaign.

Prerequisite: retain both 10M layout-2 stores with ``bench_layout_2v3.py
--keep`` and ``bench_layout_2v3_postgres.py --keep``.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import platform
import statistics
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench_layout_2v3 import fingerprint
from bench_layout_2v3_complete_breakdown import mongo_explain_summary
from bench_layout_2v3_postgres import explain as pg_explain


MONGO_VIEW = "layout2_view"
MONGO_COVER_INDEX = "layout2_rootcause_exact_cover"
PG_VIEW = "layout2_pg_view"
PG_COVER = "layout2_pg_rootcause_cover"
PG_COVER_INDEX = "layout2_pg_rootcause_cover_idx"
LEAN_MONGO_INDEX = "path_1_node_id_1"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def host_snapshot() -> dict[str, Any]:
    memory: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return {
        "captured_at": utc_now(),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "loadavg_1m_5m_15m": [round(value, 6) for value in os.getloadavg()],
        "memory_total_bytes": memory.get("MemTotal"),
        "memory_available_bytes": memory.get("MemAvailable"),
    }


def percentile(values: list[float], p: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))
    return ordered[index]


def stats(values: list[float]) -> dict[str, float | int]:
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 6) if values else 0.0,
        "p50": round(percentile(values, 50), 6),
        "p95": round(percentile(values, 95), 6),
        "min": round(min(values), 6) if values else 0.0,
        "max": round(max(values), 6) if values else 0.0,
    }


class CommandTimer:
    """Sum PyMongo find/getMore command-monitor durations for one query."""

    def __init__(self) -> None:
        self.active: str | None = None
        self.requests: dict[int, str] = {}
        self.durations_us: defaultdict[str, float] = defaultdict(float)
        self.commands: defaultdict[str, int] = defaultdict(int)

    def started(self, event: Any) -> None:
        if self.active is not None and event.command_name in {"find", "getMore"}:
            self.requests[event.request_id] = self.active

    def succeeded(self, event: Any) -> None:
        label = self.requests.pop(event.request_id, None)
        if label is not None:
            self.durations_us[label] += event.duration_micros
            self.commands[label] += 1

    def failed(self, event: Any) -> None:
        self.requests.pop(event.request_id, None)

    @contextmanager
    def measure(self, label: str):
        if self.active is not None:
            raise RuntimeError("nested Mongo command timer")
        self.active = label
        self.durations_us[label] = 0.0
        self.commands[label] = 0
        try:
            yield
        finally:
            self.active = None

    def result(self, label: str) -> tuple[float, int]:
        return self.durations_us[label] / 1_000.0, self.commands[label]


def validate_source(source: dict[str, Any], allow_nonstandard: bool) -> None:
    if source.get("status") != "complete":
        raise SystemExit("source result is not complete")
    if not allow_nonstandard and source.get("nodes") != 10_000_000:
        raise SystemExit("source is not the 10M dataset")
    if len(source.get("samples", [])) != source.get("paths"):
        raise SystemExit("source path/sample count mismatch")


def selected_indices(source: dict[str, Any], spec: str) -> list[int]:
    if spec == "all":
        return list(range(len(source["samples"])))
    indices = [int(value) for value in spec.split(",") if value.strip()]
    if not indices or min(indices) < 0 or max(indices) >= len(source["samples"]):
        raise SystemExit("invalid --indices")
    return indices


def stratified_indices(source: dict[str, Any], count: int) -> list[int]:
    ordered = sorted(
        range(len(source["samples"])), key=lambda i: source["samples"][i]["rows"]
    )
    if count >= len(ordered):
        return ordered
    return sorted({ordered[round(i * (len(ordered) - 1) / (count - 1))] for i in range(count)})


def bounds(path: str) -> tuple[str, str]:
    return path + "/", path + "0"


def path_equal_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[sample["source_index"]].append(sample)
    numeric = sorted(
        key
        for key, value in samples[0].items()
        if isinstance(value, (int, float))
        and key not in {"repeat", "source_index", "rows", "commands"}
    ) if samples else []
    output: dict[str, Any] = {
        "observations": len(samples),
        "paths": len(grouped),
        "avg_rows": round(statistics.mean(s["rows"] for s in samples), 3)
        if samples else 0.0,
    }
    for key in numeric:
        path_means = [statistics.mean(item[key] for item in group) for group in grouped.values()]
        output[key] = stats(path_means)
    return output


def summarize(output: dict[str, Any]) -> dict[str, Any]:
    by_engine_arm: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for sample in output["samples"]:
        by_engine_arm[(sample["engine"], sample["arm"])].append(sample)
    summaries = {
        engine: {
            arm: path_equal_summary(group)
            for (item_engine, arm), group in by_engine_arm.items()
            if item_engine == engine
        }
        for engine in ("mongo", "postgres")
    }

    def mean(engine: str, arm: str, key: str) -> float:
        return float(summaries[engine][arm][key]["mean"])

    deltas: dict[str, Any] = {}
    for arm in ("baseline", "covered", "id_only"):
        deltas[arm] = {
            "mongo_minus_postgres_total_ms": round(
                mean("mongo", arm, "total_ms")
                - mean("postgres", arm, "total_ms"),
                6,
            ),
            "mongo_total_ms": mean("mongo", arm, "total_ms"),
            "postgres_total_ms": mean("postgres", arm, "total_ms"),
        }

    baseline_gap = deltas["baseline"]["mongo_minus_postgres_total_ms"]
    covered_gap = deltas["covered"]["mongo_minus_postgres_total_ms"]
    coverage = {
        "mongo_baseline_minus_covered_ms": round(
            mean("mongo", "baseline", "total_ms")
            - mean("mongo", "covered", "total_ms"),
            6,
        ),
        "postgres_baseline_minus_covered_ms": round(
            mean("postgres", "baseline", "total_ms")
            - mean("postgres", "covered", "total_ms"),
            6,
        ),
        "cross_engine_gap_removed_by_coverage_ms": round(
            baseline_gap - covered_gap, 6
        ),
        "cross_engine_gap_removed_by_coverage_share": round(
            (baseline_gap - covered_gap) / baseline_gap, 6
        ) if baseline_gap else None,
    }

    plan_by_engine_arm: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for plan in output.get("server_plans", []):
        plan_by_engine_arm[(plan["engine"], plan["arm"])].append(plan)
    plan_summary = {
        engine: {
            arm: {
                "paths": len(group),
                "execution_ms": stats([item["execution_ms"] for item in group]),
                "avg_rows": round(statistics.mean(item["rows"] for item in group), 3),
            }
            for (item_engine, arm), group in plan_by_engine_arm.items()
            if item_engine == engine
        }
        for engine in ("mongo", "postgres")
    }
    server_deltas: dict[str, Any] = {}
    if all(plan_summary.get(engine) for engine in ("mongo", "postgres")):
        for arm in ("baseline", "covered", "id_only"):
            mongo = plan_summary["mongo"][arm]["execution_ms"]["mean"]
            postgres = plan_summary["postgres"][arm]["execution_ms"]["mean"]
            server_deltas[arm] = {
                "mongo_ms": mongo,
                "postgres_ms": postgres,
                "mongo_minus_postgres_ms": round(mongo - postgres, 6),
            }
        server_deltas["gap_removed_by_coverage_ms"] = round(
            server_deltas["baseline"]["mongo_minus_postgres_ms"]
            - server_deltas["covered"]["mongo_minus_postgres_ms"],
            6,
        )

    return {
        "by_engine_arm": summaries,
        "client_total_delta": deltas,
        "coverage_intervention": coverage,
        "server_plan_summary": plan_summary,
        "server_plan_delta": server_deltas,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-result",
        default="bench/db/runs/report_3eng_20260716/layout_2v3_mongo_10m_final.json",
    )
    parser.add_argument(
        "--out",
        default="bench/db/runs/report_3eng_20260716/layout_2v3_rootcause.json",
    )
    parser.add_argument("--indices", default="all")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warm-rounds", type=int, default=1)
    parser.add_argument("--plan-paths", type=int, default=40)
    parser.add_argument("--allow-nonstandard-source", action="store_true")
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--keep-diagnostic-indexes", action="store_true")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument(
        "--pg-dsn",
        default="host=localhost port=55432 dbname=bench user=postgres password=bench",
    )
    args = parser.parse_args()
    if args.repeats < 2 or args.warm_rounds < 1 or args.plan_paths < 2:
        raise SystemExit("requires repeats>=2, warm-rounds>=1, plan-paths>=2")

    source = json.loads(Path(args.source_result).read_text())
    validate_source(source, args.allow_nonstandard_source)
    indices = selected_indices(source, args.indices)

    import psycopg
    from bson.codec_options import CodecOptions
    from bson.raw_bson import RawBSONDocument
    from pymongo import MongoClient, monitoring

    class Listener(CommandTimer, monitoring.CommandListener):
        pass

    timer = Listener()
    mongo = MongoClient(
        args.mongo_uri, serverSelectionTimeoutMS=5_000, event_listeners=[timer]
    )
    mongo_db = mongo["bench"]
    raw_db = mongo.get_database(
        "bench", codec_options=CodecOptions(document_class=RawBSONDocument)
    )
    mongo_view = mongo_db[MONGO_VIEW]
    raw_view = raw_db[MONGO_VIEW]
    pg = psycopg.connect(args.pg_dsn, autocommit=True)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output: dict[str, Any] = {
        "status": "running",
        "started_at": utc_now(),
        "source_result": args.source_result,
        "nodes": source["nodes"],
        "indices": indices,
        "repeats": args.repeats,
        "warm_rounds": args.warm_rounds,
        "plan_paths": args.plan_paths,
        "environment": {
            "host_before_prepare": host_snapshot(),
            "mongo_server": mongo.server_info().get("version"),
            "pymongo": __import__("pymongo").version,
            "postgres_server": pg.info.server_version,
            "psycopg": psycopg.__version__,
            "transport": "localhost Docker",
        },
        "contracts": {
            "baseline": "exact output; lean path,node_id index plus document/heap fetch",
            "covered": "exact same output; title,summary carried by covering index",
            "id_only": "lean covered lower bound; node_id only",
            "mongo_command_ms": "sum of PyMongo find/getMore CommandSucceeded durations",
            "postgres_execute_ms": "cursor.execute through libpq result receipt",
            "postgres_fetchall_ms": "PGresult conversion into Python tuples/strings",
            "server_execution_ms": "Mongo executionStats or PostgreSQL EXPLAIN ANALYZE; diagnostic campaign",
        },
        "samples": [],
        "server_plans": [],
    }

    def save() -> None:
        out_path.write_text(json.dumps(output, indent=2))

    def check_prerequisites() -> None:
        collections = set(mongo_db.list_collection_names())
        if MONGO_VIEW not in collections:
            raise SystemExit(f"missing MongoDB collection {MONGO_VIEW}")
        tables = {
            row[0]
            for row in pg.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
            ).fetchall()
        }
        if PG_VIEW not in tables:
            raise SystemExit(f"missing PostgreSQL table {PG_VIEW}")
        mongo_count = mongo_view.estimated_document_count()
        pg_count = pg.execute(f"SELECT COUNT(*) FROM {PG_VIEW}").fetchone()[0]
        if mongo_count != source["nodes"] or pg_count != source["nodes"]:
            raise SystemExit(
                f"source count mismatch: mongo={mongo_count} pg={pg_count} expected={source['nodes']}"
            )

    def prepare() -> None:
        log("preparing exact-output covering intervention ...")
        if MONGO_COVER_INDEX in mongo_view.index_information():
            mongo_view.drop_index(MONGO_COVER_INDEX)
        mongo_view.create_index(
            [("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)],
            name=MONGO_COVER_INDEX,
        )
        pg.execute(f"DROP TABLE IF EXISTS {PG_COVER}")
        pg.execute(
            f"CREATE TABLE {PG_COVER} AS "
            f"SELECT path, node_id, title, summary FROM {PG_VIEW}"
        )
        pg.execute(
            f"CREATE INDEX {PG_COVER_INDEX} ON {PG_COVER} (path, node_id) "
            "INCLUDE (title, summary)"
        )
        pg.execute(f"VACUUM (ANALYZE) {PG_VIEW}")
        pg.execute(f"VACUUM (ANALYZE) {PG_COVER}")

    mongo_projection = {
        "baseline": {"node_id": 1, "title": 1, "summary": 1, "_id": 0},
        "covered": {"node_id": 1, "title": 1, "summary": 1, "_id": 0},
        "id_only": {"node_id": 1, "_id": 0},
        "raw_baseline": {"node_id": 1, "title": 1, "summary": 1, "_id": 0},
    }
    mongo_index = {
        "baseline": LEAN_MONGO_INDEX,
        "covered": MONGO_COVER_INDEX,
        "id_only": LEAN_MONGO_INDEX,
        "raw_baseline": LEAN_MONGO_INDEX,
    }
    pg_sql = {
        "baseline": (
            f"SELECT node_id,title,summary FROM {PG_VIEW} "
            "WHERE path >= %s AND path < %s ORDER BY path,node_id"
        ),
        "covered": (
            f"SELECT node_id,title,summary FROM {PG_COVER} "
            "WHERE path >= %s AND path < %s ORDER BY path,node_id"
        ),
        "id_only": (
            f"SELECT node_id FROM {PG_VIEW} "
            "WHERE path >= %s AND path < %s ORDER BY path,node_id"
        ),
    }

    def mongo_query(arm: str, index: int, repeat: int, validate: bool) -> dict[str, Any]:
        raw_mode = arm == "raw_baseline"
        logical_arm = "baseline" if raw_mode else arm
        source_sample = source["samples"][index]
        collection = raw_view if raw_mode else mongo_view
        label = f"{arm}:{repeat}:{index}:{time.perf_counter_ns()}"
        gc.disable()
        try:
            with timer.measure(label):
                started = time.perf_counter()
                raw_rows = list(
                    collection.find(
                        {"path": {"$gte": bounds(source_sample["path"])[0],
                                  "$lt": bounds(source_sample["path"])[1]}},
                        mongo_projection[arm],
                    )
                    .sort([("path", 1), ("node_id", 1)])
                    .hint(mongo_index[arm])
                )
                fetch_ms = (time.perf_counter() - started) * 1_000
            command_ms, commands = timer.result(label)

            started = time.perf_counter()
            if logical_arm == "id_only":
                result = [(row["node_id"],) for row in raw_rows]
            else:
                result = [
                    (row["node_id"], row.get("title", ""), row.get("summary", ""))
                    for row in raw_rows
                ]
            normalize_ms = (time.perf_counter() - started) * 1_000

            started = time.perf_counter()
            del raw_rows
            cleanup_ms = (time.perf_counter() - started) * 1_000
        finally:
            gc.enable()

        if len(result) != source_sample["rows"]:
            raise RuntimeError(f"Mongo row mismatch for source index {index}")
        if validate and logical_arm != "id_only":
            if fingerprint(result) != source_sample["fingerprint"]:
                raise RuntimeError(f"Mongo fingerprint mismatch for source index {index}")
        total_ms = fetch_ms + normalize_ms + cleanup_ms
        del result
        gc.collect()
        return {
            "engine": "mongo",
            "arm": arm,
            "source_index": index,
            "repeat": repeat,
            "rows": source_sample["rows"],
            "fetch_ms": round(fetch_ms, 6),
            "command_ms": round(command_ms, 6),
            "cursor_overhead_ms": round(fetch_ms - command_ms, 6),
            "normalize_ms": round(normalize_ms, 6),
            "cleanup_ms": round(cleanup_ms, 6),
            "total_ms": round(total_ms, 6),
            "commands": commands,
        }

    def pg_query(arm: str, index: int, repeat: int, validate: bool) -> dict[str, Any]:
        source_sample = source["samples"][index]
        cursor = pg.cursor()
        gc.disable()
        try:
            started = time.perf_counter()
            cursor.execute(pg_sql[arm], bounds(source_sample["path"]))
            execute_ms = (time.perf_counter() - started) * 1_000

            started = time.perf_counter()
            raw_rows = cursor.fetchall()
            fetchall_ms = (time.perf_counter() - started) * 1_000

            started = time.perf_counter()
            if arm == "id_only":
                result = [(node_id,) for (node_id,) in raw_rows]
            else:
                result = [
                    (node_id, title or "", summary_text or "")
                    for node_id, title, summary_text in raw_rows
                ]
            normalize_ms = (time.perf_counter() - started) * 1_000

            started = time.perf_counter()
            del raw_rows
            cleanup_ms = (time.perf_counter() - started) * 1_000
        finally:
            gc.enable()
            cursor.close()

        if len(result) != source_sample["rows"]:
            raise RuntimeError(f"PostgreSQL row mismatch for source index {index}")
        if validate and arm != "id_only":
            if fingerprint(result) != source_sample["fingerprint"]:
                raise RuntimeError(f"PostgreSQL fingerprint mismatch for source index {index}")
        fetch_ms = execute_ms + fetchall_ms
        total_ms = fetch_ms + normalize_ms + cleanup_ms
        del result
        gc.collect()
        return {
            "engine": "postgres",
            "arm": arm,
            "source_index": index,
            "repeat": repeat,
            "rows": source_sample["rows"],
            "execute_ms": round(execute_ms, 6),
            "fetchall_ms": round(fetchall_ms, 6),
            "fetch_ms": round(fetch_ms, 6),
            "normalize_ms": round(normalize_ms, 6),
            "cleanup_ms": round(cleanup_ms, 6),
            "total_ms": round(total_ms, 6),
        }

    def mongo_plan(arm: str, index: int) -> dict[str, Any]:
        sample = source["samples"][index]
        logical_arm = "baseline" if arm == "raw_baseline" else arm
        explanation = (
            mongo_view.find(
                {"path": {"$gte": bounds(sample["path"])[0],
                          "$lt": bounds(sample["path"])[1]}},
                mongo_projection[logical_arm],
            )
            .sort([("path", 1), ("node_id", 1)])
            .hint(mongo_index[logical_arm])
            .explain()
        )
        summary = mongo_explain_summary(explanation)
        return {
            "engine": "mongo",
            "arm": logical_arm,
            "source_index": index,
            "rows": sample["rows"],
            "execution_ms": float(summary["execution_time_ms"]),
            "plan": summary,
        }

    def postgres_plan(arm: str, index: int) -> dict[str, Any]:
        sample = source["samples"][index]
        summary = pg_explain(pg, pg_sql[arm], bounds(sample["path"]))
        return {
            "engine": "postgres",
            "arm": arm,
            "source_index": index,
            "rows": sample["rows"],
            "execution_ms": float(summary["execution_ms"]),
            "plan": summary,
        }

    try:
        check_prerequisites()
        if not args.skip_prepare:
            prepare()
        if PG_COVER not in {
            row[0]
            for row in pg.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
            ).fetchall()
        }:
            raise SystemExit(f"missing diagnostic table {PG_COVER}")

        # Hard plan checks on a median-row path before timing.
        median_index = sorted(indices, key=lambda i: source["samples"][i]["rows"])[
            len(indices) // 2
        ]
        mongo_checks = {arm: mongo_plan(arm, median_index) for arm in ("baseline", "covered", "id_only")}
        pg_checks = {arm: postgres_plan(arm, median_index) for arm in ("baseline", "covered", "id_only")}
        output["preflight_plans"] = {"mongo": mongo_checks, "postgres": pg_checks}
        if mongo_checks["baseline"]["plan"]["docs_examined"] == 0:
            raise RuntimeError("Mongo baseline unexpectedly covered")
        if mongo_checks["covered"]["plan"]["docs_examined"] != 0:
            raise RuntimeError("Mongo exact-output arm is not covered")
        if mongo_checks["id_only"]["plan"]["docs_examined"] != 0:
            raise RuntimeError("Mongo id-only arm is not covered")
        if not any(
            node.get("node_type") == "Index Only Scan"
            and node.get("heap_fetches", 0) == 0
            for node in pg_checks["covered"]["plan"]["nodes"]
        ):
            raise RuntimeError("PostgreSQL exact-output arm is not index-only")
        save()

        log(
            f"warming {len(indices)} paths x {args.warm_rounds} round(s), "
            "both engines and all causal arms ..."
        )
        for warm in range(args.warm_rounds):
            for position, index in enumerate(indices):
                for arm in ("baseline", "covered", "id_only"):
                    mongo_query(arm, index, -1 - warm, validate=position == 0)
                    pg_query(arm, index, -1 - warm, validate=position == 0)
                mongo_query("raw_baseline", index, -1 - warm, validate=position == 0)
                if (position + 1) % 25 == 0:
                    log(f"  warm {position + 1}/{len(indices)}")

        output["environment"]["host_before_measurement"] = host_snapshot()
        save()

        total = len(indices) * args.repeats
        done = 0
        log(f"measuring {len(indices)} paths x {args.repeats} repeats ...")
        for repeat in range(args.repeats):
            offset = (repeat * len(indices)) // args.repeats
            order = indices[offset:] + indices[:offset]
            for position, index in enumerate(order):
                arms = ["baseline", "covered", "id_only"]
                rotation = (index + repeat) % len(arms)
                arms = arms[rotation:] + arms[:rotation]
                for arm_number, arm in enumerate(arms):
                    mongo_first = (index + repeat + arm_number) % 2 == 0
                    if mongo_first:
                        output["samples"].append(
                            mongo_query(arm, index, repeat, validate=repeat == 0)
                        )
                        output["samples"].append(
                            pg_query(arm, index, repeat, validate=repeat == 0)
                        )
                    else:
                        output["samples"].append(
                            pg_query(arm, index, repeat, validate=repeat == 0)
                        )
                        output["samples"].append(
                            mongo_query(arm, index, repeat, validate=repeat == 0)
                        )
                output["samples"].append(
                    mongo_query("raw_baseline", index, repeat, validate=repeat == 0)
                )
                done += 1
                if done % 20 == 0:
                    output["summary"] = summarize(output)
                    save()
                    log(f"  measure {done}/{total}")

        log(f"collecting server-side plans on {args.plan_paths} stratified paths ...")
        plan_indices = stratified_indices(source, args.plan_paths)
        for position, index in enumerate(plan_indices):
            arms = ["baseline", "covered", "id_only"]
            rotation = index % len(arms)
            arms = arms[rotation:] + arms[:rotation]
            for arm_number, arm in enumerate(arms):
                if (index + arm_number) % 2 == 0:
                    output["server_plans"].append(mongo_plan(arm, index))
                    output["server_plans"].append(postgres_plan(arm, index))
                else:
                    output["server_plans"].append(postgres_plan(arm, index))
                    output["server_plans"].append(mongo_plan(arm, index))
            if (position + 1) % 10 == 0:
                log(f"  plans {position + 1}/{len(plan_indices)}")
                save()

        output["summary"] = summarize(output)
        output["status"] = "complete"
        output["finished_at"] = utc_now()
        output["environment"]["host_after_measurement"] = host_snapshot()
        save()
        print(json.dumps(output["summary"], indent=2))
    finally:
        if not args.keep_diagnostic_indexes:
            try:
                if MONGO_COVER_INDEX in mongo_view.index_information():
                    mongo_view.drop_index(MONGO_COVER_INDEX)
            except Exception as error:
                log(f"Mongo diagnostic index cleanup failed: {error}")
            try:
                pg.execute(f"DROP TABLE IF EXISTS {PG_COVER}")
            except Exception as error:
                log(f"PostgreSQL diagnostic table cleanup failed: {error}")
        pg.close()
        mongo.close()


if __name__ == "__main__":
    main()
