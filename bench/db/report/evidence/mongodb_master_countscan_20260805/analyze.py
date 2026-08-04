#!/usr/bin/env python3

import json
import math
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "raw"
METRICS = ("instructions_per_iteration", "cpu_time", "real_time")
PAIR_COUNT = 10
REPETITIONS = 5
BOOTSTRAP_SAMPLES = 100_000
SEED = 20_260_805


def percentile(sorted_values, fraction):
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def geometric_mean(values):
    return math.exp(sum(math.log(value) for value in values) / len(values))


def load_block(pair, arm):
    path = ROOT / f"pair{pair:02d}_{arm}.json"
    payload = json.loads(path.read_text())
    rows = [row for row in payload["benchmarks"] if row["run_type"] == "iteration"]
    assert len(rows) == REPETITIONS, (path, len(rows))
    assert [row["repetition_index"] for row in rows] == list(range(REPETITIONS)), path
    assert all(row["iterations"] == 1 for row in rows), path
    assert all(row["time_unit"] == "ns" for row in rows), path
    assert all("NonMultikeyCountScan/500000/64" in row["name"] for row in rows), path
    expected_executable = f"/tmp/mongo-count-query-bm-nolog-{'disabled' if arm == 'baseline' else 'enabled'}"
    assert payload["context"]["executable"] == expected_executable, path
    return {
        "path": path.name,
        "context": payload["context"],
        "means": {
            metric: sum(row[metric] for row in rows) / REPETITIONS for metric in METRICS
        },
    }


blocks = []
for pair in range(1, PAIR_COUNT + 1):
    baseline = load_block(pair, "baseline")
    candidate = load_block(pair, "candidate")
    blocks.append({"pair": pair, "baseline": baseline, "candidate": candidate})

summary = {
    "schema_version": 1,
    "pair_count": PAIR_COUNT,
    "repetitions_per_process": REPETITIONS,
    "bootstrap_samples": BOOTSTRAP_SAMPLES,
    "bootstrap_seed": SEED,
    "metrics": {},
    "pairs": [],
}

for block in blocks:
    pair_summary = {"pair": block["pair"], "metrics": {}}
    for metric in METRICS:
        baseline = block["baseline"]["means"][metric]
        candidate = block["candidate"]["means"][metric]
        pair_summary["metrics"][metric] = {
            "baseline_mean": baseline,
            "candidate_mean": candidate,
            "candidate_over_baseline": candidate / baseline,
        }
    summary["pairs"].append(pair_summary)

rng = random.Random(SEED)
for metric in METRICS:
    ratios = [pair["metrics"][metric]["candidate_over_baseline"] for pair in summary["pairs"]]
    ratio = geometric_mean(ratios)
    bootstrap_ratios = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sample = [ratios[rng.randrange(PAIR_COUNT)] for _ in range(PAIR_COUNT)]
        bootstrap_ratios.append(geometric_mean(sample))
    bootstrap_ratios.sort()
    ratio_low = percentile(bootstrap_ratios, 0.025)
    ratio_high = percentile(bootstrap_ratios, 0.975)
    summary["metrics"][metric] = {
        "candidate_over_baseline_geomean": ratio,
        "candidate_over_baseline_ci95": [ratio_low, ratio_high],
        "reduction_percent": (1.0 - ratio) * 100.0,
        "reduction_percent_ci95": [(1.0 - ratio_high) * 100.0, (1.0 - ratio_low) * 100.0],
        "baseline_block_mean_geomean": geometric_mean(
            [pair["metrics"][metric]["baseline_mean"] for pair in summary["pairs"]]
        ),
        "candidate_block_mean_geomean": geometric_mean(
            [pair["metrics"][metric]["candidate_mean"] for pair in summary["pairs"]]
        ),
    }

print(json.dumps(summary, indent=2, sort_keys=True))
