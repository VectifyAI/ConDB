#!/usr/bin/env python3
"""Recompute cursor-batch summaries from stored raw observations."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from bench_layout_2v3_mongo_batch import path_equal_summary


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("--out", required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.out)
    if output_path.exists() and not args.overwrite:
        raise SystemExit(f"refusing to overwrite {output_path}")
    source = json.loads(input_path.read_text())
    if source.get("status") != "complete":
        raise SystemExit("source artifact is not complete")

    script_path = Path(__file__).resolve()
    helper_path = Path(__file__).with_name("bench_layout_2v3_mongo_batch.py")
    output = {
        "source_artifact": str(input_path),
        "source_sha256": sha256(input_path),
        "source_status": source["status"],
        "source_arm": source["arm"],
        "source_paths": len(source["indices"]),
        "source_repeats": source["repeats"],
        "analysis_contract": (
            "recompute each path by taking the median across repeats, then "
            "summarize equally weighted paths and paired speedup versus default"
        ),
        "validation_limit": (
            "this reanalysis does not strengthen the producer's original "
            "ordered-output validation"
        ),
        "script": str(script_path),
        "script_sha256": sha256(script_path),
        "script_source": script_path.read_text(),
        "summary_helper": str(helper_path),
        "summary_helper_sha256": sha256(helper_path),
        "summary_helper_source": helper_path.read_text(),
        "summary": path_equal_summary(source["samples"]),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2))
    print(json.dumps(output["summary"], indent=2))


if __name__ == "__main__":
    main()
