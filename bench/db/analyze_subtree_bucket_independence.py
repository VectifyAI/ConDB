#!/usr/bin/env python3
"""Audit path-range independence in a subtree bucket selection/holdout split."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any, Sequence


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_ancestor(left: str, right: str) -> bool:
    return left != right and right.startswith(left.rstrip("/") + "/")


def percentile(values: Sequence[float], pct: int) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return float(ordered[index])


def latency_summary(samples: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    medians = [
        statistics.median(item["total_ms"] for item in sample["times"][arm])
        for sample in samples
    ]
    return {
        "roots": len(samples),
        "p50_ms": round(percentile(medians, 50), 6),
        "p95_ms": round(percentile(medians, 95), 6),
        "per_root_medians": medians,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("--arm", default="256")
    parser.add_argument("--out", required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.out)
    if output_path.exists() and not args.overwrite:
        raise SystemExit(f"refusing to overwrite {output_path}")
    source = json.loads(input_path.read_text())
    selection = source["input_partitions"]["selection"]
    holdout = source["input_partitions"]["holdout"]

    cross_pairs = [
        {
            "selection_node_id": left["node_id"],
            "selection_path": left["path"],
            "holdout_node_id": right["node_id"],
            "holdout_path": right["path"],
        }
        for left in selection
        for right in holdout
        if is_ancestor(left["path"], right["path"])
        or is_ancestor(right["path"], left["path"])
    ]
    within_pairs = [
        {
            "left_node_id": left["node_id"],
            "left_path": left["path"],
            "right_node_id": right["node_id"],
            "right_path": right["path"],
        }
        for index, left in enumerate(holdout)
        for right in holdout[index + 1 :]
        if is_ancestor(left["path"], right["path"])
        or is_ancestor(right["path"], left["path"])
    ]

    cross_affected = {
        pair["holdout_node_id"]
        for pair in cross_pairs
    }
    within_affected = {
        pair[key]
        for pair in within_pairs
        for key in ("left_node_id", "right_node_id")
    }
    excluded = cross_affected | within_affected
    holdout_samples = source["phases"]["holdout"]["samples"]
    independent_samples = [
        sample for sample in holdout_samples
        if sample["node_id"] not in excluded
    ]

    baseline = latency_summary(independent_samples, "baseline")
    candidate = latency_summary(independent_samples, args.arm)
    faster = 0
    for sample in independent_samples:
        baseline_median = statistics.median(
            item["total_ms"] for item in sample["times"]["baseline"]
        )
        candidate_median = statistics.median(
            item["total_ms"] for item in sample["times"][args.arm]
        )
        faster += candidate_median < baseline_median

    script_path = Path(__file__).resolve()
    output = {
        "input": str(input_path),
        "input_sha256": sha256(input_path),
        "script": str(script_path),
        "script_sha256": sha256(script_path),
        "script_source": script_path.read_text(),
        "split_audit": {
            "root_id_overlap": len(
                {item["node_id"] for item in selection}
                & {item["node_id"] for item in holdout}
            ),
            "selection_holdout_ancestor_pairs": len(cross_pairs),
            "selection_holdout_affected_holdout_roots": len(cross_affected),
            "within_holdout_ancestor_pairs": len(within_pairs),
            "within_holdout_affected_roots": len(within_affected),
            "cross_pairs": cross_pairs,
            "within_pairs": within_pairs,
        },
        "strict_subset": {
            "definition": (
                "remove every holdout root involved in a selection/holdout "
                "ancestor relation or any within-holdout ancestor relation"
            ),
            "excluded_node_ids": sorted(excluded),
            "remaining_roots": len(independent_samples),
            "per_root_medians_faster": faster,
            "baseline": baseline,
            args.arm: candidate,
            "marginal_speedup_p50": round(
                baseline["p50_ms"] / candidate["p50_ms"],
                6,
            ),
            "marginal_speedup_p95": round(
                baseline["p95_ms"] / candidate["p95_ms"],
                6,
            ),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2))
    print(json.dumps({
        "split_audit": output["split_audit"],
        "strict_subset": {
            key: value
            for key, value in output["strict_subset"].items()
            if key not in {"excluded_node_ids", "baseline", args.arm}
        },
    }, indent=2))


if __name__ == "__main__":
    main()
