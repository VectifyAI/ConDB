#!/usr/bin/env python3
"""Unbiased stratified small/deep-root diagnostic for the frozen v3 winner.

This is deliberately separate from bucket-size selection and holdout.  It draws
50 roots uniformly without replacement from each synthetic depth 4--8 ID range,
after excluding every root in the earlier v1 and v2 raw artifacts.  Equal depth
weights are a diagnostic design, not an estimate of the production workload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient

from bench_subtree_bucket_selection import (
    DEFAULT_PRIOR_RESULTS,
    TREE_ID,
    canonical_sha256,
    file_sha256,
    load_spectrum_population,
    prior_input_ids,
    provenance,
    run_phase,
    selection_freeze_payload,
    validate_collections,
    validation_identity,
)
from bench_subtree_buckets import SOURCE_COLLECTION


SEED = 20260801
SELECTION_RESULT = Path(
    "bench/db/runs/subtree_buckets_20260727/"
    "selection_holdout_v3_final.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument("--mongo-db", default="bench")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--out",
        default=(
            "bench/db/runs/subtree_buckets_20260727/"
            "spectrum_unbiased_v3.json"
        ),
    )
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error("repeats must be at least 2")

    out_path = Path(args.out)
    if out_path.exists():
        raise RuntimeError(f"refusing to overwrite {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    selection = json.loads(SELECTION_RESULT.read_text())
    if selection["run"]["status"] != "complete":
        raise RuntimeError("selection/holdout result is incomplete")
    if canonical_sha256(selection_freeze_payload(selection)) != selection[
        "selection_payload_sha256"
    ]:
        raise RuntimeError("selection freeze payload hash mismatch")
    for label, record in (
        ("selection", selection["provenance"]),
        ("holdout", selection["holdout_provenance"]),
    ):
        for raw_path, expected_hash in record["scripts"].items():
            source = record["script_sources"][raw_path]
            if (
                hashlib.sha256(source.encode("utf-8")).hexdigest()
                != expected_hash
            ):
                raise RuntimeError(
                    f"embedded {label} producer invalid: {raw_path}"
                )
    freeze = selection["selection_freeze_artifact"]
    if file_sha256(Path(freeze["path"])) != freeze["sha256"]:
        raise RuntimeError("selection freeze artifact changed")
    selected_size = int(selection["selection"]["selected_size"])

    prior_paths = [
        *(Path(path) for path in DEFAULT_PRIOR_RESULTS),
        SELECTION_RESULT,
    ]
    excluded = prior_input_ids(prior_paths)
    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    database = client[args.mongo_db]
    nodes = database[SOURCE_COLLECTION]
    validation = validate_collections(database)
    if validation_identity(validation) != selection["validation_identity"]:
        raise RuntimeError("live layout identity differs from selection")
    candidates = validation["candidates"]
    collection = database[candidates[selected_size]["collection"]]
    inputs = load_spectrum_population(
        nodes,
        excluded,
        random.Random(SEED),
    )
    depth_counts = {
        str(depth): sum(item["depth"] == depth for item in inputs)
        for depth in range(4, 9)
    }
    if set(depth_counts.values()) != {50}:
        raise RuntimeError(f"unexpected depth counts: {depth_counts}")

    output = {
        "run": {
            "status": "running",
            "started_at": utc_now(),
            "seed": SEED,
        },
        "protocol": {
            "selected_size": selected_size,
            "selection_result": str(SELECTION_RESULT),
            "selection_result_sha256": provenance(
                [SELECTION_RESULT]
            )["prior_results"][str(SELECTION_RESULT)],
            "prior_results_excluded": [str(path) for path in prior_paths],
            "sampling": (
                "uniform without replacement within each synthetic depth "
                "4--8 ID range; 50 roots per depth"
            ),
            "scope": (
                "equal-depth diagnostic only; not population-weighted and "
                "not a runtime routing experiment"
            ),
            "aggregation": (
                "per-input median across repeats, then workload percentile"
            ),
        },
        "environment": {
            "mongodb": client.server_info()["version"],
            "pymongo": __import__("pymongo").version,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "loadavg_before": list(os.getloadavg()),
        },
        "provenance": provenance(
            prior_paths,
            producer_scripts=(
                Path(__file__),
                Path(__file__).with_name(
                    "bench_subtree_bucket_selection.py"
                ),
                Path(__file__).with_name("bench_subtree_buckets.py"),
            ),
        ),
        "validation": validation,
        "validation_identity": validation_identity(validation),
        "input_partition": inputs,
        "depth_counts": depth_counts,
        "phases": {},
    }
    out_path.write_text(json.dumps(output, indent=2))
    output["phases"]["spectrum"] = run_phase(
        nodes,
        {selected_size: collection},
        inputs,
        (selected_size,),
        args.repeats,
        SEED + 30_000,
        "spectrum",
        output,
        out_path,
    )
    output["run"]["status"] = "complete"
    output["run"]["completed_at"] = utc_now()
    output["environment"]["loadavg_after"] = list(os.getloadavg())
    out_path.write_text(json.dumps(output, indent=2))
    client.close()
    print(json.dumps(output["phases"]["spectrum"]["summary"], indent=2))
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
