#!/usr/bin/env python3
"""Independently audit the final subtree-bucket evidence bundle.

The audit verifies producer bytes and immutable freeze evidence, recomputes all
statistics from raw observations, performs a fresh full 10M-row validation of
the source and every candidate layout, and rereads every selection, holdout,
and spectrum root through each applicable arm.  It does not collect replacement
latency measurements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from pymongo import MongoClient

from bench_subtree_bucket_selection import (
    CANDIDATE_SIZES,
    SOURCE_COLLECTION,
    TREE_ID,
    canonical_sha256,
    file_sha256,
    prior_input_ids,
    selection_freeze_payload,
    validate_collections,
    validation_identity,
    verify_phase,
    verify_selection_snapshot,
)
from bench_subtree_buckets import (
    baseline_query,
    bucket_query,
    fingerprint,
    root_path,
)


DEFAULT_RESULT = Path(
    "bench/db/runs/subtree_buckets_20260727/"
    "selection_holdout_v3_final.json"
)
DEFAULT_SPECTRUM = Path(
    "bench/db/runs/subtree_buckets_20260727/spectrum_unbiased_v3.json"
)
DEFAULT_OUTPUT = Path(
    "bench/db/runs/subtree_buckets_20260727/"
    "selection_holdout_v3.output_audit.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def verify_recorded_files(
    provenance: dict[str, Any],
    require_current: bool,
) -> None:
    for raw_path, expected in provenance["scripts"].items():
        path = Path(raw_path)
        source = provenance["script_sources"].get(raw_path)
        require(source is not None, f"missing embedded producer source {path}")
        require(
            hashlib.sha256(source.encode("utf-8")).hexdigest() == expected,
            f"embedded producer source hash differs: {path}",
        )
        if require_current:
            require(path.exists(), f"missing producer script {path}")
            require(
                file_sha256(path) == expected,
                f"producer script bytes changed: {path}",
            )
    for raw_path, expected in provenance["prior_results"].items():
        path = Path(raw_path)
        require(path.exists(), f"missing prior result {path}")
        require(
            file_sha256(path) == expected,
            f"prior result bytes changed: {path}",
        )


def verify_selection_static(
    result: dict[str, Any],
    result_path: Path,
) -> tuple[int, list[Path], dict[str, Any]]:
    require(result["run"]["status"] == "complete", "result is incomplete")
    require(
        result["protocol"]["candidate_sizes"] == list(CANDIDATE_SIZES),
        "candidate list changed",
    )
    verify_recorded_files(result["provenance"], require_current=False)
    verify_recorded_files(result["holdout_provenance"], require_current=True)
    require(
        canonical_sha256(selection_freeze_payload(result))
        == result["selection_payload_sha256"],
        "final result differs from frozen selection payload",
    )
    freeze_record = result["selection_freeze_artifact"]
    freeze_path = Path(freeze_record["path"])
    require(freeze_path.exists(), "selection freeze snapshot is missing")
    require(
        file_sha256(freeze_path) == freeze_record["sha256"],
        "selection freeze snapshot hash changed",
    )
    frozen = json.loads(freeze_path.read_text())
    require(
        canonical_sha256(selection_freeze_payload(frozen))
        == frozen["selection_payload_sha256"],
        "freeze snapshot payload hash is invalid",
    )
    require(
        selection_freeze_payload(frozen)
        == selection_freeze_payload(result),
        "final result selection fields differ from freeze snapshot",
    )
    require(
        set(result["phases"]) == {"selection", "holdout"},
        "result contains unexpected phases",
    )
    selected_size = int(result["selection"]["selected_size"])
    require(selected_size in CANDIDATE_SIZES, "winner is not a candidate")
    selection_summary = verify_phase(
        "selection",
        result["phases"]["selection"],
        result["input_partitions"]["selection"],
        CANDIDATE_SIZES,
        int(result["protocol"]["selection_repeats"]),
    )
    recomputed_winner = min(
        CANDIDATE_SIZES,
        key=lambda size: (
            selection_summary["arms"][str(size)]["total_ms"]["p95"],
            size,
        ),
    )
    require(
        recomputed_winner == selected_size,
        "raw selection observations choose a different winner",
    )
    verify_phase(
        "holdout",
        result["phases"]["holdout"],
        result["input_partitions"]["holdout"],
        (selected_size,),
        int(result["protocol"]["holdout_repeats"]),
    )
    partition_ids = {
        name: {str(item["node_id"]) for item in items}
        for name, items in result["input_partitions"].items()
    }
    require(
        len(partition_ids["selection"])
        == int(result["protocol"]["selection_inputs"]),
        "selection roots are not unique",
    )
    require(
        len(partition_ids["holdout"])
        == int(result["protocol"]["holdout_inputs"]),
        "holdout roots are not unique",
    )
    require(
        not partition_ids["selection"].intersection(partition_ids["holdout"]),
        "selection and holdout overlap",
    )
    prior_paths = [
        Path(path) for path in result["provenance"]["prior_results"]
    ]
    historic = prior_input_ids(prior_paths)
    require(
        not historic.intersection(
            partition_ids["selection"] | partition_ids["holdout"]
        ),
        "selection/holdout overlaps a prior result",
    )
    return selected_size, prior_paths, {
        "result": str(result_path),
        "result_sha256": file_sha256(result_path),
        "freeze_snapshot": str(freeze_path),
        "freeze_snapshot_sha256": freeze_record["sha256"],
        "producer_scripts": result["provenance"]["scripts"],
        "holdout_producer_scripts": result["holdout_provenance"]["scripts"],
        "summaries_recomputed": ["selection", "holdout"],
        "selection_rule_recomputed": selected_size,
        "prior_inputs_checked": len(historic),
        "partition_overlap": 0,
    }


def verify_spectrum_static(
    spectrum: dict[str, Any],
    spectrum_path: Path,
    result_path: Path,
    result: dict[str, Any],
    selected_size: int,
) -> dict[str, Any]:
    require(spectrum["run"]["status"] == "complete", "spectrum is incomplete")
    verify_recorded_files(spectrum["provenance"], require_current=True)
    script_names = {
        Path(path).name for path in spectrum["provenance"]["scripts"]
    }
    require(
        "bench_subtree_bucket_spectrum.py" in script_names,
        "spectrum entrypoint is absent from provenance",
    )
    require(
        spectrum["protocol"]["selection_result"] == str(result_path),
        "spectrum points to a different selection result",
    )
    require(
        spectrum["protocol"]["selection_result_sha256"]
        == file_sha256(result_path),
        "spectrum selection-result hash changed",
    )
    require(
        int(spectrum["protocol"]["selected_size"]) == selected_size,
        "spectrum uses a different winner",
    )
    inputs = spectrum["input_partition"]
    require(len(inputs) == 250, "spectrum input count is not 250")
    ids = [str(item["node_id"]) for item in inputs]
    require(len(ids) == len(set(ids)), "spectrum roots are not unique")
    expected_depths = {str(depth): 50 for depth in range(4, 9)}
    require(
        spectrum["depth_counts"] == expected_depths,
        "spectrum depth counts changed",
    )
    used_ids = {
        str(item["node_id"])
        for partition in result["input_partitions"].values()
        for item in partition
    }
    require(not used_ids.intersection(ids), "spectrum overlaps selection/holdout")
    prior_paths = [
        Path(path) for path in spectrum["provenance"]["prior_results"]
    ]
    require(
        not prior_input_ids(prior_paths).intersection(ids),
        "spectrum overlaps an earlier raw result",
    )
    verify_phase(
        "spectrum",
        spectrum["phases"]["spectrum"],
        inputs,
        (selected_size,),
        int(spectrum["phases"]["spectrum"]["repeats"]),
    )
    return {
        "spectrum": str(spectrum_path),
        "spectrum_sha256": file_sha256(spectrum_path),
        "producer_scripts": spectrum["provenance"]["scripts"],
        "summary_recomputed": True,
        "roots": len(inputs),
        "depth_counts": expected_depths,
        "overlap_with_selection_holdout": 0,
    }


def verify_live_phase(
    nodes: Any,
    collections: dict[int, Any],
    phase_name: str,
    samples: list[dict[str, Any]],
    sizes: Sequence[int],
    aggregate: Any,
) -> dict[str, Any]:
    arm_checks = {"baseline": 0, **{str(size): 0 for size in sizes}}
    materialized_rows = 0
    for index, sample in enumerate(samples, start=1):
        node_id = str(sample["node_id"])
        live_path = root_path(nodes, TREE_ID, node_id)
        require(live_path == sample["path"], f"live path changed for {node_id}")
        expected, _ = baseline_query(nodes, TREE_ID, node_id)
        expected_fingerprint = fingerprint(expected)
        require(
            len(expected) == int(sample["rows"]),
            f"live row count changed for {phase_name}/{node_id}",
        )
        require(
            expected_fingerprint == sample["fingerprint"],
            f"live fingerprint changed for {phase_name}/{node_id}",
        )
        materialized_rows += len(expected)
        arm_checks["baseline"] += 1
        aggregate.update(
            f"{phase_name}\0{node_id}\0baseline\0"
            f"{expected_fingerprint}\n".encode()
        )
        for size in sizes:
            actual, _ = bucket_query(
                nodes,
                collections[size],
                TREE_ID,
                node_id,
            )
            require(
                actual == expected,
                f"ordered output mismatch for {phase_name}/{node_id}/{size}",
            )
            actual_fingerprint = fingerprint(actual)
            arm_checks[str(size)] += 1
            aggregate.update(
                f"{phase_name}\0{node_id}\0{size}\0"
                f"{actual_fingerprint}\n".encode()
            )
        if index % 25 == 0:
            print(
                f"  {phase_name}: {index}/{len(samples)} roots audited",
                flush=True,
            )
    return {
        "roots": len(samples),
        "materialized_rows_across_roots": materialized_rows,
        "arm_checks": arm_checks,
        "path_checks": len(samples),
        "stored_fingerprint_checks": len(samples),
        "ordered_output_mismatches": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument("--mongo-db", default="bench")
    parser.add_argument("--result", type=Path, default=DEFAULT_RESULT)
    parser.add_argument("--spectrum", type=Path, default=DEFAULT_SPECTRUM)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.out.exists():
        raise RuntimeError(f"refusing to overwrite {args.out}")

    started_at = utc_now()
    result = json.loads(args.result.read_text())
    spectrum = json.loads(args.spectrum.read_text())
    selected_size, prior_paths, selection_static = verify_selection_static(
        result,
        args.result,
    )
    spectrum_static = verify_spectrum_static(
        spectrum,
        args.spectrum,
        args.result,
        result,
        selected_size,
    )

    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    try:
        server_version = client.server_info()["version"]
        database = client[args.mongo_db]
        validation = validate_collections(database)
        live_identity = validation_identity(validation)
        require(
            live_identity == result["validation_identity"],
            "live full validation differs from selection identity",
        )
        require(
            live_identity == spectrum["validation_identity"],
            "live full validation differs from spectrum identity",
        )
        freeze_path = Path(result["selection_freeze_artifact"]["path"])
        frozen = json.loads(freeze_path.read_text())
        verify_selection_snapshot(
            frozen,
            database[SOURCE_COLLECTION],
            prior_paths,
            int(result["protocol"]["selection_repeats"]),
        )
        nodes = database[SOURCE_COLLECTION]
        collections = {
            int(size): database[metadata["collection"]]
            for size, metadata in result["collections"].items()
        }
        aggregate = hashlib.sha256()
        live = {
            "selection": verify_live_phase(
                nodes,
                collections,
                "selection",
                result["phases"]["selection"]["samples"],
                CANDIDATE_SIZES,
                aggregate,
            ),
            "holdout": verify_live_phase(
                nodes,
                collections,
                "holdout",
                result["phases"]["holdout"]["samples"],
                (selected_size,),
                aggregate,
            ),
            "spectrum": verify_live_phase(
                nodes,
                collections,
                "spectrum",
                spectrum["phases"]["spectrum"]["samples"],
                (selected_size,),
                aggregate,
            ),
        }
        live["aggregate_sha256"] = aggregate.hexdigest()
    finally:
        client.close()

    output = {
        "status": "complete",
        "purpose": (
            "static provenance/statistics audit plus fresh full-layout and "
            "untimed live-output validation; not replacement latency evidence"
        ),
        "started_at": started_at,
        "completed_at": utc_now(),
        "static": {
            "selection_holdout": selection_static,
            "spectrum": spectrum_static,
        },
        "full_validation_identity_sha256": canonical_sha256(live_identity),
        "live": live,
        "environment": {
            "mongodb": server_version,
            "git_revision": git_revision(),
            "audit_script": str(Path(__file__).resolve()),
            "audit_script_sha256": file_sha256(Path(__file__).resolve()),
            "audit_script_source": Path(__file__).read_text(),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2))
    print(json.dumps(output["live"], indent=2))
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
