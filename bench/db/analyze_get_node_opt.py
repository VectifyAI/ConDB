#!/usr/bin/env python3
"""Paired bootstrap intervals over the retained get_node optimization blocks.

bench_get_node_opt.py reports per-block paired medians and a count of blocks
favouring the arm.  That says an effect is consistent; it does not say how
precisely it is known.  The protocol this project already holds itself to for
MongoDB source work (report/evidence/mongodb_master_countscan_20260805) resamples
complete matched pairs with a fixed seed and reports a percentile interval, and
the same is done here.

Resampling is over whole blocks, with replacement, keeping the arm and the
baseline from the same block together -- the pairing is the whole point, since a
block's absolute level moves with whatever else the box is doing and the paired
delta does not.

Post-processing only.  This reads retained JSON and touches no database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zlib
import random
import statistics
from pathlib import Path
from typing import Any, Sequence

RUNS = (
    ("r7", "arms_r7_head.json"),
    ("r8", "arms_r8_sample.json"),
    ("r9", "arms_r9_head.json"),
    ("r10", "arms_r10_sample.json"),
)

COMPARISONS = (
    ("idhack", "baseline", "full stack vs baseline"),
    ("idhack", "control", "full stack vs control"),
    ("control", "baseline", "control vs baseline (freshness)"),
    ("command", "baseline", "command vs baseline"),
    ("unpinned", "command", "wrapper + implicit session"),
    ("pinned", "unpinned", "pool checkout + server selection"),
    ("pinned", "command", "pinned increment (all three)"),
    ("idhack", "pinned", "idhack alone"),
)

METRICS = ("wall_us", "ccpu_us", "scpu_us")


def paired_deltas(
    blocks: dict[str, list[dict]], arm: str, base: str, metric: str
) -> tuple[list[float], list[float]]:
    a, b = blocks[arm], blocks[base]
    absolute = [a[i][metric] - b[i][metric] for i in range(len(b))]
    relative = [
        100.0 * absolute[i] / b[i][metric] if b[i][metric] else 0.0
        for i in range(len(b))
    ]
    return absolute, relative


def outlier_blocks(blocks: dict[str, list[dict]], factor: float = 1.5) -> list[int]:
    """Same rule as bench_get_node_opt.outlier_blocks, recomputed here.

    Applied uniformly to every run, so a run produced by an older harness that
    recorded no screen is treated the same as one that did.
    """
    flagged: set[int] = set()
    for rows in blocks.values():
        walls = sorted(row["wall_us"] for row in rows)
        middle = walls[len(walls) // 2]
        for index, row in enumerate(rows):
            if row["wall_us"] > factor * middle:
                flagged.add(index)
    return sorted(flagged)


def bootstrap(
    values: Sequence[float], resamples: int, seed: int, statistic=statistics.median
) -> tuple[float, float]:
    """Percentile 95% interval for `statistic` over whole-block resamples."""
    rng = random.Random(seed)
    n = len(values)
    draws = sorted(
        statistic([values[rng.randrange(n)] for _ in range(n)])
        for _ in range(resamples)
    )
    lo = draws[int(0.025 * resamples)]
    hi = draws[min(int(0.975 * resamples), resamples - 1)]
    return lo, hi


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="runs/get_node_opt")
    parser.add_argument("--resamples", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument("--out", default="runs/get_node_opt/intervals.json")
    args = parser.parse_args()

    root = Path(args.dir)
    result: dict[str, Any] = {
        "method": {
            "resampling": "whole blocks, with replacement, arm and baseline "
                          "kept together within a block",
            "statistic": "median of the per-block paired delta",
            "interval": "percentile 95%",
            "resamples": args.resamples,
            "seed": args.seed,
        },
        "runs": {},
    }

    for tag, name in RUNS:
        path = root / name
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        blocks = data["blocks"]
        entry: dict[str, Any] = {
            "artifact": name,
            "sha256": sha256(path),
            "inputs": data["run"]["inputs"],
            "input_mode": data["run"].get("input_mode", "head"),
            "blocks": data["run"]["blocks"],
            "iters_per_block": data["run"]["iters_per_block"],
            "comparisons": {},
        }
        dropped = outlier_blocks(blocks)
        keep = [i for i in range(data["run"]["blocks"]) if i not in set(dropped)]
        entry["blocks_dropped_by_screen"] = dropped
        entry["blocks_kept"] = len(keep)
        for arm, base, label in COMPARISONS:
            if arm not in blocks or base not in blocks:
                continue
            row: dict[str, Any] = {}
            for metric in METRICS:
                absolute_all, relative_all = paired_deltas(blocks, arm, base, metric)
                absolute = [absolute_all[i] for i in keep]
                relative = [relative_all[i] for i in keep]
                # One seed per (run, comparison, metric) so the intervals are
                # independent draws rather than the same random stream reused.
                # crc32, not hash(): str.__hash__ is per-process randomized, so
                # an earlier version's "seed 20260807" did not describe a
                # reproducible computation.
                seed = args.seed + zlib.crc32(
                    f"{tag}|{arm}|{base}|{metric}".encode()
                ) % 100_000
                lo_a, hi_a = bootstrap(absolute, args.resamples, seed)
                lo_r, hi_r = bootstrap(relative, args.resamples, seed)
                row[metric] = {
                    "median_delta_us": round(statistics.median(absolute), 3),
                    "ci95_delta_us": [round(lo_a, 3), round(hi_a, 3)],
                    "median_pct": round(statistics.median(relative), 2),
                    "ci95_pct": [round(lo_r, 2), round(hi_r, 2)],
                    "blocks_favouring_arm": sum(1 for d in absolute if d < 0),
                    "blocks": len(absolute),
                    "blocks_before_screen": len(absolute_all),
                    "excludes_zero": hi_r < 0 or lo_r > 0,
                }
            entry["comparisons"][label] = row
        result["runs"][tag] = entry

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))

    print(f"{'comparison':<32}{'metric':<9}{'median':>10}{'  95% CI':>20}{'blk':>8}  sig")
    for tag in result["runs"]:
        print(f"-- {tag}  {result['runs'][tag]['artifact']}")
        for label, row in result["runs"][tag]["comparisons"].items():
            for metric, cell in row.items():
                unit = "us" if metric == "ccpu_us" else "%"
                med = cell["median_delta_us"] if unit == "us" else cell["median_pct"]
                ci = cell["ci95_delta_us"] if unit == "us" else cell["ci95_pct"]
                print(
                    f"   {label:<29}{metric:<9}{med:>9.2f}{unit:<1}"
                    f"  [{ci[0]:>7.2f},{ci[1]:>7.2f}]"
                    f"{cell['blocks_favouring_arm']:>4}/{cell['blocks']:<3}"
                    f"  {'yes' if cell['excludes_zero'] else 'NO'}"
                )
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
