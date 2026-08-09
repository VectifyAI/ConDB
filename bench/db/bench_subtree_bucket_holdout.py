#!/usr/bin/env python3
"""Consume an immutable selection snapshot and run only its frozen holdout.

This entrypoint exists to keep the phase boundary explicit.  It never samples,
selects, or rewrites the winner.  The output is created in holdout-running
state before the first holdout subtree query and cannot be resumed or
overwritten after interruption.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient

from bench_subtree_bucket_selection import (
    SOURCE_COLLECTION,
    file_sha256,
    provenance,
    run_phase,
    validate_collections,
    validation_identity,
    verify_phase,
    verify_selection_snapshot,
)


DEFAULT_FREEZE = Path(
    "bench/db/runs/subtree_buckets_20260727/"
    "selection_holdout_v3.selection_freeze.json"
)
DEFAULT_OUTPUT = Path(
    "bench/db/runs/subtree_buckets_20260727/selection_holdout_v3_final.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_embedded_producers(snapshot: dict) -> None:
    import hashlib

    provenance_record = snapshot["provenance"]
    for raw_path, expected in provenance_record["scripts"].items():
        source = provenance_record["script_sources"].get(raw_path)
        if source is None:
            raise RuntimeError(f"missing embedded producer source {raw_path}")
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != expected:
            raise RuntimeError(f"embedded producer hash mismatch {raw_path}")
    for raw_path, expected in provenance_record["prior_results"].items():
        path = Path(raw_path)
        if not path.exists() or file_sha256(path) != expected:
            raise RuntimeError(f"prior result changed: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument("--mongo-db", default="bench")
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.out.exists():
        raise RuntimeError(f"refusing to overwrite {args.out}")

    frozen = json.loads(args.freeze.read_text())
    if frozen["protocol"]["freeze_snapshot"] != str(args.freeze):
        raise RuntimeError("snapshot records a different freeze path")
    verify_embedded_producers(frozen)
    prior_paths = [
        Path(path) for path in frozen["provenance"]["prior_results"]
    ]

    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    try:
        database = client[args.mongo_db]
        nodes = database[SOURCE_COLLECTION]
        validation = validate_collections(database)
        if validation_identity(validation) != frozen["validation_identity"]:
            raise RuntimeError(
                "live source/collection/index identity differs from selection"
            )
        selected_size = verify_selection_snapshot(
            frozen,
            nodes,
            prior_paths,
            int(frozen["protocol"]["selection_repeats"]),
        )
        holdout_inputs = frozen["input_partitions"]["holdout"]
        holdout_repeats = int(frozen["protocol"]["holdout_repeats"])
        collection = database[
            validation["candidates"][selected_size]["collection"]
        ]

        output = json.loads(json.dumps(frozen))
        output["selection_freeze_artifact"] = {
            "path": str(args.freeze),
            "sha256": file_sha256(args.freeze),
        }
        output["holdout_provenance"] = provenance(
            prior_paths,
            producer_scripts=(
                Path(__file__),
                Path(__file__).with_name(
                    "bench_subtree_bucket_selection.py"
                ),
                Path(__file__).with_name("bench_subtree_buckets.py"),
            ),
        )
        output["run"]["holdout_started_at"] = utc_now()
        output["run"]["status"] = "holdout-running"
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(output, indent=2))

        print("holdout phase", flush=True)
        output["phases"]["holdout"] = run_phase(
            nodes,
            {selected_size: collection},
            holdout_inputs,
            (selected_size,),
            holdout_repeats,
            int(frozen["protocol"]["selection_seed"]) + 20_000,
            "holdout",
            output,
            args.out,
        )
        verify_phase(
            "holdout",
            output["phases"]["holdout"],
            holdout_inputs,
            (selected_size,),
            holdout_repeats,
        )
        output["run"]["status"] = "complete"
        output["run"]["completed_at"] = utc_now()
        output["environment"]["loadavg_after"] = list(os.getloadavg())
        args.out.write_text(json.dumps(output, indent=2))
        print(json.dumps({
            "selection": output["selection"],
            "holdout": output["phases"]["holdout"]["summary"],
        }, indent=2))
        print(f"wrote {args.out}", flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
