#!/usr/bin/env python3
"""Select and evaluate a bucket size without reusing the reported workload.

Protocol (fixed before reading new timings):

1. Candidate sizes are 128, 256, 512, 2048, and 8192 rows.
2. The original 200 report roots and every earlier spectrum root are excluded.
3. Disjoint 75-root selection and 100-root holdout samples are drawn from the
   remaining shallow roots with seed 20260731.
4. All five candidates run on the selection split in one randomized,
   cyclically interleaved experiment.  The candidate with the lowest absolute
   P95 latency is selected; speedup ratio is not the selection criterion.
5. Only the selected candidate runs on the untouched holdout split.

Every arm fully materializes ordered output.  Exact row count and a
type-sensitive BSON/SHA-256 fingerprint are checked outside the timed region.
The benchmark is read-only: it reuses fully validated bucket collections.  A
separate immutable selection-complete snapshot is required by --resume.
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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from pymongo import MongoClient

from bench_subtree_buckets import (
    FIRST_PATH_INDEX,
    LAST_PATH_INDEX,
    NODE_INDEX,
    SEQUENCE_INDEX,
    SOURCE_COLLECTION,
    SOURCE_INDEX_SPECS,
    baseline_query,
    bucket_query,
    coll_stats,
    digest_source,
    fingerprint,
    root_path,
    validate_buckets,
)


CANDIDATE_SIZES = (128, 256, 512, 2048, 8192)
DEFAULT_COLLECTIONS = {
    size: f"subtree_buckets_v2_{size}" for size in CANDIDATE_SIZES
}
TREE_ID = "base"
SELECTION_SEED = 20260731
SELECTION_INPUTS = 75
HOLDOUT_INPUTS = 100
SPECTRUM_INPUTS_PER_DEPTH = 50
DEFAULT_PRIOR_RESULTS = (
    "bench/db/runs/subtree_buckets_20260724/bucket_8192_10m.json",
    "bench/db/runs/subtree_buckets_20260724/bucket_8192_10m_r2.json",
    "bench/db/runs/subtree_buckets_20260724/final_512_10m.json",
    "bench/db/runs/subtree_buckets_20260724/screen_128_10m.json",
    "bench/db/runs/subtree_buckets_20260724/screen_256_10m.json",
    "bench/db/runs/subtree_buckets_20260724/screen_512_10m.json",
    "bench/db/runs/subtree_buckets_20260724/screen_2048_10m.json",
    "bench/db/runs/subtree_buckets_20260727/selection_holdout_v2.json",
    "bench/db/runs/subtree_buckets_20260727/spectrum_unbiased_v2.json",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_output(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def provenance(
    paths: Sequence[Path],
    producer_scripts: Sequence[Path] | None = None,
) -> dict[str, Any]:
    selection_script = Path(__file__).resolve()
    bucket_script = selection_script.with_name("bench_subtree_buckets.py")
    scripts = (
        [selection_script, bucket_script]
        if producer_scripts is None
        else [path.resolve() for path in producer_scripts]
    )
    return {
        "argv": list(sys.argv),
        "git_revision": git_output("rev-parse", "HEAD"),
        "git_status_sha256": hashlib.sha256(
            git_output("status", "--porcelain=v1").encode("utf-8")
        ).hexdigest(),
        "scripts": {
            str(script): file_sha256(script) for script in scripts
        },
        "script_sources": {
            str(script): script.read_text() for script in scripts
        },
        "prior_results": {
            str(path): file_sha256(path) for path in paths
        },
    }


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: Sequence[float], pct: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return round(float(ordered[index]), 6)


def distribution(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 6) if values else 0.0,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "min": round(min(values), 6) if values else 0.0,
        "max": round(max(values), 6) if values else 0.0,
    }


def prior_input_ids(paths: Sequence[Path]) -> set[str]:
    result: set[str] = set()
    for path in paths:
        if not path.exists():
            raise RuntimeError(f"required prior result is missing: {path}")
        document = json.loads(path.read_text())
        for benchmark in document.get("benchmarks", {}).values():
            for sample in benchmark.get("samples", []):
                result.add(str(sample["node_id"]))
        for phase in document.get("phases", {}).values():
            for sample in phase.get("samples", []):
                result.add(str(sample["node_id"]))
        for partition in document.get("input_partitions", {}).values():
            for sample in partition:
                result.add(str(sample["node_id"]))
    return result


def load_shallow_population(nodes: Any, excluded: set[str]) -> list[dict[str, Any]]:
    cursor = (
        nodes.find(
            {
                "tree_id": TREE_ID,
                "node_id": {"$lte": "000778"},
            },
            {"_id": 0, "node_id": 1, "path": 1, "depth": 1},
        )
        .sort("node_id", 1)
        .hint(NODE_INDEX)
    )
    population = [
        {
            "node_id": str(row["node_id"]),
            "path": str(row["path"]),
            "depth": int(row["depth"]),
        }
        for row in cursor
        if int(row["depth"]) <= 3 and str(row["node_id"]) not in excluded
    ]
    if len(population) < SELECTION_INPUTS + HOLDOUT_INPUTS:
        raise RuntimeError(
            f"only {len(population)} unused shallow roots; need "
            f"{SELECTION_INPUTS + HOLDOUT_INPUTS}"
        )
    return population


def load_spectrum_population(
    nodes: Any,
    excluded: set[str],
    rng: random.Random,
) -> list[dict[str, Any]]:
    depth_ranges = {
        4: (779, 7_806),
        5: (7_807, 78_120),
        6: (78_121, 781_762),
        7: (781_763, 7_820_281),
        8: (7_820_282, 9_999_999),
    }
    result: list[dict[str, Any]] = []
    for depth, (low, high) in depth_ranges.items():
        candidates: list[str] = []
        while len(candidates) < SPECTRUM_INPUTS_PER_DEPTH:
            node_id = f"{rng.randint(low, high):06d}"
            if node_id not in excluded:
                candidates.append(node_id)
                excluded.add(node_id)
        rows = list(
            nodes.find(
                {"tree_id": TREE_ID, "node_id": {"$in": candidates}},
                {"_id": 0, "node_id": 1, "path": 1, "depth": 1},
            ).hint(NODE_INDEX)
        )
        rows = [row for row in rows if int(row["depth"]) == depth]
        if len(rows) < SPECTRUM_INPUTS_PER_DEPTH:
            raise RuntimeError(
                f"depth {depth} produced only {len(rows)} usable roots"
            )
        by_id = {str(row["node_id"]): row for row in rows}
        result.extend(
            {
                "node_id": node_id,
                "path": str(by_id[node_id]["path"]),
                "depth": int(by_id[node_id]["depth"]),
            }
            for node_id in candidates
        )
    return result


def validate_collections(database: Any) -> dict[str, Any]:
    output: dict[int, dict[str, Any]] = {}
    expected_indexes = {
        FIRST_PATH_INDEX: {
            "key": [("tree_id", 1), ("first_path", 1), ("seq", 1)],
            "partialFilterExpression": {"kind": "bucket"},
            "unique": False,
        },
        LAST_PATH_INDEX: {
            "key": [("tree_id", 1), ("last_path", 1), ("seq", 1)],
            "partialFilterExpression": {"kind": "bucket"},
            "unique": False,
        },
        SEQUENCE_INDEX: {
            "key": [("tree_id", 1), ("seq", 1)],
            "partialFilterExpression": {"kind": "bucket"},
            "unique": True,
        },
    }
    for size, name in DEFAULT_COLLECTIONS.items():
        collection = database[name]
        options = collection.options()
        collation = options.get("collation")
        if collation is not None and collation.get("locale") != "simple":
            raise RuntimeError(f"{name} must use simple collation")
        manifest = collection.find_one({"_id": f"{TREE_ID}:manifest"})
        if manifest is None or manifest.get("status") != "complete":
            raise RuntimeError(f"{name} has no complete manifest")
        if manifest.get("rows_per_bucket") != size:
            raise RuntimeError(f"{name} rows_per_bucket mismatch")
        if manifest.get("tree_id") != TREE_ID:
            raise RuntimeError(f"{name} tree_id mismatch")
        if manifest.get("source_count") != 10_000_000:
            raise RuntimeError(f"{name} source_count mismatch")
        if manifest.get("rows") != 10_000_000:
            raise RuntimeError(f"{name} row count mismatch")
        expected_buckets = (10_000_000 + size - 1) // size
        if manifest.get("buckets") != expected_buckets:
            raise RuntimeError(f"{name} manifest bucket count mismatch")
        if collection.count_documents({}) != expected_buckets + 1:
            raise RuntimeError(f"{name} contains unexpected documents")
        indexes = collection.index_information()
        for index_name, expected in expected_indexes.items():
            actual = indexes.get(index_name)
            if actual is None:
                raise RuntimeError(f"{name} lacks index {index_name}")
            if actual.get("key") != expected["key"]:
                raise RuntimeError(f"{name} index {index_name} key mismatch")
            if (
                actual.get("partialFilterExpression")
                != expected["partialFilterExpression"]
            ):
                raise RuntimeError(
                    f"{name} index {index_name} partial filter mismatch"
                )
            if bool(actual.get("unique", False)) != expected["unique"]:
                raise RuntimeError(
                    f"{name} index {index_name} uniqueness mismatch"
                )
            if actual.get("collation") is not None:
                raise RuntimeError(
                    f"{name} index {index_name} must use simple collation"
                )
        if collection.count_documents(
            {"kind": "bucket", "tree_id": {"$ne": TREE_ID}}
        ):
            raise RuntimeError(f"{name} contains foreign-tree buckets")
        output[size] = {
            "collection": name,
            "collection_options": options,
            "manifest": {
                key: manifest.get(key)
                for key in (
                    "tree_id",
                    "source_count",
                    "rows_per_bucket",
                    "rows",
                    "buckets",
                    "source_digest",
                    "max_bson_bytes",
                    "build_seconds",
                )
            },
            "indexes": {
                index_name: {
                    "key": indexes[index_name]["key"],
                    "partialFilterExpression": indexes[index_name].get(
                        "partialFilterExpression"
                    ),
                    "unique": bool(indexes[index_name].get("unique", False)),
                    "collation": indexes[index_name].get("collation"),
                }
                for index_name in expected_indexes
            },
            "storage": coll_stats(database, name),
        }
    digests = {
        value["manifest"]["source_digest"] for value in output.values()
    }
    if len(digests) != 1:
        raise RuntimeError(f"candidate source digests differ: {digests}")
    source_scan = digest_source(database, TREE_ID, 0.0)
    expected_digest = next(iter(digests))
    if source_scan["digest"] != expected_digest:
        raise RuntimeError("live source digest differs from candidate manifests")
    if source_scan["rows"] != 10_000_000:
        raise RuntimeError("live source row count is not 10,000,000")
    if set(source_scan["source_contract"]["indexes"]) != set(
        SOURCE_INDEX_SPECS
    ):
        raise RuntimeError("source query-index identity is incomplete")
    for size, metadata in output.items():
        validation = validate_buckets(
            database,
            metadata["collection"],
            TREE_ID,
            expected_digest,
            0.0,
            size,
        )
        if validation["rows"] != metadata["manifest"]["rows"]:
            raise RuntimeError(f"bucket {size} validation row-count mismatch")
        if validation["buckets"] != metadata["manifest"]["buckets"]:
            raise RuntimeError(f"bucket {size} validation bucket-count mismatch")
        if validation["max_bson_bytes"] != metadata["manifest"]["max_bson_bytes"]:
            raise RuntimeError(f"bucket {size} max-BSON mismatch")
        metadata["validation"] = validation
    return {
        "source_rescan": source_scan,
        "candidates": output,
    }


def validation_identity(bundle: dict[str, Any]) -> dict[str, Any]:
    source = bundle["source_rescan"]
    identity = {
        "source": {
            key: source[key]
            for key in (
                "tree_id",
                "rows",
                "digest",
                "first_key",
                "last_key",
                "path_grammar",
                "source_contract",
            )
        },
        "candidates": {
            str(size): {
                "collection": metadata["collection"],
                "collection_options": metadata["collection_options"],
                "manifest": metadata["manifest"],
                "indexes": metadata["indexes"],
                "validation": {
                    key: metadata["validation"][key]
                    for key in (
                        "rows",
                        "buckets",
                        "digest",
                        "first_key",
                        "last_key",
                        "min_bson_bytes",
                        "max_bson_bytes",
                        "rows_per_bucket",
                        "short_bucket_seqs",
                    )
                },
            }
            for size, metadata in bundle["candidates"].items()
        },
    }
    return json.loads(json.dumps(identity))


def prepare_samples(
    nodes: Any,
    collections: dict[int, Any],
    inputs: list[dict[str, Any]],
    arm_sizes: Sequence[int],
) -> list[dict[str, Any]]:
    arms = ["baseline", *(str(size) for size in arm_sizes)]
    samples = [
        {
            **item,
            "rows": None,
            "fingerprint": None,
            "times": {arm: [] for arm in arms},
        }
        for item in inputs
    ]
    for index, sample in enumerate(samples):
        live_path = root_path(nodes, TREE_ID, sample["node_id"])
        if live_path is None:
            raise RuntimeError(f"sample root is missing: {sample['node_id']}")
        if live_path != sample["path"]:
            raise RuntimeError(
                f"sample path changed for {sample['node_id']}: "
                f"{live_path!r} != {sample['path']!r}"
            )
        expected_rows, _ = baseline_query(nodes, TREE_ID, sample["node_id"])
        sample["rows"] = len(expected_rows)
        sample["fingerprint"] = fingerprint(expected_rows)
        for size in arm_sizes:
            actual, _ = bucket_query(
                nodes,
                collections[size],
                TREE_ID,
                sample["node_id"],
            )
            if actual != expected_rows:
                raise RuntimeError(
                    f"warm exact-output mismatch node={sample['node_id']} "
                    f"bucket={size}"
                )
        if (index + 1) % 50 == 0:
            print(f"  exact-output warmup {index + 1}/{len(samples)}", flush=True)
    return samples


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    arms = list(samples[0]["times"]) if samples else []
    metrics = (
        "total_ms",
        "root_ms",
        "directory_ms",
        "fetch_ms",
        "normalize_filter_ms",
        "bucket_docs",
        "rows_read",
        "overfetch_rows",
    )
    per_input: dict[str, list[dict[str, float]]] = {}
    output: dict[str, Any] = {
        "inputs": len(samples),
        "avg_rows": round(
            statistics.mean(float(sample["rows"]) for sample in samples),
            3,
        ) if samples else 0.0,
        "aggregation": "per-input median across repeats, then workload percentile",
        "arms": {},
        "paired_vs_baseline": {},
    }
    for arm in arms:
        per_input[arm] = [
            {
                metric: statistics.median(
                    observation[metric]
                    for observation in sample["times"][arm]
                )
                for metric in metrics
            }
            for sample in samples
        ]
        output["arms"][arm] = {
            metric: distribution(
                [item[metric] for item in per_input[arm]]
            )
            for metric in metrics
        }
    baseline = per_input["baseline"]
    for arm in arms:
        if arm == "baseline":
            continue
        candidate = per_input[arm]
        ratios = [
            left["total_ms"] / right["total_ms"]
            for left, right in zip(baseline, candidate)
            if right["total_ms"] > 0
        ]
        individual_pairs = [
            (
                left["total_ms"],
                right["total_ms"],
            )
            for sample in samples
            for left, right in zip(
                sample["times"]["baseline"],
                sample["times"][arm],
            )
        ]
        output["paired_vs_baseline"][arm] = {
            "per_input_speedup": distribution(ratios),
            "per_input_faster": sum(value > 1.0 for value in ratios),
            "individual_pairs": len(individual_pairs),
            "individual_pairs_faster": sum(
                left > right for left, right in individual_pairs
            ),
            "marginal_speedup_p50": round(
                output["arms"]["baseline"]["total_ms"]["p50"]
                / output["arms"][arm]["total_ms"]["p50"],
                6,
            ),
            "marginal_speedup_p95": round(
                output["arms"]["baseline"]["total_ms"]["p95"]
                / output["arms"][arm]["total_ms"]["p95"],
                6,
            ),
        }
    return output


def generate_partitions(
    nodes: Any,
    prior_paths: Sequence[Path],
) -> dict[str, list[dict[str, Any]]]:
    excluded = prior_input_ids(prior_paths)
    population = load_shallow_population(nodes, excluded)
    random.Random(SELECTION_SEED).shuffle(population)
    return {
        "selection": population[:SELECTION_INPUTS],
        "holdout": population[
            SELECTION_INPUTS:SELECTION_INPUTS + HOLDOUT_INPUTS
        ],
    }


def verify_phase(
    phase_name: str,
    phase: dict[str, Any],
    partition: list[dict[str, Any]],
    arm_sizes: Sequence[int],
    repeats: int,
) -> dict[str, Any]:
    if phase.get("status") != "complete":
        raise RuntimeError(f"{phase_name} phase is not complete")
    if phase.get("repeats") != repeats:
        raise RuntimeError(f"{phase_name} repeat count changed")
    if phase.get("arm_sizes") != list(arm_sizes):
        raise RuntimeError(f"{phase_name} arm list changed")
    samples = phase.get("samples", [])
    if [item["node_id"] for item in samples] != [
        item["node_id"] for item in partition
    ]:
        raise RuntimeError(f"{phase_name} samples differ from frozen partition")
    expected_arms = ["baseline", *(str(size) for size in arm_sizes)]
    for sample in samples:
        if list(sample.get("times", {})) != expected_arms:
            raise RuntimeError(
                f"{phase_name}/{sample.get('node_id')} arm list changed"
            )
        for arm in expected_arms:
            observations = sample["times"][arm]
            if len(observations) != repeats:
                raise RuntimeError(
                    f"{phase_name}/{sample['node_id']}/{arm} repeats changed"
                )
            if any(
                observation.get("output_fingerprint")
                != sample.get("fingerprint")
                for observation in observations
            ):
                raise RuntimeError(
                    f"{phase_name}/{sample['node_id']}/{arm} "
                    "stored output fingerprint mismatch"
                )
    recomputed = summarize(samples)
    if recomputed != phase.get("summary"):
        raise RuntimeError(f"{phase_name} summary differs from raw observations")
    return recomputed


def selection_freeze_payload(output: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": output["protocol"],
        "validation_identity": output["validation_identity"],
        "provenance": {
            key: output["provenance"][key]
            for key in (
                "git_revision",
                "scripts",
                "script_sources",
                "prior_results",
            )
        },
        "input_partitions": output["input_partitions"],
        "selection_phase": output["phases"]["selection"],
        "selection": output["selection"],
    }


def verify_selection_snapshot(
    output: dict[str, Any],
    nodes: Any,
    prior_paths: Sequence[Path],
    selection_repeats: int,
) -> int:
    if output["run"].get("status") != "selection-complete":
        raise RuntimeError("snapshot status is not selection-complete")
    protocol = output["protocol"]
    if protocol["candidate_sizes"] != list(CANDIDATE_SIZES):
        raise RuntimeError("snapshot candidate sizes changed")
    if protocol["selection_seed"] != SELECTION_SEED:
        raise RuntimeError("snapshot selection seed changed")
    if protocol["selection_inputs"] != SELECTION_INPUTS:
        raise RuntimeError("snapshot selection size changed")
    if protocol["holdout_inputs"] != HOLDOUT_INPUTS:
        raise RuntimeError("snapshot holdout size changed")
    regenerated = generate_partitions(nodes, prior_paths)
    if output["input_partitions"] != regenerated:
        raise RuntimeError("frozen partitions do not reproduce from seed/source")
    all_ids = [
        item["node_id"]
        for partition in regenerated.values()
        for item in partition
    ]
    if len(all_ids) != len(set(all_ids)):
        raise RuntimeError("selection and holdout partitions overlap")
    historic = prior_input_ids(prior_paths)
    if historic.intersection(all_ids):
        raise RuntimeError("frozen partitions overlap prior inputs")
    selection_summary = verify_phase(
        "selection",
        output["phases"]["selection"],
        regenerated["selection"],
        CANDIDATE_SIZES,
        selection_repeats,
    )
    selected_size = min(
        CANDIDATE_SIZES,
        key=lambda size: (
            selection_summary["arms"][str(size)]["total_ms"]["p95"],
            size,
        ),
    )
    expected_p95 = {
        str(size): selection_summary["arms"][str(size)]["total_ms"]["p95"]
        for size in CANDIDATE_SIZES
    }
    if output["selection"]["selected_size"] != selected_size:
        raise RuntimeError("frozen winner differs from recomputed winner")
    if output["selection"]["candidate_p95_ms"] != expected_p95:
        raise RuntimeError("frozen candidate P95 values changed")
    expected_freeze_sha = canonical_sha256(selection_freeze_payload(output))
    if output.get("selection_payload_sha256") != expected_freeze_sha:
        raise RuntimeError("selection freeze payload hash mismatch")
    if set(output["phases"]) != {"selection"}:
        raise RuntimeError("holdout data exists in selection snapshot")
    return selected_size


def run_phase(
    nodes: Any,
    collections: dict[int, Any],
    inputs: list[dict[str, Any]],
    arm_sizes: Sequence[int],
    repeats: int,
    seed: int,
    phase: str,
    output: dict[str, Any],
    out_path: Path,
) -> dict[str, Any]:
    samples = prepare_samples(nodes, collections, inputs, arm_sizes)
    arms = ("baseline", *(str(size) for size in arm_sizes))
    for repeat in range(repeats):
        order = list(range(len(samples)))
        random.Random(seed + repeat).shuffle(order)
        for position, sample_index in enumerate(order):
            sample = samples[sample_index]
            rotation = (repeat + position) % len(arms)
            arm_order = arms[rotation:] + arms[:rotation]
            for arm in arm_order:
                gc.disable()
                try:
                    if arm == "baseline":
                        rows, metrics = baseline_query(
                            nodes,
                            TREE_ID,
                            sample["node_id"],
                        )
                    else:
                        size = int(arm)
                        rows, metrics = bucket_query(
                            nodes,
                            collections[size],
                            TREE_ID,
                            sample["node_id"],
                        )
                finally:
                    gc.enable()
                if len(rows) != sample["rows"]:
                    raise RuntimeError(
                        f"{phase} row mismatch node={sample['node_id']} arm={arm}"
                    )
                actual_fingerprint = fingerprint(rows)
                if actual_fingerprint != sample["fingerprint"]:
                    raise RuntimeError(
                        f"{phase} fingerprint mismatch "
                        f"node={sample['node_id']} arm={arm}"
                    )
                observation = {
                    key: round(float(value), 6)
                    for key, value in metrics.items()
                }
                observation["output_fingerprint"] = actual_fingerprint
                sample["times"][arm].append(observation)
            if (position + 1) % 50 == 0:
                print(
                    f"  {phase} repeat {repeat + 1}/{repeats}: "
                    f"{position + 1}/{len(samples)}",
                    flush=True,
                )
        result = {
            "status": "running",
            "repeats": repeats,
            "arm_sizes": list(arm_sizes),
            "samples": samples,
            "summary": summarize(samples),
        }
        output["phases"][phase] = result
        out_path.write_text(json.dumps(output, indent=2))
    result["status"] = "complete"
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument("--mongo-db", default="bench")
    parser.add_argument(
        "--prior-result",
        action="append",
        default=list(DEFAULT_PRIOR_RESULTS),
    )
    parser.add_argument(
        "--out",
        default=(
            "bench/db/runs/subtree_buckets_20260727/"
            "selection_holdout_v3.json"
        ),
    )
    parser.add_argument(
        "--freeze-out",
        default=(
            "bench/db/runs/subtree_buckets_20260727/"
            "selection_holdout_v3.selection_freeze.json"
        ),
    )
    parser.add_argument("--selection-repeats", type=int, default=5)
    parser.add_argument("--holdout-repeats", type=int, default=7)
    parser.add_argument(
        "--stop-after-selection",
        action="store_true",
        help="freeze the selected size and exit before touching holdout inputs",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="run holdout from the immutable selection-complete snapshot",
    )
    args = parser.parse_args()
    if args.resume and args.stop_after_selection:
        parser.error("--resume and --stop-after-selection are mutually exclusive")
    if not args.resume and not args.stop_after_selection:
        parser.error("a fresh run requires --stop-after-selection")
    if args.selection_repeats < 2 or args.holdout_repeats < 2:
        parser.error("repeat counts must be at least 2")
    prior_paths = [Path(path) for path in args.prior_result]

    out_path = Path(args.out)
    freeze_path = Path(args.freeze_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    freeze_path.parent.mkdir(parents=True, exist_ok=True)
    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    try:
        database = client[args.mongo_db]
        nodes = database[SOURCE_COLLECTION]
        validation = validate_collections(database)
        candidate_metadata = validation["candidates"]
        collections = {
            size: database[metadata["collection"]]
            for size, metadata in candidate_metadata.items()
        }

        if args.resume:
            if not out_path.exists() or not freeze_path.exists():
                raise RuntimeError("resume requires output and freeze snapshot")
            output = json.loads(out_path.read_text())
            frozen = json.loads(freeze_path.read_text())
            if output != frozen:
                raise RuntimeError(
                    "selection output differs from immutable freeze snapshot"
                )
            if output["protocol"]["freeze_snapshot"] != str(freeze_path):
                raise RuntimeError("freeze snapshot path changed")
            if output["protocol"]["selection_repeats"] != args.selection_repeats:
                raise RuntimeError("selection repeat argument changed")
            if output["protocol"]["holdout_repeats"] != args.holdout_repeats:
                raise RuntimeError("holdout repeat argument changed")
            if validation_identity(validation) != output["validation_identity"]:
                raise RuntimeError(
                    "live source/collection/index identity changed after selection"
                )
            current_provenance = provenance(prior_paths)
            for key in (
                "git_revision",
                "scripts",
                "script_sources",
                "prior_results",
            ):
                if current_provenance[key] != output["provenance"][key]:
                    raise RuntimeError(
                        f"resume provenance mismatch for {key}"
                    )
            selected_size = verify_selection_snapshot(
                output,
                nodes,
                prior_paths,
                args.selection_repeats,
            )
            holdout_inputs = output["input_partitions"]["holdout"]
            output["selection_freeze_artifact"] = {
                "path": str(freeze_path),
                "sha256": file_sha256(freeze_path),
            }
            output["run"]["resumed_at"] = utc_now()
            output["run"]["status"] = "holdout-running"
            output["resume_provenance"] = current_provenance
            out_path.write_text(json.dumps(output, indent=2))

            print("holdout phase", flush=True)
            output["phases"]["holdout"] = run_phase(
                nodes,
                collections,
                holdout_inputs,
                (selected_size,),
                args.holdout_repeats,
                SELECTION_SEED + 20_000,
                "holdout",
                output,
                out_path,
            )
            verify_phase(
                "holdout",
                output["phases"]["holdout"],
                holdout_inputs,
                (selected_size,),
                args.holdout_repeats,
            )
            output["run"]["status"] = "complete"
            output["run"]["completed_at"] = utc_now()
            output["environment"]["loadavg_after"] = list(os.getloadavg())
            out_path.write_text(json.dumps(output, indent=2))
            print(json.dumps({
                "selection": output["selection"],
                "holdout": output["phases"]["holdout"]["summary"],
            }, indent=2))
            print(f"wrote {out_path}", flush=True)
            return

        if out_path.exists() or freeze_path.exists():
            raise RuntimeError(
                "refusing to overwrite selection output or freeze snapshot"
            )
        partitions = generate_partitions(nodes, prior_paths)
        output = {
            "run": {
                "status": "running",
                "started_at": utc_now(),
                "selection_seed": SELECTION_SEED,
            },
            "protocol": {
                "candidate_sizes": list(CANDIDATE_SIZES),
                "selection_seed": SELECTION_SEED,
                "prior_inputs_excluded": len(prior_input_ids(prior_paths)),
                "selection_inputs": SELECTION_INPUTS,
                "holdout_inputs": HOLDOUT_INPUTS,
                "selection_repeats": args.selection_repeats,
                "holdout_repeats": args.holdout_repeats,
                "freeze_snapshot": str(freeze_path),
                "selection_rule": (
                    "lowest absolute selection-split P95 of per-input median "
                    "client-wall latency"
                ),
                "holdout_rule": (
                    "winner and partitions are frozen in a separate exact "
                    "snapshot before any holdout subtree query"
                ),
                "benchmark_contract": (
                    "single sealed synthetic tree with simple collation and "
                    "slash-delimited numeric paths; valid roots; root excluded; "
                    "unlimited descendants; ordered node_id/title/summary output"
                ),
            },
            "environment": {
                "mongodb": client.server_info()["version"],
                "pymongo": __import__("pymongo").version,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "loadavg_before": list(os.getloadavg()),
            },
            "validation": validation,
            "validation_identity": validation_identity(validation),
            "collections": candidate_metadata,
            "provenance": provenance(prior_paths),
            "input_partitions": partitions,
            "phases": {},
        }
        out_path.write_text(json.dumps(output, indent=2))
        print("selection phase", flush=True)
        output["phases"]["selection"] = run_phase(
            nodes,
            collections,
            partitions["selection"],
            CANDIDATE_SIZES,
            args.selection_repeats,
            SELECTION_SEED + 10_000,
            "selection",
            output,
            out_path,
        )
        selection_summary = output["phases"]["selection"]["summary"]
        selected_size = min(
            CANDIDATE_SIZES,
            key=lambda size: (
                selection_summary["arms"][str(size)]["total_ms"]["p95"],
                size,
            ),
        )
        output["selection"] = {
            "selected_size": selected_size,
            "criterion": output["protocol"]["selection_rule"],
            "candidate_p95_ms": {
                str(size): (
                    selection_summary["arms"][str(size)]["total_ms"]["p95"]
                )
                for size in CANDIDATE_SIZES
            },
            "frozen_at": utc_now(),
        }
        output["run"]["status"] = "selection-complete"
        output["selection_payload_sha256"] = canonical_sha256(
            selection_freeze_payload(output)
        )
        verify_selection_snapshot(
            output,
            nodes,
            prior_paths,
            args.selection_repeats,
        )
        serialized = json.dumps(output, indent=2)
        out_path.write_text(serialized)
        freeze_path.write_text(serialized)
        print(f"selected {selected_size} rows/bucket", flush=True)
        print(
            f"selection frozen in {out_path} and {freeze_path}",
            flush=True,
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
