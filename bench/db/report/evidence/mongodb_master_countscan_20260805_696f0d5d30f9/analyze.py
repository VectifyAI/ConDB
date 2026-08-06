#!/usr/bin/env python3

"""Validate and analyze the frozen 20-pair CountScan activation-ablation campaign."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


EVIDENCE_DIR = Path(__file__).resolve().parent
RAW_DIR = EVIDENCE_DIR / "raw"
LOG_DIR = EVIDENCE_DIR / "logs"
CAMPAIGN_PATH = EVIDENCE_DIR / "campaign.json"
RUN_RECORD_PATH = EVIDENCE_DIR / "campaign_run.json"
SUMMARY_PATH = EVIDENCE_DIR / "summary.json"

SOURCE_COMMIT = "696f0d5d30f9bb6bcdb96ade8388e6bea36a92f9"
BENCHMARK_FILTER = "CountQueryBenchmark/DirectNonDeduplicatingCountScan/400000/64$"
RUN_NAME = "CountQueryBenchmark/DirectNonDeduplicatingCountScan/400000/64"
PAIR_ORDERS = (
    "BC", "CB", "CB", "BC", "BC", "CB", "BC", "CB", "BC", "CB",
    "CB", "BC", "BC", "CB", "CB", "BC", "CB", "BC", "CB", "BC",
)
PAIR_COUNT = 20
REPETITIONS = 5
BOOTSTRAP_SAMPLES = 100_000
BOOTSTRAP_SEED = 20_260_805
METRIC_ROLES = {
    "instructions_per_iteration": "primary",
    "cpu_time": "secondary",
    "real_time": "auxiliary",
}
ARM_LABELS = {"B": "disabled_control", "C": "enabled_candidate"}
ARM_IDENTITIES = {
    "B": {
        "path": "/tmp/mongo-count-query-bm-696f0d5d-disabled",
        "sha256": "ed2bdc05a6188a0ebb6433923391417c183e3471df78516eac3523c2f825bebc",
        "build_id": "d14aea10c20ca862734eb72e930225a1e5ea263e",
    },
    "C": {
        "path": "/tmp/mongo-count-query-bm-696f0d5d-enabled",
        "sha256": "02628346e4357ab9a48d5c0dea0de68df4c0b2921ded3fb44c4f643eb5c043be",
        "build_id": "482d4815330592895592815012509a756b70ccf8",
    },
}


def fail(message: str) -> None:
    raise SystemExit(f"evidence validation failed: {message}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"missing JSON file: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read JSON file {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"top-level JSON value is not an object: {path}")
    return value


def require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        fail(f"{label}: expected {expected!r}, got {actual!r}")


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{label} is not an object")
    return value


def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        fail(f"{label} is not an array")
    return value


def positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"{label} is not numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        fail(f"{label} must be positive and finite, got {value!r}")
    return result


def nonnegative_finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"{label} is not numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        fail(f"{label} must be nonnegative and finite, got {value!r}")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        fail(f"cannot hash {path}: {exc}")
    return digest.hexdigest()


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        fail("cannot calculate percentile of an empty sample")
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def geometric_mean(values: list[float]) -> float:
    if not values:
        fail("cannot calculate geometric mean of an empty sample")
    checked = [positive_finite(value, "geometric-mean input") for value in values]
    return math.exp(sum(math.log(value) for value in checked) / len(checked))


def expected_output_sequence() -> list[dict[str, Any]]:
    sequence: list[dict[str, Any]] = []
    for pair, order in enumerate(PAIR_ORDERS, start=1):
        for position, arm in enumerate(order, start=1):
            stem = f"pair{pair:02d}_{arm}_{ARM_LABELS[arm]}"
            sequence.append(
                {
                    "pair": pair,
                    "position": position,
                    "arm": arm,
                    "raw": f"raw/{stem}.json",
                    "log": f"logs/{stem}.log",
                }
            )
    return sequence


def validate_campaign(campaign: dict[str, Any]) -> None:
    require_equal(campaign.get("schema_version"), 1, "campaign.schema_version")
    require_equal(campaign.get("comparison"), "activation_ablation", "campaign.comparison")
    require_equal(campaign.get("source_commit"), SOURCE_COMMIT, "campaign.source_commit")
    require_equal(campaign.get("frozen_before_execution"), True, "campaign frozen flag")
    require_equal(campaign.get("activation_disable_patch"), "activation_disable.patch", "patch name")
    harness_artifacts = require_mapping(campaign.get("harness_artifacts"), "campaign.harness_artifacts")
    expected_harness_files = {
        "run_blocks.sh",
        "analyze.py",
        "activation_disable.patch",
        "candidate.patch",
    }
    require_equal(set(harness_artifacts), expected_harness_files, "frozen harness artifact set")
    for filename in sorted(expected_harness_files):
        configured_sha256 = harness_artifacts.get(filename)
        if not isinstance(configured_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", configured_sha256):
            fail(f"campaign harness SHA-256 is invalid for {filename}: {configured_sha256!r}")
        require_equal(
            sha256_file(EVIDENCE_DIR / filename),
            configured_sha256,
            f"frozen harness SHA-256 for {filename}",
        )

    arms = require_mapping(campaign.get("arms"), "campaign.arms")
    require_equal(set(arms), {"B", "C"}, "campaign arm set")
    for arm in ("B", "C"):
        arm_config = require_mapping(arms.get(arm), f"campaign.arms.{arm}")
        require_equal(arm_config.get("label"), ARM_LABELS[arm], f"arm {arm} label")
        require_equal(arm_config.get("binary_path"), ARM_IDENTITIES[arm]["path"], f"arm {arm} path")
        require_equal(arm_config.get("sha256"), ARM_IDENTITIES[arm]["sha256"], f"arm {arm} SHA-256")
        require_equal(arm_config.get("build_id"), ARM_IDENTITIES[arm]["build_id"], f"arm {arm} Build ID")

    benchmark = require_mapping(campaign.get("benchmark"), "campaign.benchmark")
    require_equal(benchmark.get("filter"), BENCHMARK_FILTER, "benchmark filter")
    require_equal(benchmark.get("expected_run_name"), RUN_NAME, "benchmark run name")
    require_equal(benchmark.get("arguments"), [400000, 64], "benchmark arguments")
    require_equal(benchmark.get("repetitions_per_process"), REPETITIONS, "benchmark repetitions")
    require_equal(benchmark.get("minimum_time_seconds"), 0.01, "benchmark minimum time")
    require_equal(benchmark.get("report_aggregates_only"), False, "aggregate-only flag")
    require_equal(benchmark.get("expected_iteration_count_per_repetition"), 1, "iteration count")
    require_equal(benchmark.get("expected_library_build_type"), "release", "build type")

    execution = require_mapping(campaign.get("execution"), "campaign.execution")
    require_equal(execution.get("pair_count"), PAIR_COUNT, "pair count")
    require_equal(execution.get("process_count"), 2 * PAIR_COUNT, "process count")
    require_equal(execution.get("fresh_process_per_arm_per_pair"), True, "fresh-process flag")
    require_equal(execution.get("cpu_affinity"), 0, "CPU affinity")
    require_equal(execution.get("taskset_command"), ["taskset", "-c", "0"], "taskset command")
    require_equal(execution.get("no_early_stopping"), True, "no-early-stopping flag")
    require_equal(execution.get("partial_reruns_forbidden"), True, "partial-rerun flag")
    expected_orders = [
        {"pair": pair, "order": order}
        for pair, order in enumerate(PAIR_ORDERS, start=1)
    ]
    require_equal(execution.get("pair_order"), expected_orders, "frozen pair order")

    analysis = require_mapping(campaign.get("analysis"), "campaign.analysis")
    require_equal(
        analysis.get("process_aggregation"),
        "arithmetic_mean_of_five_iteration_rows",
        "process aggregation",
    )
    require_equal(analysis.get("pair_effect"), "candidate_C_over_control_B", "pair effect")
    require_equal(
        analysis.get("overall_estimator"),
        "geometric_mean_of_complete_pair_ratios",
        "overall estimator",
    )
    require_equal(analysis.get("order_strata"), ["BC", "CB"], "order strata")
    bootstrap = require_mapping(analysis.get("bootstrap"), "campaign.analysis.bootstrap")
    require_equal(bootstrap.get("method"), "stratified_complete_pair_resampling_with_replacement", "bootstrap method")
    require_equal(bootstrap.get("samples"), BOOTSTRAP_SAMPLES, "bootstrap samples")
    require_equal(bootstrap.get("seed"), BOOTSTRAP_SEED, "bootstrap seed")
    require_equal(bootstrap.get("confidence_level"), 0.95, "confidence level")
    require_equal(bootstrap.get("same_draws_across_metrics"), True, "shared bootstrap draws")
    expected_metrics = [
        {"name": name, "role": role} for name, role in METRIC_ROLES.items()
    ]
    require_equal(analysis.get("metrics"), expected_metrics, "metric roles")


def validate_run_record(run_record: dict[str, Any], campaign_sha256: str) -> None:
    require_equal(run_record.get("schema_version"), 1, "run record schema")
    require_equal(run_record.get("status"), "complete", "run record status")
    require_equal(run_record.get("cpu_affinity"), 0, "run record CPU affinity")
    require_equal(run_record.get("taskset_command"), ["taskset", "-c", "0"], "run record taskset")
    require_equal(run_record.get("benchmark_filter"), BENCHMARK_FILTER, "run record filter")
    require_equal(run_record.get("campaign_sha256_preflight"), campaign_sha256, "preflight campaign SHA-256")
    require_equal(run_record.get("campaign_sha256_postflight"), campaign_sha256, "postflight campaign SHA-256")
    harness_sha256 = {
        "run_blocks.sh": sha256_file(EVIDENCE_DIR / "run_blocks.sh"),
        "analyze.py": sha256_file(Path(__file__).resolve()),
        "activation_disable.patch": sha256_file(EVIDENCE_DIR / "activation_disable.patch"),
        "candidate.patch": sha256_file(EVIDENCE_DIR / "candidate.patch"),
    }
    require_equal(
        run_record.get("harness_sha256_preflight"),
        harness_sha256,
        "preflight harness SHA-256 identities",
    )
    require_equal(
        run_record.get("harness_sha256_postflight"),
        harness_sha256,
        "postflight harness SHA-256 identities",
    )
    require_equal(run_record.get("binary_identity_preflight"), ARM_IDENTITIES, "preflight binary identities")
    require_equal(run_record.get("binary_identity_postflight"), ARM_IDENTITIES, "postflight binary identities")
    require_equal(run_record.get("output_sequence"), expected_output_sequence(), "executed block order")

    host = require_mapping(run_record.get("host"), "run record host")
    for key in ("name", "kernel", "cpu_governor"):
        if not isinstance(host.get(key), str) or not host[key]:
            fail(f"run record host.{key} is empty")
    try:
        started = datetime.fromisoformat(run_record["started_at"])
        finished = datetime.fromisoformat(run_record["finished_at"])
    except (KeyError, TypeError, ValueError) as exc:
        fail(f"invalid run timestamps: {exc}")
    if finished < started:
        fail("run finished before it started")


def validate_file_set() -> tuple[list[str], list[str]]:
    if not RAW_DIR.is_dir():
        fail(f"raw directory is missing: {RAW_DIR}")
    if not LOG_DIR.is_dir():
        fail(f"log directory is missing: {LOG_DIR}")
    expected_sequence = expected_output_sequence()
    expected_raw = {Path(item["raw"]).name for item in expected_sequence}
    expected_logs = {Path(item["log"]).name for item in expected_sequence}
    actual_raw = {entry.name for entry in RAW_DIR.iterdir()}
    actual_logs = {entry.name for entry in LOG_DIR.iterdir()}
    require_equal(actual_raw, expected_raw, "exact raw file set")
    require_equal(actual_logs, expected_logs, "exact log file set")
    return sorted(expected_raw), sorted(expected_logs)


def validate_log(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        fail(f"cannot read log {path}: {exc}")
    if not text.strip():
        fail(f"empty log: {path}")
    if RUN_NAME not in text:
        fail(f"benchmark run name missing from log: {path}")
    forbidden_patterns = (
        (r'"s"\s*:\s*"[EF]"', "MongoDB E/F severity record"),
        (r"\b(?:ERROR|FATAL)\b", "error/fatal record"),
        (r"\bSlow query\b", "slow-query record"),
    )
    for pattern, description in forbidden_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            fail(f"{description} found in log: {path}")


def load_block(pair: int, arm: str) -> dict[str, Any]:
    stem = f"pair{pair:02d}_{arm}_{ARM_LABELS[arm]}"
    json_path = RAW_DIR / f"{stem}.json"
    log_path = LOG_DIR / f"{stem}.log"
    validate_log(log_path)
    payload = load_json(json_path)
    context = require_mapping(payload.get("context"), f"{json_path.name}.context")
    require_equal(context.get("executable"), ARM_IDENTITIES[arm]["path"], f"{json_path.name} executable")
    require_equal(context.get("library_build_type"), "release", f"{json_path.name} library build type")

    rows = require_list(payload.get("benchmarks"), f"{json_path.name}.benchmarks")
    if len(rows) != 8:
        fail(f"{json_path.name} must contain exactly five iteration and three aggregate rows; got {len(rows)}")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            fail(f"{json_path.name} benchmark row {index} is not an object")
        require_equal(row.get("run_name"), RUN_NAME, f"{json_path.name} row {index} run_name")

    iteration_rows = [row for row in rows if row.get("run_type") == "iteration"]
    aggregate_rows = [row for row in rows if row.get("run_type") == "aggregate"]
    if len(iteration_rows) != REPETITIONS:
        fail(f"{json_path.name} has {len(iteration_rows)} iteration rows, expected {REPETITIONS}")
    if len(aggregate_rows) != 3:
        fail(f"{json_path.name} has {len(aggregate_rows)} aggregate rows, expected 3")
    iteration_rows.sort(key=lambda row: row.get("repetition_index", -1))
    require_equal(
        [row.get("repetition_index") for row in iteration_rows],
        list(range(REPETITIONS)),
        f"{json_path.name} repetition indexes",
    )
    for repetition, row in enumerate(iteration_rows):
        prefix = f"{json_path.name} repetition {repetition}"
        require_equal(row.get("name"), RUN_NAME, f"{prefix} name")
        require_equal(row.get("iterations"), 1, f"{prefix} iterations")
        require_equal(row.get("threads"), 1, f"{prefix} threads")
        require_equal(row.get("time_unit"), "ns", f"{prefix} time unit")
        for metric in METRIC_ROLES:
            positive_finite(row.get(metric), f"{prefix} {metric}")

    aggregates_by_name: dict[str, dict[str, Any]] = {}
    for row in aggregate_rows:
        aggregate_name = row.get("aggregate_name")
        if aggregate_name not in ("mean", "median", "stddev"):
            fail(f"{json_path.name} unexpected aggregate name: {aggregate_name!r}")
        if aggregate_name in aggregates_by_name:
            fail(f"{json_path.name} duplicate aggregate: {aggregate_name}")
        aggregates_by_name[aggregate_name] = row
        require_equal(row.get("name"), f"{RUN_NAME}_{aggregate_name}", f"{json_path.name} aggregate row name")
        require_equal(row.get("iterations"), REPETITIONS, f"{json_path.name} aggregate iterations")
        require_equal(row.get("threads"), 1, f"{json_path.name} aggregate threads")
        require_equal(row.get("time_unit"), "ns", f"{json_path.name} aggregate time unit")
        for metric in METRIC_ROLES:
            if aggregate_name == "stddev":
                nonnegative_finite(row.get(metric), f"{json_path.name} stddev {metric}")
            else:
                positive_finite(row.get(metric), f"{json_path.name} {aggregate_name} {metric}")
    require_equal(set(aggregates_by_name), {"mean", "median", "stddev"}, f"{json_path.name} aggregate set")

    means = {
        metric: sum(positive_finite(row.get(metric), f"{json_path.name} {metric}") for row in iteration_rows)
        / REPETITIONS
        for metric in METRIC_ROLES
    }
    return {
        "json": json_path.name,
        "log": log_path.name,
        "context": context,
        "means": means,
    }


def validate_raw_contexts(blocks: list[dict[str, Any]], run_record: dict[str, Any]) -> None:
    run_host = require_mapping(run_record.get("host"), "run record host").get("name")
    if not isinstance(run_host, str) or not run_host:
        fail("run record host name is empty")
    contexts_by_block: dict[tuple[int, str], tuple[datetime, str]] = {}
    observed_hosts: set[str] = set()
    for block in blocks:
        pair = block["pair"]
        for arm in ("B", "C"):
            context = require_mapping(block[arm].get("context"), f"pair {pair} arm {arm} context")
            host_name = context.get("host_name")
            if not isinstance(host_name, str) or not host_name:
                fail(f"pair {pair} arm {arm} context host_name is empty")
            observed_hosts.add(host_name)
            raw_date = context.get("date")
            if not isinstance(raw_date, str) or not raw_date:
                fail(f"pair {pair} arm {arm} context date is empty")
            try:
                parsed_date = datetime.fromisoformat(raw_date)
            except ValueError as exc:
                fail(f"pair {pair} arm {arm} context date is invalid: {exc}")
            if parsed_date.tzinfo is None or parsed_date.utcoffset() is None:
                fail(f"pair {pair} arm {arm} context date lacks a UTC offset: {raw_date!r}")
            contexts_by_block[(pair, arm)] = (parsed_date, host_name)

    require_equal(observed_hosts, {run_host}, "all raw context host_name values")
    run_started = datetime.fromisoformat(run_record["started_at"])
    run_finished = datetime.fromisoformat(run_record["finished_at"])
    for pair, order in enumerate(PAIR_ORDERS, start=1):
        first, second = order
        first_date = contexts_by_block[(pair, first)][0]
        second_date = contexts_by_block[(pair, second)][0]
        if first_date > second_date:
            fail(
                f"pair {pair} raw dates contradict frozen {order} order: "
                f"{first}={first_date.isoformat()}, {second}={second_date.isoformat()}"
            )
        for arm, raw_date in ((first, first_date), (second, second_date)):
            if raw_date < run_started or raw_date > run_finished:
                fail(
                    f"pair {pair} arm {arm} raw date {raw_date.isoformat()} falls outside "
                    f"campaign interval [{run_started.isoformat()}, {run_finished.isoformat()}]"
                )


def bootstrap_ratios(
    ratios_by_metric: dict[str, dict[str, list[float]]],
) -> tuple[dict[str, list[float]], dict[str, dict[str, list[float]]]]:
    for metric, strata in ratios_by_metric.items():
        require_equal(set(strata), {"BC", "CB"}, f"{metric} bootstrap strata")
        require_equal(len(strata["BC"]), 10, f"{metric} BC count")
        require_equal(len(strata["CB"]), 10, f"{metric} CB count")

    logs = {
        metric: {
            order: [math.log(positive_finite(value, f"{metric} {order} ratio")) for value in values]
            for order, values in strata.items()
        }
        for metric, strata in ratios_by_metric.items()
    }
    overall_samples = {metric: [] for metric in METRIC_ROLES}
    stratum_samples = {
        metric: {"BC": [], "CB": []} for metric in METRIC_ROLES
    }
    rng = random.Random(BOOTSTRAP_SEED)
    for _ in range(BOOTSTRAP_SAMPLES):
        # These index draws are generated once and reused for all metrics.
        indexes = {
            "BC": [rng.randrange(10) for _ in range(10)],
            "CB": [rng.randrange(10) for _ in range(10)],
        }
        for metric in METRIC_ROLES:
            stratum_log_means: dict[str, float] = {}
            for order in ("BC", "CB"):
                sampled_log_mean = sum(logs[metric][order][index] for index in indexes[order]) / 10
                stratum_samples[metric][order].append(math.exp(sampled_log_mean))
                stratum_log_means[order] = sampled_log_mean
            overall_samples[metric].append(
                math.exp((stratum_log_means["BC"] + stratum_log_means["CB"]) / 2.0)
            )
    return overall_samples, stratum_samples


def ci95(values: list[float]) -> list[float]:
    ordered = sorted(values)
    return [percentile(ordered, 0.025), percentile(ordered, 0.975)]


def main() -> None:
    campaign = load_json(CAMPAIGN_PATH)
    validate_campaign(campaign)
    campaign_sha256 = sha256_file(CAMPAIGN_PATH)
    run_record = load_json(RUN_RECORD_PATH)
    validate_run_record(run_record, campaign_sha256)
    expected_raw, expected_logs = validate_file_set()

    blocks: list[dict[str, Any]] = []
    for pair, order in enumerate(PAIR_ORDERS, start=1):
        blocks.append(
            {
                "pair": pair,
                "order": order,
                "B": load_block(pair, "B"),
                "C": load_block(pair, "C"),
            }
        )
    validate_raw_contexts(blocks, run_record)

    pair_summaries: list[dict[str, Any]] = []
    ratios_by_metric = {
        metric: {"BC": [], "CB": []} for metric in METRIC_ROLES
    }
    for block in blocks:
        pair_summary: dict[str, Any] = {
            "pair": block["pair"],
            "order": block["order"],
            "metrics": {},
        }
        for metric in METRIC_ROLES:
            control = positive_finite(block["B"]["means"][metric], f"pair {block['pair']} B mean")
            candidate = positive_finite(block["C"]["means"][metric], f"pair {block['pair']} C mean")
            ratio = positive_finite(candidate / control, f"pair {block['pair']} {metric} ratio")
            pair_summary["metrics"][metric] = {
                "control_B_process_mean": control,
                "candidate_C_process_mean": candidate,
                "candidate_C_over_control_B": ratio,
            }
            ratios_by_metric[metric][block["order"]].append(ratio)
        pair_summaries.append(pair_summary)

    overall_bootstrap, stratum_bootstrap = bootstrap_ratios(ratios_by_metric)
    metric_summaries: dict[str, Any] = {}
    for metric, role in METRIC_ROLES.items():
        ratios = [
            pair["metrics"][metric]["candidate_C_over_control_B"]
            for pair in pair_summaries
        ]
        overall = geometric_mean(ratios)
        overall_interval = ci95(overall_bootstrap[metric])
        strata: dict[str, Any] = {}
        for order in ("BC", "CB"):
            stratum_ratio = geometric_mean(ratios_by_metric[metric][order])
            stratum_interval = ci95(stratum_bootstrap[metric][order])
            strata[order] = {
                "description": "control_then_candidate" if order == "BC" else "candidate_then_control",
                "pair_count": len(ratios_by_metric[metric][order]),
                "candidate_C_over_control_B_geomean": stratum_ratio,
                "candidate_C_over_control_B_ci95": stratum_interval,
                "reduction_percent": (1.0 - stratum_ratio) * 100.0,
            }

        leave_one_out = []
        for omitted_index, omitted_pair in enumerate(pair_summaries):
            retained = ratios[:omitted_index] + ratios[omitted_index + 1 :]
            leave_one_out.append(
                {
                    "omitted_pair": omitted_pair["pair"],
                    "omitted_order": omitted_pair["order"],
                    "candidate_C_over_control_B_geomean": geometric_mean(retained),
                }
            )
        leave_one_out_values = [item["candidate_C_over_control_B_geomean"] for item in leave_one_out]
        favorable = sum(1 for ratio in ratios if ratio < 1.0)
        ties = sum(1 for ratio in ratios if ratio == 1.0)
        overall_ci_below_one = overall_interval[1] < 1.0
        both_strata_below_one = all(
            strata[order]["candidate_C_over_control_B_geomean"] < 1.0
            for order in ("BC", "CB")
        )
        metric_summaries[metric] = {
            "role": role,
            "candidate_C_over_control_B_geomean": overall,
            "candidate_C_over_control_B_ci95": overall_interval,
            "reduction_percent": (1.0 - overall) * 100.0,
            "reduction_percent_ci95": [
                (1.0 - overall_interval[1]) * 100.0,
                (1.0 - overall_interval[0]) * 100.0,
            ],
            "control_B_process_mean_geomean": geometric_mean(
                [pair["metrics"][metric]["control_B_process_mean"] for pair in pair_summaries]
            ),
            "candidate_C_process_mean_geomean": geometric_mean(
                [pair["metrics"][metric]["candidate_C_process_mean"] for pair in pair_summaries]
            ),
            "order_strata": strata,
            "favorable_pair_count": favorable,
            "unfavorable_pair_count": PAIR_COUNT - favorable - ties,
            "tie_pair_count": ties,
            "leave_one_out": {
                "estimates": leave_one_out,
                "minimum_geomean": min(leave_one_out_values),
                "maximum_geomean": max(leave_one_out_values),
                "all_below_one": all(value < 1.0 for value in leave_one_out_values),
                "maximum_absolute_change_from_full_estimate": max(
                    abs(value - overall) for value in leave_one_out_values
                ),
            },
            "claim_checks": {
                "overall_ci95_upper_below_one": overall_ci_below_one,
                "both_order_stratum_point_estimates_below_one": both_strata_below_one,
                "primary_instructions_gate_passed": (
                    role == "primary" and overall_ci_below_one and both_strata_below_one
                ),
                "metric_specific_speedup_ci_gate_passed": overall_ci_below_one,
            },
        }

    input_hashes = {
        "campaign.json": campaign_sha256,
        "campaign_run.json": sha256_file(RUN_RECORD_PATH),
        "run_blocks.sh": sha256_file(EVIDENCE_DIR / "run_blocks.sh"),
        "analyze.py": sha256_file(Path(__file__).resolve()),
        "activation_disable.patch": sha256_file(EVIDENCE_DIR / "activation_disable.patch"),
        "candidate.patch": sha256_file(EVIDENCE_DIR / "candidate.patch"),
    }
    for name in expected_raw:
        input_hashes[f"raw/{name}"] = sha256_file(RAW_DIR / name)
    for name in expected_logs:
        input_hashes[f"logs/{name}"] = sha256_file(LOG_DIR / name)

    summary = {
        "schema_version": 2,
        "campaign_id": campaign["campaign_id"],
        "comparison": "activation_ablation",
        "source_commit": SOURCE_COMMIT,
        "arm_identities": ARM_IDENTITIES,
        "benchmark_filter": BENCHMARK_FILTER,
        "benchmark_run_name": RUN_NAME,
        "pair_count": PAIR_COUNT,
        "process_count": 2 * PAIR_COUNT,
        "repetitions_per_process": REPETITIONS,
        "pair_order": [
            {"pair": pair, "order": order}
            for pair, order in enumerate(PAIR_ORDERS, start=1)
        ],
        "bootstrap": {
            "method": "stratified_complete_pair_resampling_with_replacement",
            "samples": BOOTSTRAP_SAMPLES,
            "seed": BOOTSTRAP_SEED,
            "confidence_level": 0.95,
            "same_draws_across_metrics": True,
        },
        "metric_roles": METRIC_ROLES,
        "metrics": metric_summaries,
        "pairs": pair_summaries,
        "run_host": run_record["host"],
        "run_started_at": run_record["started_at"],
        "run_finished_at": run_record["finished_at"],
        "input_sha256": input_hashes,
    }
    serialized = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    temporary = SUMMARY_PATH.with_suffix(".json.tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(SUMMARY_PATH)
    except OSError as exc:
        fail(f"cannot write summary {SUMMARY_PATH}: {exc}")
    sys.stdout.write(serialized)


if __name__ == "__main__":
    if sys.argv[1:] == ["--validate-campaign-only"]:
        validate_campaign(load_json(CAMPAIGN_PATH))
        print("frozen campaign validation: PASS")
    elif sys.argv[1:]:
        fail(f"unexpected arguments: {sys.argv[1:]!r}")
    else:
        main()
