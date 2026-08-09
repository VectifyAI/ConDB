#!/usr/bin/env python3
"""Instrument MongoDB subtree latency without changing the report benchmark.

This script reuses collections left by ``bench_layout_2v3.py --keep`` and the
path list in that run's JSON.  It does not ingest data and it does not update
the paper.  The three-collection client path is split into:

* covered Structure scan and ID materialization;
* logical 1,000-ID Metadata fetches;
* insertion into the client Metadata map;
* order-preserving merge.

Result-list release is deliberately timed *after* the query timer.  This both
keeps one sample's cleanup out of the next sample and exposes the cleanup cost
that contaminated the original P99 measurement.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path


DB = "bench"
VIEW = "layout2_view"
STRUCT = "layout3_struct"
META = "layout3_meta"
TEXT = "layout_shared_text"

LEAN = [("path", 1), ("node_id", 1)]
PROJ_VIEW = {"node_id": 1, "title": 1, "summary": 1, "_id": 0}
PROJ_ID = {"node_id": 1, "_id": 0}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def percentile(values: list[float], p: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return ordered[index]


def stats(values: list[float]) -> dict:
    return {
        "p50_ms": round(percentile(values, 50), 3),
        "p95_ms": round(percentile(values, 95), 3),
        "p99_ms": round(percentile(values, 99), 3),
        "max_ms": round(max(values), 3) if values else 0.0,
        "mean_ms": round(statistics.mean(values), 3) if values else 0.0,
        "sum_ms": round(sum(values), 3),
    }


def fingerprint(rows: list[tuple[str, str, str]]) -> str:
    digest = hashlib.sha256()
    for node_id, title, summary_text in rows:
        for value in (node_id, title, summary_text):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def output_bytes(rows: list[tuple[str, str, str]]) -> int:
    return sum(
        len(node_id.encode("utf-8"))
        + len(title.encode("utf-8"))
        + len(summary_text.encode("utf-8"))
        for node_id, title, summary_text in rows
    )


def parse_indices(spec: str, count: int) -> list[int]:
    if spec == "all":
        return list(range(count))

    selected: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            first, last = part.split("-", 1)
            selected.extend(range(int(first), int(last) + 1))
        else:
            selected.append(int(part))

    unique = list(dict.fromkeys(selected))
    invalid = [index for index in unique if index < 0 or index >= count]
    if invalid:
        raise SystemExit(f"invalid sample indices: {invalid}")
    if not unique:
        raise SystemExit("no sample indices selected")
    return unique


def bounds(path: str) -> dict:
    return {"path": {"$gte": path + "/", "$lt": path + "0"}}


def aggregate(samples: list[dict]) -> dict:
    keys = (
        "two_total_ms",
        "two_release_ms",
        "three_total_ms",
        "structure_ms",
        "metadata_fetch_ms",
        "metadata_map_ms",
        "metadata_batch_cleanup_ms",
        "ordered_merge_ms",
        "three_unattributed_ms",
        "three_release_ms",
        "gc_collect_ms",
    )
    result = {key: stats([sample[key] for sample in samples]) for key in keys}

    total = sum(sample["three_total_ms"] for sample in samples)
    result["three_stage_share_of_total"] = {
        key: round(sum(sample[key] for sample in samples) / total, 4)
        for key in (
            "structure_ms",
            "metadata_fetch_ms",
            "metadata_map_ms",
            "metadata_batch_cleanup_ms",
            "ordered_merge_ms",
            "three_unattributed_ms",
        )
    }
    result["logical_metadata_calls"] = {
        "sum": sum(sample["metadata_calls"] for sample in samples),
        "p50": int(percentile([sample["metadata_calls"] for sample in samples], 50)),
        "p95": int(percentile([sample["metadata_calls"] for sample in samples], 95)),
        "p99": int(percentile([sample["metadata_calls"] for sample in samples], 99)),
        "max": max(sample["metadata_calls"] for sample in samples),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-result",
        default="bench/db/runs/report_3eng_20260716/layout_2v3_mongo_10m_clean.json",
        help="completed clean benchmark JSON whose path order is authoritative",
    )
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument(
        "--out",
        default="bench/db/runs/report_3eng_20260716/layout_2v3_mongo_breakdown.json",
    )
    parser.add_argument(
        "--indices",
        default="all",
        help="zero-based sample indices: all, 25, or 25-26,53-54",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "independently rebuild retained collections by running "
            "bench_layout_2v3.py --keep before instrumentation"
        ),
    )
    parser.add_argument(
        "--rebuild-result",
        default="bench/db/runs/report_3eng_20260716/"
        "layout_2v3_mongo_breakdown_seed.json",
        help="throwaway benchmark JSON written by --rebuild",
    )
    parser.add_argument(
        "--keep-collections",
        action="store_true",
        help="do not drop collections owned by an independent --rebuild run",
    )
    args = parser.parse_args()

    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")

    from pymongo import MongoClient

    source = json.loads(Path(args.source_result).read_text())
    if source.get("status") != "complete":
        raise SystemExit(f"source result is not complete: {source.get('status')!r}")
    if source.get("chunk") != 1_000:
        raise SystemExit(f"source result chunk is not 1000: {source.get('chunk')!r}")
    if len(source.get("samples", [])) != source.get("paths"):
        raise SystemExit("source sample count does not match source path count")

    indices = parse_indices(args.indices, len(source["samples"]))
    rebuilt = False
    if args.rebuild:
        benchmark = Path(__file__).with_name("bench_layout_2v3.py")
        rebuild_command = [
            sys.executable,
            str(benchmark),
            "--doc",
            source["doc"],
            "--mongo-uri",
            args.mongo_uri,
            "--out",
            args.rebuild_result,
            "--chunk",
            str(source["chunk"]),
            "--max-paths",
            str(source["paths"]),
            "--keep",
        ]
        log("rebuilding MongoDB layout collections independently ...")
        subprocess.run(rebuild_command, check=True)
        rebuilt = True

        rebuilt_result = json.loads(Path(args.rebuild_result).read_text())
        if rebuilt_result.get("status") != "complete":
            raise SystemExit("independent collection rebuild did not complete")
        rebuilt_samples = rebuilt_result.get("samples", [])
        if [sample["path"] for sample in rebuilt_samples] != [
            sample["path"] for sample in source["samples"]
        ]:
            raise SystemExit("independent rebuild selected different paths")
        if [sample["fingerprint"] for sample in rebuilt_samples] != [
            sample["fingerprint"] for sample in source["samples"]
        ]:
            raise SystemExit("independent rebuild produced different outputs")

    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    database = client[DB]
    try:
        existing = set(database.list_collection_names())
        required = {VIEW, STRUCT, META, TEXT}
        missing = sorted(required - existing)
        if missing:
            raise SystemExit(
                "missing retained collections: "
                + ", ".join(missing)
                + "; run bench_layout_2v3.py --keep first"
            )

        expected_nodes = source["nodes"]
        counts = {
            name: database[name].estimated_document_count()
            for name in (VIEW, STRUCT, META, TEXT)
        }
        for name in (VIEW, STRUCT, META):
            if counts[name] != expected_nodes:
                raise SystemExit(
                    f"collection {name} has {counts[name]:,} docs, expected "
                    f"{expected_nodes:,}"
                )

        view = database[VIEW]
        struct = database[STRUCT]
        meta = database[META]
        chunk_size = source["chunk"]

        def two_collections(path: str) -> list[tuple[str, str, str]]:
            cursor = view.find(bounds(path), PROJ_VIEW).sort(LEAN).hint(LEAN)
            return [
                (doc["node_id"], doc.get("title", ""), doc.get("summary", ""))
                for doc in cursor
            ]

        def three_staged(path: str) -> tuple[list[tuple[str, str, str]], dict]:
            started = time.perf_counter()
            ids = [
                doc["node_id"]
                for doc in struct.find(bounds(path), PROJ_ID).sort(LEAN).hint(LEAN)
            ]
            structure_ms = (time.perf_counter() - started) * 1_000

            metadata: dict[str, tuple[str, str]] = {}
            fetch_call_ms: list[float] = []
            metadata_map_ms = 0.0
            metadata_batch_cleanup_ms = 0.0
            for offset in range(0, len(ids), chunk_size):
                chunk = ids[offset : offset + chunk_size]

                started = time.perf_counter()
                documents = list(
                    meta.find(
                        {"_id": {"$in": chunk}},
                        {"title": 1, "summary": 1},
                    )
                )
                fetch_call_ms.append((time.perf_counter() - started) * 1_000)

                started = time.perf_counter()
                for document in documents:
                    metadata[document["_id"]] = (
                        document.get("title", ""),
                        document.get("summary", ""),
                    )
                metadata_map_ms += (time.perf_counter() - started) * 1_000

                started = time.perf_counter()
                del documents
                metadata_batch_cleanup_ms += (
                    time.perf_counter() - started
                ) * 1_000

            if len(metadata) != len(ids):
                raise RuntimeError(
                    f"metadata mismatch for {path}: {len(metadata):,}/{len(ids):,}"
                )

            started = time.perf_counter()
            result = [(node_id, *metadata[node_id]) for node_id in ids]
            ordered_merge_ms = (time.perf_counter() - started) * 1_000
            return result, {
                "structure_ms": structure_ms,
                "metadata_fetch_ms": sum(fetch_call_ms),
                "metadata_map_ms": metadata_map_ms,
                "metadata_batch_cleanup_ms": metadata_batch_cleanup_ms,
                "ordered_merge_ms": ordered_merge_ms,
                "metadata_calls": len(fetch_call_ms),
                "metadata_fetch_call_ms": stats(fetch_call_ms),
            }

        # Warm the exact code paths, then release results before timed work.
        log("warming retained MongoDB layouts ...")
        for source_sample in source["samples"][:3]:
            left = two_collections(source_sample["path"])
            right, _ = three_staged(source_sample["path"])
            if left != right:
                raise RuntimeError(f"warm-up mismatch for {source_sample['path']}")
            del left, right
        gc.collect()

        output = {
            "source_result": args.source_result,
            "nodes": expected_nodes,
            "source_paths": source["paths"],
            "chunk": chunk_size,
            "indices": indices,
            "repeats": args.repeats,
            "collections": counts,
            "status": "running",
            "samples": [],
        }
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Cyclic GC is not needed for these acyclic row lists.  Disable it only
        # during each paired measurement, then collect outside both timers.
        log(
            f"instrumenting {len(indices)} paths x {args.repeats} repeat(s) ..."
        )
        for repeat in range(args.repeats):
            for position, index in enumerate(indices):
                source_sample = source["samples"][index]
                path = source_sample["path"]
                two_first = (index + repeat) % 2 == 0

                gc.disable()
                try:
                    if two_first:
                        started = time.perf_counter()
                        two_rows = two_collections(path)
                        two_total_ms = (time.perf_counter() - started) * 1_000

                        started = time.perf_counter()
                        three_rows, stages = three_staged(path)
                        three_total_ms = (time.perf_counter() - started) * 1_000
                    else:
                        started = time.perf_counter()
                        three_rows, stages = three_staged(path)
                        three_total_ms = (time.perf_counter() - started) * 1_000

                        started = time.perf_counter()
                        two_rows = two_collections(path)
                        two_total_ms = (time.perf_counter() - started) * 1_000
                finally:
                    gc.enable()

                if two_rows != three_rows:
                    raise RuntimeError(f"timed output mismatch for {path}")
                if len(two_rows) != source_sample["rows"]:
                    raise RuntimeError(
                        f"row-count mismatch for {path}: "
                        f"{len(two_rows):,}/{source_sample['rows']:,}"
                    )
                digest = fingerprint(two_rows)
                if digest != source_sample["fingerprint"]:
                    raise RuntimeError(f"fingerprint mismatch for {path}")

                bytes_returned = output_bytes(two_rows)
                attributed = sum(
                    stages[key]
                    for key in (
                        "structure_ms",
                        "metadata_fetch_ms",
                        "metadata_map_ms",
                        "metadata_batch_cleanup_ms",
                        "ordered_merge_ms",
                    )
                )

                # Release each result explicitly and outside the next query's
                # timer.  These numbers diagnose the original carry-over bug.
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
                    "source_index": index,
                    "path": path,
                    "order": "two_first" if two_first else "three_first",
                    "rows": source_sample["rows"],
                    "output_utf8_bytes": bytes_returned,
                    "bytes_per_row": round(bytes_returned / source_sample["rows"], 3),
                    "two_total_ms": round(two_total_ms, 6),
                    "two_release_ms": round(two_release_ms, 6),
                    "three_total_ms": round(three_total_ms, 6),
                    "structure_ms": round(stages["structure_ms"], 6),
                    "metadata_fetch_ms": round(stages["metadata_fetch_ms"], 6),
                    "metadata_map_ms": round(stages["metadata_map_ms"], 6),
                    "metadata_batch_cleanup_ms": round(
                        stages["metadata_batch_cleanup_ms"], 6
                    ),
                    "ordered_merge_ms": round(stages["ordered_merge_ms"], 6),
                    "three_unattributed_ms": round(three_total_ms - attributed, 6),
                    "metadata_calls": stages["metadata_calls"],
                    "metadata_fetch_call_ms": stages["metadata_fetch_call_ms"],
                    "three_release_ms": round(three_release_ms, 6),
                    "gc_collect_ms": round(gc_collect_ms, 6),
                    "fingerprint": digest,
                }
                output["samples"].append(sample)

                done = repeat * len(indices) + position + 1
                total = args.repeats * len(indices)
                if done % 10 == 0 or done == total:
                    log(f"      ... {done}/{total}")
                    output_path.write_text(json.dumps(output, indent=2))

        output["aggregate"] = aggregate(output["samples"])
        output["status"] = "complete"
        output_path.write_text(json.dumps(output, indent=2))
        log(f"wrote {args.out}")

        share = output["aggregate"]["three_stage_share_of_total"]
        print("MongoDB three-collection subtree breakdown")
        print(
            f"  paths={len(indices)} repeats={args.repeats} "
            f"samples={len(output['samples'])}"
        )
        print(
            "  aggregate three-layout time share: "
            f"structure={share['structure_ms']:.1%}, "
            f"metadata fetch={share['metadata_fetch_ms']:.1%}, "
            f"metadata map={share['metadata_map_ms']:.1%}, "
            f"ordered merge={share['ordered_merge_ms']:.1%}, "
            f"other={share['three_unattributed_ms']:.1%}"
        )
    finally:
        if rebuilt and not args.keep_collections:
            for name in (VIEW, STRUCT, META, TEXT):
                database.drop_collection(name)
            log("cleaned up independently rebuilt layout collections")
        client.close()


if __name__ == "__main__":
    main()
