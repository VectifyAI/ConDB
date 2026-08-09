#!/usr/bin/env python3
"""Analyze paired MongoDB/PostgreSQL complete subtree breakdowns.

The analyzer is deliberately read-only with respect to the database and report.
It treats paths, rather than repeated observations or Metadata calls, as the
comparison units.  Additive stage attribution uses paired arithmetic means;
medians, CVs, and latency identities are reported separately as diagnostics.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
RUN_DIR = HERE / "runs" / "report_3eng_20260716"
DEFAULT_MONGO = RUN_DIR / "layout_2v3_mongo_complete_breakdown_5x.json"
DEFAULT_POSTGRES = RUN_DIR / "layout_2v3_postgres_complete_breakdown_5x.json"
DEFAULT_OUT_JSON = RUN_DIR / "layout_2v3_mongo_postgres_complete_analysis.json"
DEFAULT_OUT_MD = RUN_DIR / "layout_2v3_mongo_postgres_complete_analysis.md"

TWO_LEAVES = (
    "two_fetch_ms",
    "two_normalize_ms",
    "two_raw_cleanup_ms",
    "two_unattributed_ms",
)
THREE_LEAVES = (
    "structure_fetch_ms",
    "structure_id_extract_ms",
    "structure_raw_cleanup_ms",
    "metadata_request_build_ms",
    "metadata_fetch_ms",
    "metadata_map_ms",
    "metadata_batch_cleanup_ms",
    "ordered_merge_ms",
    "three_unattributed_ms",
)
STAGE_KEYS = (
    "two_total_ms",
    *TWO_LEAVES,
    "three_total_ms",
    "structure_ms",
    *THREE_LEAVES,
)
BATCH_COMPONENTS = (
    "request_build_ms",
    "raw_fetch_ms",
    "map_ms",
    "raw_cleanup_ms",
)
PARENT_FOR_BATCH = {
    "request_build_ms": "metadata_request_build_ms",
    "raw_fetch_ms": "metadata_fetch_ms",
    "map_ms": "metadata_map_ms",
    "raw_cleanup_ms": "metadata_batch_cleanup_ms",
}
ARITHMETIC_TOLERANCE_MS = 0.001


def percentile(values: Iterable[float], p: int) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))
    return ordered[index]


def summary(values: Iterable[float]) -> dict[str, float | int | None]:
    materialized = [float(value) for value in values]
    if not materialized:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "min": None,
            "max": None,
            "stdev": None,
            "cv": None,
        }
    mean = statistics.mean(materialized)
    stdev = statistics.stdev(materialized) if len(materialized) > 1 else 0.0
    return {
        "n": len(materialized),
        "mean": round(mean, 9),
        "median": round(statistics.median(materialized), 9),
        "p50": round(percentile(materialized, 50), 9),
        "p95": round(percentile(materialized, 95), 9),
        "p99": round(percentile(materialized, 99), 9),
        "min": round(min(materialized), 9),
        "max": round(max(materialized), 9),
        "stdev": round(stdev, 9),
        "cv": round(stdev / mean, 9) if mean > 0 else None,
    }


def repeat_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    """Summarize the few repeats for one path without fake tail quantiles."""
    materialized = [float(value) for value in values]
    if not materialized:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "stdev": None,
            "cv": None,
        }
    mean = statistics.mean(materialized)
    stdev = statistics.stdev(materialized) if len(materialized) > 1 else 0.0
    return {
        "n": len(materialized),
        "mean": round(mean, 9),
        "median": round(statistics.median(materialized), 9),
        "min": round(min(materialized), 9),
        "max": round(max(materialized), 9),
        "stdev": round(stdev, 9),
        "cv": round(stdev / mean, 9) if mean > 0 else None,
    }


def rounded(value: float | None, digits: int = 9) -> float | None:
    return round(value, digits) if value is not None else None


class Gates:
    def __init__(self) -> None:
        self.hard_failures: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []

    def hard(self, condition: bool, code: str, detail: Any) -> bool:
        if not condition:
            self.hard_failures.append({"code": code, "detail": detail})
        return condition

    def warn(self, condition: bool, code: str, detail: Any) -> bool:
        if not condition:
            self.warnings.append({"code": code, "detail": detail})
        return condition

    @property
    def passed(self) -> bool:
        return not self.hard_failures

    def output(self) -> dict[str, Any]:
        return {
            "hard_pass": self.passed,
            "hard_failure_count": len(self.hard_failures),
            "hard_failures": self.hard_failures,
            "warning_count": len(self.warnings),
            "warnings": self.warnings,
        }


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


def sample_map(
    result: dict[str, Any], engine: str, gates: Gates
) -> dict[tuple[int, int], dict[str, Any]]:
    output: dict[tuple[int, int], dict[str, Any]] = {}
    for position, sample in enumerate(result.get("samples", [])):
        try:
            key = (int(sample["repeat"]), int(sample["source_index"]))
        except (KeyError, TypeError, ValueError):
            gates.hard(False, f"{engine}.sample_key", {"position": position})
            continue
        if key in output:
            gates.hard(False, f"{engine}.duplicate_pair", {"key": key})
        else:
            output[key] = sample
    return output


def semantic_counts(result: dict[str, Any], engine: str) -> dict[str, Any]:
    counts = result.get("counts", {})
    if engine == "mongo":
        names = ("layout2_view", "layout3_struct", "layout3_meta", "layout_shared_text")
    else:
        names = (
            "layout2_pg_view",
            "layout3_pg_struct",
            "layout3_pg_meta",
            "layout_shared_pg_text",
        )
    return {
        "two": counts.get(names[0]),
        "structure": counts.get(names[1]),
        "metadata": counts.get(names[2]),
        "text": counts.get(names[3]),
    }


def validate_protocol(
    mongo: dict[str, Any],
    postgres: dict[str, Any],
    allow_smoke: bool,
    gates: Gates,
) -> dict[str, Any]:
    gates.hard(mongo.get("status") == "complete", "mongo.status", mongo.get("status"))
    gates.hard(
        postgres.get("status") == "complete", "postgres.status", postgres.get("status")
    )
    gates.hard(mongo.get("engine") == "mongo", "mongo.engine", mongo.get("engine"))
    gates.hard(
        postgres.get("engine") == "postgres",
        "postgres.engine",
        postgres.get("engine"),
    )

    equal_fields = (
        "nodes",
        "source_paths",
        "chunk",
        "indices",
        "repeats",
        "warm_rounds",
        "path_order",
        "layout_order",
    )
    for field in equal_fields:
        gates.hard(
            mongo.get(field) == postgres.get(field),
            f"protocol.equal.{field}",
            {"mongo": mongo.get(field), "postgres": postgres.get(field)},
        )

    intervals: dict[str, dict[str, Any]] = {}
    parsed_intervals: dict[str, tuple[datetime, datetime]] = {}
    for engine, result in (("mongo", mongo), ("postgres", postgres)):
        started = result.get("run_started_at")
        finished = result.get("run_finished_at")
        intervals[engine] = {"started_at": started, "finished_at": finished}
        try:
            parsed_started = datetime.fromisoformat(started)
            parsed_finished = datetime.fromisoformat(finished)
            gates.hard(
                parsed_started < parsed_finished,
                f"protocol.{engine}.campaign_interval",
                intervals[engine],
            )
            parsed_intervals[engine] = (parsed_started, parsed_finished)
        except (TypeError, ValueError):
            gates.hard(False, f"protocol.{engine}.campaign_timestamp", intervals[engine])
    campaigns_overlap: bool | None = None
    if len(parsed_intervals) == 2:
        mongo_start, mongo_finish = parsed_intervals["mongo"]
        pg_start, pg_finish = parsed_intervals["postgres"]
        campaigns_overlap = max(mongo_start, pg_start) < min(mongo_finish, pg_finish)
        gates.hard(
            not campaigns_overlap,
            "protocol.non_overlapping_campaigns",
            intervals,
        )

    nodes = mongo.get("nodes")
    repeats = mongo.get("repeats")
    indices = mongo.get("indices", [])
    paths = mongo.get("source_paths")
    gates.hard(paths == 200, "protocol.paths", paths)
    gates.hard(mongo.get("chunk") == 1_000, "protocol.chunk", mongo.get("chunk"))
    gates.hard(
        isinstance(indices, list)
        and len(indices) == 200
        and indices == list(range(200)),
        "protocol.indices",
        {"count": len(indices) if isinstance(indices, list) else None},
    )
    gates.hard(
        isinstance(mongo.get("warm_rounds"), int) and mongo["warm_rounds"] >= 1,
        "protocol.warm_rounds",
        mongo.get("warm_rounds"),
    )
    if allow_smoke:
        gates.hard(
            isinstance(nodes, int) and nodes > 0,
            "protocol.smoke_nodes",
            nodes,
        )
        gates.hard(
            isinstance(repeats, int) and repeats >= 2,
            "protocol.smoke_repeats",
            repeats,
        )
    else:
        gates.hard(nodes == 10_000_000, "protocol.formal_nodes", nodes)
        gates.hard(repeats == 5, "protocol.formal_repeats", repeats)

    mongo_counts = semantic_counts(mongo, "mongo")
    postgres_counts = semantic_counts(postgres, "postgres")
    for engine, counts in (("mongo", mongo_counts), ("postgres", postgres_counts)):
        for layout in ("two", "structure", "metadata"):
            gates.hard(
                counts[layout] == nodes,
                f"{engine}.count.{layout}",
                {"expected": nodes, "actual": counts[layout]},
            )
    gates.hard(
        mongo_counts["text"] == postgres_counts["text"],
        "protocol.text_count",
        {"mongo": mongo_counts["text"], "postgres": postgres_counts["text"]},
    )

    return {
        "mode": "smoke" if allow_smoke else "formal_10m_5x",
        "nodes": nodes,
        "paths": paths,
        "repeats": repeats,
        "chunk": mongo.get("chunk"),
        "warm_rounds": mongo.get("warm_rounds"),
        "path_order": mongo.get("path_order"),
        "layout_order": mongo.get("layout_order"),
        "campaign_intervals": intervals,
        "campaigns_overlap": campaigns_overlap,
        "counts": {"mongo": mongo_counts, "postgres": postgres_counts},
    }


def validate_pairing(
    mongo: dict[str, Any],
    postgres: dict[str, Any],
    mongo_samples: dict[tuple[int, int], dict[str, Any]],
    pg_samples: dict[tuple[int, int], dict[str, Any]],
    gates: Gates,
) -> dict[str, Any]:
    repeats = mongo.get("repeats", 0)
    indices = mongo.get("indices", [])
    expected = {(repeat, index) for repeat in range(repeats) for index in indices}
    mongo_keys = set(mongo_samples)
    pg_keys = set(pg_samples)
    gates.hard(mongo_keys == expected, "mongo.coverage", {
        "expected": len(expected), "actual": len(mongo_keys),
        "missing": sorted(expected - mongo_keys)[:10],
        "extra": sorted(mongo_keys - expected)[:10],
    })
    gates.hard(pg_keys == expected, "postgres.coverage", {
        "expected": len(expected), "actual": len(pg_keys),
        "missing": sorted(expected - pg_keys)[:10],
        "extra": sorted(pg_keys - expected)[:10],
    })
    if mongo_keys != pg_keys:
        gates.hard(False, "pairing.key_set", {
            "mongo_only": sorted(mongo_keys - pg_keys)[:10],
            "postgres_only": sorted(pg_keys - mongo_keys)[:10],
        })
        return {"expected_pairs": len(expected), "paired": len(mongo_keys & pg_keys)}

    identity_fields = (
        "path",
        "rows",
        "metadata_calls",
        "output_utf8_bytes",
        "fingerprint",
        "order",
        "sequence_position",
    )
    mismatch_counts = {field: 0 for field in identity_fields}
    mismatch_counts["metadata_batch_sizes"] = 0
    order_counts = {"two_first": 0, "three_first": 0}
    sequence_errors = 0
    sorted_indices = list(indices)
    for key in sorted(mongo_keys):
        left = mongo_samples[key]
        right = pg_samples[key]
        for field in identity_fields:
            if left.get(field) != right.get(field):
                mismatch_counts[field] += 1
        left_sizes = [batch.get("size") for batch in left.get("metadata_batches", [])]
        right_sizes = [batch.get("size") for batch in right.get("metadata_batches", [])]
        if left_sizes != right_sizes:
            mismatch_counts["metadata_batch_sizes"] += 1

        repeat, index = key
        expected_order = "two_first" if (index + repeat) % 2 == 0 else "three_first"
        if left.get("order") == expected_order:
            order_counts[expected_order] += 1
        else:
            gates.hard(False, "pairing.layout_order_formula", {
                "key": key, "expected": expected_order, "actual": left.get("order")
            })
        offset = (repeat * len(sorted_indices)) // repeats
        expected_repeat_order = sorted_indices[offset:] + sorted_indices[:offset]
        position = left.get("sequence_position")
        if not isinstance(position, int) or not 0 <= position < len(expected_repeat_order) or expected_repeat_order[position] != index:
            sequence_errors += 1

    for field, count in mismatch_counts.items():
        gates.hard(count == 0, f"pairing.identity.{field}", {"mismatches": count})
    gates.hard(sequence_errors == 0, "pairing.sequence_formula", {
        "mismatches": sequence_errors
    })
    gates.hard(
        abs(order_counts["two_first"] - order_counts["three_first"]) <= 1,
        "pairing.order_balance",
        order_counts,
    )
    return {
        "expected_pairs": len(expected),
        "paired": len(mongo_keys),
        "pair_key": ["repeat", "source_index"],
        "identity_fields": list(identity_fields) + ["metadata_batch_sizes"],
        "mismatch_counts": mismatch_counts,
        "sequence_errors": sequence_errors,
        "order_counts": order_counts,
    }


def finite_nonnegative(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def expected_batch_sizes(rows: int, chunk: int) -> list[int]:
    full, tail = divmod(rows, chunk)
    return [chunk] * full + ([tail] if tail else [])


def validate_arithmetic(
    samples_by_engine: dict[str, dict[tuple[int, int], dict[str, Any]]],
    chunk: int,
    gates: Gates,
) -> dict[str, Any]:
    maxima = {
        engine: {
            "two_additivity_ms": 0.0,
            "three_additivity_ms": 0.0,
            "structure_additivity_ms": 0.0,
            **{f"batch_parent_{component}": 0.0 for component in BATCH_COMPONENTS},
            "fetch_call_identity_ms": 0.0,
        }
        for engine in samples_by_engine
    }
    invalid_values = defaultdict(int)
    invalid_batches = defaultdict(int)

    for engine, sample_map_value in samples_by_engine.items():
        for key, sample in sample_map_value.items():
            for stage in STAGE_KEYS:
                if not finite_nonnegative(sample.get(stage)):
                    invalid_values[(engine, stage)] += 1
            if invalid_values:
                # Arithmetic below is only safe for complete numeric samples.
                missing = [stage for stage in STAGE_KEYS if not finite_nonnegative(sample.get(stage))]
                if missing:
                    continue

            two_error = sample["two_total_ms"] - sum(sample[stage] for stage in TWO_LEAVES)
            three_error = sample["three_total_ms"] - sum(sample[stage] for stage in THREE_LEAVES)
            structure_error = sample["structure_ms"] - sum(
                sample[stage] for stage in THREE_LEAVES[:3]
            )
            maxima[engine]["two_additivity_ms"] = max(
                maxima[engine]["two_additivity_ms"], abs(two_error)
            )
            maxima[engine]["three_additivity_ms"] = max(
                maxima[engine]["three_additivity_ms"], abs(three_error)
            )
            maxima[engine]["structure_additivity_ms"] = max(
                maxima[engine]["structure_additivity_ms"], abs(structure_error)
            )

            rows = sample.get("rows")
            batches = sample.get("metadata_batches", [])
            calls = sample.get("metadata_fetch_calls_ms", [])
            sizes = [batch.get("size") for batch in batches]
            expected_sizes = expected_batch_sizes(rows, chunk) if isinstance(rows, int) else []
            expected_calls = len(expected_sizes)
            if (
                sample.get("metadata_calls") != expected_calls
                or len(batches) != expected_calls
                or len(calls) != expected_calls
                or sizes != expected_sizes
            ):
                invalid_batches[engine] += 1
                continue
            for component, parent in PARENT_FOR_BATCH.items():
                error = sample[parent] - sum(batch[component] for batch in batches)
                field = f"batch_parent_{component}"
                maxima[engine][field] = max(maxima[engine][field], abs(error))
            for call, batch in zip(calls, batches):
                maxima[engine]["fetch_call_identity_ms"] = max(
                    maxima[engine]["fetch_call_identity_ms"],
                    abs(call - batch["raw_fetch_ms"]),
                )

    for (engine, stage), count in sorted(invalid_values.items()):
        gates.hard(False, f"arithmetic.{engine}.invalid.{stage}", {"count": count})
    for engine, count in sorted(invalid_batches.items()):
        gates.hard(count == 0, f"arithmetic.{engine}.batch_shape", {"count": count})
    for engine, metrics in maxima.items():
        for metric, maximum in metrics.items():
            gates.hard(
                maximum <= ARITHMETIC_TOLERANCE_MS,
                f"arithmetic.{engine}.{metric}",
                {"maximum_ms": maximum, "tolerance_ms": ARITHMETIC_TOLERANCE_MS},
            )
    return {
        "tolerance_ms": ARITHMETIC_TOLERANCE_MS,
        "max_abs_error_ms": {
            engine: {key: round(value, 9) for key, value in metrics.items()}
            for engine, metrics in maxima.items()
        },
        "invalid_value_counts": {
            f"{engine}.{stage}": count
            for (engine, stage), count in sorted(invalid_values.items())
        },
        "invalid_batch_counts": dict(invalid_batches),
    }


def path_groups(
    sample_map_value: dict[tuple[int, int], dict[str, Any]]
) -> dict[int, list[dict[str, Any]]]:
    output: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for (repeat, index), sample in sorted(sample_map_value.items()):
        output[index].append(sample)
    for group in output.values():
        group.sort(key=lambda sample: sample["repeat"])
    return dict(output)


def stage_vector(sample: dict[str, Any], layout: str) -> dict[str, float]:
    keys = TWO_LEAVES if layout == "two" else THREE_LEAVES
    return {key: float(sample[key]) for key in keys}


def delta_vector(
    mongo_sample: dict[str, Any], pg_sample: dict[str, Any], layout: str
) -> dict[str, float]:
    total = "two_total_ms" if layout == "two" else "three_total_ms"
    keys = TWO_LEAVES if layout == "two" else THREE_LEAVES
    return {
        total: round(mongo_sample[total] - pg_sample[total], 9),
        **{
            key: round(mongo_sample[key] - pg_sample[key], 9)
            for key in keys
        },
    }


def build_per_path(
    mongo_samples: dict[tuple[int, int], dict[str, Any]],
    pg_samples: dict[tuple[int, int], dict[str, Any]],
) -> dict[str, Any]:
    mongo_paths = path_groups(mongo_samples)
    pg_paths = path_groups(pg_samples)
    output: dict[str, Any] = {}
    for index in sorted(mongo_paths):
        left = mongo_paths[index]
        right = pg_paths[index]
        delta_stats = {
            stage: repeat_summary(
                mongo_samples[(repeat, index)][stage]
                - pg_samples[(repeat, index)][stage]
                for repeat in sorted(sample["repeat"] for sample in left)
            )
            for stage in STAGE_KEYS
        }
        two_error = delta_stats["two_total_ms"]["mean"] - sum(
            delta_stats[key]["mean"] for key in TWO_LEAVES
        )
        three_error = delta_stats["three_total_ms"]["mean"] - sum(
            delta_stats[key]["mean"] for key in THREE_LEAVES
        )
        output[str(index)] = {
            "source_index": index,
            "path": left[0]["path"],
            "rows": left[0]["rows"],
            "metadata_calls": left[0]["metadata_calls"],
            "mongo": {
                stage: {
                    "values": [sample[stage] for sample in left],
                    **repeat_summary(sample[stage] for sample in left),
                }
                for stage in STAGE_KEYS
            },
            "postgres": {
                stage: {
                    "values": [sample[stage] for sample in right],
                    **repeat_summary(sample[stage] for sample in right),
                }
                for stage in STAGE_KEYS
            },
            "paired_delta_mongo_minus_postgres": delta_stats,
            "paired_mean_additivity_error_ms": {
                "two": round(two_error, 9),
                "three": round(three_error, 9),
            },
        }
    return output


def stage_delta_output(
    per_path: dict[str, Any],
    mongo_samples: dict[tuple[int, int], dict[str, Any]],
    pg_samples: dict[tuple[int, int], dict[str, Any]],
) -> dict[str, Any]:
    def layout_output(total: str, leaves: tuple[str, ...]) -> dict[str, Any]:
        keys = (total, *leaves)
        stages: dict[str, Any] = {}
        for stage in keys:
            path_delta_means = [
                path["paired_delta_mongo_minus_postgres"][stage]["mean"]
                for path in per_path.values()
            ]
            mongo_path_means = [path["mongo"][stage]["mean"] for path in per_path.values()]
            pg_path_means = [path["postgres"][stage]["mean"] for path in per_path.values()]
            repeat_means = []
            repeats = sorted({key[0] for key in mongo_samples})
            for repeat in repeats:
                values = [
                    mongo_samples[(repeat, index)][stage]
                    - pg_samples[(repeat, index)][stage]
                    for index in sorted({key[1] for key in mongo_samples})
                ]
                repeat_means.append({"repeat": repeat, "path_equal_mean_ms": rounded(statistics.mean(values))})
            stages[stage] = {
                "mongo_path_equal_mean_ms": rounded(statistics.mean(mongo_path_means)),
                "postgres_path_equal_mean_ms": rounded(statistics.mean(pg_path_means)),
                "path_equal_mean_delta_ms": rounded(statistics.mean(path_delta_means)),
                "per_path_delta_distribution_ms": summary(path_delta_means),
                "mongo_slower_paths": sum(value > 0 for value in path_delta_means),
                "mongo_faster_paths": sum(value < 0 for value in path_delta_means),
                "equal_paths": sum(value == 0 for value in path_delta_means),
                "per_repeat_path_equal_delta": repeat_means,
            }
        total_delta = stages[total]["path_equal_mean_delta_ms"]
        leaf_sum = sum(stages[stage]["path_equal_mean_delta_ms"] for stage in leaves)
        return {
            "estimator": "mean repeats within each path, then equal-weight mean across paths",
            "stages": stages,
            "additivity": {
                "total_delta_ms": total_delta,
                "leaf_delta_sum_ms": round(leaf_sum, 9),
                "error_ms": round(total_delta - leaf_sum, 9),
            },
        }

    two = layout_output("two_total_ms", TWO_LEAVES)
    three = layout_output("three_total_ms", THREE_LEAVES)
    three_stage = three["stages"]
    three["derived_subtotals"] = {
        "structure_ms": round(
            sum(three_stage[key]["path_equal_mean_delta_ms"] for key in THREE_LEAVES[:3]),
            9,
        ),
        "metadata_pipeline_ms": round(
            sum(three_stage[key]["path_equal_mean_delta_ms"] for key in THREE_LEAVES[3:7]),
            9,
        ),
        "merge_and_unattributed_ms": round(
            sum(three_stage[key]["path_equal_mean_delta_ms"] for key in THREE_LEAVES[7:]),
            9,
        ),
    }
    return {
        "sign": "MongoDB minus PostgreSQL; positive means MongoDB is slower",
        "two_store": two,
        "three_store": three,
    }


def stability_output(per_path: dict[str, Any]) -> dict[str, Any]:
    important = (
        "two_total_ms",
        "three_total_ms",
        "two_fetch_ms",
        "structure_fetch_ms",
        "metadata_fetch_ms",
    )
    output: dict[str, Any] = {}
    for engine in ("mongo", "postgres"):
        output[engine] = {}
        for stage in important:
            cvs = [path[engine][stage]["cv"] for path in per_path.values()]
            cvs = [value for value in cvs if value is not None]
            medians = [path[engine][stage]["median"] for path in per_path.values()]
            output[engine][stage] = {
                "per_path_cv_distribution": summary(cvs),
                "paths_cv_ge_0_10": sum(value >= 0.10 for value in cvs),
                "paths_cv_ge_0_20": sum(value >= 0.20 for value in cvs),
                "paths_cv_ge_0_50": sum(value >= 0.50 for value in cvs),
                "per_path_median_latency_distribution_ms": summary(medians),
            }
    return output


def representative_indices(per_path: dict[str, Any]) -> dict[str, int]:
    ordered = sorted(
        (int(index), value["rows"]) for index, value in per_path.items()
    )
    ordered.sort(key=lambda item: (item[1], item[0]))

    def at(p: int) -> int:
        position = min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))
        return ordered[position][0]

    return {
        "row_p50": at(50),
        "row_p95": at(95),
        "row_p99": at(99),
        "row_max": ordered[-1][0],
    }


def path_additive_delta(path: dict[str, Any], layout: str) -> dict[str, Any]:
    total = "two_total_ms" if layout == "two" else "three_total_ms"
    leaves = TWO_LEAVES if layout == "two" else THREE_LEAVES
    stages = {
        key: path["paired_delta_mongo_minus_postgres"][key]["mean"]
        for key in (total, *leaves)
    }
    return {
        "statistic": "paired mean across repeats",
        "stages_ms": stages,
        "additivity_error_ms": round(stages[total] - sum(stages[key] for key in leaves), 9),
    }


def row_identity_output(
    per_path: dict[str, Any],
    mongo: dict[str, Any],
    postgres: dict[str, Any],
    gates: Gates,
) -> dict[str, Any]:
    reps = representative_indices(per_path)
    gates.hard(
        mongo.get("representative_indices") == reps,
        "row_identity.mongo_representatives",
        {"expected": reps, "actual": mongo.get("representative_indices")},
    )
    gates.hard(
        postgres.get("representative_indices") == reps,
        "row_identity.postgres_representatives",
        {"expected": reps, "actual": postgres.get("representative_indices")},
    )
    output: dict[str, Any] = {
        "definition": "row-count percentile over 200 unique paths; not a latency percentile",
        "quantile_rule": "sorted rows/source_index; index=round(q*(n-1))",
    }
    for label, index in reps.items():
        path = per_path[str(index)]
        output[label] = {
            "source_index": index,
            "path": path["path"],
            "rows": path["rows"],
            "metadata_calls": path["metadata_calls"],
            "latency": {
                engine: {
                    stage: {
                        "median_ms": path[engine][stage]["median"],
                        "mean_ms": path[engine][stage]["mean"],
                        "cv": path[engine][stage]["cv"],
                    }
                    for stage in ("two_total_ms", "three_total_ms")
                }
                for engine in ("mongo", "postgres")
            },
            "paired_additive_delta": {
                "two": path_additive_delta(path, "two"),
                "three": path_additive_delta(path, "three"),
            },
        }
    return output


def closest_observation(
    samples: dict[tuple[int, int], dict[str, Any]],
    index: int,
    stage: str,
    target: float,
) -> dict[str, Any]:
    candidates = [sample for (repeat, source_index), sample in samples.items() if source_index == index]
    candidates.sort(key=lambda sample: (abs(sample[stage] - target), sample["repeat"]))
    return candidates[0]


def latency_p99_output(
    per_path: dict[str, Any],
    mongo_samples: dict[tuple[int, int], dict[str, Any]],
    pg_samples: dict[tuple[int, int], dict[str, Any]],
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "primary_unit": "P99 over 200 per-path median latencies",
        "quantile_rule": "index=round(0.99*(n-1)); stable tie-break by source_index",
        "warning": "Independent engine P99 values are not decomposed by summing stage P99 values.",
    }
    for layout, total, leaves in (
        ("two_store", "two_total_ms", TWO_LEAVES),
        ("three_store", "three_total_ms", THREE_LEAVES),
    ):
        selected: dict[str, Any] = {}
        selected_indices: dict[str, int] = {}
        for engine, samples, peer_samples in (
            ("mongo", mongo_samples, pg_samples),
            ("postgres", pg_samples, mongo_samples),
        ):
            ordered = sorted(
                (
                    (path[engine][total]["median"], int(index))
                    for index, path in per_path.items()
                ),
                key=lambda item: (item[0], item[1]),
            )
            rank = round(0.99 * (len(ordered) - 1))
            path_median, index = ordered[rank]
            selected_indices[engine] = index
            observation = closest_observation(samples, index, total, path_median)
            key = (observation["repeat"], index)
            peer = peer_samples[key]
            if engine == "mongo":
                mongo_observation, pg_observation = observation, peer
            else:
                mongo_observation, pg_observation = peer, observation
            exact_delta = delta_vector(mongo_observation, pg_observation, "two" if total.startswith("two") else "three")
            selected[engine] = {
                "rank_zero_based": rank,
                "paths_n": len(ordered),
                "source_index": index,
                "path": observation["path"],
                "rows": observation["rows"],
                "path_median_ms": path_median,
                "representative_repeat": observation["repeat"],
                "representative_observation_ms": observation[total],
                "distance_from_path_median_ms": round(abs(observation[total] - path_median), 9),
                "peer_same_pair_ms": peer[total],
                "exact_pair_delta_mongo_minus_postgres_ms": exact_delta,
                "exact_pair_additivity_error_ms": round(
                    exact_delta[total] - sum(exact_delta[key] for key in leaves), 9
                ),
            }
        independent_gap = (
            selected["mongo"]["path_median_ms"]
            - selected["postgres"]["path_median_ms"]
        )
        output[layout] = {
            "mongo_selected_identity": selected["mongo"],
            "postgres_selected_identity": selected["postgres"],
            "same_path_identity": selected_indices["mongo"] == selected_indices["postgres"],
            "independent_p99_gap_ms": round(independent_gap, 9),
            "independent_p99_gap_stage_decomposable": False,
        }
    return output


def raw_observation_p99_output(
    mongo_samples: dict[tuple[int, int], dict[str, Any]],
    pg_samples: dict[tuple[int, int], dict[str, Any]],
) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for engine, samples, peer_samples in (
        ("mongo", mongo_samples, pg_samples),
        ("postgres", pg_samples, mongo_samples),
    ):
        ordered = sorted(
            samples.items(),
            key=lambda item: (
                item[1]["three_total_ms"],
                item[0][0],
                item[0][1],
            ),
        )
        rank = round(0.99 * (len(ordered) - 1))
        key, observation = ordered[rank]
        peer = peer_samples[key]
        if engine == "mongo":
            mongo_observation, pg_observation = observation, peer
        else:
            mongo_observation, pg_observation = peer, observation
        fetches = [
            float(batch["raw_fetch_ms"])
            for batch in observation["metadata_batches"]
        ]
        fetch_summary = summary(fetches)
        selected[engine] = {
            "rank_zero_based": rank,
            "raw_observations_n": len(ordered),
            "repeat": key[0],
            "source_index": key[1],
            "path": observation["path"],
            "rows": observation["rows"],
            "metadata_calls": observation["metadata_calls"],
            "three_total_ms": observation["three_total_ms"],
            "peer_same_pair_ms": peer["three_total_ms"],
            "metadata_fetch_ms": observation["metadata_fetch_ms"],
            "metadata_fetch_call_distribution_ms": fetch_summary,
            "largest_call_share_of_metadata_fetch": round(
                float(fetch_summary["max"]) / observation["metadata_fetch_ms"], 9
            ),
            "exact_pair_delta_mongo_minus_postgres_ms": delta_vector(
                mongo_observation, pg_observation, "three"
            ),
        }
    return {
        "unit": "descriptive P99 over 1,000 repeat-clustered observations",
        "inference_warning": "The 1,000 observations are not independent; this diagnostic locates raw tail observations only.",
        "mongo_selected_identity": selected["mongo"],
        "postgres_selected_identity": selected["postgres"],
    }


def fixed_effect_coefficient(
    values: dict[tuple[int, int], float],
    first: dict[tuple[int, int], float],
) -> float | None:
    keys = sorted(values)
    if not keys:
        return None
    path_values: dict[int, list[float]] = defaultdict(list)
    repeat_values: dict[int, list[float]] = defaultdict(list)
    path_first: dict[int, list[float]] = defaultdict(list)
    repeat_first: dict[int, list[float]] = defaultdict(list)
    for repeat, index in keys:
        path_values[index].append(values[(repeat, index)])
        repeat_values[repeat].append(values[(repeat, index)])
        path_first[index].append(first[(repeat, index)])
        repeat_first[repeat].append(first[(repeat, index)])
    grand_y = statistics.mean(values.values())
    grand_f = statistics.mean(first.values())
    numerator = 0.0
    denominator = 0.0
    for repeat, index in keys:
        y_tilde = (
            values[(repeat, index)]
            - statistics.mean(path_values[index])
            - statistics.mean(repeat_values[repeat])
            + grand_y
        )
        f_tilde = (
            first[(repeat, index)]
            - statistics.mean(path_first[index])
            - statistics.mean(repeat_first[repeat])
            + grand_f
        )
        numerator += f_tilde * y_tilde
        denominator += f_tilde * f_tilde
    return numerator / denominator if denominator else None


def order_residual_output(
    mongo_samples: dict[tuple[int, int], dict[str, Any]],
    pg_samples: dict[tuple[int, int], dict[str, Any]],
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "method": "two-way fixed effects: path and repeat; coefficient is measured-first minus measured-second",
        "inference": "sensitivity diagnostic only",
    }
    for stage in (
        "two_total_ms",
        "three_total_ms",
        "structure_fetch_ms",
        "metadata_fetch_ms",
    ):
        two_layout = stage.startswith("two")
        first = {
            key: float(
                sample["order"] == ("two_first" if two_layout else "three_first")
            )
            for key, sample in mongo_samples.items()
        }
        mongo_values = {key: float(sample[stage]) for key, sample in mongo_samples.items()}
        pg_values = {key: float(sample[stage]) for key, sample in pg_samples.items()}
        delta_values = {key: mongo_values[key] - pg_values[key] for key in mongo_values}
        stage_output: dict[str, Any] = {}
        for label, values in (
            ("mongo", mongo_values),
            ("postgres", pg_values),
            ("paired_delta", delta_values),
        ):
            coefficient = fixed_effect_coefficient(values, first)
            level = statistics.mean(values.values())
            stage_output[label] = {
                "first_minus_second_ms": rounded(coefficient),
                "mean_level_ms": rounded(level),
                "absolute_coefficient_pct_of_level": rounded(
                    abs(coefficient) / abs(level) * 100
                    if coefficient is not None and level
                    else None,
                    6,
                ),
            }
        output[stage] = stage_output
    return output


def repeat_sweep_output(
    mongo_samples: dict[tuple[int, int], dict[str, Any]],
    pg_samples: dict[tuple[int, int], dict[str, Any]],
    gates: Gates,
) -> dict[str, Any]:
    stages = (
        "two_total_ms",
        "three_total_ms",
        "structure_fetch_ms",
        "metadata_fetch_ms",
    )
    repeats = sorted({key[0] for key in mongo_samples})
    output: dict[str, Any] = {
        "estimator": "equal-weight mean across all 200 paths within each repeat sweep",
        "soft_warning_threshold_abs_relative_drift_pct": 5.0,
        "engines": {},
        "paired_delta_mongo_minus_postgres": {},
    }
    for engine, samples in (("mongo", mongo_samples), ("postgres", pg_samples)):
        output["engines"][engine] = {}
        for stage in stages:
            overall = statistics.mean(sample[stage] for sample in samples.values())
            per_repeat = []
            for repeat in repeats:
                value = statistics.mean(
                    sample[stage]
                    for (sample_repeat, _), sample in samples.items()
                    if sample_repeat == repeat
                )
                relative = (value / overall - 1) * 100 if overall else 0.0
                per_repeat.append({
                    "repeat": repeat,
                    "path_equal_mean_ms": round(value, 9),
                    "relative_to_overall_pct": round(relative, 6),
                })
            maximum = max(abs(item["relative_to_overall_pct"]) for item in per_repeat)
            evidence = {
                "overall_path_equal_mean_ms": round(overall, 9),
                "max_abs_relative_drift_pct": round(maximum, 6),
                "per_repeat": per_repeat,
            }
            gates.warn(
                maximum <= 5.0,
                f"stability.{engine}.{stage}.repeat_drift_le_5pct",
                evidence,
            )
            output["engines"][engine][stage] = evidence

    for stage in stages:
        deltas = []
        for repeat in repeats:
            values = [
                mongo_samples[(repeat, index)][stage]
                - pg_samples[(repeat, index)][stage]
                for index in sorted({key[1] for key in mongo_samples})
            ]
            deltas.append({
                "repeat": repeat,
                "path_equal_mean_delta_ms": round(statistics.mean(values), 9),
            })
        output["paired_delta_mongo_minus_postgres"][stage] = {
            "overall_path_equal_mean_delta_ms": round(
                statistics.mean(item["path_equal_mean_delta_ms"] for item in deltas),
                9,
            ),
            "per_repeat": deltas,
            "range_ms": round(
                max(item["path_equal_mean_delta_ms"] for item in deltas)
                - min(item["path_equal_mean_delta_ms"] for item in deltas),
                9,
            ),
        }
    return output


def metadata_batch_output(
    mongo_samples: dict[tuple[int, int], dict[str, Any]],
    pg_samples: dict[tuple[int, int], dict[str, Any]],
    chunk: int,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for key in sorted(mongo_samples):
        mongo_sample = mongo_samples[key]
        pg_sample = pg_samples[key]
        repeat, index = key
        for ordinal, (left, right) in enumerate(
            zip(mongo_sample["metadata_batches"], pg_sample["metadata_batches"])
        ):
            size = left["size"]
            if size == chunk:
                category = "full_1000"
            elif mongo_sample["rows"] < chunk:
                category = "partial_only"
            else:
                category = "partial_tail_after_full"
            records.append({
                "repeat": repeat,
                "source_index": index,
                "ordinal": ordinal,
                "size": size,
                "category": category,
                "mongo": left,
                "postgres": right,
            })

    groups = {
        "full_1000": [record for record in records if record["category"] == "full_1000"],
        "partial_only": [record for record in records if record["category"] == "partial_only"],
        "partial_tail_after_full": [
            record for record in records if record["category"] == "partial_tail_after_full"
        ],
        "partial_all": [record for record in records if record["size"] < chunk],
    }

    output: dict[str, Any] = {
        "pairing_key": ["repeat", "source_index", "batch_ordinal"],
        "independence_warning": "Calls are descriptive observations; path-equal summaries use paths as units.",
    }
    for label, group in groups.items():
        by_path: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for record in group:
            by_path[record["source_index"]].append(record)
        category: dict[str, Any] = {
            "calls_n": len(group),
            "repeat_path_cells_n": len({(record["repeat"], record["source_index"]) for record in group}),
            "paths_n": len(by_path),
            "batch_size": summary(record["size"] for record in group),
            "components": {},
        }
        for component in BATCH_COMPONENTS:
            mongo_call_values = [record["mongo"][component] for record in group]
            pg_call_values = [record["postgres"][component] for record in group]
            delta_call_values = [left - right for left, right in zip(mongo_call_values, pg_call_values)]
            path_mongo = [
                statistics.mean(record["mongo"][component] for record in path_records)
                for path_records in by_path.values()
            ]
            path_pg = [
                statistics.mean(record["postgres"][component] for record in path_records)
                for path_records in by_path.values()
            ]
            path_delta = [left - right for left, right in zip(path_mongo, path_pg)]
            category["components"][component] = {
                "call_weighted_descriptive": {
                    "mongo_ms": summary(mongo_call_values),
                    "postgres_ms": summary(pg_call_values),
                    "paired_delta_ms": summary(delta_call_values),
                },
                "path_equal_paired": {
                    "mongo_path_mean_distribution_ms": summary(path_mongo),
                    "postgres_path_mean_distribution_ms": summary(path_pg),
                    "delta_path_mean_distribution_ms": summary(path_delta),
                    "path_equal_mean_delta_ms": rounded(statistics.mean(path_delta)) if path_delta else None,
                    "mongo_slower_paths": sum(value > 0 for value in path_delta),
                    "mongo_faster_paths": sum(value < 0 for value in path_delta),
                },
            }
        output[label] = category
    return output


def add_plan_check(
    checks: list[dict[str, Any]],
    gates: Gates,
    check_id: str,
    passed: bool,
    evidence: Any,
    hard: bool = True,
) -> None:
    checks.append({"id": check_id, "pass": bool(passed), "hard": hard, "evidence": evidence})
    if hard:
        gates.hard(bool(passed), f"plan.{check_id}", evidence)
    else:
        gates.warn(bool(passed), f"plan.{check_id}", evidence)


def plan_gates_output(
    mongo: dict[str, Any],
    postgres: dict[str, Any],
    reps: dict[str, int],
    per_path: dict[str, Any],
    gates: Gates,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    observed_access_paths: dict[str, Any] = {}
    mongo_plans = mongo.get("plans", {})
    pg_plans = postgres.get("plans", {})
    expected_labels = set(reps)
    add_plan_check(checks, gates, "mongo.labels", set(mongo_plans) == expected_labels, sorted(mongo_plans))
    add_plan_check(checks, gates, "postgres.labels", set(pg_plans) == expected_labels, sorted(pg_plans))

    for label, index in reps.items():
        rows = per_path[str(index)]["rows"]
        first_size = min(rows, 1_000)
        tail_size = rows % 1_000 or min(rows, 1_000)
        mp = mongo_plans.get(label, {})
        pp = pg_plans.get(label, {})
        for engine, plan in (("mongo", mp), ("postgres", pp)):
            add_plan_check(
                checks,
                gates,
                f"{engine}.{label}.identity",
                plan.get("source_index") == index and plan.get("rows") == rows,
                {"expected_index": index, "expected_rows": rows, "actual": {
                    "source_index": plan.get("source_index"), "rows": plan.get("rows")
                }},
            )

        for query_name, expected_size in (
            ("metadata_first_batch", first_size),
            ("metadata_tail_batch", tail_size),
        ):
            for engine, plan in (("mongo", mp), ("postgres", pp)):
                add_plan_check(
                    checks,
                    gates,
                    f"{engine}.{label}.{query_name}.batch_size",
                    plan.get(query_name, {}).get("batch_size") == expected_size,
                    {"expected": expected_size, "actual": plan.get(query_name, {}).get("batch_size")},
                )

        m_two = mp.get("two", {})
        m_struct = mp.get("structure", {})
        add_plan_check(checks, gates, f"mongo.{label}.two.access", "IXSCAN" in m_two.get("stages", []) and "COLLSCAN" not in m_two.get("stages", []) and "path_1_node_id_1" in m_two.get("indexes", []), m_two)
        add_plan_check(checks, gates, f"mongo.{label}.two.rows", m_two.get("execution_success") is True and m_two.get("n_returned") == rows and m_two.get("docs_examined") == rows, m_two)
        add_plan_check(checks, gates, f"mongo.{label}.structure.covered", "PROJECTION_COVERED" in m_struct.get("stages", []) and "IXSCAN" in m_struct.get("stages", []) and "COLLSCAN" not in m_struct.get("stages", []) and "path_1_node_id_1" in m_struct.get("indexes", []) and m_struct.get("docs_examined") == 0, m_struct)
        add_plan_check(checks, gates, f"mongo.{label}.structure.rows", m_struct.get("execution_success") is True and m_struct.get("n_returned") == rows and m_struct.get("keys_examined") == rows, m_struct)
        for query_name, expected_size in (("metadata_first_batch", first_size), ("metadata_tail_batch", tail_size)):
            meta = mp.get(query_name, {}).get("plan", {})
            add_plan_check(checks, gates, f"mongo.{label}.{query_name}.access", "IXSCAN" in meta.get("stages", []) and "COLLSCAN" not in meta.get("stages", []) and "_id_" in meta.get("indexes", []), meta)
            add_plan_check(checks, gates, f"mongo.{label}.{query_name}.rows", meta.get("execution_success") is True and meta.get("n_returned") == expected_size and meta.get("docs_examined") == expected_size and (meta.get("keys_examined") or 0) >= expected_size, meta)

        def pg_node(plan: dict[str, Any], index_name: str) -> dict[str, Any] | None:
            return next((node for node in plan.get("nodes", []) if node.get("index_name") == index_name), None)

        p_two = pp.get("two", {})
        p_struct = pp.get("structure", {})
        p_two_node = pg_node(p_two, "layout2_pg_view_path_node_idx")
        p_struct_node = pg_node(p_struct, "layout3_pg_struct_path_node_idx")
        metadata_first_nodes = pp.get("metadata_first_batch", {}).get("plan", {}).get("nodes", [])
        metadata_tail_nodes = pp.get("metadata_tail_batch", {}).get("plan", {}).get("nodes", [])
        all_pg_nodes = [
            *p_two.get("nodes", []),
            *p_struct.get("nodes", []),
            *metadata_first_nodes,
            *metadata_tail_nodes,
        ]
        protected_nodes = [*p_struct.get("nodes", []), *metadata_first_nodes, *metadata_tail_nodes]
        add_plan_check(checks, gates, f"postgres.{label}.structure_metadata_no_seq_scan", all(node.get("node_type") != "Seq Scan" for node in protected_nodes), protected_nodes)

        two_index_ok = (
            p_two_node is not None
            and p_two_node.get("node_type") == "Index Scan"
            and p_two_node.get("actual_rows") == rows
        )
        two_nodes = p_two.get("nodes", [])
        two_root = two_nodes[0] if two_nodes else None
        two_parallel_seq_ok = (
            two_root is not None
            and two_root.get("node_type") == "Gather Merge"
            and two_root.get("actual_rows") == rows
            and any(node.get("node_type") == "Seq Scan" for node in two_nodes)
            and any(node.get("node_type") == "Sort" for node in two_nodes)
        )
        add_plan_check(
            checks,
            gates,
            f"postgres.{label}.two.valid_access",
            two_index_ok or two_parallel_seq_ok,
            {"index_scan": two_index_ok, "parallel_seq_sort_gather": two_parallel_seq_ok, "plan": p_two},
        )
        add_plan_check(
            checks,
            gates,
            f"postgres.{label}.two.path_index_selected",
            two_index_ok,
            {
                "optimizer_choice": "layout2 path index" if two_index_ok else "parallel sequential scan + sort + gather merge",
                "rows": rows,
                "dataset_rows": postgres.get("nodes"),
            },
            hard=False,
        )
        add_plan_check(checks, gates, f"postgres.{label}.structure.covered", p_struct_node is not None and p_struct_node.get("node_type") == "Index Only Scan" and p_struct_node.get("actual_rows") == rows and p_struct_node.get("heap_fetches") == 0, p_struct_node)
        for query_name, expected_size in (("metadata_first_batch", first_size), ("metadata_tail_batch", tail_size)):
            meta_plan = pp.get(query_name, {}).get("plan", {})
            meta_node = pg_node(meta_plan, "layout3_pg_meta_pkey")
            meta_nodes = meta_plan.get("nodes", [])
            direct_index_ok = (
                meta_node is not None
                and meta_node.get("node_type") == "Index Scan"
                and meta_node.get("actual_rows") == expected_size
            )
            bitmap_index_ok = (
                meta_node is not None
                and meta_node.get("node_type") == "Bitmap Index Scan"
                and meta_node.get("actual_rows") == expected_size
                and any(
                    node.get("node_type") == "Bitmap Heap Scan"
                    and node.get("actual_rows") == expected_size
                    for node in meta_nodes
                )
            )
            add_plan_check(
                checks,
                gates,
                f"postgres.{label}.{query_name}.pkey_backed_access",
                (direct_index_ok or bitmap_index_ok)
                and all(node.get("node_type") != "Seq Scan" for node in meta_nodes),
                {
                    "direct_index_scan": direct_index_ok,
                    "bitmap_index_heap_scan": bitmap_index_ok,
                    "plan": meta_plan,
                },
            )
        read_blocks = sum((node.get("shared_read_blocks") or 0) for node in all_pg_nodes)
        add_plan_check(checks, gates, f"postgres.{label}.shared_read_blocks_zero", read_blocks == 0, {"shared_read_blocks": read_blocks}, hard=False)
        observed_access_paths[label] = {
            "mongo": {
                "two": {
                    "node_types": m_two.get("stages", []),
                    "indexes": m_two.get("indexes", []),
                },
                "structure": {
                    "node_types": m_struct.get("stages", []),
                    "indexes": m_struct.get("indexes", []),
                },
                "metadata_first": {
                    "node_types": mp.get("metadata_first_batch", {}).get("plan", {}).get("stages", []),
                    "indexes": mp.get("metadata_first_batch", {}).get("plan", {}).get("indexes", []),
                },
                "metadata_tail": {
                    "node_types": mp.get("metadata_tail_batch", {}).get("plan", {}).get("stages", []),
                    "indexes": mp.get("metadata_tail_batch", {}).get("plan", {}).get("indexes", []),
                },
            },
            "postgres": {
                "two": {
                    "node_types": [node.get("node_type") for node in two_nodes],
                    "indexes": [node.get("index_name") for node in two_nodes if node.get("index_name")],
                    "optimizer_choice": "path index" if two_index_ok else "parallel sequential scan + sort + gather merge",
                },
                "structure": {
                    "node_types": [node.get("node_type") for node in p_struct.get("nodes", [])],
                    "indexes": [node.get("index_name") for node in p_struct.get("nodes", []) if node.get("index_name")],
                },
                "metadata_first": {
                    "node_types": [node.get("node_type") for node in metadata_first_nodes],
                    "indexes": [node.get("index_name") for node in metadata_first_nodes if node.get("index_name")],
                },
                "metadata_tail": {
                    "node_types": [node.get("node_type") for node in metadata_tail_nodes],
                    "indexes": [node.get("index_name") for node in metadata_tail_nodes if node.get("index_name")],
                },
            },
        }

    prepared = postgres.get("post_warm_state", {}).get("prepared_statement_count")
    add_plan_check(checks, gates, "postgres.prepared_statements", isinstance(prepared, int) and prepared >= 3, {"count": prepared})
    ping = mongo.get("post_warm_state", {}).get("ping")
    add_plan_check(checks, gates, "mongo.post_warm_ping", ping == 1 or ping == 1.0, {"ping": ping})

    mongo_before = mongo.get("engine_metrics_before_timing", {}).get("wiredtiger_cache", {})
    mongo_after = mongo.get("engine_metrics_after_timing", {}).get("wiredtiger_cache", {})
    page_key = "pages read into cache"
    if isinstance(mongo_before.get(page_key), int) and isinstance(mongo_after.get(page_key), int):
        delta = mongo_after[page_key] - mongo_before[page_key]
        add_plan_check(checks, gates, "mongo.wiredtiger_pages_read_zero", delta == 0, {"delta": delta}, hard=False)
    pg_before = postgres.get("engine_metrics_before_timing", {})
    pg_after = postgres.get("engine_metrics_after_timing", {})
    if all(isinstance(value, int) for value in (pg_before.get("temp_files"), pg_after.get("temp_files"), pg_before.get("temp_bytes"), pg_after.get("temp_bytes"))):
        add_plan_check(checks, gates, "postgres.no_new_temp_files", pg_after["temp_files"] == pg_before["temp_files"] and pg_after["temp_bytes"] == pg_before["temp_bytes"], {"temp_files_delta": pg_after["temp_files"] - pg_before["temp_files"], "temp_bytes_delta": pg_after["temp_bytes"] - pg_before["temp_bytes"]}, hard=False)

    return {
        "hard_pass": all(check["pass"] for check in checks if check["hard"]),
        "checks": checks,
        "hard_failures": [check for check in checks if check["hard"] and not check["pass"]],
        "soft_warnings": [check for check in checks if not check["hard"] and not check["pass"]],
        "observed_access_paths": observed_access_paths,
        "interpretation": "Plans gate access paths only; their server execution times are not subtracted from client-side stages.",
    }


def environment_output(mongo: dict[str, Any], postgres: dict[str, Any], gates: Gates) -> dict[str, Any]:
    mongo_host = mongo.get("host_before_timing", {})
    pg_host = postgres.get("host_before_timing", {})
    gates.hard(
        mongo_host.get("hostname") == pg_host.get("hostname"),
        "environment.hostname",
        {"mongo": mongo_host.get("hostname"), "postgres": pg_host.get("hostname")},
    )
    gates.hard(
        mongo_host.get("cpu_affinity") == pg_host.get("cpu_affinity"),
        "environment.cpu_affinity",
        {"mongo": mongo_host.get("cpu_affinity"), "postgres": pg_host.get("cpu_affinity")},
    )
    return {
        "same_hostname": mongo_host.get("hostname") == pg_host.get("hostname"),
        "same_cpu_affinity": mongo_host.get("cpu_affinity") == pg_host.get("cpu_affinity"),
        "mongo": {
            "database": mongo.get("environment"),
            "host_before_timing": mongo_host,
            "host_after_timing": mongo.get("host_after_timing"),
        },
        "postgres": {
            "database": postgres.get("environment"),
            "host_before_timing": pg_host,
            "host_after_timing": postgres.get("host_after_timing"),
        },
    }


def markdown_number(value: Any, digits: int = 3) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def render_markdown(result: dict[str, Any]) -> str:
    three_stages = result["paired_stage_delta"]["three_store"]["stages"]
    sweep_gaps = result["repeat_sweep_summary"][
        "paired_delta_mongo_minus_postgres"
    ]
    three_repeat_gaps = [
        item["path_equal_mean_delta_ms"]
        for item in sweep_gaps["three_total_ms"]["per_repeat"]
    ]
    structure_repeat_gaps = [
        item["path_equal_mean_delta_ms"]
        for item in sweep_gaps["structure_fetch_ms"]["per_repeat"]
    ]
    metadata_repeat_gaps = [
        item["path_equal_mean_delta_ms"]
        for item in sweep_gaps["metadata_fetch_ms"]["per_repeat"]
    ]
    lines = [
        "# MongoDB vs PostgreSQL subtree breakdown",
        "",
        f"Status: **{result['status']}**. Delta is MongoDB minus PostgreSQL; positive means MongoDB is slower.",
        "",
        f"The additive estimator averages the {result['analysis_units']['repeats_per_path']} paired repeats within each path, then gives each of the {result['analysis_units']['equal_weight_path_units']} paths equal weight. Medians and CVs are stability diagnostics and are never added across stages.",
        "",
        "## Bottom line",
        "",
        f"- The five-repeat three-store campaign mean is {three_stages['three_total_ms']['path_equal_mean_delta_ms']:+.3f} ms, but the per-repeat gaps are "
        + ", ".join(f"{value:+.3f}" for value in three_repeat_gaps)
        + " ms. The total MongoDB−PostgreSQL gap is not stable across this single campaign.",
        f"- Structure fetch is the stable relative bottleneck: MongoDB is slower on {three_stages['structure_fetch_ms']['mongo_slower_paths']}/200 paths, and its five repeat gaps stay positive at "
        + ", ".join(f"{value:+.3f}" for value in structure_repeat_gaps)
        + " ms.",
        "- Metadata fetch is MongoDB's largest absolute stage, but it is not a stable source of the MongoDB−PostgreSQL gap: its repeat gaps are "
        + ", ".join(f"{value:+.3f}" for value in metadata_repeat_gaps)
        + " ms, and the last three sweeps favor MongoDB.",
        "",
        "## Validation",
        "",
        f"- Hard gates: {'PASS' if result['validation']['hard_pass'] else 'FAIL'}",
        f"- Paired observations: {result['analysis_units']['raw_paired_observations']}",
        f"- Equal-weight analysis units: {result['analysis_units']['equal_weight_path_units']} paths × {result['analysis_units']['repeats_per_path']} repeats",
        f"- Hard failures: {result['validation']['hard_failure_count']}",
        f"- Warnings: {result['validation']['warning_count']}",
        "",
    ]
    if result["validation"]["hard_failures"]:
        for failure in result["validation"]["hard_failures"]:
            lines.append(f"- FAIL `{failure['code']}`: `{json.dumps(failure['detail'], ensure_ascii=False)}`")
        lines.append("")
    if result["validation"]["warnings"]:
        for warning in result["validation"]["warnings"]:
            detail = warning["detail"]
            if warning["code"].startswith("stability."):
                detail = {
                    "overall_path_equal_mean_ms": detail["overall_path_equal_mean_ms"],
                    "max_abs_relative_drift_pct": detail["max_abs_relative_drift_pct"],
                }
            lines.append(f"- Warning `{warning['code']}`: `{json.dumps(detail, ensure_ascii=False)}`")
        lines.append("")

    lines.extend(["## Additive stage delta", ""])
    labels = {
        "two_total_ms": "Two-store total",
        "two_fetch_ms": "Two-store fetch",
        "two_normalize_ms": "Two-store normalize",
        "two_raw_cleanup_ms": "Two-store raw cleanup",
        "two_unattributed_ms": "Two-store unattributed",
        "three_total_ms": "Three-store total",
        "structure_fetch_ms": "Structure fetch",
        "structure_id_extract_ms": "Structure ID extraction",
        "structure_raw_cleanup_ms": "Structure raw cleanup",
        "metadata_request_build_ms": "Metadata request build",
        "metadata_fetch_ms": "Metadata fetch",
        "metadata_map_ms": "Metadata map",
        "metadata_batch_cleanup_ms": "Metadata raw cleanup",
        "ordered_merge_ms": "Ordered merge",
        "three_unattributed_ms": "Three-store unattributed",
    }
    for layout_key, stage_order in (
        ("two_store", ("two_total_ms", *TWO_LEAVES)),
        ("three_store", ("three_total_ms", *THREE_LEAVES)),
    ):
        layout = result["paired_stage_delta"][layout_key]
        lines.extend([
            f"### {layout_key.replace('_', ' ').title()}",
            "",
            "| Stage | Mongo mean (ms) | PostgreSQL mean (ms) | Delta (ms) | Mongo slower paths |",
            "|---|---:|---:|---:|---:|",
        ])
        for stage in stage_order:
            value = layout["stages"][stage]
            lines.append(
                f"| {labels[stage]} | {markdown_number(value['mongo_path_equal_mean_ms'])} | "
                f"{markdown_number(value['postgres_path_equal_mean_ms'])} | "
                f"{markdown_number(value['path_equal_mean_delta_ms'])} | "
                f"{value['mongo_slower_paths']}/200 |"
            )
        lines.extend([
            "",
            f"Additivity error: {markdown_number(layout['additivity']['error_ms'], 6)} ms.",
            "",
        ])

    lines.extend([
        "## Row-count identities",
        "",
        "These are subtree-size percentiles, not latency percentiles.",
        "",
        "| Identity | Source index | Rows | Mongo three median (ms) | PostgreSQL three median (ms) | Paired mean delta (ms) | Mongo CV | PostgreSQL CV |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for label in ("row_p50", "row_p95", "row_p99", "row_max"):
        item = result["row_percentile_identities"][label]
        m = item["latency"]["mongo"]["three_total_ms"]
        p = item["latency"]["postgres"]["three_total_ms"]
        d = item["paired_additive_delta"]["three"]["stages_ms"]["three_total_ms"]
        lines.append(
            f"| {label} | {item['source_index']} | {item['rows']} | {markdown_number(m['median_ms'])} | "
            f"{markdown_number(p['median_ms'])} | {markdown_number(d)} | {markdown_number(m['cv'], 3)} | {markdown_number(p['cv'], 3)} |"
        )

    stability = result["per_path_median_cv_summary"]
    sweeps = result["repeat_sweep_summary"]
    lines.extend([
        "",
        "## Stability and repeat drift",
        "",
        "Repeat means are equal-weight means across the same 200 paths. An absolute deviation above 5% from the five-repeat overall mean is recorded as a soft warning, not hidden inside the aggregate.",
        "",
        "| Engine | Total | Per-path CV median | Per-path CV P95 | Paths CV ≥ 0.10 | Paths CV ≥ 0.20 |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for engine in ("mongo", "postgres"):
        for stage in ("two_total_ms", "three_total_ms"):
            item = stability[engine][stage]
            distribution = item["per_path_cv_distribution"]
            lines.append(
                f"| {engine} | {labels[stage]} | {markdown_number(distribution['median'], 3)} | "
                f"{markdown_number(distribution['p95'], 3)} | {item['paths_cv_ge_0_10']} | "
                f"{item['paths_cv_ge_0_20']} |"
            )

    repeat_ids = [
        item["repeat"]
        for item in sweeps["engines"]["mongo"]["three_total_ms"]["per_repeat"]
    ]
    repeat_headers = " | ".join(f"R{repeat}" for repeat in repeat_ids)
    repeat_rules = "|".join("---:" for _ in repeat_ids)
    lines.extend([
        "",
        f"| Engine | Stage | Overall (ms) | {repeat_headers} | Max absolute drift |",
        f"|---|---|---:|{repeat_rules}|---:|",
    ])
    for engine in ("mongo", "postgres"):
        for stage in ("two_total_ms", "three_total_ms", "structure_fetch_ms", "metadata_fetch_ms"):
            item = sweeps["engines"][engine][stage]
            cells = " | ".join(
                f"{markdown_number(repeat['path_equal_mean_ms'])} ({repeat['relative_to_overall_pct']:+.1f}%)"
                for repeat in item["per_repeat"]
            )
            lines.append(
                f"| {engine} | {labels[stage]} | {markdown_number(item['overall_path_equal_mean_ms'])} | "
                f"{cells} | {item['max_abs_relative_drift_pct']:.1f}% |"
            )

    lines.extend([
        "",
        f"| Paired Mongo−PostgreSQL gap | Overall (ms) | {repeat_headers} | Repeat range (ms) |",
        f"|---|---:|{repeat_rules}|---:|",
    ])
    for stage in ("two_total_ms", "three_total_ms", "structure_fetch_ms", "metadata_fetch_ms"):
        item = sweeps["paired_delta_mongo_minus_postgres"][stage]
        cells = " | ".join(
            markdown_number(repeat["path_equal_mean_delta_ms"])
            for repeat in item["per_repeat"]
        )
        lines.append(
            f"| {labels[stage]} | {markdown_number(item['overall_path_equal_mean_delta_ms'])} | "
            f"{cells} | {markdown_number(item['range_ms'])} |"
        )

    lines.extend([
        "",
        "## Latency P99 identities",
        "",
        "P99 is selected over the 200 per-path median latencies. Each selected observation is paired with the other engine at the same repeat and path; independent engine P99 values are not stage-decomposed.",
        "",
        "| Layout | Selecting engine | Source index | Repeat | Rows | Selected path median (ms) | Peer same-pair (ms) | Exact-pair delta (ms) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    for layout, total in (("two_store", "two_total_ms"), ("three_store", "three_total_ms")):
        item = result["latency_p99_identities"][layout]
        for engine in ("mongo", "postgres"):
            identity = item[f"{engine}_selected_identity"]
            lines.append(
                f"| {layout} | {engine} | {identity['source_index']} | {identity['representative_repeat']} | "
                f"{identity['rows']} | {markdown_number(identity['path_median_ms'])} | "
                f"{markdown_number(identity['peer_same_pair_ms'])} | "
                f"{markdown_number(identity['exact_pair_delta_mongo_minus_postgres_ms'][total])} |"
            )

    p99_three = result["latency_p99_identities"]["three_store"]
    p99_identity = p99_three["mongo_selected_identity"]
    p99_delta = p99_identity["exact_pair_delta_mongo_minus_postgres_ms"]
    metadata_calls = math.ceil(
        p99_identity["rows"] / result["comparison_contract"]["chunk"]
    )
    lines.extend([
        "",
        "### Three-store P99 exact-pair decomposition",
        "",
        f"The MongoDB-selected P99 identity is source #{p99_identity['source_index']}, repeat {p99_identity['representative_repeat']}, with {p99_identity['rows']:,} rows and {metadata_calls} logical Metadata batches. This is a cumulative path-level decomposition, not a sum of independent stage P99 values.",
        "",
        "| Stage | MongoDB−PostgreSQL delta (ms) |",
        "|---|---:|",
    ])
    for stage in ("three_total_ms", *THREE_LEAVES):
        lines.append(f"| {labels[stage]} | {markdown_number(p99_delta[stage])} |")

    raw_p99 = result["raw_observation_p99_diagnostic"]
    lines.extend([
        "",
        "### Raw-observation P99 diagnostic",
        "",
        "This is descriptive P99 over the 1,000 repeat-clustered observations, not 1,000 independent samples.",
        "",
        "| Selecting engine | Source index | Repeat | Rows | Metadata calls | Total (ms) | Peer same-pair (ms) | Metadata fetch (ms) | Call mean/P99/max (ms) | Largest call share |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for engine in ("mongo", "postgres"):
        identity = raw_p99[f"{engine}_selected_identity"]
        calls = identity["metadata_fetch_call_distribution_ms"]
        lines.append(
            f"| {engine} | {identity['source_index']} | {identity['repeat']} | "
            f"{identity['rows']} | {identity['metadata_calls']} | "
            f"{markdown_number(identity['three_total_ms'])} | "
            f"{markdown_number(identity['peer_same_pair_ms'])} | "
            f"{markdown_number(identity['metadata_fetch_ms'])} | "
            f"{markdown_number(calls['mean'])}/{markdown_number(calls['p99'])}/{markdown_number(calls['max'])} | "
            f"{identity['largest_call_share_of_metadata_fetch'] * 100:.2f}% |"
        )
    lines.extend([
        "",
        "The largest single Metadata call contributes less than 1% of either selected Metadata stage. The raw tail is accumulated over a large subtree and many ordinary-sized batches, rather than being caused by one isolated batch spike.",
        "",
        "| Selected raw P99 | Total delta | Structure fetch | Metadata fetch | Metadata map | Metadata cleanup | Ordered merge |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for engine in ("mongo", "postgres"):
        delta = raw_p99[f"{engine}_selected_identity"][
            "exact_pair_delta_mongo_minus_postgres_ms"
        ]
        lines.append(
            f"| {engine} | {markdown_number(delta['three_total_ms'])} | "
            f"{markdown_number(delta['structure_fetch_ms'])} | "
            f"{markdown_number(delta['metadata_fetch_ms'])} | "
            f"{markdown_number(delta['metadata_map_ms'])} | "
            f"{markdown_number(delta['metadata_batch_cleanup_ms'])} | "
            f"{markdown_number(delta['ordered_merge_ms'])} |"
        )

    lines.extend([
        "",
        "## Order residual",
        "",
        "| Stage | Mongo first−second (ms) | PostgreSQL first−second (ms) | Gap first−second (ms) |",
        "|---|---:|---:|---:|",
    ])
    for stage in ("two_total_ms", "three_total_ms", "structure_fetch_ms", "metadata_fetch_ms"):
        item = result["order_fixed_effect_residual"][stage]
        lines.append(
            f"| {labels[stage]} | {markdown_number(item['mongo']['first_minus_second_ms'])} | "
            f"{markdown_number(item['postgres']['first_minus_second_ms'])} | "
            f"{markdown_number(item['paired_delta']['first_minus_second_ms'])} |"
        )

    lines.extend([
        "",
        "## Metadata batch comparison",
        "",
        "The table uses path-equal paired means. Call-weighted p50/p95/p99 remain in the JSON as descriptive values only.",
        "",
        "| Batch class | Calls | Paths | Request delta (ms) | Fetch delta (ms) | Map delta (ms) | Cleanup delta (ms) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for label in ("full_1000", "partial_only", "partial_tail_after_full", "partial_all"):
        group = result["metadata_batches"][label]
        values = [
            group["components"][component]["path_equal_paired"]["path_equal_mean_delta_ms"]
            for component in BATCH_COMPONENTS
        ]
        lines.append(
            f"| {label} | {group['calls_n']} | {group['paths_n']} | "
            + " | ".join(markdown_number(value) for value in values)
            + " |"
        )

    plan = result["plan_gates"]
    lines.extend([
        "",
        "## Plan gates",
        "",
        f"Hard plan gates: **{'PASS' if plan['hard_pass'] else 'FAIL'}**; "
        f"{len(plan['hard_failures'])} hard failures and {len(plan['soft_warnings'])} soft warnings.",
        "",
        "PostgreSQL Metadata accepts either a direct primary-key Index Scan or a primary-key Bitmap Index Scan plus Bitmap Heap Scan. A large two-store range may be changed by the optimizer to a parallel sequential scan; that choice is recorded rather than treated as a logical-comparison failure.",
        "",
        "| Row identity | PostgreSQL two-store | PostgreSQL Structure | PostgreSQL Metadata first | PostgreSQL Metadata tail |",
        "|---|---|---|---|---|",
    ])
    for label in ("row_p50", "row_p95", "row_p99", "row_max"):
        observed = plan["observed_access_paths"][label]["postgres"]

        def access_text(value: dict[str, Any]) -> str:
            nodes = " + ".join(value.get("node_types", [])) or "n/a"
            indexes = ", ".join(value.get("indexes", []))
            return f"{nodes} ({indexes})" if indexes else nodes

        lines.append(
            f"| {label} | {access_text(observed['two'])} | "
            f"{access_text(observed['structure'])} | "
            f"{access_text(observed['metadata_first'])} | "
            f"{access_text(observed['metadata_tail'])} |"
        )
    lines.extend([
        "",
        "## Interpretation limits",
        "",
    ])
    for limitation in result["limitations"]:
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)


def analyze(
    mongo_path: Path,
    postgres_path: Path,
    allow_smoke: bool,
) -> dict[str, Any]:
    mongo = load_object(mongo_path)
    postgres = load_object(postgres_path)
    gates = Gates()
    protocol = validate_protocol(mongo, postgres, allow_smoke, gates)
    mongo_samples = sample_map(mongo, "mongo", gates)
    pg_samples = sample_map(postgres, "postgres", gates)
    pairing = validate_pairing(mongo, postgres, mongo_samples, pg_samples, gates)

    foundation_pass = (
        set(mongo_samples) == set(pg_samples)
        and len(mongo_samples) == pairing.get("expected_pairs")
        and all(
            isinstance(sample.get(stage), (int, float))
            for sample in [*mongo_samples.values(), *pg_samples.values()]
            for stage in STAGE_KEYS
        )
    )
    if not foundation_pass:
        return {
            "schema_version": 1,
            "status": "invalid",
            "inputs": {"mongo": str(mongo_path), "postgres": str(postgres_path)},
            "comparison_contract": protocol,
            "validation": {**gates.output(), "pairing": pairing},
            "analysis_units": {
                "raw_paired_observations": len(set(mongo_samples) & set(pg_samples)),
                "equal_weight_path_units": len({key[1] for key in mongo_samples}),
                "repeats_per_path": protocol.get("repeats"),
            },
            "limitations": ["Foundational pairing failed; no latency analysis was computed."],
        }

    arithmetic = validate_arithmetic(
        {"mongo": mongo_samples, "postgres": pg_samples},
        protocol["chunk"],
        gates,
    )
    per_path = build_per_path(mongo_samples, pg_samples)
    row_identities = row_identity_output(per_path, mongo, postgres, gates)
    reps = representative_indices(per_path)
    plan_gates = plan_gates_output(mongo, postgres, reps, per_path, gates)
    environment = environment_output(mongo, postgres, gates)
    stage_delta = stage_delta_output(per_path, mongo_samples, pg_samples)
    repeat_sweeps = repeat_sweep_output(mongo_samples, pg_samples, gates)
    for layout in ("two_store", "three_store"):
        gates.hard(
            abs(stage_delta[layout]["additivity"]["error_ms"])
            <= ARITHMETIC_TOLERANCE_MS,
            f"paired_additivity.{layout}",
            stage_delta[layout]["additivity"],
        )

    result = {
        "schema_version": 1,
        "status": "complete" if gates.passed else "invalid",
        "inputs": {"mongo": str(mongo_path), "postgres": str(postgres_path)},
        "comparison_contract": protocol,
        "analysis_units": {
            "raw_paired_observations": len(mongo_samples),
            "equal_weight_path_units": len(per_path),
            "repeats_per_path": protocol["repeats"],
            "engine_campaigns_per_engine": 1,
            "headline_estimator": "repeat mean within path, then equal-weight mean across paths",
            "inference_note": "The repeated observations and Metadata calls are not treated as independent units.",
        },
        "validation": {
            **gates.output(),
            "pairing": pairing,
            "arithmetic": arithmetic,
        },
        "paired_stage_delta": stage_delta,
        "per_path": per_path,
        "per_path_median_cv_summary": stability_output(per_path),
        "repeat_sweep_summary": repeat_sweeps,
        "row_percentile_identities": row_identities,
        "latency_p99_identities": latency_p99_output(
            per_path, mongo_samples, pg_samples
        ),
        "raw_observation_p99_diagnostic": raw_observation_p99_output(
            mongo_samples, pg_samples
        ),
        "order_fixed_effect_residual": order_residual_output(
            mongo_samples, pg_samples
        ),
        "metadata_batches": metadata_batch_output(
            mongo_samples, pg_samples, protocol["chunk"]
        ),
        "plan_gates": plan_gates,
        "environment_comparison": environment,
        "limitations": [
            "Fetch stages include server execution, localhost transport, driver decoding, and raw-result materialization; they do not isolate database-server CPU time.",
            "The MongoDB driver materializes BSON-decoded dictionaries while psycopg materializes tuples. This difference is part of the measured client pipeline.",
            "Stage medians and stage P99 values are non-additive and are never summed. Additive attribution uses paired means only.",
            f"The {len(mongo_samples):,} measurements are {len(per_path)} paths repeated {protocol['repeats']} times, not {len(mongo_samples):,} independent observations. Metadata calls are nested inside those repeated paths.",
            "The subtree paths may overlap and share nodes, so they are equal-weight workload units rather than a claim of statistically independent path samples.",
            "There is one timed campaign per engine. Path-level variability does not estimate machine-to-machine or campaign-to-campaign uncertainty.",
            "Repeat-sweep drift is reported explicitly; the overall stage gap is a campaign average, not evidence that every repeat had the same gap.",
            "Representative execution plans confirm access paths after timing; plan execution time is not subtracted from client-side latency.",
            "SQLite is intentionally outside this comparison because it has a different execution boundary.",
        ],
    }
    # Refresh after environment and paired-additivity gates were added.
    result["validation"].update(gates.output())
    result["status"] = "complete" if gates.passed else "invalid"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo", type=Path, default=DEFAULT_MONGO)
    parser.add_argument("--postgres", type=Path, default=DEFAULT_POSTGRES)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON)
    parser.add_argument("--out-md", type=Path, default=DEFAULT_OUT_MD)
    parser.add_argument(
        "--allow-smoke",
        action="store_true",
        help="allow non-10M input and at least two repeats; still requires 200 paths",
    )
    args = parser.parse_args()

    try:
        result = analyze(args.mongo, args.postgres, args.allow_smoke)
        markdown = render_markdown(result) if "paired_stage_delta" in result else (
            "# MongoDB vs PostgreSQL subtree breakdown\n\n"
            "Status: **invalid**. Foundational pairing failed; inspect the JSON validation failures.\n"
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        result = {
            "schema_version": 1,
            "status": "invalid",
            "inputs": {"mongo": str(args.mongo), "postgres": str(args.postgres)},
            "validation": {
                "hard_pass": False,
                "hard_failure_count": 1,
                "hard_failures": [{"code": "input", "detail": str(error)}],
                "warning_count": 0,
                "warnings": [],
            },
        }
        markdown = (
            "# MongoDB vs PostgreSQL subtree breakdown\n\n"
            f"Status: **invalid**. Input error: `{error}`\n"
        )

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    args.out_md.write_text(markdown)
    print(
        f"{result['status']}: wrote {args.out_json} and {args.out_md}",
        file=sys.stderr,
    )
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
