#!/usr/bin/env python3
"""Read-only batching experiment for MongoDB's short PageIndex operations.

The report attributes much of get_node/get_children/get_entity latency to fixed
request and query setup.  This harness tests the corresponding application-level
optimization on the retained ten-million-node dataset:

* baseline: one equality query per requested node/parent;
* batched: one $in query for the same requested inputs.

Both arms return the same logical result in the original input order.  Runs are
paired and cyclically interleaved.  Command monitoring records the number and
driver-observed duration of find/getMore commands without enabling the server
profiler or modifying any collection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


TREE_ID = "base"
NODE_COLLECTION = "layout2_view"
TEXT_COLLECTION = "layout_shared_text"
NODE_INDEX = "allops_tree_node"
CHILD_INDEX = "allops_tree_parent_path"
OPERATIONS = ("get_node", "get_children", "get_entity")
VARIANTS = ("baseline", "batched")
MAX_THREAD_WORKERS = 16


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def provenance() -> dict[str, Any]:
    script_path = Path(__file__).resolve()
    repo_path = script_path.parents[2]
    script_bytes = script_path.read_bytes()

    def git_output(*args: str) -> bytes:
        return subprocess.check_output(
            ("git", *args),
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
        )

    try:
        revision = git_output("rev-parse", "HEAD").decode().strip()
        status = git_output("status", "--porcelain=v1", "-z")
    except (OSError, subprocess.CalledProcessError):
        revision = None
        status = b""
    return {
        "argv": sys.argv,
        "git_revision": revision,
        "git_dirty": bool(status),
        "git_status_sha256": hashlib.sha256(status).hexdigest(),
        "script": str(script_path.relative_to(repo_path)),
        "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
        "script_source": script_bytes.decode(),
    }


def percentile(values: Sequence[float], pct: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return round(float(ordered[index]), 6)


def stats(values: Iterable[float]) -> dict[str, float | int]:
    samples = list(values)
    if not samples:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0}
    return {
        "n": len(samples),
        "mean": round(statistics.mean(samples), 6),
        "p50": percentile(samples, 50),
        "p95": percentile(samples, 95),
        "min": round(min(samples), 6),
        "max": round(max(samples), 6),
    }


def median(values: Iterable[float]) -> float:
    return float(statistics.median(values))


def normalize(values: Iterable[Any]) -> tuple[Any, ...]:
    return tuple("" if value is None else value for value in values)


class CommandTimer:
    """Collect find/getMore duration for one measured application operation."""

    def __init__(self) -> None:
        self.active: str | None = None
        self.requests: dict[int, tuple[str, str]] = {}
        self.events: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)

    def started(self, event: Any) -> None:
        if self.active is not None and event.command_name in {"find", "getMore"}:
            self.requests[event.request_id] = (self.active, event.command_name)

    def succeeded(self, event: Any) -> None:
        request = self.requests.pop(event.request_id, None)
        if request is None:
            return
        label, command = request
        cursor = event.reply.get("cursor", {})
        batch = cursor.get("firstBatch", cursor.get("nextBatch", []))
        self.events[label].append({
            "command": command,
            "duration_ms": event.duration_micros / 1_000.0,
            "rows": len(batch),
        })

    def failed(self, event: Any) -> None:
        self.requests.pop(event.request_id, None)

    @contextmanager
    def measure(self, label: str):
        if self.active is not None:
            raise RuntimeError("nested command timing is not supported")
        self.active = label
        self.events[label] = []
        try:
            yield
        finally:
            self.active = None

    def take(self, label: str) -> list[dict[str, Any]]:
        return self.events.pop(label)


def input_ids(source: dict[str, Any], operation: str) -> list[str]:
    samples = source["samples"]["mongodb"][operation]["full"]
    result = list(dict.fromkeys(str(sample["node_id"]) for sample in samples))
    if not result:
        raise RuntimeError(f"source contains no inputs for {operation}")
    return result


def build_runners(
    database: Any,
    executor: ThreadPoolExecutor | None = None,
) -> dict[str, dict[str, Callable[[list[str]], tuple[Any, ...]]]]:
    nodes = database[NODE_COLLECTION]
    text = database[TEXT_COLLECTION]
    node_projection = {
        "_id": 0,
        "node_id": 1,
        "parent_id": 1,
        "depth": 1,
        "title": 1,
        "summary": 1,
        "start_index": 1,
        "end_index": 1,
    }
    child_projection = {
        "_id": 0,
        "parent_id": 1,
        "node_id": 1,
        "title": 1,
        "summary": 1,
    }

    def node_row(row: dict[str, Any]) -> tuple[Any, ...]:
        return normalize((
            row.get("node_id"),
            row.get("parent_id"),
            row.get("depth"),
            row.get("title"),
            row.get("summary"),
            row.get("start_index"),
            row.get("end_index"),
        ))

    def node_baseline(ids: list[str]) -> tuple[Any, ...]:
        output = []
        for node_id in ids:
            row = nodes.find_one(
                {"tree_id": TREE_ID, "node_id": node_id},
                node_projection,
                hint=NODE_INDEX,
            )
            output.append(node_row(row) if row is not None else None)
        return tuple(output)

    def node_batched(ids: list[str]) -> tuple[Any, ...]:
        rows = nodes.find(
            {"tree_id": TREE_ID, "node_id": {"$in": ids}},
            node_projection,
        ).hint(NODE_INDEX).batch_size(100_000)
        by_id = {str(row["node_id"]): node_row(row) for row in rows}
        return tuple(by_id.get(node_id) for node_id in ids)

    def child_row(row: dict[str, Any]) -> tuple[Any, ...]:
        return normalize((
            row.get("node_id"),
            row.get("title"),
            row.get("summary"),
        ))

    def children_baseline(ids: list[str]) -> tuple[Any, ...]:
        output = []
        for parent_id in ids:
            rows = (
                nodes.find(
                    {"tree_id": TREE_ID, "parent_id": parent_id},
                    child_projection,
                )
                .sort([("path", 1), ("node_id", 1)])
                .hint(CHILD_INDEX)
                .batch_size(100_000)
            )
            output.append(tuple(child_row(row) for row in rows))
        return tuple(output)

    def children_batched(ids: list[str]) -> tuple[Any, ...]:
        rows = (
            nodes.find(
                {"tree_id": TREE_ID, "parent_id": {"$in": ids}},
                child_projection,
            )
            .sort([("parent_id", 1), ("path", 1), ("node_id", 1)])
            .hint(CHILD_INDEX)
            .batch_size(100_000)
        )
        by_parent: defaultdict[str, list[tuple[Any, ...]]] = defaultdict(list)
        for row in rows:
            by_parent[str(row["parent_id"])].append(child_row(row))
        return tuple(tuple(by_parent[parent_id]) for parent_id in ids)

    def entity_baseline(ids: list[str]) -> tuple[Any, ...]:
        output = []
        for node_id in ids:
            row = text.find_one(
                {"_id": node_id},
                {"_id": 1, "text": 1},
                hint="_id_",
            )
            output.append(
                normalize((row.get("_id"), row.get("text")))
                if row is not None
                else None
            )
        return tuple(output)

    def entity_batched(ids: list[str]) -> tuple[Any, ...]:
        rows = text.find(
            {"_id": {"$in": ids}},
            {"_id": 1, "text": 1},
        ).hint("_id_").batch_size(100_000)
        by_id = {
            str(row["_id"]): normalize((row.get("_id"), row.get("text")))
            for row in rows
        }
        return tuple(by_id.get(node_id) for node_id in ids)

    runners = {
        "get_node": {
            "baseline": node_baseline,
            "batched": node_batched,
        },
        "get_children": {
            "baseline": children_baseline,
            "batched": children_batched,
        },
        "get_entity": {
            "baseline": entity_baseline,
            "batched": entity_batched,
        },
    }
    if executor is not None:
        for operation in OPERATIONS:
            single = runners[operation]["baseline"]

            def threaded(
                ids: list[str],
                single_runner: Callable[[list[str]], tuple[Any, ...]] = single,
            ) -> tuple[Any, ...]:
                futures = [
                    executor.submit(single_runner, [input_id])
                    for input_id in ids
                ]
                return tuple(future.result()[0] for future in futures)

            runners[operation]["threaded"] = threaded
    return runners


def summarize(
    samples: list[dict[str, Any]],
    variants: Sequence[str] = VARIANTS,
) -> dict[str, Any]:
    grouped: defaultdict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[
            (sample["operation"], sample["batch_size"], sample["group"])
        ].append(sample)

    output: dict[str, Any] = {}
    for operation in OPERATIONS:
        output[operation] = {}
        batch_sizes = sorted({
            sample["batch_size"]
            for sample in samples
            if sample["operation"] == operation
        })
        for batch_size in batch_sizes:
            groups = [
                group
                for (item_operation, item_size, _), group in grouped.items()
                if item_operation == operation and item_size == batch_size
            ]
            by_variant: dict[str, dict[str, Any]] = {}
            group_medians: dict[str, list[dict[str, float]]] = {
                variant: [] for variant in variants
            }
            for group in groups:
                for variant in variants:
                    arm = [item for item in group if item["variant"] == variant]
                    group_medians[variant].append({
                        "total_ms": median(item["total_ms"] for item in arm),
                        "command_ms": median(item["command_ms"] for item in arm),
                        "commands": median(item["commands"] for item in arm),
                        "rows": median(item["rows"] for item in arm),
                    })
            for variant in variants:
                medians = group_medians[variant]
                by_variant[variant] = {
                    key: stats(item[key] for item in medians)
                    for key in ("total_ms", "command_ms", "commands", "rows")
                }
            baseline = group_medians["baseline"]
            batched = group_medians["batched"]
            paired_speedup = [
                left["total_ms"] / right["total_ms"]
                for left, right in zip(baseline, batched)
                if right["total_ms"] > 0
            ]
            baseline_p50 = float(by_variant["baseline"]["total_ms"]["p50"])
            batched_p50 = float(by_variant["batched"]["total_ms"]["p50"])
            baseline_p95 = float(by_variant["baseline"]["total_ms"]["p95"])
            batched_p95 = float(by_variant["batched"]["total_ms"]["p95"])
            output[operation][str(batch_size)] = {
                "groups": len(groups),
                "observations_per_variant": sum(
                    len([item for item in group if item["variant"] == "baseline"])
                    for group in groups
                ),
                **by_variant,
                "paired_speedup": stats(paired_speedup),
                "speedup_p50": round(
                    baseline_p50 / batched_p50, 3
                ) if batched_p50 else None,
                "speedup_p95": round(
                    baseline_p95 / batched_p95, 3
                ) if batched_p95 else None,
            }
            if "threaded" in variants:
                threaded = group_medians["threaded"]
                paired_vs_threaded = [
                    left["total_ms"] / right["total_ms"]
                    for left, right in zip(threaded, batched)
                    if right["total_ms"] > 0
                ]
                threaded_p50 = float(
                    by_variant["threaded"]["total_ms"]["p50"]
                )
                output[operation][str(batch_size)].update({
                    "paired_speedup_vs_threaded": stats(paired_vs_threaded),
                    "speedup_vs_threaded_p50": round(
                        threaded_p50 / batched_p50,
                        3,
                    ) if batched_p50 else None,
                })
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument("--mongo-db", default="bench")
    parser.add_argument(
        "--source",
        default=(
            "bench/db/runs/short_ops_server_profile_20260724/"
            "matched_server_profile_10m.json"
        ),
    )
    parser.add_argument(
        "--out",
        default=(
            "bench/db/runs/short_ops_batching_20260724/"
            "mongo_batching_10m.json"
        ),
    )
    parser.add_argument("--batch-sizes", default="1,2,3,4,8,16,32,64")
    parser.add_argument("--groups", type=int, default=40)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260724)
    parser.add_argument(
        "--measurement-mode",
        choices=("wall", "telemetry"),
        default="wall",
        help=(
            "wall registers no PyMongo listener and is the primary client-wall "
            "measurement; telemetry additionally records find/getMore events"
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow replacing an existing output artifact",
    )
    parser.add_argument(
        "--threaded-control",
        action="store_true",
        help=(
            "add a third arm that issues the individual equality queries "
            "concurrently through a shared thread pool"
        ),
    )
    parser.add_argument(
        "--thread-workers",
        type=int,
        default=MAX_THREAD_WORKERS,
        help=f"shared threaded-control pool size (maximum {MAX_THREAD_WORKERS})",
    )
    args = parser.parse_args()
    if args.groups < 10 or args.repeats < 2:
        raise SystemExit("requires groups>=10 and repeats>=2")
    batch_sizes = [int(item) for item in args.batch_sizes.split(",")]
    if any(size < 1 for size in batch_sizes):
        raise SystemExit("batch sizes must be positive")
    if not 1 <= args.thread_workers <= MAX_THREAD_WORKERS:
        raise SystemExit(
            f"thread workers must be between 1 and {MAX_THREAD_WORKERS}"
        )

    from pymongo import MongoClient, monitoring

    class Listener(CommandTimer, monitoring.CommandListener):
        pass

    source_path = Path(args.source)
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    pools = {
        operation: input_ids(source, operation)
        for operation in OPERATIONS
    }
    if max(batch_sizes) > min(len(pool) for pool in pools.values()):
        raise SystemExit("largest batch exceeds an operation's input pool")

    listener = Listener()
    client_options: dict[str, Any] = {
        "serverSelectionTimeoutMS": 5_000,
    }
    if args.measurement_mode == "telemetry":
        client_options["event_listeners"] = [listener]
    client = MongoClient(args.mongo_uri, **client_options)
    database = client[args.mongo_db]
    indexes = database[NODE_COLLECTION].index_information()
    missing = {NODE_INDEX, CHILD_INDEX} - set(indexes)
    if missing:
        raise SystemExit(f"missing retained indexes: {sorted(missing)}")
    if database[NODE_COLLECTION].estimated_document_count() != 10_000_000:
        raise SystemExit("retained node collection is not the 10M dataset")

    executor = (
        ThreadPoolExecutor(max_workers=args.thread_workers)
        if args.threaded_control
        else None
    )
    variants = (
        ("baseline", "threaded", "batched")
        if args.threaded_control
        else VARIANTS
    )
    runners = build_runners(database, executor)
    output: dict[str, Any] = {
        "run": {
            "status": "running",
            "started_at": utc_now(),
            "source": args.source,
            "groups": args.groups,
            "repeats": args.repeats,
            "batch_sizes": batch_sizes,
            "seed": args.seed,
            "measurement_mode": args.measurement_mode,
            "variants": list(variants),
            "thread_workers": args.thread_workers if executor else None,
        },
        "provenance": {
            **provenance(),
            "source_artifact": str(source_path),
            "source_artifact_sha256": hashlib.sha256(source_bytes).hexdigest(),
        },
        "contract": {
            "baseline": "one equality find per requested input",
            "threaded": (
                "the same individual equality finds submitted concurrently "
                "through a shared client-side thread pool"
                if executor
                else "not measured"
            ),
            "batched": "one $in find for the same requested inputs",
            "result": (
                "same fields and logical rows, restored to input order; "
                "children retain path/node_id order within each parent"
            ),
            "timing": (
                "paired client wall latency including result materialization "
                "and reassembly; PyMongo command monitoring is disabled in "
                "wall mode and enabled only in telemetry mode"
            ),
            "mutation": "none; retained collections are read only",
        },
        "environment": {
            "mongodb": client.server_info()["version"],
            "pymongo": __import__("pymongo").version,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "loadavg_before": list(__import__("os").getloadavg()),
            "node_documents": database[NODE_COLLECTION].estimated_document_count(),
            "text_documents": database[TEXT_COLLECTION].estimated_document_count(),
        },
        "input_pool_sizes": {
            operation: len(pool) for operation, pool in pools.items()
        },
        "input_groups": {
            operation: {} for operation in OPERATIONS
        },
        "validation": {
            "all_outputs_match": True,
            "checks": 0,
        },
        "samples": [],
        "summary": {},
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and not args.overwrite:
        raise SystemExit(
            f"refusing to overwrite existing artifact {out_path}; "
            "choose a new --out or pass --overwrite"
        )

    def save() -> None:
        output["summary"] = summarize(output["samples"], variants)
        out_path.write_text(json.dumps(output, indent=2))

    save()
    for operation_index, operation in enumerate(OPERATIONS):
        pool = pools[operation]
        # Warm both access shapes over the retained input set before timing.
        warm_ids = pool[: min(len(pool), 200)]
        expected = runners[operation]["baseline"](warm_ids[:8])
        for variant in variants[1:]:
            actual = runners[operation][variant](warm_ids[:8])
            if actual != expected:
                raise RuntimeError(
                    f"warm-up output mismatch for {operation}/{variant}"
                )
        runners[operation]["batched"](warm_ids)

        for batch_size in batch_sizes:
            rng = random.Random(
                args.seed + operation_index * 100_000 + batch_size
            )
            groups = [
                rng.sample(pool, batch_size)
                for _ in range(args.groups)
            ]
            output["input_groups"][operation][str(batch_size)] = groups
            for repeat in range(args.repeats):
                order = list(range(args.groups))
                random.Random(
                    args.seed
                    + operation_index * 1_000_000
                    + batch_size * 1_000
                    + repeat
                ).shuffle(order)
                for position, group_index in enumerate(order):
                    ids = groups[group_index]
                    rotation = (repeat + position) % len(variants)
                    variant_order = (
                        variants[rotation:] + variants[:rotation]
                    )
                    results: dict[str, tuple[Any, ...]] = {}
                    for variant in variant_order:
                        label = (
                            f"{operation}:{batch_size}:{group_index}:"
                            f"{repeat}:{variant}:{time.perf_counter_ns()}"
                        )
                        with listener.measure(label):
                            started = time.perf_counter()
                            result = runners[operation][variant](ids)
                            total_ms = (time.perf_counter() - started) * 1_000
                        events = listener.take(label)
                        results[variant] = result
                        rows = (
                            sum(len(item) for item in result)
                            if operation == "get_children"
                            else sum(item is not None for item in result)
                        )
                        output["samples"].append({
                            "operation": operation,
                            "batch_size": batch_size,
                            "group": group_index,
                            "repeat": repeat,
                            "variant": variant,
                            "rows": rows,
                            "total_ms": round(total_ms, 6),
                            "command_ms": round(
                                sum(event["duration_ms"] for event in events),
                                6,
                            ),
                            "commands": len(events),
                            "find_commands": sum(
                                event["command"] == "find" for event in events
                            ),
                            "getmore_commands": sum(
                                event["command"] == "getMore"
                                for event in events
                            ),
                        })
                    if any(
                        results[variant] != results["baseline"]
                        for variant in variants[1:]
                    ):
                        output["validation"]["all_outputs_match"] = False
                        save()
                        raise RuntimeError(
                            f"output mismatch for {operation} batch={batch_size} "
                            f"group={group_index} repeat={repeat}"
                        )
                    output["validation"]["checks"] += 1
            save()
            item = output["summary"][operation][str(batch_size)]
            print(
                f"{operation} batch={batch_size}: "
                f"{item['baseline']['total_ms']['p50']:.3f} -> "
                f"{item['batched']['total_ms']['p50']:.3f} ms "
                f"({item['speedup_p50']:.2f}x)",
                flush=True,
            )

    output["run"]["status"] = "complete"
    output["run"]["completed_at"] = utc_now()
    output["environment"]["loadavg_after"] = list(__import__("os").getloadavg())
    save()
    if executor is not None:
        executor.shutdown(wait=True)
    client.close()
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
