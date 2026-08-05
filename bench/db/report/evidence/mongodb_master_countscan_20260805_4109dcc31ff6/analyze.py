#!/usr/bin/env python3

"""Fail-closed validator and analyzer for the frozen three-arm CountScan campaign.

The adoption gates read one interval family only: the pre-frozen stratified
log-ratio Welch-t interval implemented in this file. The stratified bootstrap is
retained as a clearly separated sensitivity output and is structurally unable to
reach the gate function, which is handed a gate-input mapping derived solely from
the t intervals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
from array import array
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from math import fsum
from pathlib import Path
from typing import Any

EVIDENCE_DIR = Path(__file__).resolve().parent
CAMPAIGN_PATH = EVIDENCE_DIR / "campaign.json"
RUN_RECORD_PATH = EVIDENCE_DIR / "campaign_run.json"
PARTIAL_JOURNAL_PATH = EVIDENCE_DIR / "campaign_partial.json"
LEDGER_PATH = EVIDENCE_DIR / "attempt_ledger.jsonl"
BUILD_ATTESTATION_PATH = EVIDENCE_DIR / "build_attestation.json"
RAW_DIR = EVIDENCE_DIR / "raw"
LOG_DIR = EVIDENCE_DIR / "logs"
SUMMARY_PATH = EVIDENCE_DIR / "summary.json"

ARMS = ("A", "B", "C")
ARM_LABELS = {
    "A": "upstream_production_normalized_harness",
    "B": "rejected_heavyweight_implementation",
    "C": "final_candidate",
}
ARM_COMMITS = {
    "A": "0561c098b99ac5e929005e70a2e37d7a97a82423",
    "B": "4109dcc31ff6df595c6b2e5caf3fbce077c488ba",
    "C": "90814b83d3e55f099c1244266d86700b5f633972",
}
HARNESS_SOURCE_COMMIT = "4109dcc31ff6df595c6b2e5caf3fbce077c488ba"
ARM_BINARY_PATHS = {
    "A": "/tmp/mongo-count-query-bm-countscan-A-upstream-production-normalized-harness",
    "B": "/tmp/mongo-count-query-bm-countscan-B-rejected-heavyweight-implementation",
    "C": "/tmp/mongo-count-query-bm-countscan-C-final-candidate",
}
# Provisional smoke/reference snapshots that predate the attested builder. They can never
# become formal campaign arms because their provenance cannot be reconstructed after the fact.
FORBIDDEN_BINARY_PATHS = (
    "/tmp/mongo-count-query-bm-4109dcc-A-pristine",
    "/tmp/mongo-count-query-bm-4109dcc-C-final.c1",
    "/tmp/mongo-count-query-bm-4109dcc-B-previous",
    "/tmp/mongo-count-query-bm-4109dcc-C-final",
)
# Rejecting the provisional snapshots by filename alone is not enough: the same bytes could be
# copied to a canonical path. These are the observed contents of the provisional builds.
FORBIDDEN_BINARY_SHA256 = (
    "3fd2cbee48ac182086b40226cdc7ffd54326161a810ff6e0d6c6a96332042c2a",
    "94c021e6b523256270ca519ea4c26bbbdbbc09720f50fe40bbd22460f18d1382",
)
PRODUCTION_FILES = (
    "src/mongo/db/exec/classic/count.cpp",
    "src/mongo/db/exec/classic/count.h",
    "src/mongo/db/exec/classic/count_scan.cpp",
    "src/mongo/db/exec/classic/count_scan.h",
    "src/mongo/db/exec/classic/plan_stage.h",
    "src/mongo/db/exec/classic/working_set.h",
)
COMMON_HARNESS_FILES = (
    "buildscripts/resmokeconfig/suites/benchmarks_query.yml",
    "src/mongo/db/query/BUILD.bazel",
    "src/mongo/db/query/count_query_bm.cpp",
    "src/mongo/db/query/query_bm_fixture.cpp",
    "src/mongo/db/query/query_bm_fixture.h",
)
MANIFEST_FILES = tuple(sorted(set(PRODUCTION_FILES) | set(COMMON_HARNESS_FILES)))
# Commit-derived Git blob identities, pinned so that a reader of this bundle alone can detect a
# doctored local repository or a swapped arm label without trusting the machine that built it.
ARM_PRODUCTION_BLOBS = {
    "A": {
        "src/mongo/db/exec/classic/count.cpp": "9f4c97f374315f912eda92767ef38b750abc8b92",
        "src/mongo/db/exec/classic/count.h": "e74f83b830b5be94bda5250f4f700c801e32b3bf",
        "src/mongo/db/exec/classic/count_scan.cpp": "768d1d2066bcab98c6957dba0ce1c28ad057c0c3",
        "src/mongo/db/exec/classic/count_scan.h": "262473b32d38cce412ab29dc6b0f6c6d79cd1273",
        "src/mongo/db/exec/classic/plan_stage.h": "083e605f7f46a393443ac3b3200eb66dc18bc7f0",
        "src/mongo/db/exec/classic/working_set.h": "011adacfe93c532104f2e5d02e08b750fcf614c3",
    },
    "B": {
        "src/mongo/db/exec/classic/count.cpp": "6cc8d99e0c433d0fa1fe297159d970008e10020f",
        "src/mongo/db/exec/classic/count.h": "0f638ba2b822a9d0a642de5d3d95e569a5dfdfe7",
        "src/mongo/db/exec/classic/count_scan.cpp": "0cb3b921e84754d7f7cb125934696319f80d660c",
        "src/mongo/db/exec/classic/count_scan.h": "b958a1210eefa8f9eccfbf8a6b14387cf20be5ca",
        "src/mongo/db/exec/classic/plan_stage.h": "9649aac1d351227d60f61482d0ab5152f9e86c6c",
        "src/mongo/db/exec/classic/working_set.h": "a7488fa87416c966ce4e56f1226026fb7755d353",
    },
    "C": {
        "src/mongo/db/exec/classic/count.cpp": "109f7d553f429de6f8a13f68925f483293a8f8bd",
        "src/mongo/db/exec/classic/count.h": "e74f83b830b5be94bda5250f4f700c801e32b3bf",
        "src/mongo/db/exec/classic/count_scan.cpp": "87f37933a92a18720ac2e8144fc7d5200ad2f9d4",
        "src/mongo/db/exec/classic/count_scan.h": "29b4a8db7bd7ccef98ef6fb59f6d18fe76781e76",
        "src/mongo/db/exec/classic/plan_stage.h": "6dba84ffd2dceb1c4ed4ccb5130bd751d76db7de",
        "src/mongo/db/exec/classic/working_set.h": "011adacfe93c532104f2e5d02e08b750fcf614c3",
    },
}
# Four harness files come unchanged from the candidate commit; count_query_bm.cpp is a new file
# there and additionally carries the evidence-only point-query control, so it is pinned directly.
POINT_CONTROL_FILE = "src/mongo/db/query/count_query_bm.cpp"
POINT_CONTROL_BLOB = "223e28efbc6b474cf4c32bc7328f7578b4569edc"
COMMON_HARNESS_BLOBS = {
    "buildscripts/resmokeconfig/suites/benchmarks_query.yml": "0aa7433db8e59c5b9a258d131993a1bcbc0db9e3",
    "src/mongo/db/query/BUILD.bazel": "bbc0ea37c3478f7767b0157944b0d148e4f7a907",
    "src/mongo/db/query/query_bm_fixture.cpp": "55e0c66e2825c62972d1a60be4cce759ca55be7c",
    "src/mongo/db/query/query_bm_fixture.h": "a37c66eee4446692be9ad5cd825e9fc40d7ef1b3",
    POINT_CONTROL_FILE: POINT_CONTROL_BLOB,
}
# count_query_bm.cpp does not exist at the reconstruction base, so resetting an arm must delete it
# rather than restore it, and it legitimately appears as an untracked addition afterwards.
BASE_ABSENT_MANIFEST_FILES = (POINT_CONTROL_FILE,)


def expected_manifest_blobs(arm: str) -> dict[str, str]:
    return {**ARM_PRODUCTION_BLOBS[arm], **COMMON_HARNESS_BLOBS}
SOURCE_ARTIFACTS = (
    "arm_A_production.patch",
    "arm_A_source_manifest.json",
    "arm_B_production.patch",
    "arm_B_source_manifest.json",
    "arm_C_production.patch",
    "arm_C_source_manifest.json",
    "build_attestation.json",
    "common_harness.patch",
)
PROTOCOL_ARTIFACTS = (
    "analyze.py",
    "build_arms.py",
    "run_campaign.py",
    "test_protocol.py",
)
BUILD_COMMAND = ("bazel", "build", "--config=opt", "//src/mongo/db/query:count_query_bm")
BUILD_ORDER = ("C1", "A", "B", "C2")
BUILD_ARM_OF = {"C1": "C", "A": "A", "B": "B", "C2": "C"}
CAMPAIGN_BUILD_OF_ARM = {"A": "A", "B": "B", "C": "C2"}

WORKLOADS = ("S", "M", "W", "P", "X")
WORKLOAD_SPECS = {
    "S": {
        "filter": "^ScalarCountQueryBenchmark/DirectNonDeduplicatingCountScan/400000/64$",
        "run_name": "ScalarCountQueryBenchmark/DirectNonDeduplicatingCountScan/400000/64",
        "role": "count_endpoint",
        "minimum_time_seconds": 0.01,
    },
    "M": {
        "filter": "^MultikeyCountQueryBenchmark/DirectDeduplicatingCountScan/200000/64$",
        "run_name": "MultikeyCountQueryBenchmark/DirectDeduplicatingCountScan/200000/64",
        "role": "count_endpoint",
        "minimum_time_seconds": 0.01,
    },
    "W": {
        "filter": "^CompoundWildcardCountQueryBenchmark/DirectNonMultikeyCountScan/200000/64$",
        "run_name": "CompoundWildcardCountQueryBenchmark/DirectNonMultikeyCountScan/200000/64",
        "role": "count_endpoint",
        "minimum_time_seconds": 0.01,
    },
    "P": {
        "filter": "^ClassicPointControlQueryBenchmark/HintedUniqueFieldPointQuery/10000/64$",
        "run_name": "ClassicPointControlQueryBenchmark/HintedUniqueFieldPointQuery/10000/64",
        "role": "negative_control",
        "minimum_time_seconds": 0.05,
    },
    # The only workload where the optimization cannot fire, and therefore the only place a
    # regression could hide. Its plan is COUNT -> FETCH -> IXSCAN, so CountStage's child is a FETCH.
    # It doubles as the many-work() control the rejected implementation's base-class refactor needs:
    # a fetching count drives on the order of one work() call per document through stages this
    # change does not touch. Measured at the campaign size it retires 1.876G instructions per
    # iteration with 0.001% spread between repetitions, and settles on exactly one iteration.
    "X": {
        "filter": "^UnoptimizedCountQueryBenchmark/FetchingCountWithoutCountScan/200000/64$",
        "run_name": "UnoptimizedCountQueryBenchmark/FetchingCountWithoutCountScan/200000/64",
        "role": "non_intrusion_control",
        "minimum_time_seconds": 0.01,
    },
}
COUNT_WORKLOADS = tuple(key for key in WORKLOADS if WORKLOAD_SPECS[key]["role"] == "count_endpoint")
CONTROL_WORKLOADS = tuple(key for key in WORKLOADS if WORKLOAD_SPECS[key]["role"].endswith("control"))
ARM_PERMUTATIONS = ("ABC", "ACB", "BAC", "BCA", "CAB", "CBA")
WORKLOAD_ROTATIONS = ("SMWPX", "MWPXS", "WPXSM", "PXSMW", "XSMWP")
BLOCK_SCHEDULE = (
    ("ABC", "SMWPX"),
    ("ACB", "SMWPX"),
    ("BAC", "SMWPX"),
    ("BCA", "SMWPX"),
    ("CAB", "SMWPX"),
    ("CBA", "SMWPX"),
    ("ABC", "MWPXS"),
    ("ACB", "MWPXS"),
    ("BAC", "MWPXS"),
    ("BCA", "MWPXS"),
    ("CAB", "MWPXS"),
    ("CBA", "MWPXS"),
    ("ABC", "WPXSM"),
    ("ACB", "WPXSM"),
    ("BAC", "WPXSM"),
    ("BCA", "WPXSM"),
    ("CAB", "WPXSM"),
    ("CBA", "WPXSM"),
    ("ABC", "PXSMW"),
    ("ACB", "PXSMW"),
    ("BAC", "PXSMW"),
    ("BCA", "PXSMW"),
    ("CAB", "PXSMW"),
    ("CBA", "PXSMW"),
    ("ABC", "XSMWP"),
    ("ACB", "XSMWP"),
    ("BAC", "XSMWP"),
    ("BCA", "XSMWP"),
    ("CAB", "XSMWP"),
    ("CBA", "XSMWP"),
)
BLOCK_EXECUTION_SEED = 410_920_260_805
# The 30 blocks keep their frozen (arm order, workload rotation) assignment, but the order in
# which they execute is a fixed permutation derived once from BLOCK_EXECUTION_SEED with
# random.Random(seed).shuffle(list(range(1, BLOCK_COUNT + 1))). Systematic execution would place each
# arm-order stratum at a fixed period of six blocks; a host disturbance near that period would
# then alias onto one stratum and bias the estimate without widening an interval whose standard
# error deliberately excludes between-stratum variance.
BLOCK_EXECUTION_ORDER = (
    21, 20, 17, 13, 23, 18, 22, 15, 2, 26, 6, 19, 12, 16, 11, 14,
    1, 24, 29, 7, 4, 9, 25, 5, 10, 30, 27, 28, 3, 8,
)
COMPARISONS = {
    "C_over_A": ("C", "A", "final_vs_upstream_adoption"),
    "C_over_B": ("C", "B", "final_vs_previous_incremental_adoption"),
    "B_over_A": ("B", "A", "descriptive_previous_vs_upstream_only"),
}
METRIC_ROLES = {
    "instructions_per_iteration": "primary",
    "cpu_time": "secondary",
    "real_time": "auxiliary",
}
REPETITIONS = 5
BLOCK_COUNT = 30
PROCESS_COUNT = 450
BLOCKS_PER_STRATUM = 5
BOOTSTRAP_SAMPLES = 200_000
BOOTSTRAP_SEED = 410_920_260_805
ORDINARY_CONFIDENCE = 0.95
ADJUSTED_CONFIDENCE = 0.9833333333333333
# The C/B comparison asks whether the previous implementation's extra machinery bought anything
# worth its cost. That is a NONINFERIORITY claim, and it must be tested as one: the previous
# implementation is a strict mechanistic superset of the candidate (it additionally devirtualizes
# the child call, inlines isEOF, and constant-folds the materialization branch away), so it is
# expected to be slightly faster. Requiring the candidate to be strictly faster than a superset of
# itself would be a gate designed to fail. The margin below is fixed on practical grounds BEFORE
# any measurement: a difference at or under it does not justify a template on the base class of
# every classic stage plus two friend declarations and a second entry point.
NONINFERIORITY_MARGIN = 1.01
# CPU time is secondary, but "secondary" must not mean "absent from the decision": an
# instruction-count win paired with a CPU-time regression is the classic failure mode this
# non-regression bound exists to catch.
CPU_NON_REGRESSION_UPPER = 1.01
# The un-optimized control must not regress. One-sided on purpose: an improvement there would be
# implausible but harmless, whereas a regression is the failure this control exists to detect.
NON_INTRUSION_BAND = (0.998, 1.002)
# Set from the instrument, not a round number: point-control instructions reproduce to about
# 0.1% between repetitions, so this band is several times the 95% half-width while still being
# far tighter than the effect claimed on the count endpoints.
POINT_CONTROL_INSTRUCTION_BAND = (0.998, 1.002)
POINT_CONTROL_CPU_BAND = (0.97, 1.03)
COUNT_FAMILY_ENDPOINT_COUNT = 3
PLACEHOLDER_PREFIX = "PLACEHOLDER_"

SELECTED_CPU = 0
SIBLING_CPU = 48
PREFLIGHT_MAX_BUSY_FRACTION = 0.10
PER_PROCESS_MAX_SIBLING_BUSY_FRACTION = 0.25
PER_PROCESS_MIN_SELECTED_BUSY_FRACTION = 0.50
COOLDOWN_SECONDS = 30
PREFLIGHT_SAMPLE_SECONDS = 5


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


def require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label} is empty or not a string")
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


def fraction(value: Any, label: str) -> float:
    result = nonnegative_finite(value, label)
    if result > 1.0:
        fail(f"{label} must be a fraction in [0, 1], got {value!r}")
    return result


def positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        fail(f"{label} must be a positive integer, got {value!r}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        fail(f"cannot hash {path}: {exc}")
    return digest.hexdigest()


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        fail(f"{label} is empty")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        fail(f"{label} is invalid: {exc}")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        fail(f"{label} lacks a UTC offset: {value!r}")
    return timestamp


# ---------------------------------------------------------------------------
# Self-contained Student-t quantile
# ---------------------------------------------------------------------------

_FP_MIN = 1e-300

# Two-sided critical values at the two frozen confidence levels for integer degrees of
# freedom. Generated with, and independently re-validated against, SciPy in
# test_protocol.py. Because the Student-t critical value decreases monotonically in the
# degrees of freedom, the entries at floor(df) and ceil(df) bracket the exact value; the
# bracket is used as a conservative fail-closed guard on the self-contained quantile.
_T_TABLE_MAX_DF = 40
_T_CRITICAL_TABLE = {
    "ordinary_ci95": (
        12.706204736174694,
        4.302652729749462,
        3.1824463052837078,
        2.7764451051977934,
        2.5705818356363146,
        2.4469118511449786,
        2.364624251592784,
        2.306004135204166,
        2.262157162798205,
        2.228138851986274,
        2.200985160091639,
        2.1788128296672284,
        2.1603686564627913,
        2.144786687917804,
        2.131449545559776,
        2.1199052992212546,
        2.1098155778333156,
        2.1009220402410382,
        2.0930240544083087,
        2.085963447265864,
        2.0796138447276795,
        2.0738730679040254,
        2.0686576104190486,
        2.0638985616280245,
        2.0595385527532972,
        2.0555294386428735,
        2.0518305164802846,
        2.0484071417952454,
        2.045229642132703,
        2.0422724563012378,
        2.039513446396408,
        2.0369333434601016,
        2.0345152974493383,
        2.0322445093177186,
        2.030107928250343,
        2.0280940009804502,
        2.0261924630291093,
        2.0243941639119694,
        2.022690920036761,
        2.021075390306273,
    ),
    "bonferroni_adjusted_ci98_333333": (
        38.188459297025744,
        7.648803937915553,
        4.856657272768983,
        3.9607864827701835,
        3.53411070405837,
        3.287455157088189,
        3.1275522742463706,
        3.0157618368871684,
        2.93332408837399,
        2.8700725556589806,
        2.8200336999976012,
        2.779473101781101,
        2.745938723172568,
        2.7177551594597733,
        2.6937393191964882,
        2.6730322864426266,
        2.654995583554841,
        2.639144819415993,
        2.625105913222787,
        2.612585423033488,
        2.601349952556038,
        2.5912115559036564,
        2.582017198304117,
        2.573641017040885,
        2.565978552077901,
        2.558942385718912,
        2.552458805791156,
        2.5464652227804265,
        2.540908149498905,
        2.535741605435288,
        2.530925845218133,
        2.5264263369378113,
        2.5222129348896756,
        2.5182592049204926,
        2.514541870529143,
        2.511040355246327,
        2.507736402325894,
        2.504613756932469,
        2.5016578991672045,
        2.498855818693544,
    ),
}


def _betacf(a: float, b: float, x: float) -> float:
    """Continued-fraction expansion for the incomplete beta function."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FP_MIN:
        d = _FP_MIN
    d = 1.0 / d
    h = d
    for iteration in range(1, 501):
        m2 = 2 * iteration
        numerator = iteration * (b - iteration) * x / ((qam + m2) * (a + m2))
        d = 1.0 + numerator * d
        if abs(d) < _FP_MIN:
            d = _FP_MIN
        c = 1.0 + numerator / c
        if abs(c) < _FP_MIN:
            c = _FP_MIN
        d = 1.0 / d
        h *= d * c
        numerator = -(a + iteration) * (qab + iteration) * x / ((a + m2) * (qap + m2))
        d = 1.0 + numerator * d
        if abs(d) < _FP_MIN:
            d = _FP_MIN
        c = 1.0 + numerator / c
        if abs(c) < _FP_MIN:
            c = _FP_MIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            return h
    fail("incomplete beta continued fraction did not converge")
    raise AssertionError("unreachable")


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    if not (math.isfinite(a) and math.isfinite(b)) or a <= 0.0 or b <= 0.0:
        fail(f"regularized incomplete beta requires positive finite parameters, got a={a!r}, b={b!r}")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, df: float) -> float:
    if not math.isfinite(t):
        fail(f"Student-t CDF requires a finite quantile, got {t!r}")
    if not math.isfinite(df) or df <= 0.0:
        fail(f"Student-t CDF requires positive finite degrees of freedom, got {df!r}")
    tail = 0.5 * regularized_incomplete_beta(df / 2.0, 0.5, df / (df + t * t))
    return 1.0 - tail if t > 0.0 else tail


def student_t_ppf(p: float, df: float) -> float:
    """Self-contained two-sided Student-t quantile, validated against SciPy in the tests."""
    if not math.isfinite(p) or not 0.0 < p < 1.0:
        fail(f"Student-t quantile requires a probability in (0, 1), got {p!r}")
    if not math.isfinite(df) or df <= 0.0:
        fail(f"Student-t quantile requires positive finite degrees of freedom, got {df!r}")
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -student_t_ppf(1.0 - p, df)
    low = 0.0
    high = 1.0
    while student_t_cdf(high, df) < p:
        high *= 2.0
        if high > 1e12:
            fail(f"Student-t quantile failed to bracket p={p!r} at df={df!r}")
    for _ in range(200):
        middle = 0.5 * (low + high)
        if student_t_cdf(middle, df) < p:
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


def _table_critical(level_key: str, df_index: int) -> float:
    table = _T_CRITICAL_TABLE[level_key]
    return table[min(max(df_index, 1), _T_TABLE_MAX_DF) - 1]


def guarded_t_critical(confidence: float, df: float, level_key: str) -> float:
    """Return the exact critical value after a conservative floor-df bracket check."""
    if df < 1.0:
        fail(f"degrees of freedom below one are not admissible for a t interval: {df!r}")
    critical = student_t_ppf(1.0 - (1.0 - confidence) / 2.0, df)
    if not math.isfinite(critical) or critical <= 0.0:
        fail(f"non-finite or non-positive t critical value at df={df!r}")
    upper_guard = _table_critical(level_key, math.floor(df))
    lower_guard = _table_critical(level_key, math.ceil(df))
    if not lower_guard - 1e-9 <= critical <= upper_guard + 1e-9:
        fail(
            f"t critical value {critical!r} at df={df!r} escapes the conservative floor-df bracket "
            f"[{lower_guard!r}, {upper_guard!r}] for {level_key}"
        )
    return critical


# ---------------------------------------------------------------------------
# Stratified log-ratio Welch-t interval (the only gate interval)
# ---------------------------------------------------------------------------

STRATUM_BLOCKS = {
    permutation: tuple(
        block for block, (candidate, _) in enumerate(BLOCK_SCHEDULE, start=1) if candidate == permutation
    )
    for permutation in ARM_PERMUTATIONS
}

CONFIDENCE_LEVELS = {
    "ordinary_ci95": ORDINARY_CONFIDENCE,
    "bonferroni_adjusted_ci98_333333": ADJUSTED_CONFIDENCE,
}


def stratified_log_ratio_t_interval(log_ratio_by_block: dict[int, float], label: str) -> dict[str, Any]:
    """Pre-frozen stratified log-ratio Welch-t interval.

    theta = mean over strata of the stratum mean log ratio
    a_h   = s_h^2 / (H^2 * n_h)
    SE    = sqrt(sum a_h)
    df    = (sum a_h)^2 / sum(a_h^2 / (n_h - 1))
    """
    strata_count = len(ARM_PERMUTATIONS)
    stratum_means: list[float] = []
    contributions: list[float] = []
    stratum_detail: dict[str, Any] = {}
    for permutation in ARM_PERMUTATIONS:
        blocks = STRATUM_BLOCKS[permutation]
        size = len(blocks)
        if size < 2:
            fail(f"{label}: stratum {permutation} needs at least two blocks, got {size}")
        try:
            values = [float(log_ratio_by_block[block]) for block in blocks]
        except KeyError as exc:
            fail(f"{label}: stratum {permutation} is missing block {exc}")
            raise AssertionError("unreachable") from exc
        for value in values:
            if not math.isfinite(value):
                fail(f"{label}: stratum {permutation} contains a non-finite log ratio")
        mean = fsum(values) / size
        variance = fsum((value - mean) ** 2 for value in values) / (size - 1)
        if not math.isfinite(variance) or variance < 0.0:
            fail(f"{label}: stratum {permutation} has a non-finite or negative variance")
        contribution = variance / (strata_count * strata_count * size)
        stratum_means.append(mean)
        contributions.append(contribution)
        stratum_detail[permutation] = {
            "blocks": list(blocks),
            "block_count": size,
            "mean_log_ratio": mean,
            "sample_variance_log_ratio": variance,
            "variance_contribution": contribution,
            "ratio_geomean": math.exp(mean),
        }
    theta = fsum(stratum_means) / strata_count
    variance_total = fsum(contributions)
    if not math.isfinite(variance_total) or variance_total <= 0.0:
        fail(f"{label}: stratified variance is zero or non-finite; the interval is not defined")
    standard_error = math.sqrt(variance_total)
    if not math.isfinite(standard_error) or standard_error <= 0.0:
        fail(f"{label}: stratified standard error is zero or non-finite")
    denominator = fsum(
        contribution * contribution / (len(STRATUM_BLOCKS[permutation]) - 1)
        for permutation, contribution in zip(ARM_PERMUTATIONS, contributions)
    )
    if not math.isfinite(denominator) or denominator <= 0.0:
        fail(f"{label}: Welch-Satterthwaite denominator is zero or non-finite")
    degrees_of_freedom = variance_total * variance_total / denominator
    if not math.isfinite(degrees_of_freedom) or degrees_of_freedom < 1.0:
        fail(f"{label}: Welch-Satterthwaite degrees of freedom are not admissible: {degrees_of_freedom!r}")

    result: dict[str, Any] = {
        "method": "stratified_log_ratio_welch_t",
        "point_estimate_ratio": math.exp(theta),
        "mean_log_ratio": theta,
        "standard_error_log_scale": standard_error,
        "degrees_of_freedom": degrees_of_freedom,
        "strata": stratum_detail,
    }
    for level_key, confidence in CONFIDENCE_LEVELS.items():
        critical = guarded_t_critical(confidence, degrees_of_freedom, level_key)
        result[level_key] = [
            math.exp(theta - critical * standard_error),
            math.exp(theta + critical * standard_error),
        ]
        result[f"{level_key}_t_critical"] = critical
    return result


def percentile(sorted_values: Sequence[float], value_fraction: float) -> float:
    if not sorted_values:
        fail("cannot calculate a percentile of an empty sample")
    position = (len(sorted_values) - 1) * value_fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return float(sorted_values[lower]) * (1.0 - weight) + float(sorted_values[upper]) * weight


def confidence_interval(sorted_values: Sequence[float], confidence: float) -> list[float]:
    tail = (1.0 - confidence) / 2.0
    return [percentile(sorted_values, tail), percentile(sorted_values, 1.0 - tail)]


def geometric_mean(values: Iterable[float]) -> float:
    checked = [positive_finite(value, "geometric-mean input") for value in values]
    if not checked:
        fail("cannot calculate a geometric mean of an empty sample")
    return math.exp(fsum(math.log(value) for value in checked) / len(checked))


def process_stem(block: int, workload: str, arm: str) -> str:
    return f"block{block:02d}_{workload}_{arm}_{ARM_LABELS[arm]}"


def expected_output_sequence() -> list[dict[str, Any]]:
    require_equal(
        sorted(BLOCK_EXECUTION_ORDER),
        list(range(1, BLOCK_COUNT + 1)),
        "block execution order must be a permutation of every block exactly once",
    )
    sequence: list[dict[str, Any]] = []
    process_index = 0
    for execution_position, block in enumerate(BLOCK_EXECUTION_ORDER, start=1):
        arm_order, workload_order = BLOCK_SCHEDULE[block - 1]
        for workload_position, workload in enumerate(workload_order, start=1):
            for arm_position, arm in enumerate(arm_order, start=1):
                process_index += 1
                stem = process_stem(block, workload, arm)
                sequence.append(
                    {
                        "process_index": process_index,
                        "block": block,
                        "block_execution_position": execution_position,
                        "arm_order": arm_order,
                        "workload_order": workload_order,
                        "workload_position": workload_position,
                        "arm_position": arm_position,
                        "workload": workload,
                        "arm": arm,
                        "raw": f"raw/{stem}.json",
                        "log": f"logs/{stem}.log",
                    }
                )
    require_equal(len(sequence), PROCESS_COUNT, "derived process count")
    return sequence


def expected_benchmark_command(campaign: dict[str, Any], item: dict[str, Any], raw_path: Path) -> list[str]:
    workload = campaign["workloads"][item["workload"]]
    return [
        "taskset",
        "-c",
        str(campaign["execution"]["cpu_affinity"]),
        campaign["arms"][item["arm"]]["binary_path"],
        f"--benchmark_filter={workload['filter']}",
        f"--benchmark_min_time={workload['minimum_time_seconds']}",
        f"--benchmark_repetitions={campaign['benchmark']['repetitions_per_process']}",
        "--benchmark_report_aggregates_only=false",
        f"--benchmark_out={raw_path}",
        "--benchmark_out_format=json",
    ]


def _validate_identity(value: Any, label: str, pattern: str, allow_placeholders: bool) -> None:
    if not isinstance(value, str):
        fail(f"{label} is not a string")
    if value.startswith(PLACEHOLDER_PREFIX):
        if allow_placeholders:
            return
        fail(f"{label} is still an execution-blocking placeholder: {value}")
    if not re.fullmatch(pattern, value):
        fail(f"{label} has an invalid format: {value!r}")


def _has_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PLACEHOLDER_PREFIX)


def validate_protocol_artifacts(campaign: dict[str, Any], allow_placeholders: bool) -> None:
    artifacts = require_mapping(campaign.get("protocol_artifacts"), "campaign.protocol_artifacts")
    require_equal(set(artifacts), set(PROTOCOL_ARTIFACTS), "protocol artifact set")
    for name in sorted(artifacts):
        configured = artifacts[name]
        _validate_identity(configured, f"protocol SHA-256 for {name}", r"[0-9a-f]{64}", allow_placeholders)
        if not _has_placeholder(configured):
            require_equal(sha256_file(EVIDENCE_DIR / name), configured, f"protocol SHA-256 for {name}")


def validate_source_manifest(campaign: dict[str, Any], arm: str) -> dict[str, Any]:
    arm_config = campaign["arms"][arm]
    manifest_name = arm_config["source_manifest"]
    manifest = load_json(EVIDENCE_DIR / manifest_name)
    require_equal(
        set(manifest),
        {
            "schema_version",
            "arm",
            "reconstruction_base_commit",
            "production_source_commit",
            "common_harness_patch",
            "production_patch",
            "files",
        },
        f"arm {arm} source manifest top-level fields",
    )
    require_equal(manifest.get("schema_version"), 1, f"arm {arm} source manifest schema")
    require_equal(manifest.get("arm"), arm, f"arm {arm} source manifest arm")
    require_equal(
        manifest.get("reconstruction_base_commit"),
        ARM_COMMITS["A"],
        f"arm {arm} source manifest reconstruction base",
    )
    require_equal(
        manifest.get("production_source_commit"),
        ARM_COMMITS[arm],
        f"arm {arm} source manifest production commit",
    )
    common = require_mapping(manifest.get("common_harness_patch"), f"arm {arm} source manifest common harness patch")
    require_equal(common.get("path"), "common_harness.patch", f"arm {arm} common harness path")
    require_equal(
        common.get("sha256"),
        campaign["source_artifacts"]["common_harness.patch"],
        f"arm {arm} common harness SHA-256",
    )
    production = require_mapping(manifest.get("production_patch"), f"arm {arm} source manifest production patch")
    expected_patch = f"arm_{arm}_production.patch"
    require_equal(production.get("path"), expected_patch, f"arm {arm} production patch path")
    require_equal(
        production.get("sha256"),
        campaign["source_artifacts"][expected_patch],
        f"arm {arm} production patch SHA-256",
    )
    files = require_mapping(manifest.get("files"), f"arm {arm} source manifest files")
    require_equal(set(files), set(MANIFEST_FILES), f"arm {arm} complete 11-file manifest set")
    expected_blobs = expected_manifest_blobs(arm)
    for path in MANIFEST_FILES:
        identity = require_mapping(files.get(path), f"arm {arm} source identity for {path}")
        require_equal(set(identity), {"sha256", "git_blob"}, f"arm {arm} source identity fields for {path}")
        _validate_identity(identity.get("sha256"), f"arm {arm} source SHA-256 for {path}", r"[0-9a-f]{64}", False)
        _validate_identity(identity.get("git_blob"), f"arm {arm} source Git blob for {path}", r"[0-9a-f]{40}", False)
        # Pinned against the commit-derived identity so that a doctored local repository, or a
        # swapped B/C label, is detectable from this bundle alone.
        require_equal(
            identity.get("git_blob"),
            expected_blobs[path],
            f"arm {arm} Git blob for {path} must match the pinned commit-derived identity",
        )
    return manifest


def validate_cross_arm_source_invariants(manifests: dict[str, dict[str, Any]]) -> None:
    """The five common harness identities must be equal across all three arms."""
    for path in COMMON_HARNESS_FILES:
        identities = {arm: manifests[arm]["files"][path] for arm in ARMS}
        reference = identities["A"]
        for arm in ARMS:
            require_equal(
                identities[arm],
                reference,
                f"common harness file {path} must be byte-identical across arms (arm {arm})",
            )
    # Every arm must differ from at least one other arm somewhere in the production set,
    # otherwise two arms are secretly the same binary input.
    for left in ARMS:
        for right in ARMS:
            if left >= right:
                continue
            identical = all(
                manifests[left]["files"][path] == manifests[right]["files"][path] for path in PRODUCTION_FILES
            )
            if identical:
                fail(f"arms {left} and {right} have identical production sources; the comparison would be vacuous")


_FORBIDDEN_EXTENDED_HEADERS = (
    "old mode ",
    "new mode ",
    "deleted file mode ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "similarity index ",
    "dissimilarity index ",
    "GIT binary patch",
    "Binary files ",
)


def parse_patch_paths(patch_path: Path, allowed_new_files: frozenset[str] = frozenset()) -> set[str]:
    """Enumerate the paths a patch touches, failing closed on anything not fully understood.

    A header-only regex scan is not sufficient: `git apply` also accepts traditional unified-diff
    fragments that carry no `diff --git` header at all, so a patch could pass a header scan while
    writing files nobody enumerated. This parser instead consumes the patch exactly -- extended
    headers, `---`/`+++` pairs, and hunks by their declared line counts -- and rejects any line it
    cannot account for, so an unenumerated fragment cannot hide inside a hunk body.
    """
    try:
        text = patch_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        fail(f"cannot read patch {patch_path}: {exc}")
        raise AssertionError("unreachable") from exc
    name = patch_path.name
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    touched: set[str] = set()
    index = 0
    while index < len(lines):
        header = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", lines[index])
        if header is None:
            fail(f"{name} line {index + 1} is not an understood `diff --git` header: {lines[index]!r}")
            raise AssertionError("unreachable")
        path, mirrored = header.group(1), header.group(2)
        if path != mirrored:
            fail(f"{name} renames {path} to {mirrored}; renames are not permitted")
        if path in touched:
            fail(f"{name} contains more than one section for {path}")
        touched.add(path)
        index += 1
        while index < len(lines) and not lines[index].startswith(("--- ", "@@ ")):
            extended = lines[index]
            for forbidden in _FORBIDDEN_EXTENDED_HEADERS:
                if extended.startswith(forbidden):
                    fail(f"{name} uses the forbidden patch header {forbidden.strip()!r} for {path}")
            if extended.startswith("new file mode "):
                if path not in allowed_new_files:
                    fail(f"{name} creates {path}, which is not a declared new file")
                mode = extended[len("new file mode ") :].strip()
                if mode not in {"100644", "100755"}:
                    fail(f"{name} creates {path} with mode {mode!r}; only regular file modes are permitted")
            elif not extended.startswith("index "):
                fail(f"{name} has an unrecognised extended header for {path}: {extended!r}")
            index += 1
        if index + 1 >= len(lines):
            fail(f"{name} ends before the file headers for {path}")
        if lines[index] not in (f"--- a/{path}", "--- /dev/null"):
            fail(f"{name} has an unexpected source header for {path}: {lines[index]!r}")
        if lines[index + 1] not in (f"+++ b/{path}", "+++ /dev/null"):
            fail(f"{name} has an unexpected target header for {path}: {lines[index + 1]!r}")
        # The `new file mode` header is optional in a unified diff, so the creation check has to
        # key off the /dev/null source as well or the declared-new-file rule can simply be skipped.
        if lines[index] == "--- /dev/null" and path not in allowed_new_files:
            fail(f"{name} creates {path}, which is not a declared new file")
        if lines[index + 1] == "+++ /dev/null":
            fail(f"{name} deletes {path}; deletions are not permitted")
        index += 2
        while index < len(lines) and lines[index].startswith("@@ "):
            hunk = re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", lines[index])
            if hunk is None:
                fail(f"{name} has a malformed hunk header for {path}: {lines[index]!r}")
                raise AssertionError("unreachable")
            remaining_old = int(hunk.group(1)) if hunk.group(1) is not None else 1
            remaining_new = int(hunk.group(2)) if hunk.group(2) is not None else 1
            index += 1
            while remaining_old > 0 or remaining_new > 0:
                if index >= len(lines):
                    fail(f"{name} has a truncated hunk for {path}")
                body = lines[index]
                index += 1
                if body.startswith("\\"):
                    continue
                marker = body[:1]
                if marker in (" ", ""):
                    remaining_old -= 1
                    remaining_new -= 1
                elif marker == "-":
                    remaining_old -= 1
                elif marker == "+":
                    remaining_new -= 1
                else:
                    fail(f"{name} has an unrecognised hunk line for {path}: {body!r}")
                if remaining_old < 0 or remaining_new < 0:
                    fail(f"{name} has a hunk for {path} whose line counts disagree with its header")
            # "\ No newline at end of file" most often follows the LAST counted line of a hunk, so
            # it sits outside the counting loop and must be consumed here too. A real
            # `git diff` of a file without a trailing newline emits exactly this.
            while index < len(lines) and lines[index].startswith("\\"):
                index += 1
    return touched


def validate_patch_whitelists() -> None:
    harness_touched = parse_patch_paths(
        EVIDENCE_DIR / "common_harness.patch", frozenset(BASE_ABSENT_MANIFEST_FILES)
    )
    require_equal(harness_touched, set(COMMON_HARNESS_FILES), "common_harness.patch touched path set")
    if (EVIDENCE_DIR / "arm_A_production.patch").stat().st_size != 0:
        fail("arm_A_production.patch must be the canonical zero-byte upstream production delta")
    for arm in ("B", "C"):
        touched = parse_patch_paths(EVIDENCE_DIR / f"arm_{arm}_production.patch")
        if not touched:
            fail(f"arm_{arm}_production.patch touches no file")
        illegal = touched - set(PRODUCTION_FILES)
        if illegal:
            fail(f"arm_{arm}_production.patch touches non-production paths: {sorted(illegal)}")


def validate_build_attestation(campaign: dict[str, Any], manifests: dict[str, dict[str, Any]]) -> None:
    attestation = load_json(BUILD_ATTESTATION_PATH)
    require_equal(attestation.get("schema_version"), 1, "build attestation schema")
    require_equal(attestation.get("campaign_id"), campaign["campaign_id"], "build attestation campaign ID")
    require_equal(
        attestation.get("reconstruction_base_commit"),
        ARM_COMMITS["A"],
        "build attestation reconstruction base",
    )
    require_equal(attestation.get("build_command"), list(BUILD_COMMAND), "build attestation build command")
    require_equal(attestation.get("build_order"), list(BUILD_ORDER), "build attestation build order")
    worktree = require_nonempty_string(attestation.get("worktree_path"), "build attestation worktree path")
    if not worktree.startswith("/"):
        fail(f"build attestation worktree path is not absolute: {worktree}")

    toolchain = require_mapping(attestation.get("toolchain"), "build attestation toolchain")
    for key in ("bazel_version", "compiler_version", "kernel", "hostname", "libc"):
        require_nonempty_string(toolchain.get(key), f"build attestation toolchain.{key}")

    builds = require_mapping(attestation.get("builds"), "build attestation builds")
    require_equal(set(builds), set(BUILD_ORDER), "build attestation build set")
    for build_key in BUILD_ORDER:
        entry = require_mapping(builds.get(build_key), f"build attestation build {build_key}")
        arm = BUILD_ARM_OF[build_key]
        require_equal(entry.get("arm"), arm, f"build {build_key} arm")
        require_equal(entry.get("worktree_path"), worktree, f"build {build_key} worktree path")
        require_equal(entry.get("build_command"), list(BUILD_COMMAND), f"build {build_key} command")
        require_equal(
            entry.get("source_manifest"),
            f"arm_{arm}_source_manifest.json",
            f"build {build_key} source manifest name",
        )
        require_equal(
            entry.get("source_manifest_sha256"),
            campaign["source_artifacts"][f"arm_{arm}_source_manifest.json"],
            f"build {build_key} source manifest SHA-256",
        )
        files = require_mapping(entry.get("verified_files"), f"build {build_key} verified files")
        require_equal(set(files), set(MANIFEST_FILES), f"build {build_key} verified file set")
        for path in MANIFEST_FILES:
            require_equal(
                files.get(path),
                manifests[arm]["files"][path],
                f"build {build_key} verified identity for {path}",
            )
        _validate_identity(entry.get("output_sha256"), f"build {build_key} output SHA-256", r"[0-9a-f]{64}", False)
        _validate_identity(entry.get("build_id"), f"build {build_key} GNU Build ID", r"[0-9a-f]{8,128}", False)
        _validate_identity(entry.get("log_sha256"), f"build {build_key} log SHA-256", r"[0-9a-f]{64}", False)
        log_name = require_nonempty_string(entry.get("log"), f"build {build_key} log path")
        require_equal(
            sha256_file(EVIDENCE_DIR / log_name),
            entry["log_sha256"],
            f"build {build_key} log SHA-256 on disk",
        )
        smoke = require_mapping(entry.get("smoke"), f"build {build_key} smoke")
        require_equal(set(smoke.get("workloads", [])), set(WORKLOADS), f"build {build_key} smoke workload set")
        require_equal(smoke.get("passed"), True, f"build {build_key} smoke result")
        # The smoke must have exercised the real campaign sizes and confirmed the frozen iteration
        # rule per arm, so that a faster arm cannot abort the campaign mid-attempt.
        iterations = require_mapping(
            smoke.get("iterations_at_campaign_size"), f"build {build_key} smoke iteration counts"
        )
        require_equal(set(iterations), set(WORKLOADS), f"build {build_key} smoke iteration workload set")
        for workload in WORKLOADS:
            count = positive_integer(iterations.get(workload), f"build {build_key} smoke iterations for {workload}")
            if WORKLOAD_SPECS[workload]["role"] == "count_endpoint" and count != 1:
                fail(
                    f"build {build_key} recorded {count} benchmark iterations for count workload {workload} at its "
                    f"campaign size; the frozen protocol requires exactly one"
                )
        require_nonempty_string(entry.get("compiler_version"), f"build {build_key} compiler version")
        require_mapping(entry.get("build_environment"), f"build {build_key} build environment")
        _validate_identity(smoke.get("log_sha256"), f"build {build_key} smoke log SHA-256", r"[0-9a-f]{64}", False)
        smoke_log = require_nonempty_string(smoke.get("log"), f"build {build_key} smoke log path")
        require_equal(
            sha256_file(EVIDENCE_DIR / smoke_log),
            smoke["log_sha256"],
            f"build {build_key} smoke log SHA-256 on disk",
        )
        started = parse_timestamp(entry.get("started_at"), f"build {build_key} started_at")
        finished = parse_timestamp(entry.get("finished_at"), f"build {build_key} finished_at")
        if finished < started:
            fail(f"build {build_key} finished before it started")
        # MongoDB's bazel wrapper injects flags and can rewrite `.bazelrc.sync` from a remote flag
        # service, and `.bazelrc.local` is try-imported while being git-ignored. Pinning every
        # bazelrc digest around each build closes that channel.
        digests = require_mapping(entry.get("bazelrc_digests"), f"build {build_key} bazelrc digests")
        for phase in ("before", "after"):
            phase_digests = require_mapping(digests.get(phase), f"build {build_key} bazelrc digests {phase}")
            if not phase_digests:
                fail(f"build {build_key} recorded no bazelrc digests {phase} the build")
            for rc_name, rc_digest in sorted(phase_digests.items()):
                _validate_identity(rc_digest, f"build {build_key} {phase} digest for {rc_name}", r"[0-9a-f]{64}", False)
        if digests["before"] != digests["after"]:
            fail(f"build {build_key} changed a bazelrc file while building")
        require_nonempty_string(entry.get("effective_command"), f"build {build_key} effective command")
        require_nonempty_string(entry.get("bazel_process_summary"), f"build {build_key} bazel process summary")

    # Every arm must share one toolchain, one effective flag expansion, one build environment and
    # one bazelrc content set. The C1/C2 bracket alone cannot see a change made during the A or B
    # build and reverted before C2.
    for field, accessor in (
        ("bazelrc digests", lambda entry: entry["bazelrc_digests"]["before"]),
        ("compiler version", lambda entry: entry["compiler_version"]),
        ("effective command", lambda entry: entry["effective_command"]),
        ("build environment", lambda entry: entry["build_environment"]),
    ):
        reference = accessor(builds[BUILD_ORDER[0]])
        for build_key in BUILD_ORDER:
            if accessor(builds[build_key]) != reference:
                fail(f"build {build_key} used a different {field} than the first attested build")
    for earlier, later in zip(BUILD_ORDER, BUILD_ORDER[1:]):
        if parse_timestamp(builds[later]["started_at"], "") < parse_timestamp(builds[earlier]["finished_at"], ""):
            fail(f"attested build {later} started before {earlier} finished; the frozen build order is not evidenced")

    check = require_mapping(attestation.get("reproducibility_check"), "build attestation reproducibility check")
    require_equal(check.get("output_sha256_equal"), True, "C1/C2 output SHA-256 equality")
    require_equal(check.get("build_id_equal"), True, "C1/C2 GNU Build ID equality")
    # C1 and C2 share one worktree and one bazel output base, as the protocol requires, so C2 is
    # substantially served from the action cache. The check therefore evidences that the A and B
    # builds left the shared build state undisturbed; it is not a hermetic from-scratch rebuild,
    # and the attestation must say so rather than let the equality be read as more than it is.
    require_equal(
        check.get("scope"),
        "same_worktree_and_output_base_state_stability_not_hermetic_rebuild",
        "C1/C2 reproducibility claim scope",
    )
    require_equal(builds["C1"]["output_sha256"], builds["C2"]["output_sha256"], "C1/C2 output SHA-256")
    require_equal(builds["C1"]["build_id"], builds["C2"]["build_id"], "C1/C2 GNU Build ID")

    # Every campaign arm binary must be the attested output of its designated build.
    for arm in ARMS:
        build_key = CAMPAIGN_BUILD_OF_ARM[arm]
        require_equal(
            campaign["arms"][arm]["sha256"],
            builds[build_key]["output_sha256"],
            f"arm {arm} campaign binary SHA-256 must equal attested build {build_key}",
        )
        require_equal(
            campaign["arms"][arm]["build_id"],
            builds[build_key]["build_id"],
            f"arm {arm} campaign build ID must equal attested build {build_key}",
        )
        require_equal(
            builds[build_key].get("campaign_binary_path"),
            campaign["arms"][arm]["binary_path"],
            f"arm {arm} attested campaign binary path",
        )


def validate_source_artifacts(campaign: dict[str, Any], allow_placeholders: bool) -> None:
    artifacts = require_mapping(campaign.get("source_artifacts"), "campaign.source_artifacts")
    require_equal(set(artifacts), set(SOURCE_ARTIFACTS), "source artifact set")
    ready = True
    for name in sorted(artifacts):
        configured = artifacts[name]
        _validate_identity(configured, f"source artifact SHA-256 for {name}", r"[0-9a-f]{64}", allow_placeholders)
        if _has_placeholder(configured):
            ready = False
        else:
            require_equal(sha256_file(EVIDENCE_DIR / name), configured, f"source artifact SHA-256 for {name}")
    if not ready:
        return
    validate_patch_whitelists()
    manifests = {arm: validate_source_manifest(campaign, arm) for arm in ARMS}
    validate_cross_arm_source_invariants(manifests)
    if any(_has_placeholder(campaign["arms"][arm].get("sha256")) for arm in ARMS):
        return
    validate_build_attestation(campaign, manifests)


def validate_campaign(campaign: dict[str, Any], *, allow_placeholders: bool) -> None:
    require_equal(campaign.get("schema_version"), 3, "campaign.schema_version")
    require_equal(
        campaign.get("campaign_id"),
        "mongodb-master-countscan-three-arm-20260806-90814b83d3e5",
        "campaign.campaign_id",
    )
    expected_status = (
        {"protocol_draft_unexecutable_placeholders", "frozen_ready"} if allow_placeholders else {"frozen_ready"}
    )
    if campaign.get("status") not in expected_status:
        fail(f"campaign.status must be one of {sorted(expected_status)!r}, got {campaign.get('status')!r}")
    require_equal(campaign.get("frozen_before_execution"), True, "campaign frozen flag")
    require_equal(
        campaign.get("comparison"),
        "upstream_previous_final_three_arm",
        "campaign.comparison",
    )
    require_equal(campaign.get("attempt_ledger"), "attempt_ledger.jsonl", "campaign attempt ledger name")
    validate_protocol_artifacts(campaign, allow_placeholders)

    source = require_mapping(campaign.get("source_design"), "campaign.source_design")
    require_equal(source.get("reconstruction_base_commit"), ARM_COMMITS["A"], "source reconstruction base commit")
    # The harness was authored in the rejected heavyweight commit and is overlaid identically on
    # every arm. It is deliberately NOT tied to the candidate: re-pinning the candidate to a commit
    # that does not contain the fixture would silently redefine the "common" harness.
    require_equal(
        source.get("common_harness_source_commit"),
        HARNESS_SOURCE_COMMIT,
        "common harness source commit",
    )
    require_equal(source.get("common_harness_patch"), "common_harness.patch", "common harness patch")
    require_equal(source.get("production_files"), list(PRODUCTION_FILES), "production file set")
    require_equal(source.get("common_harness_files"), list(COMMON_HARNESS_FILES), "common harness file set")
    require_equal(source.get("build_command"), list(BUILD_COMMAND), "build command")
    require_equal(source.get("build_order"), list(BUILD_ORDER), "build order")
    require_equal(source.get("build_attestation"), "build_attestation.json", "build attestation name")
    harness_rule = source.get("common_harness_rule")
    if not isinstance(harness_rule, str) or "byte-identical" not in harness_rule or "only the six" not in harness_rule:
        fail("common harness rule does not pin byte-identical harnesses and six-file production variation")
    recipe = source.get("reconstruction_recipe")
    for fragment in ("reconstruction_base_commit", "common_harness.patch", "zero-byte", "source manifest", "whitelist"):
        if not isinstance(recipe, str) or fragment not in recipe:
            fail(f"source reconstruction recipe does not contain {fragment!r}")

    arms = require_mapping(campaign.get("arms"), "campaign.arms")
    require_equal(set(arms), set(ARMS), "campaign arm set")
    binary_hashes: set[str] = set()
    build_ids: set[str] = set()
    for arm in ARMS:
        config = require_mapping(arms.get(arm), f"campaign.arms.{arm}")
        require_equal(config.get("label"), ARM_LABELS[arm], f"arm {arm} label")
        require_equal(
            config.get("production_source_commit"),
            ARM_COMMITS[arm],
            f"arm {arm} production source commit",
        )
        require_equal(config.get("production_patch"), f"arm_{arm}_production.patch", f"arm {arm} production patch")
        require_equal(config.get("source_manifest"), f"arm_{arm}_source_manifest.json", f"arm {arm} source manifest")
        require_equal(config.get("binary_path"), ARM_BINARY_PATHS[arm], f"arm {arm} binary path")
        if config.get("binary_path") in FORBIDDEN_BINARY_PATHS:
            fail(f"arm {arm} points at a provisional reference snapshot, which can never be a formal arm")
        description = require_nonempty_string(config.get("description"), f"arm {arm} description")
        if arm == "A" and ("pristine" in description.lower() or "pristine" in ARM_LABELS[arm]):
            fail("arm A must not be described as a pristine binary; the common harness is overlaid")
        _validate_identity(config.get("sha256"), f"arm {arm} binary SHA-256", r"[0-9a-f]{64}", allow_placeholders)
        _validate_identity(config.get("build_id"), f"arm {arm} GNU Build ID", r"[0-9a-f]{8,128}", allow_placeholders)
        if not allow_placeholders:
            if config["sha256"] in FORBIDDEN_BINARY_SHA256:
                fail(
                    f"arm {arm} has the contents of a provisional reference snapshot, whose provenance "
                    f"predates the attested builder and cannot be reconstructed"
                )
            if config["sha256"] in binary_hashes:
                fail(f"arm {arm} reuses another arm's binary SHA-256")
            if config["build_id"] in build_ids:
                fail(f"arm {arm} reuses another arm's GNU Build ID")
            binary_hashes.add(config["sha256"])
            build_ids.add(config["build_id"])
    validate_source_artifacts(campaign, allow_placeholders)

    workloads = require_mapping(campaign.get("workloads"), "campaign.workloads")
    require_equal(set(workloads), set(WORKLOADS), "campaign workload set")
    for workload in WORKLOADS:
        config = require_mapping(workloads.get(workload), f"campaign.workloads.{workload}")
        expected = WORKLOAD_SPECS[workload]
        require_equal(config.get("filter"), expected["filter"], f"workload {workload} filter")
        require_equal(config.get("expected_run_name"), expected["run_name"], f"workload {workload} run name")
        require_equal(config.get("role"), expected["role"], f"workload {workload} role")
        require_equal(
            config.get("minimum_time_seconds"),
            expected["minimum_time_seconds"],
            f"workload {workload} per-workload minimum time",
        )
        require_nonempty_string(config.get("guard_contract"), f"workload {workload} guard contract")
    require_equal(
        config_iteration_rules(campaign),
        {
            "S": "exactly_one",
            "M": "exactly_one",
            "W": "exactly_one",
            "P": "equal_and_positive",
            # Verified at the campaign size, not extrapolated: one iteration takes 149 ms against a
            # 10 ms minimum. The 10k variant settles on two iterations, so assuming from the small
            # size would have aborted the campaign mid-attempt.
            "X": "exactly_one",
        },
        "per-workload iteration rules",
    )
    point_guard = workloads["P"]["guard_contract"]
    for required_fragment in ("classic", "hinted unique-field", "FETCH -> IXSCAN", "exactly one"):
        if required_fragment not in point_guard:
            fail(f"P guard contract does not contain {required_fragment!r}")

    benchmark = require_mapping(campaign.get("benchmark"), "campaign.benchmark")
    require_equal(benchmark.get("repetitions_per_process"), REPETITIONS, "benchmark repetitions")
    require_equal(benchmark.get("report_aggregates_only"), False, "aggregate-only flag")
    require_equal(benchmark.get("expected_library_build_type"), "release", "library build type")
    require_equal(benchmark.get("required_metrics"), list(METRIC_ROLES), "required metric list")
    if "minimum_time_seconds" in benchmark:
        fail("a single global benchmark minimum time is forbidden; use the per-workload values")

    execution = require_mapping(campaign.get("execution"), "campaign.execution")
    require_equal(execution.get("block_count"), BLOCK_COUNT, "block count")
    require_equal(execution.get("process_count"), PROCESS_COUNT, "process count")
    require_equal(execution.get("fresh_process_per_arm_workload_block"), True, "fresh-process flag")
    require_equal(execution.get("process_order_within_block"), "workload_then_arm", "within-block order")
    require_equal(execution.get("cpu_affinity"), SELECTED_CPU, "CPU affinity")
    require_equal(execution.get("sibling_cpu"), SIBLING_CPU, "SMT sibling CPU")
    require_equal(execution.get("taskset_command"), ["taskset", "-c", str(SELECTED_CPU)], "taskset command")
    require_equal(execution.get("required_cpu_governor"), "performance", "required CPU governor")
    require_equal(execution.get("governor_checked_before_every_process"), True, "per-process governor check flag")
    require_equal(execution.get("no_early_stopping"), True, "no-early-stopping flag")
    require_equal(execution.get("partial_reruns_forbidden"), True, "partial-rerun flag")
    require_equal(execution.get("atomic_partial_journal"), "campaign_partial.json", "partial journal")
    gates = require_mapping(execution.get("runtime_gates"), "campaign.execution.runtime_gates")
    require_equal(
        gates.get("preflight_max_busy_fraction_selected_and_sibling"),
        PREFLIGHT_MAX_BUSY_FRACTION,
        "preflight idle gate",
    )
    require_equal(
        gates.get("per_process_max_sibling_busy_fraction"),
        PER_PROCESS_MAX_SIBLING_BUSY_FRACTION,
        "per-process sibling contention gate",
    )
    require_equal(
        gates.get("per_process_min_selected_busy_fraction"),
        PER_PROCESS_MIN_SELECTED_BUSY_FRACTION,
        "per-process affinity sanity gate",
    )
    require_equal(gates.get("cooldown_seconds_before_campaign"), COOLDOWN_SECONDS, "cooldown seconds")
    require_equal(gates.get("preflight_sample_seconds"), PREFLIGHT_SAMPLE_SECONDS, "preflight sample seconds")
    require_equal(execution.get("arm_permutations"), list(ARM_PERMUTATIONS), "arm permutations")
    require_equal(execution.get("workload_rotations"), list(WORKLOAD_ROTATIONS), "workload rotations")
    expected_blocks = [
        {"block": block, "arm_order": arm_order, "workload_order": workload_order}
        for block, (arm_order, workload_order) in enumerate(BLOCK_SCHEDULE, start=1)
    ]
    require_equal(execution.get("blocks"), expected_blocks, "frozen block schedule")
    require_equal(execution.get("block_execution_order"), list(BLOCK_EXECUTION_ORDER), "frozen block execution order")
    require_equal(execution.get("block_execution_order_seed"), BLOCK_EXECUTION_SEED, "block execution order seed")
    require_equal(
        execution.get("block_execution_order_rule"),
        "random.Random(block_execution_order_seed).shuffle(list(range(1, 31)))",
        "block execution order derivation rule",
    )
    require_equal(
        Counter(arm_order for arm_order, _ in BLOCK_SCHEDULE),
        Counter({value: BLOCKS_PER_STRATUM for value in ARM_PERMUTATIONS}),
        "arm permutation balance",
    )
    require_equal(
        Counter(workload_order for _, workload_order in BLOCK_SCHEDULE),
        Counter({value: 6 for value in WORKLOAD_ROTATIONS}),
        "workload rotation balance",
    )
    require_equal(
        Counter(BLOCK_SCHEDULE),
        Counter(
            {(arm_order, workload_order): 1 for arm_order in ARM_PERMUTATIONS for workload_order in WORKLOAD_ROTATIONS}
        ),
        "arm-permutation/workload-rotation cross-balance",
    )
    expected_output_sequence()

    analysis = require_mapping(campaign.get("analysis"), "campaign.analysis")
    require_equal(
        analysis.get("process_aggregation"),
        "arithmetic_mean_of_five_iteration_rows",
        "process aggregation",
    )
    require_equal(
        analysis.get("block_effect"),
        "numerator_arm_process_mean_over_denominator_arm_process_mean_within_the_same_block_and_workload",
        "block effect",
    )
    require_equal(
        analysis.get("overall_estimator"),
        f"geometric_mean_of_{BLOCK_COUNT}_complete_block_ratios",
        "overall estimator",
    )
    interval = require_mapping(analysis.get("gate_interval"), "campaign.analysis.gate_interval")
    require_equal(interval.get("method"), "stratified_log_ratio_welch_t", "gate interval method")
    require_equal(interval.get("strata"), list(ARM_PERMUTATIONS), "gate interval strata")
    require_equal(interval.get("blocks_per_stratum"), BLOCKS_PER_STRATUM, "gate interval blocks per stratum")
    require_equal(interval.get("scale"), "natural_log_of_block_ratio", "gate interval scale")
    require_equal(
        interval.get("degrees_of_freedom_rule"),
        "welch_satterthwaite",
        "gate interval degrees of freedom rule",
    )
    require_equal(interval.get("ordinary_confidence_level"), ORDINARY_CONFIDENCE, "ordinary confidence")
    require_equal(interval.get("fail_closed_on_zero_or_non_finite_se_or_df"), True, "interval fail-closed flag")
    require_equal(
        interval.get("t_quantile_source"),
        "self_contained_validated_against_scipy_with_conservative_floor_df_bracket_guard",
        "t quantile source",
    )
    adjustment = require_mapping(interval.get("count_family_adjustment"), "count adjustment")
    require_equal(adjustment.get("method"), "Bonferroni", "count adjustment method")
    require_equal(adjustment.get("family_alpha"), 0.05, "count family alpha")
    require_equal(adjustment.get("endpoint_count"), COUNT_FAMILY_ENDPOINT_COUNT, "count family endpoint count")
    # Bind the declared family size to the loop that actually enforces it, so the two cannot drift.
    require_equal(len(COUNT_WORKLOADS), COUNT_FAMILY_ENDPOINT_COUNT, "enforced count endpoint count")
    require_equal(adjustment.get("two_sided_confidence_level"), ADJUSTED_CONFIDENCE, "adjusted confidence")
    # Six adjusted intervals are computed overall (two comparisons x three count endpoints). The
    # adoption decision within each comparison is an intersection-union test, which needs no
    # multiplicity adjustment at all and for which Bonferroni is merely conservative; the two
    # comparisons answer separate pre-registered questions. This is stated so that the 98.333333%
    # level is not silently read as simultaneous coverage of all six per-endpoint claims.
    require_equal(
        adjustment.get("multiplicity_scope"),
        "intersection_union_test_within_each_comparison_family_of_three_count_endpoints",
        "count family multiplicity scope",
    )

    thresholds = require_mapping(analysis.get("decision_thresholds"), "campaign.analysis.decision_thresholds")
    require_equal(thresholds.get("noninferiority_margin"), NONINFERIORITY_MARGIN, "noninferiority margin")
    require_equal(
        thresholds.get("cpu_non_regression_upper"),
        CPU_NON_REGRESSION_UPPER,
        "CPU non-regression upper bound",
    )
    require_equal(
        thresholds.get("point_control_instruction_band"),
        list(POINT_CONTROL_INSTRUCTION_BAND),
        "point control instruction band",
    )
    require_equal(
        thresholds.get("point_control_cpu_band"),
        list(POINT_CONTROL_CPU_BAND),
        "point control CPU band",
    )
    require_equal(thresholds.get("superiority_upper"), 1.0, "superiority upper bound")

    sensitivity = require_mapping(analysis.get("sensitivity_bootstrap"), "campaign.analysis.sensitivity_bootstrap")
    require_equal(sensitivity.get("role"), "sensitivity_only_never_read_by_any_gate_or_claim", "bootstrap role")
    require_equal(
        sensitivity.get("method"),
        "six_arm_order_strata_complete_block_resampling_with_replacement",
        "bootstrap method",
    )
    require_equal(sensitivity.get("strata"), list(ARM_PERMUTATIONS), "bootstrap strata")
    require_equal(sensitivity.get("blocks_per_stratum"), BLOCKS_PER_STRATUM, "blocks per bootstrap stratum")
    require_equal(sensitivity.get("samples"), BOOTSTRAP_SAMPLES, "bootstrap samples")
    require_equal(sensitivity.get("seed"), BOOTSTRAP_SEED, "bootstrap seed")
    require_equal(
        sensitivity.get("same_draws_across_all_comparisons_workloads_and_metrics"),
        True,
        "shared bootstrap draws",
    )

    comparisons = require_mapping(analysis.get("comparisons"), "campaign.analysis.comparisons")
    require_equal(set(comparisons), set(COMPARISONS), "comparison set")
    for name, (numerator, denominator, role) in COMPARISONS.items():
        config = require_mapping(comparisons.get(name), f"comparison {name}")
        require_equal(config.get("numerator"), numerator, f"comparison {name} numerator")
        require_equal(config.get("denominator"), denominator, f"comparison {name} denominator")
        require_equal(config.get("role"), role, f"comparison {name} role")
    require_equal(analysis.get("metric_roles"), METRIC_ROLES, "metric roles")
    adoption = require_mapping(analysis.get("adoption_gates"), "campaign.analysis.adoption_gates")
    require_equal(
        set(adoption),
        {
            "interval_family",
            "C_over_A_count_family",
            "C_over_B_count_family",
            "C_over_B_scalar_wording_states",
            "cpu_non_regression",
            "non_intrusion_control",
            "point_controls",
            "B_over_A",
            "cpu_speedup_wording",
            "wall_time_wording",
        },
        "adoption gate descriptions",
    )
    require_equal(
        adoption.get("C_over_B_scalar_wording_states"),
        ["faster", "noninferior_only", "noninferiority_failed"],
        "C/B scalar wording states",
    )
    if "bootstrap" in str(adoption.get("interval_family")).lower():
        fail("the adoption gate interval family must not reference the sensitivity bootstrap")
    # The stated rules must be exactly the enforced rules, not prose that merely resembles them.
    for key, text in expected_adoption_gate_text().items():
        require_equal(adoption.get(key), text, f"adoption gate statement for {key}")
    for key, text in GATE_INTERVAL_TEXT.items():
        require_equal(interval.get(key), text, f"gate interval statement for {key}")


GATE_INTERVAL_TEXT = {
    "point_estimator": "theta = (1/H) * sum_h mean_h, exponentiated",
    "variance_rule": "a_h = s_h^2 / (H^2 * n_h); SE = sqrt(sum_h a_h)",
    "degrees_of_freedom_formula": "df = (sum_h a_h)^2 / sum_h (a_h^2 / (n_h - 1))",
    "interval_construction": (
        "t interval on the log scale, bounds exponentiated; computed separately for every comparison, "
        "workload, and metric"
    ),
}


def expected_adoption_gate_text() -> dict[str, str]:
    """Build the human-readable pre-registration from the constants that actually enforce it.

    These statements are what a reader treats as the pre-registered rules, so they must not be
    free text that can silently drift from the thresholds in this module. Generating them from
    the constants makes divergence impossible rather than merely unlikely.
    """
    instruction_low, instruction_high = POINT_CONTROL_INSTRUCTION_BAND
    cpu_low, cpu_high = POINT_CONTROL_CPU_BAND
    return {
        "interval_family": "All adoption gates read the stratified log-ratio Welch-t interval only.",
        "C_over_A_count_family": (
            f"For S, M, and W, each Bonferroni-adjusted 98.333333% two-sided instructions ratio CI must have "
            f"upper bound below {1.0}."
        ),
        "C_over_B_count_family": (
            f"For S, M, and W, each Bonferroni-adjusted 98.333333% two-sided instructions ratio CI upper bound "
            f"must be below {NONINFERIORITY_MARGIN}. This is a noninferiority claim, not a superiority claim: the "
            f"previous implementation is a strict mechanistic superset of the candidate and is expected to be "
            f"marginally faster, so the question is whether its additional complexity bought a difference worth "
            f"paying for. An endpoint is called faster only when its adjusted upper bound is below {1.0}. This "
            f"comparison is reported as a complexity trade-off and deliberately does not veto adoption, which "
            f"is decided by C/A, the controls, and CPU non-regression."
        ),
        "cpu_non_regression": (
            f"For S, M, and W on C/A only, the ordinary 95% CPU-time ratio CI upper bound must be below "
            f"{CPU_NON_REGRESSION_UPPER}. CPU time is secondary but not absent from the decision: an "
            f"instruction reduction paired with a CPU-time regression must not be adopted. The same bound is "
            f"deliberately NOT applied to C/B, because CPU time at this block count resolves to roughly plus "
            f"or minus 1.5 percent, which cannot separate two implementations expected to differ by about one "
            f"percent; the C/B CPU interval is reported and marked unresolvable rather than gated."
        ),
        "point_controls": (
            f"For both C/A and C/B on P, the ordinary 95% instructions ratio CI must be wholly within "
            f"[{instruction_low}, {instruction_high}] and the ordinary 95% CPU-time ratio CI wholly within "
            f"[{cpu_low}, {cpu_high}]."
        ),
        "non_intrusion_control": (
            f"On workload X, whose plan is COUNT -> FETCH -> IXSCAN so the optimization cannot fire, the "
            f"ordinary 95% instructions ratio CI must lie wholly within "
            f"[{NON_INTRUSION_BAND[0]}, {NON_INTRUSION_BAND[1]}] for both C/A and C/B. The band is two-sided "
            f"because on a path neither arm touches an apparent improvement is as diagnostic of an "
            f"uncontrolled difference as a regression. This is the only workload where a regression could hide."
        ),
        "B_over_A": "Always descriptive; never an adoption gate.",
        "cpu_speedup_wording": (
            f"A CPU-time speedup may be claimed for a specific comparison/count workload only when its ordinary "
            f"95% CI upper bound is below {1.0}."
        ),
        "wall_time_wording": "Real time is auxiliary and cannot independently establish a performance claim.",
    }


def config_iteration_rules(campaign: dict[str, Any]) -> dict[str, str]:
    workloads = require_mapping(campaign.get("workloads"), "campaign.workloads")
    return {
        workload: require_nonempty_string(
            workloads[workload].get("iteration_rule"), f"workload {workload} iteration rule"
        )
        for workload in WORKLOADS
    }


def validate_log(path: Path, run_name: str) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        fail(f"cannot read log {path}: {exc}")
        raise AssertionError("unreachable") from exc
    if not text.strip():
        fail(f"empty benchmark log: {path}")
    if run_name not in text:
        fail(f"expected benchmark run name is absent from log {path}: {run_name}")
    # These patterns run mid-campaign, and under partial_reruns_forbidden a false positive costs a
    # whole attempt. A case-insensitive \bERROR\b would match any structured log field merely named
    # "error", so the checks are anchored to the structured severity field and to specific failure
    # phrasings instead.
    forbidden = (
        (r'"s"\s*:\s*"[EF]"', "MongoDB E/F severity record", 0),
        (r"^ERROR\b", "error line", re.MULTILINE),
        (r"\bFATAL\b", "fatal record", 0),
        (r"\bInvariant failure\b", "invariant failure", 0),
        (r"\bAssertion failure\b", "assertion failure", 0),
        (r"\bSlow query\b", "slow-query record", re.IGNORECASE),
    )
    for pattern, description, flags in forbidden:
        if re.search(pattern, text, flags=flags):
            fail(f"{description} found in log: {path}")


def validate_process_artifacts(
    campaign: dict[str, Any],
    item: dict[str, Any],
    raw_path: Path,
    log_path: Path,
) -> dict[str, Any]:
    arm = item["arm"]
    workload = item["workload"]
    run_name = WORKLOAD_SPECS[workload]["run_name"]
    validate_log(log_path, run_name)
    payload = load_json(raw_path)
    context = require_mapping(payload.get("context"), f"{raw_path.name}.context")
    require_equal(context.get("executable"), campaign["arms"][arm]["binary_path"], f"{raw_path.name} executable")
    require_equal(context.get("library_build_type"), "release", f"{raw_path.name} build type")
    require_equal(context.get("cpu_scaling_enabled"), False, f"{raw_path.name} CPU scaling flag")

    rows = require_list(payload.get("benchmarks"), f"{raw_path.name}.benchmarks")
    if len(rows) != REPETITIONS + 3:
        fail(f"{raw_path.name} must contain exactly {REPETITIONS} iteration and three aggregate rows; got {len(rows)}")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            fail(f"{raw_path.name} row {index} is not an object")
        require_equal(row.get("run_name"), run_name, f"{raw_path.name} row {index} run_name")

    iteration_rows = [row for row in rows if row.get("run_type") == "iteration"]
    aggregate_rows = [row for row in rows if row.get("run_type") == "aggregate"]
    require_equal(len(iteration_rows), REPETITIONS, f"{raw_path.name} iteration row count")
    require_equal(len(aggregate_rows), 3, f"{raw_path.name} aggregate row count")
    iteration_rows.sort(key=lambda row: row.get("repetition_index", -1))
    require_equal(
        [row.get("repetition_index") for row in iteration_rows],
        list(range(REPETITIONS)),
        f"{raw_path.name} repetition indexes",
    )
    iteration_counts: list[int] = []
    for repetition, row in enumerate(iteration_rows):
        prefix = f"{raw_path.name} repetition {repetition}"
        require_equal(row.get("name"), run_name, f"{prefix} name")
        require_equal(row.get("repetitions"), REPETITIONS, f"{prefix} repetition total")
        iteration_counts.append(positive_integer(row.get("iterations"), f"{prefix} iterations"))
        require_equal(row.get("threads"), 1, f"{prefix} threads")
        require_equal(row.get("time_unit"), "ns", f"{prefix} time unit")
        for metric in METRIC_ROLES:
            positive_finite(row.get(metric), f"{prefix} {metric}")

    rule = campaign["workloads"][workload]["iteration_rule"]
    if rule == "exactly_one":
        if any(count != 1 for count in iteration_counts):
            fail(
                f"{raw_path.name}: count workload {workload} requires exactly one benchmark iteration in every "
                f"repetition, got {iteration_counts}"
            )
    elif rule == "equal_and_positive":
        if len(set(iteration_counts)) != 1:
            fail(
                f"{raw_path.name}: point control requires equal positive iteration counts within a process, "
                f"got {iteration_counts}"
            )
    else:
        fail(f"{raw_path.name}: unknown iteration rule {rule!r}")

    aggregates: dict[str, dict[str, Any]] = {}
    for row in aggregate_rows:
        aggregate_name = row.get("aggregate_name")
        if aggregate_name not in {"mean", "median", "stddev"}:
            fail(f"{raw_path.name} has an unexpected aggregate: {aggregate_name!r}")
        if aggregate_name in aggregates:
            fail(f"{raw_path.name} has a duplicate aggregate: {aggregate_name}")
        aggregates[aggregate_name] = row
        require_equal(row.get("name"), f"{run_name}_{aggregate_name}", f"{raw_path.name} aggregate name")
        require_equal(row.get("iterations"), REPETITIONS, f"{raw_path.name} aggregate iterations")
        require_equal(row.get("threads"), 1, f"{raw_path.name} aggregate threads")
        require_equal(row.get("time_unit"), "ns", f"{raw_path.name} aggregate time unit")
        for metric in METRIC_ROLES:
            checker = nonnegative_finite if aggregate_name == "stddev" else positive_finite
            checker(row.get(metric), f"{raw_path.name} {aggregate_name} {metric}")
    require_equal(set(aggregates), {"mean", "median", "stddev"}, f"{raw_path.name} aggregate set")

    means = {
        metric: fsum(positive_finite(row.get(metric), f"{raw_path.name} {metric}") for row in iteration_rows)
        / REPETITIONS
        for metric in METRIC_ROLES
    }
    # Free integrity check on the raw file: our independently computed mean must agree with the
    # mean google-benchmark reported for the same rows.
    for metric in METRIC_ROLES:
        reported = float(aggregates["mean"][metric])
        if not math.isclose(reported, means[metric], rel_tol=1e-6):
            fail(
                f"{raw_path.name}: the reported {metric} mean {reported!r} disagrees with the mean "
                f"{means[metric]!r} of its own five iteration rows"
            )
    return {"context": context, "means": means, "iterations": iteration_counts}


def ledger_line_digest(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


GENESIS_LEDGER_DIGEST = "0" * 64
LEDGER_RECORD_TYPES = ("preregistration", "started", "outcome")
# Three states, not two. "unstarted" is the pre-launch gate, "started" is the runner's own in-line
# analysis (which must precede the outcome it decides), and "outcome" is post-hoc analysis. Folding
# "started" into "unstarted" would make the runner refuse its own mid-attempt state.
LEDGER_EXPECTED_STATES = ("unstarted", "started", "outcome")


def validate_attempt_ledger(campaign: dict[str, Any], campaign_sha256: str, *, expect_state: str) -> dict[str, Any]:
    """Validate the append-only attempt ledger and return the active attempt's records.

    The ledger is deliberately kept out of campaign.json's own hash maps to avoid a circular
    digest, but every record binds the attempt to the exact campaign.json SHA-256 and to every
    protocol and source artifact hash, and every record carries the digest of the preceding line
    so that removing or reordering a record breaks the chain.

    Acknowledged limitations, stated here rather than left implicit. This is a locally held file,
    and a backward hash chain only binds each record to its predecessor. Therefore:

    * Editing or removing a record in the MIDDLE of the ledger is detectable, because every later
      record's `previous_record_sha256` would no longer match.
    * Truncating the TAIL is NOT detectable from the ledger alone. After an interrupted run the
      `started` record is the last line, so deleting that one line -- together with the partial
      journal and output directories, which the runner independently refuses to overwrite --
      restores a state that validates as a clean, never-started attempt.
    * Deleting the entire ledger and starting over is likewise not disprovable locally.

    The external anchor is the only defence against those, and it is a partial one: the frozen
    campaign.json and this preregistration are committed and pushed to a remote before the first
    benchmark process runs, so the remote timestamp bounds when the protocol was fixed and proves
    the protocol was not tuned after seeing results. It does not witness anything appended later.
    The anchor commit recorded in `external_anchor` is the evidence a reader should check; the
    ledger alone is not sufficient, and the report must say so.
    """
    try:
        text = LEDGER_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"cannot read the attempt ledger {LEDGER_PATH}: {exc}")
        raise AssertionError("unreachable") from exc
    records: list[dict[str, Any]] = []
    previous_digest = GENESIS_LEDGER_DIGEST
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            fail(f"attempt ledger line {number} is blank; the ledger must be strictly append-only JSON lines")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            fail(f"attempt ledger line {number} is not valid JSON: {exc}")
            raise AssertionError("unreachable") from exc
        if not isinstance(record, dict):
            fail(f"attempt ledger line {number} is not a JSON object")
        if record.get("record_type") not in LEDGER_RECORD_TYPES:
            fail(f"attempt ledger line {number} has an unknown record type: {record.get('record_type')!r}")
        if record.get("previous_record_sha256") != previous_digest:
            fail(
                f"attempt ledger line {number} breaks the hash chain: expected previous digest "
                f"{previous_digest}, got {record.get('previous_record_sha256')!r}"
            )
        require_equal(record.get("schema_version"), 2, f"attempt ledger line {number} schema")
        attempt_id = require_nonempty_string(record.get("attempt_id"), f"attempt ledger line {number} attempt ID")
        if not re.fullmatch(r"attempt-\d{3}", attempt_id):
            fail(f"attempt ledger line {number} has a malformed attempt ID: {attempt_id!r}")
        parse_timestamp(record.get("created_at"), f"attempt ledger line {number} created_at")
        records.append(record)
        previous_digest = ledger_line_digest(line)
    if not records:
        fail("attempt ledger is empty; attempt 001 must be pre-registered before any execution")

    by_type: dict[str, list[dict[str, Any]]] = {kind: [] for kind in LEDGER_RECORD_TYPES}
    for record in records:
        by_type[record["record_type"]].append(record)
    preregistrations = by_type["preregistration"]
    if not preregistrations:
        fail("attempt ledger contains no preregistration")
    attempt_ids = [record["attempt_id"] for record in preregistrations]
    if len(set(attempt_ids)) != len(attempt_ids):
        fail("attempt ledger contains duplicate preregistrations for one attempt ID")
    # Contiguity, not merely ascending order: a gap means an attempt was removed wholesale.
    expected_ids = [f"attempt-{number:03d}" for number in range(1, len(attempt_ids) + 1)]
    require_equal(attempt_ids, expected_ids, "attempt ledger preregistration sequence")

    active = preregistrations[-1]
    active_id = active["attempt_id"]
    require_equal(active.get("status"), "preregistered", f"{active_id} preregistration status")
    require_equal(active.get("campaign_id"), campaign["campaign_id"], f"{active_id} campaign ID")
    require_equal(active.get("campaign_sha256"), campaign_sha256, f"{active_id} campaign SHA-256 binding")
    require_equal(
        active.get("protocol_artifacts"),
        campaign["protocol_artifacts"],
        f"{active_id} protocol artifact binding",
    )
    require_equal(
        active.get("source_artifacts"),
        campaign["source_artifacts"],
        f"{active_id} source artifact binding",
    )
    # The external anchor is the only defence against wholesale local deletion, so it must be a
    # structured, checkable reference rather than free prose.
    anchor = require_mapping(active.get("external_anchor"), f"{active_id} external anchor")
    require_equal(
        set(anchor),
        {"remote", "branch", "commit", "pushed_at", "note"},
        f"{active_id} external anchor fields",
    )
    require_nonempty_string(anchor.get("remote"), f"{active_id} anchor remote")
    require_nonempty_string(anchor.get("branch"), f"{active_id} anchor branch")
    _validate_identity(anchor.get("commit"), f"{active_id} anchor commit", r"[0-9a-f]{40}", False)
    parse_timestamp(anchor.get("pushed_at"), f"{active_id} anchor pushed_at")
    require_nonempty_string(anchor.get("note"), f"{active_id} anchor note")

    starts = {record["attempt_id"] for record in by_type["started"]}
    closed = {record["attempt_id"] for record in by_type["outcome"]}
    # A start or outcome must belong to an attempt that was actually pre-registered.
    known = set(attempt_ids)
    for kind in ("started", "outcome"):
        for record in by_type[kind]:
            if record["attempt_id"] not in known:
                fail(f"attempt ledger has a {kind} record for {record['attempt_id']!r}, which was never pre-registered")
    # Records are append-only, so their creation times must not go backwards.
    timestamps = [parse_timestamp(record["created_at"], "attempt ledger created_at") for record in records]
    if timestamps != sorted(timestamps):
        fail("attempt ledger records are not in non-decreasing creation order")
    outcomes_by_attempt = {record["attempt_id"]: record for record in by_type["outcome"]}
    for attempt_id in attempt_ids[:-1]:
        if attempt_id not in closed:
            fail(f"{attempt_id} was pre-registered but never closed; unfavorable attempts must be recorded, not erased")
        # A closed earlier attempt must say what actually happened, so that a completed but
        # unfavourable run cannot be disposed of with a bare hand-written "failed" line.
        prior = outcomes_by_attempt[attempt_id]
        if prior.get("status") not in {"succeeded", "failed"}:
            fail(f"{attempt_id} has an outcome with an unrecognised status: {prior.get('status')!r}")
        if not isinstance(prior.get("completed_process_count"), int) or isinstance(
            prior.get("completed_process_count"), bool
        ):
            fail(f"{attempt_id} outcome does not record how many processes it completed")
        if prior["completed_process_count"] >= PROCESS_COUNT and not prior.get("run_record_sha256"):
            fail(
                f"{attempt_id} completed every process but records no run-record digest; a completed attempt's "
                f"evidence must be retained, not discarded"
            )
    for kind in ("started", "outcome"):
        seen = [record["attempt_id"] for record in by_type[kind]]
        if len(set(seen)) != len(seen):
            fail(f"attempt ledger has more than one {kind} record for a single attempt")

    active_outcomes = [record for record in by_type["outcome"] if record["attempt_id"] == active_id]
    started_here = active_id in starts
    if expect_state == "outcome":
        if not started_here:
            fail(f"{active_id} has no start record; a completed attempt must record when it began")
        if len(active_outcomes) != 1:
            fail(f"{active_id} must have exactly one terminal outcome record, got {len(active_outcomes)}")
        outcome = active_outcomes[0]
        require_equal(outcome.get("status"), "succeeded", f"{active_id} outcome status")
        require_equal(outcome.get("campaign_sha256"), campaign_sha256, f"{active_id} outcome campaign SHA-256")
        require_equal(outcome.get("completed_process_count"), PROCESS_COUNT, f"{active_id} outcome process count")
    elif expect_state == "unstarted":
        if active_outcomes:
            fail(f"{active_id} already has a terminal outcome; pre-register a new attempt instead of rerunning it")
        # A start record with no outcome means a previous run of this same attempt was killed
        # before it could record its fate. Silently reusing the preregistration would be exactly
        # the undetectable restart this ledger exists to prevent.
        if started_here:
            fail(
                f"{active_id} was already started but never closed. Its partial output must be archived and a new "
                f"attempt pre-registered; reusing this preregistration would hide a restart."
            )
    elif expect_state == "started":
        # The runner's own in-line analysis, which must run BEFORE the outcome is written so that
        # the outcome can reflect the analysis result. The attempt is deliberately open here.
        if not started_here:
            fail(f"{active_id} has no start record, so this analysis is not running inside its own attempt")
        if active_outcomes:
            fail(f"{active_id} already has a terminal outcome; the in-run analysis must precede it")
    else:
        fail(f"unknown ledger expectation {expect_state!r}; expected one of {sorted(LEDGER_EXPECTED_STATES)}")
    return {
        "attempt_id": active_id,
        "preregistration": active,
        "outcomes": active_outcomes,
        "records": records,
        "attempt_count": len(attempt_ids),
        "history": [
            {
                "attempt_id": record["attempt_id"],
                "record_type": record["record_type"],
                "status": record.get("status"),
                "campaign_sha256": record.get("campaign_sha256"),
            }
            for record in records
        ],
        "last_digest": previous_digest,
    }


def _validate_runtime_provenance(run_record: dict[str, Any]) -> None:
    provenance = require_mapping(run_record.get("runtime_provenance"), "run record runtime provenance")
    # Sentinels must not satisfy a presence check: an unreadable field is missing provenance, not
    # provenance whose value happens to be the word "unknown".
    sentinels = {"unknown", "na", "n/a", "", "static or unreadable", "none"}
    for key in (
        "cpu_model",
        "microcode",
        "kernel",
        "libc",
        "bazel_version",
        "compiler_version",
        "python_executable",
        "python_version",
    ):
        value = require_nonempty_string(provenance.get(key), f"runtime provenance {key}")
        if value.strip().lower() in sentinels:
            fail(f"runtime provenance {key} is the placeholder {value!r}; the real value was not captured")
    require_equal(provenance.get("selected_cpu"), SELECTED_CPU, "runtime provenance selected CPU")
    require_equal(provenance.get("smt_sibling_cpu"), SIBLING_CPU, "runtime provenance SMT sibling CPU")
    require_equal(
        provenance.get("selected_cpu_thread_siblings"),
        [SELECTED_CPU, SIBLING_CPU],
        "runtime provenance thread sibling list",
    )
    # The same sentinel screen as above: turbo state in particular is provenance whose absence
    # silently undermines the measurement, so an all-"NA" reading must not satisfy a presence check.
    topology = require_nonempty_string(provenance.get("numa_topology"), "runtime provenance NUMA topology")
    if "node" not in topology:
        fail(f"runtime provenance NUMA topology was not captured: {topology!r}")
    turbo = require_nonempty_string(provenance.get("turbo_state"), "runtime provenance turbo state")
    if not re.search(r"=(?!NA\b)[^\s]+", turbo):
        fail(f"runtime provenance turbo state was not captured: {turbo!r}")
    governors = require_mapping(provenance.get("governors"), "runtime provenance governors")
    require_equal(governors.get(str(SELECTED_CPU)), "performance", "selected CPU governor")
    require_equal(governors.get(str(SIBLING_CPU)), "performance", "sibling CPU governor")
    libraries = require_mapping(provenance.get("binary_shared_libraries"), "runtime provenance shared libraries")
    require_equal(set(libraries), set(ARMS), "shared library arm set")
    for arm in ARMS:
        listing = require_nonempty_string(libraries.get(arm), f"arm {arm} shared library listing")
        if listing.strip().lower() in sentinels:
            fail(f"arm {arm} shared library listing was not captured: {listing!r}")
    environment = require_mapping(provenance.get("environment"), "runtime provenance environment")
    # A preloaded or audited library silently changes what the primary metric counts.
    for hostile in ("LD_PRELOAD", "LD_AUDIT"):
        if environment.get(hostile):
            fail(f"{hostile} was set during the campaign: {environment[hostile]!r}")

    # The binaries must have been built on the machine that measured them: a different host means
    # a different microcode, toolchain and library set than the recorded provenance describes.
    attestation = load_json(BUILD_ATTESTATION_PATH)
    require_equal(
        attestation["toolchain"]["hostname"],
        run_record["host"]["name"],
        "the attested build host must be the measurement host",
    )
    require_equal(
        attestation["toolchain"]["kernel"],
        run_record["host"]["kernel"],
        "the attested build kernel must be the measurement kernel",
    )
    require_equal(
        provenance.get("build_attestation_sha256"),
        sha256_file(BUILD_ATTESTATION_PATH),
        "runtime provenance build attestation SHA-256",
    )

    preflight = require_mapping(run_record.get("idle_preflight"), "run record idle preflight")
    require_equal(preflight.get("sample_seconds"), PREFLIGHT_SAMPLE_SECONDS, "preflight sample seconds")
    require_equal(preflight.get("cooldown_seconds"), COOLDOWN_SECONDS, "preflight cooldown seconds")
    for key in ("selected_cpu_busy_fraction", "sibling_cpu_busy_fraction"):
        value = fraction(preflight.get(key), f"preflight {key}")
        if value > PREFLIGHT_MAX_BUSY_FRACTION:
            fail(f"preflight {key} of {value} exceeds the pre-frozen idle gate {PREFLIGHT_MAX_BUSY_FRACTION}")
    require_list(preflight.get("loadavg"), "preflight loadavg")


def validate_run_record(
    campaign: dict[str, Any], run_record: dict[str, Any], campaign_sha256: str
) -> list[dict[str, Any]]:
    require_equal(run_record.get("schema_version"), 3, "run record schema")
    require_equal(run_record.get("status"), "complete", "run record status")
    require_equal(run_record.get("campaign_id"), campaign["campaign_id"], "run record campaign ID")
    require_nonempty_string(run_record.get("attempt_id"), "run record attempt ID")
    require_equal(run_record.get("campaign_sha256_preflight"), campaign_sha256, "preflight campaign SHA-256")
    require_equal(run_record.get("campaign_sha256_postflight"), campaign_sha256, "postflight campaign SHA-256")
    expected_protocol_hashes = campaign["protocol_artifacts"]
    require_equal(run_record.get("protocol_sha256_preflight"), expected_protocol_hashes, "preflight protocol hashes")
    require_equal(run_record.get("protocol_sha256_postflight"), expected_protocol_hashes, "postflight protocol hashes")
    expected_source_hashes = campaign["source_artifacts"]
    require_equal(
        run_record.get("source_artifact_sha256_preflight"),
        expected_source_hashes,
        "preflight source artifact hashes",
    )
    require_equal(
        run_record.get("source_artifact_sha256_postflight"),
        expected_source_hashes,
        "postflight source artifact hashes",
    )
    expected_identities = {
        arm: {
            "path": campaign["arms"][arm]["binary_path"],
            "sha256": campaign["arms"][arm]["sha256"],
            "build_id": campaign["arms"][arm]["build_id"],
        }
        for arm in ARMS
    }
    require_equal(run_record.get("binary_identity_preflight"), expected_identities, "preflight binary identities")
    require_equal(run_record.get("binary_identity_postflight"), expected_identities, "postflight binary identities")
    preflight_stats = require_mapping(run_record.get("binary_stat_preflight"), "preflight binary stats")
    postflight_stats = require_mapping(run_record.get("binary_stat_postflight"), "postflight binary stats")
    require_equal(set(preflight_stats), set(ARMS), "preflight binary-stat arm set")
    require_equal(postflight_stats, preflight_stats, "preflight/postflight binary stats")
    for arm in ARMS:
        stat = require_mapping(preflight_stats.get(arm), f"arm {arm} binary stat")
        require_equal(
            set(stat),
            {"device", "inode", "size", "mtime_ns", "ctime_ns"},
            f"arm {arm} binary stat fields",
        )
        for field, value in stat.items():
            positive_integer(value, f"arm {arm} binary stat {field}")
    require_equal(run_record.get("cpu_affinity"), SELECTED_CPU, "run record CPU affinity")
    require_equal(run_record.get("taskset_command"), ["taskset", "-c", str(SELECTED_CPU)], "run record taskset")
    require_equal(run_record.get("process_count"), PROCESS_COUNT, "run record process count")
    require_equal(run_record.get("repetitions_per_process"), REPETITIONS, "run record repetitions per process")

    host = require_mapping(run_record.get("host"), "run record host")
    for key in ("name", "kernel", "machine", "python"):
        require_nonempty_string(host.get(key), f"run record host.{key}")
    require_equal(host.get("cpu_governor"), "performance", "run record CPU governor")
    _validate_runtime_provenance(run_record)
    started = parse_timestamp(run_record.get("started_at"), "run record started_at")
    finished = parse_timestamp(run_record.get("finished_at"), "run record finished_at")
    if finished < started:
        fail("run record finished before it started")

    expected = expected_output_sequence()
    completed = require_list(run_record.get("completed_processes"), "run record completed processes")
    require_equal(len(completed), PROCESS_COUNT, "completed process count")
    previous_finished: datetime | None = None
    for index, (actual, static_expected) in enumerate(zip(completed, expected), start=1):
        config = require_mapping(actual, f"completed process {index}")
        for key, value in static_expected.items():
            require_equal(config.get(key), value, f"completed process {index} {key}")
        positive_integer(config.get("pid"), f"completed process {index} pid")
        require_equal(config.get("returncode"), 0, f"completed process {index} returncode")
        require_equal(config.get("governor"), "performance", f"completed process {index} governor")
        temporary_raw = (EVIDENCE_DIR / static_expected["raw"]).with_name(
            f".{Path(static_expected['raw']).name}.incomplete"
        )
        require_equal(
            config.get("command"),
            expected_benchmark_command(campaign, static_expected, temporary_raw),
            f"completed process {index} command",
        )
        for key in ("raw_sha256", "log_sha256"):
            _validate_identity(config.get(key), f"completed process {index} {key}", r"[0-9a-f]{64}", False)
        require_equal(
            config.get("raw_sha256"),
            sha256_file(EVIDENCE_DIR / static_expected["raw"]),
            f"completed process {index} raw SHA-256",
        )
        require_equal(
            config.get("log_sha256"),
            sha256_file(EVIDENCE_DIR / static_expected["log"]),
            f"completed process {index} log SHA-256",
        )
        selected_busy = fraction(config.get("selected_cpu_busy_fraction"), f"completed process {index} selected busy")
        sibling_busy = fraction(config.get("sibling_cpu_busy_fraction"), f"completed process {index} sibling busy")
        if sibling_busy > PER_PROCESS_MAX_SIBLING_BUSY_FRACTION:
            fail(
                f"completed process {index} ran with SMT sibling CPU {SIBLING_CPU} at busy fraction {sibling_busy}, "
                f"above the pre-frozen contention gate {PER_PROCESS_MAX_SIBLING_BUSY_FRACTION}"
            )
        if selected_busy < PER_PROCESS_MIN_SELECTED_BUSY_FRACTION:
            fail(
                f"completed process {index} left selected CPU {SELECTED_CPU} at busy fraction {selected_busy}, "
                f"below the pre-frozen affinity sanity gate {PER_PROCESS_MIN_SELECTED_BUSY_FRACTION}"
            )
        require_list(config.get("loadavg_before"), f"completed process {index} loadavg_before")
        require_list(config.get("loadavg_after"), f"completed process {index} loadavg_after")
        process_started = parse_timestamp(config.get("started_at"), f"completed process {index} started_at")
        process_exited = parse_timestamp(
            config.get("process_exited_at"), f"completed process {index} process_exited_at"
        )
        process_finished = parse_timestamp(config.get("finished_at"), f"completed process {index} finished_at")
        if process_exited < process_started or process_finished < process_exited:
            fail(f"completed process {index} has contradictory start/exit/finish timestamps")
        if process_started < started or process_finished > finished:
            fail(f"completed process {index} falls outside the campaign interval")
        if previous_finished is not None and process_started < previous_finished:
            fail(f"completed process {index} overlaps or contradicts the frozen serial order")
        previous_finished = process_finished
    return completed


def validate_partial_journal(
    campaign: dict[str, Any],
    run_record: dict[str, Any],
    completed: list[dict[str, Any]],
    campaign_sha256: str,
) -> None:
    journal = load_json(PARTIAL_JOURNAL_PATH)
    require_equal(journal.get("schema_version"), 2, "partial journal schema")
    require_equal(journal.get("status"), "complete", "partial journal status")
    require_equal(journal.get("campaign_id"), campaign["campaign_id"], "partial journal campaign ID")
    require_equal(journal.get("attempt_id"), run_record["attempt_id"], "partial journal attempt ID")
    require_equal(journal.get("campaign_sha256"), campaign_sha256, "partial journal campaign SHA-256")
    require_equal(journal.get("expected_process_count"), PROCESS_COUNT, "partial journal expected count")
    require_equal(journal.get("last_completed_process_index"), PROCESS_COUNT, "partial journal last process")
    require_equal(journal.get("current_process"), None, "partial journal current process")
    require_equal(journal.get("completed_processes"), completed, "partial journal completed processes")
    require_equal(journal.get("started_at"), run_record["started_at"], "partial journal start time")
    require_equal(journal.get("finished_at"), run_record["finished_at"], "partial journal finish time")
    require_equal(
        journal.get("run_record_sha256"),
        sha256_file(RUN_RECORD_PATH),
        "partial journal run-record SHA-256",
    )


def validate_exact_file_set(sequence: list[dict[str, Any]]) -> None:
    if not RAW_DIR.is_dir():
        fail(f"raw directory is missing: {RAW_DIR}")
    if not LOG_DIR.is_dir():
        fail(f"log directory is missing: {LOG_DIR}")
    expected_raw = {Path(item["raw"]).name for item in sequence}
    expected_logs = {Path(item["log"]).name for item in sequence}
    actual_raw = {entry.name for entry in RAW_DIR.iterdir()}
    actual_logs = {entry.name for entry in LOG_DIR.iterdir()}
    require_equal(actual_raw, expected_raw, "exact raw file set")
    require_equal(actual_logs, expected_logs, "exact log file set")


def _bootstrap(logs_by_key_and_block: dict[tuple[str, str, str], dict[int, float]]) -> dict[tuple[str, str, str], array]:
    """Sensitivity-only stratified bootstrap. No gate or claim may read its output."""
    for permutation, blocks in STRATUM_BLOCKS.items():
        require_equal(len(blocks), BLOCKS_PER_STRATUM, f"bootstrap block count for stratum {permutation}")
    expected_blocks = set(range(1, BLOCK_COUNT + 1))
    for key, values in logs_by_key_and_block.items():
        require_equal(set(values), expected_blocks, f"complete bootstrap blocks for {key}")

    samples = {key: array("d") for key in logs_by_key_and_block}
    rng = random.Random(BOOTSTRAP_SEED)
    keys = sorted(logs_by_key_and_block)
    for _ in range(BOOTSTRAP_SAMPLES):
        # The same six-stratum draw is reused for every comparison, workload, and metric.
        drawn_blocks = [
            STRATUM_BLOCKS[permutation][rng.randrange(BLOCKS_PER_STRATUM)]
            for permutation in ARM_PERMUTATIONS
            for _ in range(BLOCKS_PER_STRATUM)
        ]
        for key in keys:
            values = logs_by_key_and_block[key]
            samples[key].append(math.exp(fsum(values[block] for block in drawn_blocks) / BLOCK_COUNT))
    return samples


def _gate_summary(gate_inputs: dict[str, dict[str, dict[str, dict[str, list[float]]]]]) -> dict[str, Any]:
    """Compute the adoption gates.

    The only argument is a mapping of pre-frozen Welch-t intervals. The sensitivity
    bootstrap is structurally unable to influence any value produced here.
    """
    for comparison, workloads in gate_inputs.items():
        for workload, metrics in workloads.items():
            for metric, intervals in metrics.items():
                for level_key, bounds in intervals.items():
                    label = f"{comparison} {workload} {metric} {level_key}"
                    if not isinstance(bounds, list) or len(bounds) != 2:
                        fail(f"{label} is not a two-element interval")
                    lower = positive_finite(bounds[0], f"{label} lower bound")
                    upper = positive_finite(bounds[1], f"{label} upper bound")
                    if lower > upper:
                        fail(f"{label} is inverted: [{lower}, {upper}]")

    ca_count_checks = {
        workload: gate_inputs["C_over_A"][workload]["instructions_per_iteration"][
            "bonferroni_adjusted_ci98_333333"
        ][1]
        < 1.0
        for workload in COUNT_WORKLOADS
    }
    # An empty derived endpoint tuple would make all() vacuously true and pass the family.
    if len(ca_count_checks) != COUNT_FAMILY_ENDPOINT_COUNT:
        fail(f"the C/A count family must cover exactly {COUNT_FAMILY_ENDPOINT_COUNT} endpoints")
    # Uniform noninferiority across every count endpoint. The previous implementation is a strict
    # mechanistic superset of the candidate, so demanding the candidate be strictly faster would be
    # a gate designed to fail; the question this comparison exists to answer is whether the extra
    # machinery bought anything worth its maintenance cost.
    cb_count_checks: dict[str, Any] = {}
    for workload in COUNT_WORKLOADS:
        upper = gate_inputs["C_over_B"][workload]["instructions_per_iteration"][
            "bonferroni_adjusted_ci98_333333"
        ][1]
        if upper < 1.0:
            state = "faster"
        elif upper < NONINFERIORITY_MARGIN:
            state = "noninferior_only"
        else:
            state = "noninferiority_failed"
        cb_count_checks[workload] = {"adjusted_upper": upper, "state": state}
    if len(cb_count_checks) != COUNT_FAMILY_ENDPOINT_COUNT:
        fail(f"the C/B count family must cover exactly {COUNT_FAMILY_ENDPOINT_COUNT} endpoints")

    # CPU time is secondary but must not be absent from the decision: an instruction-count win
    # paired with a CPU-time regression is exactly the failure this bound exists to catch.
    # CPU time is gated for C/A only. Measured per-block log-SD for CPU time is ~0.041, giving a
    # 95% half-width near 1.55% at this block count, so a C/B CPU bound tight enough to be
    # meaningful is unreachable: the rejected implementation is a strict superset and its true CPU
    # ratio is at or above 1.0, so the upper bound would sit above any useful margin for reasons
    # unrelated to the claim. C/B CPU is reported and explicitly marked unresolvable rather than
    # gated on a test the instrument cannot pass.
    cpu_non_regression: dict[str, Any] = {}
    for comparison in ("C_over_A",):
        checks = {
            workload: gate_inputs[comparison][workload]["cpu_time"]["ordinary_ci95"][1]
            < CPU_NON_REGRESSION_UPPER
            for workload in COUNT_WORKLOADS
        }
        if len(checks) != COUNT_FAMILY_ENDPOINT_COUNT:
            fail(f"the {comparison} CPU non-regression family must cover every count endpoint")
        cpu_non_regression[comparison] = {"endpoint_checks": checks, "passed": all(checks.values())}

    point_controls: dict[str, Any] = {}
    instruction_low, instruction_high = POINT_CONTROL_INSTRUCTION_BAND
    cpu_low, cpu_high = POINT_CONTROL_CPU_BAND
    for comparison in ("C_over_A", "C_over_B"):
        instruction_ci = gate_inputs[comparison]["P"]["instructions_per_iteration"]["ordinary_ci95"]
        cpu_ci = gate_inputs[comparison]["P"]["cpu_time"]["ordinary_ci95"]
        instruction_pass = instruction_ci[0] >= instruction_low and instruction_ci[1] <= instruction_high
        cpu_pass = cpu_ci[0] >= cpu_low and cpu_ci[1] <= cpu_high
        point_controls[comparison] = {
            "instructions_ci95_wholly_within_band": instruction_pass,
            "cpu_time_ci95_wholly_within_band": cpu_pass,
            # A control can sit inside its equivalence band and still exclude 1.0. That is not a
            # gate failure, but it must never be reported as a speedup on the negative control.
            "instructions_ci95_excludes_one": instruction_ci[1] < 1.0 or instruction_ci[0] > 1.0,
            "cpu_time_ci95_excludes_one": cpu_ci[1] < 1.0 or cpu_ci[0] > 1.0,
            "passed": instruction_pass and cpu_pass,
        }

    # The un-optimized control: where the optimization cannot fire, neither arm may regress.
    # Two-sided on purpose. On a path neither arm touches, an apparent improvement is not harmless:
    # it is diagnostic of an uncontrolled difference such as changed inlining or a guard that
    # stopped firing. The band is set from the instrument's resolution, not from a round number:
    # retired instructions here reproduce to about 0.01% between repetitions, so a 0.2% band is
    # roughly twenty times the noise while still being far tighter than the effect being claimed.
    non_intrusion: dict[str, Any] = {}
    low, high = NON_INTRUSION_BAND
    for comparison in ("C_over_A", "C_over_B"):
        interval = gate_inputs[comparison]["X"]["instructions_per_iteration"]["ordinary_ci95"]
        non_intrusion[comparison] = {
            "instructions_ci95": list(interval),
            "within_band": interval[0] >= low and interval[1] <= high,
        }
    non_intrusion_pass = all(value["within_band"] for value in non_intrusion.values())

    # Restricted twice over: to the count endpoints, because the point control is not a
    # performance endpoint and must never appear as a speedup claim; and to the two adoption
    # comparisons, because B/A is declared descriptive-only and must not carry claim eligibility.
    cpu_speedup_claims = {
        comparison: {
            workload: gate_inputs[comparison][workload]["cpu_time"]["ordinary_ci95"][1] < 1.0
            for workload in COUNT_WORKLOADS
        }
        for comparison in ("C_over_A", "C_over_B")
    }
    ca_pass = all(ca_count_checks.values())
    cb_pass = all(entry["state"] != "noninferiority_failed" for entry in cb_count_checks.values())
    point_pass = all(value["passed"] for value in point_controls.values())
    cpu_pass_all = all(value["passed"] for value in cpu_non_regression.values())
    return {
        "interval_family": "stratified_log_ratio_welch_t",
        "C_over_A_count_family": {"endpoint_checks": ca_count_checks, "passed": ca_pass},
        "C_over_B_count_family": {
            "endpoint_checks": cb_count_checks,
            "passed": cb_pass,
            "wording_by_workload": {key: value["state"] for key, value in cb_count_checks.items()},
            "noninferiority_margin": NONINFERIORITY_MARGIN,
            "claim_form": "noninferiority",
        },
        "cpu_non_regression": {
            "comparisons": cpu_non_regression,
            "upper_bound": CPU_NON_REGRESSION_UPPER,
            "passed": cpu_pass_all,
        },
        "point_controls": {
            "comparisons": point_controls,
            "instruction_band": list(POINT_CONTROL_INSTRUCTION_BAND),
            "cpu_band": list(POINT_CONTROL_CPU_BAND),
            "passed": point_pass,
        },
        "non_intrusion_control": {
            "comparisons": non_intrusion,
            "band": list(NON_INTRUSION_BAND),
            "workload": "X",
            "meaning": (
                "Workload X plans to COUNT -> FETCH -> IXSCAN, so the optimization cannot fire. It is the "
                "only workload here where a regression could hide, and it is also the many-work() control "
                "for changes that touch a shared base class."
            ),
            "passed": non_intrusion_pass,
        },
        "cpu_speedup_claim_eligible_by_comparison_and_count_workload": cpu_speedup_claims,
        "B_over_A_is_descriptive_only": True,
        # Deliberately excludes cb_pass. C/B asks whether the rejected implementation's extra
        # machinery bought anything worth its cost -- a design question about that implementation,
        # not about whether this candidate should replace upstream. Letting it veto adoption would
        # mean a result of "the superset is 1.2% faster" gets published as "the candidate failed".
        "complexity_tradeoff_versus_rejected_implementation_passed": cb_pass,
        "overall_adoption_gate_passed": (
            ca_pass and point_pass and cpu_pass_all and non_intrusion_pass
        ),
    }


def analyze_campaign(*, emit: bool = True, ledger_state: str = "outcome") -> dict[str, Any]:
    """Validate and analyze a completed campaign.

    `ledger_state` is "started" only when the runner calls this in-line: at that moment the
    attempt's terminal outcome has deliberately not been written yet, because the outcome must
    reflect whether this analysis succeeded. Writing it first would let an analysis-time validity
    failure append a second, contradictory outcome record and permanently brick the attempt.
    """
    campaign = load_json(CAMPAIGN_PATH)
    validate_campaign(campaign, allow_placeholders=False)
    campaign_sha256 = sha256_file(CAMPAIGN_PATH)
    ledger = validate_attempt_ledger(campaign, campaign_sha256, expect_state=ledger_state)
    run_record = load_json(RUN_RECORD_PATH)
    require_equal(run_record.get("attempt_id"), ledger["attempt_id"], "run record attempt binding")
    completed = validate_run_record(campaign, run_record, campaign_sha256)
    validate_partial_journal(campaign, run_record, completed, campaign_sha256)
    sequence = expected_output_sequence()
    validate_exact_file_set(sequence)

    observations: dict[int, dict[str, dict[str, dict[str, Any]]]] = {
        block: {workload: {} for workload in WORKLOADS} for block in range(1, BLOCK_COUNT + 1)
    }
    observed_context_dates: list[datetime] = []
    observed_hosts: set[str] = set()
    for item in sequence:
        raw_path = EVIDENCE_DIR / item["raw"]
        log_path = EVIDENCE_DIR / item["log"]
        result = validate_process_artifacts(campaign, item, raw_path, log_path)
        observations[item["block"]][item["workload"]][item["arm"]] = result
        context = result["context"]
        observed_hosts.add(require_nonempty_string(context.get("host_name"), f"{raw_path.name} context host_name"))
        observed_context_dates.append(parse_timestamp(context.get("date"), f"{raw_path.name} context date"))
    require_equal(observed_hosts, {run_record["host"]["name"]}, "raw context host names")
    if observed_context_dates != sorted(observed_context_dates):
        fail("raw context dates contradict the frozen serial process order")
    campaign_started = parse_timestamp(run_record["started_at"], "run record started_at")
    campaign_finished = parse_timestamp(run_record["finished_at"], "run record finished_at")
    if observed_context_dates[0] < campaign_started.replace(microsecond=0):
        fail("first raw context date precedes the campaign start")
    if observed_context_dates[-1] > campaign_finished:
        fail("last raw context date follows the campaign finish")

    # The point control's iteration count is auto-calibrated by google-benchmark from a noisy
    # timing, so the three arms need not land on the same integer -- and they are least likely to
    # when the arms genuinely differ in speed. Requiring cross-arm equality would therefore make
    # attempt retention conditional on the measured effect, which is selection on the outcome
    # inside a pre-registered protocol. Both reported metrics are already per-iteration, so
    # unequal counts do not bias the ratio. The counts are recorded, not gated. Equality *within*
    # a process is still required, in validate_process_artifacts.
    point_control_iterations = {
        block: {arm: observations[block]["P"][arm]["iterations"][0] for arm in ARMS}
        for block in range(1, BLOCK_COUNT + 1)
    }

    ratios: dict[tuple[str, str, str], dict[int, float]] = {}
    log_ratios: dict[tuple[str, str, str], dict[int, float]] = {}
    for comparison, (numerator, denominator, _) in COMPARISONS.items():
        for workload in WORKLOADS:
            for metric in METRIC_ROLES:
                key = (comparison, workload, metric)
                values: dict[int, float] = {}
                for block in range(1, BLOCK_COUNT + 1):
                    numerator_value = positive_finite(
                        observations[block][workload][numerator]["means"][metric],
                        f"block {block} {workload} {numerator} {metric}",
                    )
                    denominator_value = positive_finite(
                        observations[block][workload][denominator]["means"][metric],
                        f"block {block} {workload} {denominator} {metric}",
                    )
                    values[block] = positive_finite(
                        numerator_value / denominator_value,
                        f"block {block} {comparison} {workload} {metric} ratio",
                    )
                ratios[key] = values
                log_ratios[key] = {block: math.log(value) for block, value in values.items()}

    bootstrap_samples = _bootstrap(log_ratios)
    results: dict[str, dict[str, dict[str, Any]]] = {
        comparison: {workload: {} for workload in WORKLOADS} for comparison in COMPARISONS
    }
    gate_inputs: dict[str, dict[str, dict[str, dict[str, list[float]]]]] = {
        comparison: {workload: {} for workload in WORKLOADS} for comparison in COMPARISONS
    }
    for comparison, (numerator, denominator, role) in COMPARISONS.items():
        for workload in WORKLOADS:
            for metric, metric_role in METRIC_ROLES.items():
                key = (comparison, workload, metric)
                values = ratios[key]
                estimate = geometric_mean(values.values())
                welch = stratified_log_ratio_t_interval(log_ratios[key], f"{comparison} {workload} {metric}")
                if not math.isclose(welch["point_estimate_ratio"], estimate, rel_tol=1e-9):
                    fail(
                        f"{comparison} {workload} {metric}: the stratified point estimate "
                        f"{welch['point_estimate_ratio']!r} disagrees with the geometric mean {estimate!r}"
                    )
                gate_inputs[comparison][workload][metric] = {
                    level_key: list(welch[level_key]) for level_key in CONFIDENCE_LEVELS
                }
                # The separation between the gate intervals and the sensitivity bootstrap lives
                # here, in the caller, not only inside _gate_summary. Assert it explicitly.
                for level_key in CONFIDENCE_LEVELS:
                    if gate_inputs[comparison][workload][metric][level_key] != list(welch[level_key]):
                        fail(f"{comparison} {workload} {metric}: gate input is not the Welch-t interval")
                ordered_samples = sorted(bootstrap_samples[key])
                # Deliberately different key names from the gate intervals: with four blocks per
                # stratum the percentile bootstrap undercovers, and identical key names invite a
                # downstream table generator to quote it as if it were the gate interval.
                sensitivity = {
                    f"sensitivity_percentile_{level_key}_not_for_citation": confidence_interval(
                        ordered_samples, confidence
                    )
                    for level_key, confidence in CONFIDENCE_LEVELS.items()
                }
                leave_one_out = [
                    geometric_mean(value for block, value in values.items() if block != omitted)
                    for omitted in range(1, BLOCK_COUNT + 1)
                ]
                numerator_means = [
                    observations[block][workload][numerator]["means"][metric] for block in range(1, BLOCK_COUNT + 1)
                ]
                denominator_means = [
                    observations[block][workload][denominator]["means"][metric] for block in range(1, BLOCK_COUNT + 1)
                ]
                results[comparison][workload][metric] = {
                    "comparison_role": role,
                    "metric_role": metric_role,
                    "numerator_arm": numerator,
                    "denominator_arm": denominator,
                    "ratio_geomean": estimate,
                    "gate_interval": welch,
                    "ordinary_ci95": list(welch["ordinary_ci95"]),
                    "bonferroni_adjusted_ci98_333333": list(welch["bonferroni_adjusted_ci98_333333"]),
                    "geometric_mean_reduction_percent": (1.0 - estimate) * 100.0,
                    "ordinary_reduction_percent_ci95": [
                        (1.0 - welch["ordinary_ci95"][1]) * 100.0,
                        (1.0 - welch["ordinary_ci95"][0]) * 100.0,
                    ],
                    "bonferroni_adjusted_reduction_percent_ci98_333333": [
                        (1.0 - welch["bonferroni_adjusted_ci98_333333"][1]) * 100.0,
                        (1.0 - welch["bonferroni_adjusted_ci98_333333"][0]) * 100.0,
                    ],
                    "sensitivity_bootstrap_never_used_for_gates": sensitivity,
                    "numerator_process_mean_geomean": geometric_mean(numerator_means),
                    "denominator_process_mean_geomean": geometric_mean(denominator_means),
                    "favorable_block_count": sum(value < 1.0 for value in values.values()),
                    "tie_block_count": sum(value == 1.0 for value in values.values()),
                    "unfavorable_block_count": sum(value > 1.0 for value in values.values()),
                    "leave_one_block_out": {
                        "minimum_geomean": min(leave_one_out),
                        "maximum_geomean": max(leave_one_out),
                        "all_below_one": all(value < 1.0 for value in leave_one_out),
                        "maximum_absolute_change_from_full_estimate": max(
                            abs(value - estimate) for value in leave_one_out
                        ),
                    },
                    "block_ratios": [
                        {
                            "block": block,
                            "arm_order": BLOCK_SCHEDULE[block - 1][0],
                            "workload_order": BLOCK_SCHEDULE[block - 1][1],
                            "ratio": values[block],
                        }
                        for block in range(1, BLOCK_COUNT + 1)
                    ],
                }

    gate_summary = _gate_summary(gate_inputs)
    input_hashes = {
        "campaign.json": campaign_sha256,
        "campaign_run.json": sha256_file(RUN_RECORD_PATH),
        "campaign_partial.json": sha256_file(PARTIAL_JOURNAL_PATH),
        "attempt_ledger.jsonl": sha256_file(LEDGER_PATH),
    }
    for name in PROTOCOL_ARTIFACTS:
        input_hashes[name] = sha256_file(EVIDENCE_DIR / name)
    for name in SOURCE_ARTIFACTS:
        input_hashes[name] = sha256_file(EVIDENCE_DIR / name)
    for item in sequence:
        input_hashes[item["raw"]] = sha256_file(EVIDENCE_DIR / item["raw"])
        input_hashes[item["log"]] = sha256_file(EVIDENCE_DIR / item["log"])

    summary = {
        "schema_version": 5,
        "campaign_id": campaign["campaign_id"],
        "comparison": campaign["comparison"],
        "attempt_id": ledger["attempt_id"],
        "attempt_count": ledger["attempt_count"],
        "attempt_history": ledger["history"],
        # The docstring tells a reader that the external anchor is the only defence against
        # wholesale local deletion, so the anchor has to reach the artifact the reader receives.
        "external_anchor": ledger["preregistration"]["external_anchor"],
        "point_control_iterations_by_block": point_control_iterations,
        "valid_complete_campaign": True,
        "arm_identities": {
            arm: {
                "label": campaign["arms"][arm]["label"],
                "production_source_commit": campaign["arms"][arm]["production_source_commit"],
                "binary_path": campaign["arms"][arm]["binary_path"],
                "sha256": campaign["arms"][arm]["sha256"],
                "build_id": campaign["arms"][arm]["build_id"],
            }
            for arm in ARMS
        },
        "block_count": BLOCK_COUNT,
        "process_count": PROCESS_COUNT,
        "repetitions_per_process": REPETITIONS,
        "block_schedule": campaign["execution"]["blocks"],
        "gate_interval": campaign["analysis"]["gate_interval"],
        "sensitivity_bootstrap": campaign["analysis"]["sensitivity_bootstrap"],
        "results": results,
        "adoption_gates": gate_summary,
        "run_host": run_record["host"],
        "runtime_provenance": run_record["runtime_provenance"],
        "run_started_at": run_record["started_at"],
        "run_finished_at": run_record["finished_at"],
        "completed_process_count": len(completed),
        "input_sha256": input_hashes,
    }
    serialized = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    temporary = SUMMARY_PATH.with_name(f".{SUMMARY_PATH.name}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
        temporary.replace(SUMMARY_PATH)
    except OSError as exc:
        fail(f"cannot atomically write summary {SUMMARY_PATH}: {exc}")
    if emit:
        sys.stdout.write(serialized)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--validate-template",
        action="store_true",
        help="validate the frozen design while permitting obvious execution-blocking identity placeholders",
    )
    group.add_argument(
        "--validate-campaign-only",
        action="store_true",
        help="require an execution-ready frozen campaign and validate it without reading results",
    )
    arguments = parser.parse_args()
    campaign = load_json(CAMPAIGN_PATH)
    if arguments.validate_template:
        validate_campaign(campaign, allow_placeholders=True)
        print("frozen campaign template validation: PASS (execution remains blocked by placeholders)")
        return 0
    if arguments.validate_campaign_only:
        validate_campaign(campaign, allow_placeholders=False)
        validate_attempt_ledger(campaign, sha256_file(CAMPAIGN_PATH), expect_state="unstarted")
        print("execution-ready frozen campaign validation: PASS")
        return 0
    summary = analyze_campaign()
    # A failed adoption gate is a real outcome, not a crash -- but it must not look like success to
    # a shell or CI wrapper that keys on the exit status.
    passed = summary["adoption_gates"]["overall_adoption_gate_passed"]
    print(f"overall_adoption_gate_passed={passed}", file=sys.stderr)
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
