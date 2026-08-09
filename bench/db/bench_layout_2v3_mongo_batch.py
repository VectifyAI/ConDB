#!/usr/bin/env python3
"""Measure how much MongoDB cursor batching contributes to the 2-vs-3 gap.

The retained 10M stores and diagnostic covering index from
``bench_layout_2v3_rootcause.py`` are prerequisites.  Every batch variant
returns the same ordered rows; only the requested MongoDB cursor batch size
changes.  Command monitoring records the number and sizes of find/getMore
replies so the round-trip intervention is directly observable.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import random
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench_layout_2v3 import fingerprint
from bench_layout_2v3_rootcause import (
    LEAN_MONGO_INDEX,
    MONGO_COVER_INDEX,
    MONGO_VIEW,
    bounds,
    host_snapshot,
    stats,
    stratified_indices,
    validate_source,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def provenance() -> dict[str, Any]:
    script_path = Path(__file__).resolve()
    repo_path = script_path.parents[2]
    script_bytes = script_path.read_bytes()
    dependency_paths = [
        script_path.with_name("bench_layout_2v3.py"),
        script_path.with_name("bench_layout_2v3_rootcause.py"),
    ]

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
        "local_dependencies": [
            {
                "path": str(path.relative_to(repo_path)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source": path.read_text(),
            }
            for path in dependency_paths
        ],
    }


def bootstrap_median_ci(
    values: list[float],
    *,
    seed: int,
    draws: int = 10_000,
) -> list[float] | None:
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    estimates = [
        statistics.median(values[rng.randrange(n)] for _ in range(n))
        for _ in range(draws)
    ]
    estimates.sort()
    return [
        round(estimates[int(0.025 * (draws - 1))], 6),
        round(estimates[int(0.975 * (draws - 1))], 6),
    ]


def ordered_fingerprint(
    rows: list[tuple[Any, ...]],
    *,
    arm: str,
) -> str:
    if arm == "covered":
        return fingerprint(rows)
    payload = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class CommandTimer:
    """Capture driver-observed duration and row count for each cursor reply."""

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
        self.events[label].append(
            {
                "command": command,
                "duration_ms": event.duration_micros / 1_000.0,
                "rows": len(batch),
                "cursor_alive": bool(cursor.get("id", 0)),
            }
        )

    def failed(self, event: Any) -> None:
        self.requests.pop(event.request_id, None)

    @contextmanager
    def measure(self, label: str):
        if self.active is not None:
            raise RuntimeError("nested command timer")
        self.active = label
        self.events[label] = []
        try:
            yield
        finally:
            self.active = None

    def result(self, label: str) -> list[dict[str, Any]]:
        return self.events.pop(label)


def path_equal_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: defaultdict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[(sample["variant"], sample["source_index"])].append(sample)
    output: dict[str, Any] = {}
    for variant in sorted({sample["variant"] for sample in samples}):
        groups = [
            group
            for (item_variant, _), group in grouped.items()
            if item_variant == variant
        ]
        numeric = (
            "total_ms",
            "fetch_ms",
            "command_ms",
            "cursor_overhead_ms",
            "normalize_ms",
            "cleanup_ms",
            "commands",
            "getmore_commands",
            "first_batch_rows",
            "largest_batch_rows",
        )
        summary: dict[str, Any] = {
            "paths": len(groups),
            "observations": sum(len(group) for group in groups),
            "avg_rows": round(
                statistics.mean(group[0]["rows"] for group in groups), 3
            ),
        }
        for key in numeric:
            summary[key] = stats(
                [statistics.median(item[key] for item in group) for group in groups]
            )
        output[variant] = summary
    default_groups = {
        source_index: group
        for (variant, source_index), group in grouped.items()
        if variant == "default"
    }
    for variant_index, variant in enumerate(sorted(output)):
        if variant == "default":
            continue
        variant_groups = {
            source_index: group
            for (item_variant, source_index), group in grouped.items()
            if item_variant == variant
        }
        paired_speedups = []
        for source_index in sorted(set(default_groups) & set(variant_groups)):
            baseline = statistics.median(
                item["total_ms"] for item in default_groups[source_index]
            )
            candidate = statistics.median(
                item["total_ms"] for item in variant_groups[source_index]
            )
            if candidate > 0:
                paired_speedups.append(baseline / candidate)
        output[variant]["paired_speedup_vs_default"] = {
            **stats(paired_speedups),
            "median_bootstrap_95ci": bootstrap_median_ci(
                paired_speedups,
                seed=20260728 + variant_index,
            ),
            "roots_faster": sum(value > 1.0 for value in paired_speedups),
            "roots_total": len(paired_speedups),
        }
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-result",
        default="bench/db/runs/rootcause_20260718/mongo_seed_10m.json",
    )
    parser.add_argument(
        "--out",
        default="bench/db/runs/rootcause_20260718/mongo_batch_10m.json",
    )
    parser.add_argument("--paths", type=int, default=40)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--arm", choices=("covered", "id_only"), default="id_only")
    parser.add_argument("--batch-sizes", default="default,101,1000,10000,1000000")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument(
        "--measurement-mode",
        choices=("wall", "telemetry"),
        default="wall",
        help=(
            "wall registers no PyMongo listener and is the primary latency "
            "measurement; telemetry additionally records find/getMore events"
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow replacing an existing output artifact",
    )
    args = parser.parse_args()
    if args.paths < 3 or args.repeats < 2:
        raise SystemExit("requires paths>=3 and repeats>=2")

    source_path = Path(args.source_result)
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    validate_source(source, allow_nonstandard=False)
    indices = stratified_indices(source, args.paths)
    variants: list[tuple[str, int | None]] = []
    for item in args.batch_sizes.split(","):
        if item == "default":
            variants.append(("default", None))
        else:
            value = int(item)
            if value < 1:
                raise SystemExit("batch sizes must be positive")
            variants.append((str(value), value))

    from pymongo import MongoClient, monitoring

    class Listener(CommandTimer, monitoring.CommandListener):
        pass

    timer = Listener()
    client_options: dict[str, Any] = {
        "serverSelectionTimeoutMS": 5_000,
    }
    if args.measurement_mode == "telemetry":
        client_options["event_listeners"] = [timer]
    mongo = MongoClient(args.mongo_uri, **client_options)
    collection = mongo["bench"][MONGO_VIEW]
    projection = (
        {"node_id": 1, "_id": 0}
        if args.arm == "id_only"
        else {"node_id": 1, "title": 1, "summary": 1, "_id": 0}
    )
    index_name = LEAN_MONGO_INDEX if args.arm == "id_only" else MONGO_COVER_INDEX
    if index_name not in collection.index_information():
        raise SystemExit(f"missing retained diagnostic index {index_name}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output: dict[str, Any] = {
        "status": "running",
        "started_at": utc_now(),
        "provenance": {
            **provenance(),
            "source_artifact": str(source_path),
            "source_artifact_sha256": hashlib.sha256(source_bytes).hexdigest(),
        },
        "source_result": args.source_result,
        "source_result_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "nodes": source["nodes"],
        "arm": args.arm,
        "measurement_mode": args.measurement_mode,
        "indices": indices,
        "repeats": args.repeats,
        "variants": [
            {"name": name, "requested_batch_size": value}
            for name, value in variants
        ],
        "contract": (
            "same MongoDB collection, index, bounds, sort, projection, ordered rows, "
            "and client normalization; only Cursor.batch_size changes; summaries "
            "first take the per-path median across repeats; command monitoring "
            "is disabled in wall mode and enabled only in telemetry mode"
        ),
        "validation": {
            "all_ordered_outputs_match": True,
            "checks": 0,
        },
        "environment": {
            "before": host_snapshot(),
            "mongo_server": mongo.server_info().get("version"),
            "pymongo": __import__("pymongo").version,
            "transport": "localhost Docker",
        },
        "samples": [],
    }
    if out_path.exists() and not args.overwrite:
        raise SystemExit(
            f"refusing to overwrite existing artifact {out_path}; "
            "choose a new --out or pass --overwrite"
        )

    def save() -> None:
        output["summary"] = path_equal_summary(output["samples"])
        out_path.write_text(json.dumps(output, indent=2))

    def query(index: int, repeat: int, variant: str, batch_size: int | None) -> dict[str, Any]:
        sample = source["samples"][index]
        lower, upper = bounds(sample["path"])
        cursor = (
            collection.find({"path": {"$gte": lower, "$lt": upper}}, projection)
            .sort([("path", 1), ("node_id", 1)])
            .hint(index_name)
        )
        if batch_size is not None:
            cursor = cursor.batch_size(batch_size)
        label = f"{variant}:{repeat}:{index}:{time.perf_counter_ns()}"
        gc.disable()
        try:
            with timer.measure(label):
                started = time.perf_counter()
                raw_rows = list(cursor)
                fetch_ms = (time.perf_counter() - started) * 1_000
            events = timer.result(label)
            started = time.perf_counter()
            if args.arm == "id_only":
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

        if len(result) != sample["rows"]:
            raise RuntimeError(f"row mismatch at source index {index}")
        result_fingerprint = ordered_fingerprint(result, arm=args.arm)
        if args.arm == "covered":
            if result_fingerprint != sample["fingerprint"]:
                raise RuntimeError(f"fingerprint mismatch at source index {index}")
        command_ms = sum(event["duration_ms"] for event in events)
        total_ms = fetch_ms + normalize_ms + cleanup_ms
        del result
        gc.collect()
        return {
            "source_index": index,
            "repeat": repeat,
            "variant": variant,
            "requested_batch_size": batch_size,
            "rows": sample["rows"],
            "fetch_ms": round(fetch_ms, 6),
            "command_ms": round(command_ms, 6),
            "cursor_overhead_ms": round(fetch_ms - command_ms, 6),
            "normalize_ms": round(normalize_ms, 6),
            "cleanup_ms": round(cleanup_ms, 6),
            "total_ms": round(total_ms, 6),
            "commands": len(events),
            "getmore_commands": sum(event["command"] == "getMore" for event in events),
            "first_batch_rows": events[0]["rows"] if events else 0,
            "largest_batch_rows": max((event["rows"] for event in events), default=0),
            "output_fingerprint": result_fingerprint,
            "command_events": events,
        }

    try:
        # Warm every selected range once with the driver's normal batching.  This
        # removes storage/cache state without pre-favoring a batch intervention.
        print(f"warming {len(indices)} paths", flush=True)
        for position, index in enumerate(indices):
            query(index, -1, "default", None)
            if (position + 1) % 10 == 0:
                print(f"  warm {position + 1}/{len(indices)}", flush=True)

        print(
            f"measuring {len(indices)} paths x {args.repeats} repeats x "
            f"{len(variants)} variants ({args.arm})",
            flush=True,
        )
        done = 0
        for repeat in range(args.repeats):
            offset = repeat * len(indices) // args.repeats
            path_order = indices[offset:] + indices[:offset]
            for position, index in enumerate(path_order):
                rotation = (index + repeat) % len(variants)
                variant_order = variants[rotation:] + variants[:rotation]
                path_samples = []
                for variant, batch_size in variant_order:
                    path_samples.append(query(index, repeat, variant, batch_size))
                expected_fingerprint = path_samples[0]["output_fingerprint"]
                if any(
                    sample["output_fingerprint"] != expected_fingerprint
                    for sample in path_samples[1:]
                ):
                    output["validation"]["all_ordered_outputs_match"] = False
                    save()
                    raise RuntimeError(
                        f"ordered output mismatch at source index {index}, "
                        f"repeat {repeat}"
                    )
                output["validation"]["checks"] += len(path_samples) - 1
                output["samples"].extend(path_samples)
                done += 1
                if done % 10 == 0:
                    save()
                    print(f"  measure {done}/{len(indices) * args.repeats}", flush=True)

        output["status"] = "complete"
        output["finished_at"] = utc_now()
        output["environment"]["after"] = host_snapshot()
        save()
        print(json.dumps(output["summary"], indent=2))
    finally:
        mongo.close()


if __name__ == "__main__":
    main()
