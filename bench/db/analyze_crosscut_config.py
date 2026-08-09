#!/usr/bin/env python3
"""Paired analysis of the cross-cutting configuration campaigns.

Reads the raw per-observation timings written by ``bench_crosscut_queryopts.py``
and ``bench_crosscut_config.py`` and reports, for every arm against the
baseline:

* the mean of the per-block paired deltas at P50, P90, P95 and the mean, with
  the standard error over blocks -- the block is the unit, because that is the
  level at which the arms are randomised;
* the per-input paired delta (median over blocks per input, then compared
  input-by-input), which removes input-mix variation entirely and gives a sign
  test over inputs;
* the per-input delta split by result size, because a lever that helps a 5 MB
  reply need not help a 600-byte one.

No effect smaller than the reported spread is claimable.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def percentile(values, pct):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def block_stat(block, stat):
    if stat == "mean":
        return statistics.mean(block)
    return percentile(block, float(stat))


def analyze(path: Path, stats=("50", "90", "95", "mean")) -> None:
    document = json.loads(path.read_text())
    run = document["run"]
    order = run["arms"]
    baseline = run["baseline_arm"]
    print(f"\n########## {path.name}")
    print(f"# {run.get('label', '')}")
    print(f"# arms={order} baseline={baseline} blocks={run['blocks']} "
          f"shared_connection={run.get('shared_connection', False)}")

    for operation, entry in document["results"].items():
        raw = entry["raw_ms"]
        blocks = len(raw[baseline])
        n_inputs = len(raw[baseline][0])
        print(f"\n=== {operation}  (avg_rows={entry['avg_rows']}, "
              f"{n_inputs} inputs x {blocks} blocks)")
        header = "  {:<22s}".format("arm") + "".join(
            f"{'d' + s:>12s}" for s in stats) + f"{'sem(P50)':>10s}{'blocks<':>9s}"
        print(header)
        for arm in order:
            cells = []
            sem50 = 0.0
            for stat in stats:
                base = [block_stat(raw[baseline][b], stat) for b in range(blocks)]
                arm_v = [block_stat(raw[arm][b], stat) for b in range(blocks)]
                deltas = [(a - b) / b * 100.0 for a, b in zip(arm_v, base)]
                cells.append(statistics.mean(deltas))
                if stat == "50":
                    sem50 = (statistics.stdev(deltas) / blocks ** 0.5
                             if blocks > 1 else 0.0)
                    wins = sum(1 for d in deltas if d < 0)
            row = "  {:<22s}".format(arm) + "".join(
                f"{c:+11.3f}%" for c in cells)
            print(row + f"{sem50:9.3f} {wins:>4d}/{blocks}")

        # per-input paired view: median over blocks for each input
        print("  -- per-input paired (median over blocks per input) --")
        base_per_input = [
            statistics.median([raw[baseline][b][i] for b in range(blocks)])
            for i in range(n_inputs)
        ]
        for arm in order:
            if arm == baseline:
                continue
            arm_per_input = [
                statistics.median([raw[arm][b][i] for b in range(blocks)])
                for i in range(n_inputs)
            ]
            ratios = [(a - b) / b * 100.0 for a, b in zip(arm_per_input, base_per_input)]
            faster = sum(1 for r in ratios if r < 0)
            # split by baseline cost decile: cheap half vs expensive half
            paired = sorted(zip(base_per_input, ratios))
            half = len(paired) // 2
            cheap = statistics.median([r for _, r in paired[:half]])
            costly = statistics.median([r for _, r in paired[half:]])
            top10 = statistics.median(
                [r for _, r in paired[int(len(paired) * 0.9):]])
            print(f"     {arm:<20s} median_d={statistics.median(ratios):+7.3f}% "
                  f"mean_d={statistics.mean(ratios):+7.3f}% "
                  f"cheap_half={cheap:+7.3f}% costly_half={costly:+7.3f}% "
                  f"top10pct={top10:+7.3f}% [{faster}/{n_inputs} inputs faster]")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    for path in args.paths:
        analyze(Path(path))


if __name__ == "__main__":
    main()
