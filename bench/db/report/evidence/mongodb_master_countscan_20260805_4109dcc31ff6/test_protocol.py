#!/usr/bin/env python3

"""Self-contained fail-closed tests for the frozen three-arm CountScan protocol.

Run with `python3 test_protocol.py`. The suite needs no third-party package: the SciPy
reference values it checks against are embedded below. When SciPy happens to be importable
the same assertions are additionally re-checked against the live library, which is how the
embedded tables were validated before the protocol was frozen.
"""

from __future__ import annotations

import copy
import json
import math
import random
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import analyze

REAL_EVIDENCE_DIR = Path(__file__).resolve().parent

# Reference values produced with SciPy 1.18.0 (scipy.stats.t.ppf).
SCIPY_ORACLE_VERSION = "1.18.0"
T_QUANTILE_ORACLE = (
    (0.975, 1.0, 12.706204736174694),
    (0.975, 2.0, 4.302652729749462),
    (0.975, 3.0, 3.1824463052837078),
    (0.975, 3.5, 2.9400886379827282),
    (0.975, 4.25, 2.7132058471460594),
    (0.975, 5.0, 2.5705818356363146),
    (0.975, 7.3, 2.345066736547703),
    (0.975, 10.0, 2.228138851986274),
    (0.975, 12.5, 2.1691859427125406),
    (0.975, 15.0, 2.131449545559776),
    (0.975, 17.0, 2.1098155778333156),
    (0.975, 17.999, 2.1009304070166808),
    (0.975, 18.0, 2.1009220402410382),
    (0.975, 25.0, 2.0595385527532972),
    (0.975, 40.0, 2.021075390306273),
    (0.975, 120.0, 1.9799304050824402),
    (0.9916666666666666, 1.0, 38.18845929702524),
    (0.9916666666666666, 2.0, 7.648803937915502),
    (0.9916666666666666, 3.0, 4.856657272768959),
    (0.9916666666666666, 3.5, 4.313527661590146),
    (0.9916666666666666, 4.25, 3.8276410338826783),
    (0.9916666666666666, 5.0, 3.5341107040583575),
    (0.9916666666666666, 7.3, 3.0900568506640336),
    (0.9916666666666666, 10.0, 2.870072555658972),
    (0.9916666666666666, 12.5, 2.7619464739387136),
    (0.9916666666666666, 15.0, 2.69373931919648),
    (0.9916666666666666, 17.0, 2.6549955835548347),
    (0.9916666666666666, 17.999, 2.6391597103373883),
    (0.9916666666666666, 18.0, 2.639144819415987),
    (0.9916666666666666, 25.0, 2.565978552077895),
    (0.9916666666666666, 40.0, 2.4988558186935386),
    (0.9916666666666666, 120.0, 2.428004094070071),
)

# Every entry of analyze._T_CRITICAL_TABLE, taken independently from SciPy 1.18.0, so the
# guard table is validated against an external source rather than against the quantile it guards.
T_CRITICAL_TABLE_ORACLE = {
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

WELCH_ORACLE_LOG_RATIOS = {
    1: -0.05563411606076841,
    2: -0.05436281029269727,
    3: -0.05643551996776053,
    4: -0.059027209981231704,
    5: -0.06383569709865256,
    6: -0.0601176619927957,
    7: -0.05537205360512484,
    8: -0.053042567013506466,
    9: -0.05435152605903297,
    10: -0.062176084443557475,
    11: -0.06299996082620281,
    12: -0.06014629167200174,
    13: -0.05531933185269343,
    14: -0.05203757057722051,
    15: -0.05739884863937153,
    16: -0.060151613266660256,
    17: -0.061845589967518225,
    18: -0.060003948987086705,
    19: -0.055400491161348195,
    20: -0.056348218997089486,
    21: -0.05565337744585577,
    22: -0.05803540523716161,
    23: -0.06038488161670068,
    24: -0.05962231344802649,
    25: -0.06052940700039109,
    26: -0.055949766198081584,
    27: -0.05417449628638198,
    28: -0.05591637684676852,
    29: -0.05865453553685186,
    30: -0.06395212649637144,
}
WELCH_ORACLE = {
    "theta": -0.05796265995249706,
    "se": 0.00036097880990777287,
    "df": 21.833674081056927,
    "point": 0.9436851840285259,
    "ordinary_ci95": (0.9429786708778183, 0.9443922265240082),
    "bonferroni_adjusted_ci98_333333": (0.9428023492480841, 0.9445688454904562),
}

FAILURES: list[str] = []
PASSED = 0


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def assert_fails(action: Callable[[], Any], fragment: str = "") -> str:
    try:
        action()
    except SystemExit as exc:
        text = str(exc)
        check(fragment in text, f"expected failure containing {fragment!r}, got {text!r}")
        return text
    raise AssertionError(f"expected a fail-closed SystemExit containing {fragment!r}, but the call succeeded")


# ---------------------------------------------------------------------------
# Statistical oracle tests
# ---------------------------------------------------------------------------


def test_t_quantile_matches_scipy_oracle() -> None:
    for probability, degrees, expected in T_QUANTILE_ORACLE:
        actual = analyze.student_t_ppf(probability, degrees)
        relative = abs(actual - expected) / expected
        check(
            relative < 1e-10,
            f"t quantile at p={probability}, df={degrees}: {actual!r} differs from SciPy {expected!r} "
            f"by relative {relative:.3e}",
        )


def test_t_quantile_matches_live_scipy_when_available() -> None:
    try:
        import scipy
        from scipy.stats import t as scipy_t
    except ImportError:
        print(
            "    *** SKIPPED: SciPy is not importable on this machine, so the live cross-check did NOT run. "
            "The embedded oracle above was validated against SciPy "
            f"{SCIPY_ORACLE_VERSION} before the protocol was frozen. ***"
        )
        return
    print(f"    live SciPy {scipy.__version__} cross-check (oracle was generated with {SCIPY_ORACLE_VERSION})")
    for degrees in (1.0, 3.0, 3.5, 6.75, 11.327484916417989, 18.0, 30.0):
        for probability in (0.975, 0.9916666666666666, 0.995):
            expected = float(scipy_t.ppf(probability, degrees))
            actual = analyze.student_t_ppf(probability, degrees)
            check(
                abs(actual - expected) / expected < 1e-10,
                f"live SciPy mismatch at p={probability}, df={degrees}: {actual!r} vs {expected!r}",
            )


def test_embedded_critical_table_matches_an_independent_oracle() -> None:
    """Validate the guard table against SciPy, NOT against the quantile it exists to guard.

    Comparing the table to `student_t_ppf` would be circular: a common-mode error would make the
    table wrong and the comparison green at the same time.
    """
    for level_key in analyze.CONFIDENCE_LEVELS:
        oracle = T_CRITICAL_TABLE_ORACLE[level_key]
        check(len(oracle) == analyze._T_TABLE_MAX_DF, f"{level_key} oracle must cover every tabulated df")
        for degrees in range(1, analyze._T_TABLE_MAX_DF + 1):
            tabulated = analyze._table_critical(level_key, degrees)
            expected = oracle[degrees - 1]
            check(
                abs(tabulated - expected) / expected < 1e-12,
                f"embedded {level_key} table entry at df={degrees} is {tabulated!r}, SciPy says {expected!r}",
            )


def test_bracket_guard_actually_fires_on_a_wrong_quantile() -> None:
    """The guard is only worth having if it rejects a bad quantile; make it do so."""
    original = analyze.student_t_ppf
    try:
        analyze.student_t_ppf = lambda probability, degrees: original(probability, degrees) * 1.5
        assert_fails(
            lambda: analyze.guarded_t_critical(0.95, 11.327484916417989, "ordinary_ci95"),
            "escapes the conservative floor-df bracket",
        )
        analyze.student_t_ppf = lambda probability, degrees: original(probability, degrees) * 0.5
        assert_fails(
            lambda: analyze.guarded_t_critical(0.95, 11.327484916417989, "ordinary_ci95"),
            "escapes the conservative floor-df bracket",
        )
    finally:
        analyze.student_t_ppf = original
    # And the honest value must still pass after restoration.
    analyze.guarded_t_critical(0.95, 11.327484916417989, "ordinary_ci95")


def test_t_cdf_and_ppf_are_inverse() -> None:
    for degrees in (3.0, 4.5, 12.0, 18.0):
        for probability in (0.6, 0.9, 0.975, 0.9916666666666666, 0.999):
            quantile = analyze.student_t_ppf(probability, degrees)
            recovered = analyze.student_t_cdf(quantile, degrees)
            check(
                abs(recovered - probability) < 1e-12,
                f"CDF/PPF round trip failed at p={probability}, df={degrees}: {recovered!r}",
            )
    check(analyze.student_t_cdf(0.0, 5.0) == 0.5, "the Student-t CDF must be 0.5 at zero")


def test_t_quantile_fails_closed_on_bad_input() -> None:
    assert_fails(lambda: analyze.student_t_ppf(0.0, 5.0), "probability in (0, 1)")
    assert_fails(lambda: analyze.student_t_ppf(1.0, 5.0), "probability in (0, 1)")
    assert_fails(lambda: analyze.student_t_ppf(0.975, 0.0), "positive finite degrees of freedom")
    assert_fails(lambda: analyze.student_t_ppf(0.975, float("nan")), "positive finite degrees of freedom")
    assert_fails(lambda: analyze.guarded_t_critical(0.95, 0.5, "ordinary_ci95"), "below one are not admissible")


def test_guarded_critical_value_is_bracketed_by_the_floor_df_table() -> None:
    for degrees in (3.0, 3.25, 7.5, 11.327484916417989, 17.9, 18.0):
        for level_key, confidence in analyze.CONFIDENCE_LEVELS.items():
            critical = analyze.guarded_t_critical(confidence, degrees, level_key)
            floor_value = analyze._table_critical(level_key, math.floor(degrees))
            ceil_value = analyze._table_critical(level_key, math.ceil(degrees))
            check(
                ceil_value - 1e-9 <= critical <= floor_value + 1e-9,
                f"critical value {critical!r} at df={degrees} escaped [{ceil_value!r}, {floor_value!r}]",
            )


def test_welch_interval_matches_independent_oracle() -> None:
    result = analyze.stratified_log_ratio_t_interval(WELCH_ORACLE_LOG_RATIOS, "oracle")
    check(
        abs(result["mean_log_ratio"] - WELCH_ORACLE["theta"]) < 1e-14,
        f"theta {result['mean_log_ratio']!r} != oracle {WELCH_ORACLE['theta']!r}",
    )
    check(
        abs(result["standard_error_log_scale"] - WELCH_ORACLE["se"]) / WELCH_ORACLE["se"] < 1e-12,
        f"SE {result['standard_error_log_scale']!r} != oracle {WELCH_ORACLE['se']!r}",
    )
    check(
        abs(result["degrees_of_freedom"] - WELCH_ORACLE["df"]) / WELCH_ORACLE["df"] < 1e-12,
        f"df {result['degrees_of_freedom']!r} != oracle {WELCH_ORACLE['df']!r}",
    )
    check(
        abs(result["point_estimate_ratio"] - WELCH_ORACLE["point"]) / WELCH_ORACLE["point"] < 1e-12,
        f"point estimate {result['point_estimate_ratio']!r} != oracle {WELCH_ORACLE['point']!r}",
    )
    for level_key in analyze.CONFIDENCE_LEVELS:
        for index, expected in enumerate(WELCH_ORACLE[level_key]):
            actual = result[level_key][index]
            check(
                abs(actual - expected) / expected < 1e-10,
                f"{level_key}[{index}] {actual!r} != oracle {expected!r}",
            )


def test_welch_point_estimate_equals_geometric_mean() -> None:
    result = analyze.stratified_log_ratio_t_interval(WELCH_ORACLE_LOG_RATIOS, "oracle")
    geometric = analyze.geometric_mean(math.exp(value) for value in WELCH_ORACLE_LOG_RATIOS.values())
    check(
        math.isclose(result["point_estimate_ratio"], geometric, rel_tol=1e-12),
        "the balanced stratified point estimate must equal the geometric mean of the block ratios",
    )


def test_welch_degrees_of_freedom_are_capped_at_the_balanced_maximum() -> None:
    # Six strata of four blocks with equal stratum variances give the maximum attainable df.
    strata_count = len(analyze.ARM_PERMUTATIONS)
    per_stratum = analyze.BLOCKS_PER_STRATUM
    maximum = float(strata_count * (per_stratum - 1))
    minimum = float(per_stratum - 1)
    equal = {block: (0.01 if block % 4 < 2 else -0.01) for block in range(1, analyze.BLOCK_COUNT + 1)}
    result = analyze.stratified_log_ratio_t_interval(equal, "equal-variance")
    check(
        abs(result["degrees_of_freedom"] - maximum) < 1e-9,
        f"balanced equal-variance df should be {maximum}, got {result['degrees_of_freedom']!r}",
    )
    check(maximum <= analyze._T_TABLE_MAX_DF, "the guard table must cover the attainable df range")
    for block_values in (WELCH_ORACLE_LOG_RATIOS, equal):
        computed = analyze.stratified_log_ratio_t_interval(block_values, "range")["degrees_of_freedom"]
        check(minimum - 1e-9 <= computed <= maximum + 1e-9,
              f"df {computed!r} outside the attainable [{minimum}, {maximum}]")


def test_welch_fails_closed_on_degenerate_input() -> None:
    constant = {block: -0.05 for block in range(1, analyze.BLOCK_COUNT + 1)}
    assert_fails(
        lambda: analyze.stratified_log_ratio_t_interval(constant, "constant"),
        "stratified variance is zero or non-finite",
    )
    incomplete = dict(WELCH_ORACLE_LOG_RATIOS)
    del incomplete[7]
    assert_fails(lambda: analyze.stratified_log_ratio_t_interval(incomplete, "incomplete"), "is missing block")
    nonfinite = dict(WELCH_ORACLE_LOG_RATIOS)
    nonfinite[3] = float("inf")
    assert_fails(lambda: analyze.stratified_log_ratio_t_interval(nonfinite, "nonfinite"), "non-finite log ratio")


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------


def _gate_inputs(
    *,
    ca_upper: float = 0.96,
    ca_upper_by_workload: dict[str, float] | None = None,
    cb_scalar_upper: float = 0.99,
    cb_other_upper: float = 0.97,
    cpu_upper: float | None = None,
    point_instructions: tuple[float, float] = (0.9995, 1.0005),
    point_cpu: tuple[float, float] = (0.995, 1.005),
    non_intrusion: tuple[float, float] = (0.9995, 1.0005),
) -> dict[str, Any]:
    def entry(ordinary: tuple[float, float], adjusted: tuple[float, float]) -> dict[str, list[float]]:
        return {"ordinary_ci95": list(ordinary), "bonferroni_adjusted_ci98_333333": list(adjusted)}

    inputs: dict[str, Any] = {}
    for comparison in analyze.COMPARISONS:
        inputs[comparison] = {}
        for workload in analyze.WORKLOADS:
            if workload == "P":
                inputs[comparison][workload] = {
                    "instructions_per_iteration": entry(point_instructions, point_instructions),
                    "cpu_time": entry(point_cpu, point_cpu),
                    "real_time": entry(point_cpu, point_cpu),
                }
                continue
            if workload == "X":
                # The un-optimized control: no arm touches that path, so all arms must agree.
                inputs[comparison][workload] = {
                    "instructions_per_iteration": entry(non_intrusion, non_intrusion),
                    "cpu_time": entry(point_cpu, point_cpu),
                    "real_time": entry(point_cpu, point_cpu),
                }
                continue
            if comparison == "C_over_A":
                upper = (ca_upper_by_workload or {}).get(workload, ca_upper)
            elif comparison == "C_over_B":
                upper = cb_scalar_upper if workload == "S" else cb_other_upper
            else:
                upper = 0.98
            span = (upper - 0.01, upper)
            cpu_span = span if cpu_upper is None else (cpu_upper - 0.01, cpu_upper)
            inputs[comparison][workload] = {
                "instructions_per_iteration": entry(span, span),
                "cpu_time": entry(cpu_span, cpu_span),
                "real_time": entry(span, span),
            }
    return inputs


def test_frozen_artifacts_match_the_pinned_arm_identities() -> None:
    """The manifests on disk must describe the arms this protocol actually pins.

    The synthetic-bundle tests build every manifest from analyze.py's own constants, so they cannot
    detect a real artifact that was generated before an arm was re-pinned. This test reads the
    artifacts as they exist and compares them against the pinned commit-derived identities.
    """
    for arm in analyze.ARMS:
        path = REAL_EVIDENCE_DIR / f"arm_{arm}_source_manifest.json"
        if not path.is_file():
            print(f"    (arm {arm} manifest not generated yet; skipping)")
            continue
        manifest = analyze.load_json(path)
        expected = analyze.expected_manifest_blobs(arm)
        for source_path in analyze.MANIFEST_FILES:
            actual = manifest["files"][source_path]["git_blob"]
            check(
                actual == expected[source_path],
                f"arm {arm} manifest for {source_path} is {actual}, but this protocol pins "
                f"{expected[source_path]}; the artifacts were generated for a different arm set",
            )


def test_build_arms_does_not_duplicate_pinned_identities() -> None:
    """Duplicated literals are how the artifacts and the protocol drifted apart once already."""
    import build_arms

    check(
        build_arms.EXPECTED_POINT_CONTROL_BLOB == analyze.POINT_CONTROL_BLOB,
        "the point-control blob must have exactly one source of truth",
    )
    check(
        build_arms.HARNESS_SOURCE_COMMIT == analyze.HARNESS_SOURCE_COMMIT,
        "the harness source commit must have exactly one source of truth",
    )
    source = (REAL_EVIDENCE_DIR / "build_arms.py").read_text()
    campaign_id = analyze.load_json(REAL_EVIDENCE_DIR / "campaign.json")["campaign_id"]
    check(
        f'"{campaign_id}"' not in source,
        "build_arms.py must read the campaign id rather than hard-code it",
    )


def test_gate_previous_implementation_is_a_noninferiority_test() -> None:
    """C/B must be a noninferiority test, applied uniformly to every count endpoint.

    The previous implementation is a strict mechanistic superset of the candidate, so a superiority
    gate there would be designed to fail. Every endpoint uses the same margin; there is no
    per-workload carve-out.
    """
    margin = analyze.NONINFERIORITY_MARGIN

    faster = analyze._gate_summary(_gate_inputs(cb_scalar_upper=0.995, cb_other_upper=0.995))
    states = faster["C_over_B_count_family"]["wording_by_workload"]
    check(set(states) == set(analyze.COUNT_WORKLOADS), "every count endpoint must be reported")
    check(all(value == "faster" for value in states.values()), "upper below 1 must read faster")
    check(faster["complexity_tradeoff_versus_rejected_implementation_passed"] is True, "baseline C/B passes")

    # The previous implementation being marginally ahead must still pass, on every endpoint.
    edge = margin - 0.001
    noninferior = analyze._gate_summary(_gate_inputs(cb_scalar_upper=edge, cb_other_upper=edge))
    states = noninferior["C_over_B_count_family"]["wording_by_workload"]
    check(all(value == "noninferior_only" for value in states.values()), f"upper in [1, {margin}) is noninferior")
    check(
        noninferior["complexity_tradeoff_versus_rejected_implementation_passed"] is True,
        "noninferior_only must pass the complexity trade-off",
    )

    # A single endpoint breaching the margin fails the family, whichever endpoint it is.
    for workload in analyze.COUNT_WORKLOADS:
        overrides = {"cb_scalar_upper": 0.99, "cb_other_upper": 0.99}
        if workload == "S":
            overrides["cb_scalar_upper"] = margin
        else:
            overrides["cb_other_upper"] = margin
        failed = analyze._gate_summary(_gate_inputs(**overrides))
        breached = [k for k, v in failed["C_over_B_count_family"]["wording_by_workload"].items()
                    if v == "noninferiority_failed"]
        check(bool(breached), f"an upper at the margin must be labelled noninferiority_failed ({workload})")
        check(
            failed["complexity_tradeoff_versus_rejected_implementation_passed"] is False,
            f"a breached margin must fail the complexity trade-off ({workload})",
        )
        check(
            failed["overall_adoption_gate_passed"] is True,
            f"C/B must NOT veto adoption: it answers a design question, not the adoption question ({workload})",
        )

    check(
        analyze._gate_summary(_gate_inputs())["C_over_B_count_family"]["claim_form"] == "noninferiority",
        "the C/B claim form must be recorded as noninferiority, not superiority",
    )


def test_gate_requires_every_count_endpoint_individually() -> None:
    """Flip one endpoint at a time, so an all()/any() confusion cannot hide."""
    for workload in analyze.COUNT_WORKLOADS:
        summary = analyze._gate_summary(_gate_inputs(ca_upper_by_workload={workload: 1.0}))
        check(
            summary["C_over_A_count_family"]["passed"] is False,
            f"a single failing C/A endpoint ({workload}) must fail the family",
        )
        check(summary["overall_adoption_gate_passed"] is False, f"{workload} failure must fail the overall gate")
    # C/B is a reported complexity trade-off, not an adoption gate, so it cannot fail adoption.
    summary = analyze._gate_summary(_gate_inputs(cb_other_upper=1.0))
    check(summary["overall_adoption_gate_passed"] is True, "C/B must not veto adoption")
    check(
        summary["complexity_tradeoff_versus_rejected_implementation_passed"] is True,
        "a C/B upper of 1.0 is within the noninferiority margin",
    )


def test_gate_rejects_an_empty_or_short_endpoint_family() -> None:
    original = analyze.COUNT_WORKLOADS
    try:
        analyze.COUNT_WORKLOADS = ()
        assert_fails(lambda: analyze._gate_summary(_gate_inputs()), "must cover exactly")
        analyze.COUNT_WORKLOADS = ("S",)
        assert_fails(lambda: analyze._gate_summary(_gate_inputs()), "must cover exactly")
    finally:
        analyze.COUNT_WORKLOADS = original


def test_gate_requires_cpu_non_regression_on_the_adoption_comparison_only() -> None:
    """CPU time cannot resolve C/B at this block count, so it is gated on C/A alone."""
    passing = analyze._gate_summary(_gate_inputs(cpu_upper=1.009))
    check(passing["cpu_non_regression"]["passed"] is True, "a CPU upper below the bound must pass")
    check(passing["overall_adoption_gate_passed"] is True, "a CPU upper below the bound must not fail the gate")
    check(
        set(passing["cpu_non_regression"]["comparisons"]) == {"C_over_A"},
        "the CPU gate must cover C/A only; a C/B CPU bound is unreachable at this block count",
    )

    regressed = analyze._gate_summary(_gate_inputs(cpu_upper=1.011))
    check(regressed["cpu_non_regression"]["passed"] is False, "a CPU upper above the bound must fail its gate")
    check(
        regressed["overall_adoption_gate_passed"] is False,
        "an instruction win paired with a CPU regression must not be adopted",
    )


def test_gate_non_intrusion_control_is_two_sided() -> None:
    """Workload X is where a regression could hide, and an apparent improvement is equally telling."""
    low, high = analyze.NON_INTRUSION_BAND
    inside = analyze._gate_summary(_gate_inputs())
    check(inside["non_intrusion_control"]["passed"] is True, "an in-band control must pass")
    check(inside["overall_adoption_gate_passed"] is True, "an in-band control must not fail adoption")

    for band, label in (((high, high + 0.001), "regression"), ((low - 0.001, low), "improvement")):
        summary = analyze._gate_summary(_gate_inputs(non_intrusion=band))
        check(
            summary["non_intrusion_control"]["passed"] is False,
            f"an out-of-band control ({label}) must fail: {band}",
        )
        check(
            summary["overall_adoption_gate_passed"] is False,
            f"an out-of-band control ({label}) must fail adoption",
        )


def test_gate_point_control_bounds_on_both_sides() -> None:
    low, high = analyze.POINT_CONTROL_INSTRUCTION_BAND
    for band in ((low - 0.001, 1.0), (1.0, high + 0.001)):
        check(
            analyze._gate_summary(_gate_inputs(point_instructions=band))["overall_adoption_gate_passed"] is False,
            f"a point control instructions CI outside the band must fail: {band}",
        )
    for band in ((0.96, 1.001), (0.999, 1.031)):
        check(
            analyze._gate_summary(_gate_inputs(point_cpu=band))["overall_adoption_gate_passed"] is False,
            f"a point control CPU CI outside the band must fail: {band}",
        )


def test_gate_never_claims_a_speedup_on_the_point_control() -> None:
    summary = analyze._gate_summary(_gate_inputs(point_cpu=(0.972, 0.995)))
    claims = summary["cpu_speedup_claim_eligible_by_comparison_and_count_workload"]
    for comparison in claims:
        check("P" not in claims[comparison], "the negative control must never appear as a speedup claim")
    check(
        summary["point_controls"]["comparisons"]["C_over_A"]["cpu_time_ci95_excludes_one"] is True,
        "a control interval that excludes 1.0 must be flagged even while inside its band",
    )
    check(summary["point_controls"]["passed"] is True, "that interval is still inside the equivalence band")


def test_gate_rejects_malformed_intervals() -> None:
    inverted = _gate_inputs()
    inverted["C_over_A"]["S"]["instructions_per_iteration"]["ordinary_ci95"] = [5.0, 0.5]
    assert_fails(lambda: analyze._gate_summary(inverted), "is inverted")
    negative = _gate_inputs()
    negative["C_over_A"]["S"]["instructions_per_iteration"]["ordinary_ci95"] = [-1.0, 0.5]
    assert_fails(lambda: analyze._gate_summary(negative), "must be positive and finite")


def test_gate_cannot_read_the_sensitivity_bootstrap() -> None:
    import ast
    import inspect
    import textwrap

    signature = inspect.signature(analyze._gate_summary)
    check(len(signature.parameters) == 1, "the gate function must take exactly one argument")

    # Inspect the executable body only: the docstring is allowed to explain the separation.
    tree = ast.parse(textwrap.dedent(inspect.getsource(analyze._gate_summary)))
    function = tree.body[0]
    body = function.body[1:] if ast.get_docstring(function) is not None else function.body
    referenced: set[str] = set()
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                referenced.add(node.value)
    for forbidden in ("bootstrap", "percentile", "sensitivity"):
        offending = sorted(name for name in referenced if forbidden in name.lower())
        check(not offending, f"the gate function body must not reference {forbidden!r}: {offending}")
    # The structural scan above only covers the gate function. The separation that actually
    # matters lives in analyze_campaign, which builds gate_inputs. Drive the real analyzer with a
    # deliberately absurd bootstrap and require byte-identical gates.
    def run(root: Path) -> None:
        baseline = analyze.analyze_campaign(emit=False)
        original_bootstrap = analyze._bootstrap
        try:

            def absurd(logs_by_key_and_block):  # type: ignore[no-untyped-def]
                from array import array

                return {key: array("d", [1000.0] * 8) for key in logs_by_key_and_block}

            analyze._bootstrap = absurd
            poisoned = analyze.analyze_campaign(emit=False)
        finally:
            analyze._bootstrap = original_bootstrap
        check(
            poisoned["adoption_gates"] == baseline["adoption_gates"],
            "poisoning the sensitivity bootstrap must not change a single gate value",
        )
        sensitivity = poisoned["results"]["C_over_A"]["S"]["instructions_per_iteration"][
            "sensitivity_bootstrap_never_used_for_gates"
        ]
        check(
            any(abs(bounds[0] - 1000.0) < 1e-9 for bounds in sensitivity.values()),
            "the poisoned bootstrap should be visible in the sensitivity output, proving it was used there",
        )
        for key in sensitivity:
            check("not_for_citation" in key, f"sensitivity key {key!r} must be marked not-for-citation")

    _with_synthetic_bundle(run)


# ---------------------------------------------------------------------------
# Template and reconstruction tests
# ---------------------------------------------------------------------------


def _draft_campaign() -> dict[str, Any]:
    return analyze.load_json(REAL_EVIDENCE_DIR / "campaign.json")


def test_placeholders_block_execution_and_the_frozen_campaign_validates() -> None:
    """Two invariants, tested against synthetic states rather than the file's current one.

    An earlier version of this test asserted that campaign.json was still a draft, which made it
    fail the moment the campaign was legitimately frozen. The invariants worth pinning are that a
    draft cannot execute and that the frozen article validates strictly -- not which of the two the
    file happens to be today.
    """
    frozen = _draft_campaign()
    if frozen["status"] == "frozen_ready":
        analyze.validate_campaign(frozen, allow_placeholders=False)
    else:
        analyze.validate_campaign(frozen, allow_placeholders=True)

    # A draft with placeholders must be refused by strict validation, whichever gate fires first.
    draft = _draft_campaign()
    draft["status"] = "protocol_draft_unexecutable_placeholders"
    assert_fails(
        lambda: analyze.validate_campaign(draft, allow_placeholders=False),
        "campaign.status must be one of ['frozen_ready']",
    )
    placeholdered = _draft_campaign()
    placeholdered["status"] = "frozen_ready"
    placeholdered["arms"]["C"]["sha256"] = "PLACEHOLDER_BINARY_SHA256_ARM_C"
    assert_fails(
        lambda: analyze.validate_campaign(placeholdered, allow_placeholders=False),
        "execution-blocking placeholder",
    )


def test_campaign_negative_mutations() -> None:
    mutations: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
        ("schema", lambda c: c.__setitem__("schema_version", 2), "campaign.schema_version"),
        (
            "global min time",
            lambda c: c["benchmark"].__setitem__("minimum_time_seconds", 0.01),
            "single global benchmark minimum time is forbidden",
        ),
        (
            "point control min time",
            lambda c: c["workloads"]["P"].__setitem__("minimum_time_seconds", 0.01),
            "workload P per-workload minimum time",
        ),
        (
            "pristine label",
            lambda c: c["arms"]["A"].__setitem__("label", "pristine_upstream"),
            "arm A label",
        ),
        (
            "provisional binary",
            lambda c: c["arms"]["A"].__setitem__("binary_path", "/tmp/mongo-count-query-bm-4109dcc-A-pristine"),
            "arm A binary path",
        ),
        (
            "pristine description",
            lambda c: c["arms"]["A"].__setitem__("description", "A pristine upstream binary."),
            "must not be described as a pristine",
        ),
        (
            "missing sibling",
            lambda c: c["execution"].pop("sibling_cpu"),
            "SMT sibling CPU",
        ),
        (
            "loosened contention gate",
            lambda c: c["execution"]["runtime_gates"].__setitem__("per_process_max_sibling_busy_fraction", 0.9),
            "per-process sibling contention gate",
        ),
        (
            "bootstrap gate",
            lambda c: c["analysis"]["adoption_gates"].__setitem__("interval_family", "percentile bootstrap"),
            "must not reference the sensitivity bootstrap",
        ),
        (
            "wrong interval method",
            lambda c: c["analysis"]["gate_interval"].__setitem__("method", "percentile_bootstrap"),
            "gate interval method",
        ),
        (
            "no fail-closed flag",
            lambda c: c["analysis"]["gate_interval"].__setitem__("fail_closed_on_zero_or_non_finite_se_or_df", False),
            "interval fail-closed flag",
        ),
        (
            "missing wording states",
            lambda c: c["analysis"]["adoption_gates"].__setitem__(
                "C_over_B_scalar_wording_states", ["faster", "noninferior_only"]
            ),
            "C/B scalar wording states",
        ),
        (
            "iteration rule",
            lambda c: c["workloads"]["S"].__setitem__("iteration_rule", "equal_and_positive"),
            "per-workload iteration rules",
        ),
        (
            "governor flag",
            lambda c: c["execution"].__setitem__("governor_checked_before_every_process", False),
            "per-process governor check flag",
        ),
        (
            "unbalanced schedule",
            lambda c: c["execution"]["blocks"][0].__setitem__("arm_order", "CBA"),
            "frozen block schedule",
        ),
        ("no ledger", lambda c: c.pop("attempt_ledger"), "campaign attempt ledger name"),
        (
            "missing build attestation artifact",
            lambda c: c["source_artifacts"].pop("build_attestation.json"),
            "source artifact set",
        ),
        (
            "recipe without whitelist",
            lambda c: c["source_design"].__setitem__("reconstruction_recipe", "just build it"),
            "reconstruction recipe does not contain",
        ),
    ]
    for name, mutate, fragment in mutations:
        campaign = _draft_campaign()
        mutate(campaign)
        assert_fails(lambda bound=campaign: analyze.validate_campaign(bound, allow_placeholders=True), fragment)
        print(f"    rejected mutation: {name}")


def _retarget(root: Path) -> None:
    analyze.EVIDENCE_DIR = root
    analyze.CAMPAIGN_PATH = root / "campaign.json"
    analyze.RUN_RECORD_PATH = root / "campaign_run.json"
    analyze.PARTIAL_JOURNAL_PATH = root / "campaign_partial.json"
    analyze.LEDGER_PATH = root / "attempt_ledger.jsonl"
    analyze.BUILD_ATTESTATION_PATH = root / "build_attestation.json"
    analyze.RAW_DIR = root / "raw"
    analyze.LOG_DIR = root / "logs"
    analyze.SUMMARY_PATH = root / "summary.json"


def _restore_target() -> None:
    _retarget(REAL_EVIDENCE_DIR)


def _section(path: str, *, new_file: bool = False) -> str:
    if new_file:
        return (
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            "@@ -0,0 +1 @@\n"
            "+added\n"
        )
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        " context\n"
        "-old\n"
        "+new\n"
    )


def test_patch_whitelists_reject_foreign_paths() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _retarget(root)
        try:
            (root / "arm_A_production.patch").write_bytes(b"")
            harness = "".join(
                _section(path, new_file=path in analyze.BASE_ABSENT_MANIFEST_FILES)
                for path in analyze.COMMON_HARNESS_FILES
            )
            (root / "common_harness.patch").write_text(harness, encoding="utf-8")
            good = "".join(_section(path) for path in analyze.PRODUCTION_FILES)
            (root / "arm_B_production.patch").write_text(good, encoding="utf-8")
            (root / "arm_C_production.patch").write_text(good, encoding="utf-8")
            analyze.validate_patch_whitelists()

            foreign = good + _section("src/mongo/db/exec/sbe/stages/scan.cpp")
            (root / "arm_C_production.patch").write_text(foreign, encoding="utf-8")
            assert_fails(analyze.validate_patch_whitelists, "touches non-production paths")

            (root / "arm_C_production.patch").write_text(good, encoding="utf-8")
            (root / "arm_A_production.patch").write_text(_section("x"), encoding="utf-8")
            assert_fails(analyze.validate_patch_whitelists, "canonical zero-byte")

            (root / "arm_A_production.patch").write_bytes(b"")
            renamed = "diff --git a/src/mongo/db/exec/classic/count.cpp b/src/mongo/db/exec/classic/other.cpp\n"
            (root / "arm_B_production.patch").write_text(renamed, encoding="utf-8")
            assert_fails(analyze.validate_patch_whitelists, "renames")

            (root / "arm_B_production.patch").write_text(good, encoding="utf-8")
            (root / "common_harness.patch").write_text(harness + good, encoding="utf-8")
            assert_fails(analyze.validate_patch_whitelists, "common_harness.patch touched path set")

            # A harness patch that omits the declared new file must not pass either.
            partial = "".join(
                _section(path)
                for path in analyze.COMMON_HARNESS_FILES
                if path not in analyze.BASE_ABSENT_MANIFEST_FILES
            )
            (root / "common_harness.patch").write_text(partial, encoding="utf-8")
            assert_fails(analyze.validate_patch_whitelists, "common_harness.patch touched path set")
        finally:
            _restore_target()


PRODUCTION_PATH = analyze.PRODUCTION_FILES[0]
LEGITIMATE_SECTION = (
    f"diff --git a/{PRODUCTION_PATH} b/{PRODUCTION_PATH}\n"
    "index 1111111111111111111111111111111111111111..2222222222222222222222222222222222222222 100644\n"
    f"--- a/{PRODUCTION_PATH}\n"
    f"+++ b/{PRODUCTION_PATH}\n"
    "@@ -1,2 +1,2 @@\n"
    " context\n"
    "-old\n"
    "+new\n"
)


def _parse_patch_text(text: str, allowed_new_files: frozenset[str] = frozenset()) -> set[str]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "candidate.patch"
        path.write_text(text, encoding="utf-8")
        return analyze.parse_patch_paths(path, allowed_new_files)


def test_patch_parser_accepts_a_wellformed_section() -> None:
    check(
        _parse_patch_text(LEGITIMATE_SECTION) == {PRODUCTION_PATH},
        "a well-formed single-file section must parse to exactly that path",
    )


def test_patch_parser_is_hunk_aware_not_line_matching() -> None:
    """A removed line whose content begins with '-- ' renders as '--- ...' inside the hunk.

    A scanner that merely looks for header-shaped lines would mistake it for a file header. The
    parser must consume hunks by their declared line counts instead.
    """
    tricky = (
        f"diff --git a/{PRODUCTION_PATH} b/{PRODUCTION_PATH}\n"
        f"--- a/{PRODUCTION_PATH}\n"
        f"+++ b/{PRODUCTION_PATH}\n"
        "@@ -1,2 +1,2 @@\n"
        " context\n"
        "--- this is a removed line, not a header\n"
        "+new\n"
    )
    check(_parse_patch_text(tricky) == {PRODUCTION_PATH}, "a '---' hunk body line must not be read as a header")


def test_patch_parser_rejects_header_bypasses() -> None:
    """The bypass class that a `diff --git` regex scan cannot see.

    `git apply` also accepts traditional unified-diff fragments carrying no `diff --git` header, so
    a patch could enumerate one innocuous file while writing another -- for example `.bazelrc.local`,
    which MongoDB's `.bazelrc` try-imports and `.gitignore` hides, giving an arm arbitrary compiler
    flags with a green whitelist.
    """
    bypasses = {
        "traditional fragment with no diff --git header": (
            LEGITIMATE_SECTION
            + "--- a/.bazelrc.local\n"
            + "+++ b/.bazelrc.local\n"
            + "@@ -0,0 +1 @@\n"
            + "+build --copt=-O0\n"
        ),
        "bare fragment as the whole patch": (
            "--- a/.bazelrc.local\n+++ b/.bazelrc.local\n@@ -0,0 +1 @@\n+build --copt=-O0\n"
        ),
        "path containing a space": (
            "diff --git a/src/mongo/db/exec/classic/evil hack.h b/src/mongo/db/exec/classic/evil hack.h\n"
            "--- a/src/mongo/db/exec/classic/evil hack.h\n"
            "+++ b/src/mongo/db/exec/classic/evil hack.h\n"
            "@@ -0,0 +1 @@\n"
            "+evil\n"
        ),
        "C-quoted path": (
            'diff --git "a/src/mongo/x.h" "b/src/mongo/x.h"\n'
            "--- a/src/mongo/x.h\n+++ b/src/mongo/x.h\n@@ -0,0 +1 @@\n+evil\n"
        ),
        "undeclared new file": (
            "diff --git a/.bazelrc.local b/.bazelrc.local\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/.bazelrc.local\n"
            "@@ -0,0 +1 @@\n"
            "+build --copt=-O0\n"
        ),
        "file deletion": (
            f"diff --git a/{PRODUCTION_PATH} b/{PRODUCTION_PATH}\n"
            "deleted file mode 100644\n"
            f"--- a/{PRODUCTION_PATH}\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-gone\n"
        ),
        "mode change": (
            f"diff --git a/{PRODUCTION_PATH} b/{PRODUCTION_PATH}\nold mode 100644\nnew mode 100755\n"
        ),
        "rename": (
            f"diff --git a/{PRODUCTION_PATH} b/src/mongo/db/exec/classic/renamed.cpp\n"
            "rename from src/mongo/db/exec/classic/count.cpp\n"
            "rename to src/mongo/db/exec/classic/renamed.cpp\n"
        ),
        "binary patch": (
            f"diff --git a/{PRODUCTION_PATH} b/{PRODUCTION_PATH}\nGIT binary patch\nliteral 4\nzcmZQ\n"
        ),
        "duplicate section for one path": LEGITIMATE_SECTION + LEGITIMATE_SECTION,
        "hunk line counts disagreeing with the header": (
            f"diff --git a/{PRODUCTION_PATH} b/{PRODUCTION_PATH}\n"
            f"--- a/{PRODUCTION_PATH}\n"
            f"+++ b/{PRODUCTION_PATH}\n"
            "@@ -1,2 +1,2 @@\n"
            " context\n"
        ),
        "unrecognised extended header": (
            f"diff --git a/{PRODUCTION_PATH} b/{PRODUCTION_PATH}\n"
            "something-unexpected: yes\n"
            f"--- a/{PRODUCTION_PATH}\n+++ b/{PRODUCTION_PATH}\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        ),
    }
    expected_reason = {
        "traditional fragment with no diff --git header": "not an understood `diff --git` header",
        "bare fragment as the whole patch": "not an understood `diff --git` header",
        "path containing a space": "not an understood `diff --git` header",
        "C-quoted path": "not an understood `diff --git` header",
        "undeclared new file": "not a declared new file",
        "file deletion": "forbidden patch header",
        "mode change": "forbidden patch header",
        "rename": "renames",
        "binary patch": "forbidden patch header",
        "duplicate section for one path": "more than one section",
        "hunk line counts disagreeing with the header": "truncated hunk",
        "unrecognised extended header": "unrecognised extended header",
    }
    check(set(expected_reason) == set(bypasses), "every bypass case must declare why it is expected to fail")
    for name, text in bypasses.items():
        assert_fails(lambda bound=text: _parse_patch_text(bound), expected_reason[name])
        print(f"    rejected patch bypass: {name}")

    # The declared new file is the one creation the harness patch legitimately needs.
    declared = (
        "diff --git a/src/mongo/db/query/count_query_bm.cpp b/src/mongo/db/query/count_query_bm.cpp\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/mongo/db/query/count_query_bm.cpp\n"
        "@@ -0,0 +1 @@\n"
        "+// harness\n"
    )
    check(
        _parse_patch_text(declared, frozenset(analyze.BASE_ABSENT_MANIFEST_FILES))
        == {"src/mongo/db/query/count_query_bm.cpp"},
        "the declared base-absent harness file must be accepted",
    )


def test_runner_helpers_fail_closed() -> None:
    """Cover the runner's two fail-open risks directly."""
    import run_campaign

    check(run_campaign.parse_cpu_list("0,3-5") == [0, 3, 4, 5], "CPU ranges must expand, not be read as endpoints")
    check(run_campaign.parse_cpu_list("0,48") == [0, 48], "a plain sibling pair must parse")
    check(run_campaign.parse_cpu_list("2-3") == [2, 3], "a bare range must expand")
    # A zero-length or backwards /proc/stat sample must read as fully contended, never as idle:
    # the SMT-sibling gate is an upper bound, so 0.0 there would silently pass.
    check(run_campaign.busy_fraction((100, 50), (100, 50)) == 1.0, "an empty sample must fail closed")
    check(run_campaign.busy_fraction((100, 50), (90, 45)) == 1.0, "a backwards sample must fail closed")
    check(abs(run_campaign.busy_fraction((0, 0), (100, 10)) - 0.9) < 1e-12, "a normal sample must compute correctly")


def test_cross_arm_source_invariants() -> None:
    def manifest(harness_value: str, production_value: str) -> dict[str, Any]:
        files = {path: {"sha256": "a" * 64, "git_blob": harness_value} for path in analyze.COMMON_HARNESS_FILES}
        files.update({path: {"sha256": "b" * 64, "git_blob": production_value} for path in analyze.PRODUCTION_FILES})
        return {"files": files}

    good = {"A": manifest("h", "pa"), "B": manifest("h", "pb"), "C": manifest("h", "pc")}
    analyze.validate_cross_arm_source_invariants(good)

    drifted = copy.deepcopy(good)
    drifted["B"]["files"][analyze.COMMON_HARNESS_FILES[0]]["git_blob"] = "other"
    assert_fails(
        lambda: analyze.validate_cross_arm_source_invariants(drifted),
        "must be byte-identical across arms",
    )

    duplicated = {"A": manifest("h", "pa"), "B": manifest("h", "pa"), "C": manifest("h", "pc")}
    assert_fails(
        lambda: analyze.validate_cross_arm_source_invariants(duplicated),
        "identical production sources",
    )


# ---------------------------------------------------------------------------
# Synthetic end-to-end campaign
# ---------------------------------------------------------------------------

HOST_NAME = "synthetic-host"
BASE_INSTRUCTIONS = {"S": 1.02e9, "M": 1.13e9, "W": 6.36e8, "P": 1.5e5, "X": 1.876e9}
ARM_FACTORS = {
    "S": {"A": 1.0, "B": 0.97, "C": 0.955},
    "M": {"A": 1.0, "B": 0.96, "C": 0.94},
    "W": {"A": 1.0, "B": 0.97, "C": 0.95},
    "P": {"A": 1.0, "B": 1.0, "C": 1.0},
    # X is the un-optimized control: the optimization cannot fire there, so all arms must match.
    "X": {"A": 1.0, "B": 1.0, "C": 1.0},
}
POINT_ITERATIONS = 3115


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _first_process_index(workload: str) -> int:
    """Locate a process by workload rather than by a hardcoded index.

    The block schedule and workload set both changed once already; indices written by hand went
    stale silently and pointed the fixtures at the wrong workload.
    """
    for item in analyze.expected_output_sequence():
        if item["workload"] == workload:
            return int(item["process_index"])
    raise AssertionError(f"no process for workload {workload}")


def _build_synthetic_bundle(
    root: Path,
    *,
    scalar_cb_factor: float | None = None,
    point_drift: float = 1.0,
    sibling_busy_override: tuple[int, float] | None = None,
    repetitions_override: int | None = None,
    iteration_override: tuple[int, int] | None = None,
    within_process_iteration_drift: tuple[int, int] | None = None,
) -> None:
    """Create a complete, internally consistent campaign bundle for the analyzer."""
    rng = random.Random(20260805)
    for name in analyze.PROTOCOL_ARTIFACTS:
        shutil.copy2(REAL_EVIDENCE_DIR / name, root / name)
    (root / "build_logs").mkdir()
    for build_key in analyze.BUILD_ORDER:
        (root / "build_logs" / f"build_{build_key}.log").write_text(f"synthetic build {build_key}\n", encoding="utf-8")
        (root / "build_logs" / f"smoke_{build_key}.log").write_text(
            "".join(f"{spec['run_name']}\n" for spec in analyze.WORKLOAD_SPECS.values()),
            encoding="utf-8",
        )

    (root / "arm_A_production.patch").write_bytes(b"")
    harness_patch = "".join(
        _section(path, new_file=path in analyze.BASE_ABSENT_MANIFEST_FILES)
        for path in analyze.COMMON_HARNESS_FILES
    )
    (root / "common_harness.patch").write_text(harness_patch, encoding="utf-8")
    production_patch = "".join(_section(path) for path in analyze.PRODUCTION_FILES)
    for arm in ("B", "C"):
        (root / f"arm_{arm}_production.patch").write_text(production_patch, encoding="utf-8")

    manifests: dict[str, dict[str, Any]] = {}
    for arm in analyze.ARMS:
        # Git blobs must be the pinned commit-derived identities; only the sha256 side is synthetic.
        pinned = analyze.expected_manifest_blobs(arm)
        files = {}
        for path in analyze.MANIFEST_FILES:
            # Harness identities must be byte-identical across arms; only production files vary.
            key = path if path in analyze.COMMON_HARNESS_FILES else (arm, path)
            files[path] = {"sha256": f"{abs(hash(key)):064x}"[:64], "git_blob": pinned[path]}
        manifests[arm] = {
            "schema_version": 1,
            "arm": arm,
            "reconstruction_base_commit": analyze.ARM_COMMITS["A"],
            "production_source_commit": analyze.ARM_COMMITS[arm],
            "common_harness_patch": {"path": "common_harness.patch", "sha256": ""},
            "production_patch": {"path": f"arm_{arm}_production.patch", "sha256": ""},
            "files": {path: files[path] for path in analyze.MANIFEST_FILES},
        }

    harness_sha = analyze.sha256_file(root / "common_harness.patch")
    for arm in analyze.ARMS:
        manifests[arm]["common_harness_patch"]["sha256"] = harness_sha
        manifests[arm]["production_patch"]["sha256"] = analyze.sha256_file(root / f"arm_{arm}_production.patch")
        _write_json(root / f"arm_{arm}_source_manifest.json", manifests[arm])

    binary_sha = {arm: f"{index + 1:064x}" for index, arm in enumerate(analyze.ARMS)}
    build_id = {arm: f"{index + 17:040x}" for index, arm in enumerate(analyze.ARMS)}
    builds = {}
    build_clock = datetime(2026, 8, 5, 20, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    for build_position, build_key in enumerate(analyze.BUILD_ORDER):
        arm = analyze.BUILD_ARM_OF[build_key]
        log_name = f"build_logs/build_{build_key}.log"
        smoke_name = f"build_logs/smoke_{build_key}.log"
        build_started = build_clock + timedelta(minutes=20 * build_position)
        build_finished = build_started + timedelta(minutes=10)
        builds[build_key] = {
            "build_key": build_key,
            "arm": arm,
            "worktree_path": "/tmp/synthetic-attested-worktree",
            "build_command": list(analyze.BUILD_COMMAND),
            "source_manifest": f"arm_{arm}_source_manifest.json",
            "source_manifest_sha256": analyze.sha256_file(root / f"arm_{arm}_source_manifest.json"),
            "verified_files": manifests[arm]["files"],
            "attested_output_path": f"/tmp/attested-{build_key}",
            "output_sha256": binary_sha[arm],
            "build_id": build_id[arm],
            "log": log_name,
            "log_sha256": analyze.sha256_file(root / log_name),
            "started_at": build_started.isoformat(),
            "finished_at": build_finished.isoformat(),
            "campaign_binary_path": analyze.ARM_BINARY_PATHS[arm]
            if analyze.CAMPAIGN_BUILD_OF_ARM.get(arm) == build_key
            else None,
            "bazelrc_digests": {"before": {".bazelrc": "a" * 64}, "after": {".bazelrc": "a" * 64}},
            "effective_command": "frozen=bazel build --config=opt //target :: canonicalized(--config=opt)=synthetic",
            "bazel_process_summary": "INFO: 1 process: 1 internal",
            "compiler_version": "MongoDB clang version synthetic",
            "build_environment": {"CC": "", "CXX": "", "BAZEL_FLAGS": "", "BAZELISK_HOME": ""},
            "smoke": {
                "command": ["synthetic"],
                "returncode": 0,
                "workloads": sorted(analyze.WORKLOADS),
                "iterations_at_campaign_size": {"S": 1, "M": 1, "W": 1, "P": POINT_ITERATIONS, "X": 1},
                "passed": True,
                "log": smoke_name,
                "log_sha256": analyze.sha256_file(root / smoke_name),
            },
        }
    _write_json(
        root / "build_attestation.json",
        {
            "schema_version": 1,
            "campaign_id": "mongodb-master-countscan-three-arm-20260806-90814b83d3e5",
            "reconstruction_base_commit": analyze.ARM_COMMITS["A"],
            "common_harness_source_commit": analyze.ARM_COMMITS["C"],
            "worktree_path": "/tmp/synthetic-attested-worktree",
            "source_repository": "/synthetic/mongo",
            "build_command": list(analyze.BUILD_COMMAND),
            "build_order": list(analyze.BUILD_ORDER),
            "toolchain": {
                "bazel_version": "bazel 7.5.0-synthetic",
                "compiler_version": "GCC synthetic",
                "kernel": "synthetic",
                "hostname": HOST_NAME,
                "libc": "ldd synthetic",
                "machine": "x86_64",
            },
            "builds": builds,
            "reproducibility_check": {
                "c1_output_sha256": binary_sha["C"],
                "c2_output_sha256": binary_sha["C"],
                "output_sha256_equal": True,
                "c1_build_id": build_id["C"],
                "c2_build_id": build_id["C"],
                "build_id_equal": True,
                "scope": "same_worktree_and_output_base_state_stability_not_hermetic_rebuild",
                "interpretation": "synthetic",
                "c1_bazel_process_summary": "INFO: synthetic",
                "c2_bazel_process_summary": "INFO: synthetic",
            },
            "campaign_binaries": {arm: analyze.ARM_BINARY_PATHS[arm] for arm in analyze.ARMS},
            "created_at": "2026-08-05T22:20:00+08:00",
        },
    )

    campaign = _draft_campaign()
    campaign["status"] = "frozen_ready"
    campaign["protocol_artifacts"] = {name: analyze.sha256_file(root / name) for name in analyze.PROTOCOL_ARTIFACTS}
    campaign["source_artifacts"] = {name: analyze.sha256_file(root / name) for name in analyze.SOURCE_ARTIFACTS}
    for arm in analyze.ARMS:
        campaign["arms"][arm]["sha256"] = binary_sha[arm]
        campaign["arms"][arm]["build_id"] = build_id[arm]
    campaign["analysis"]["sensitivity_bootstrap"]["samples"] = analyze.BOOTSTRAP_SAMPLES
    _write_json(root / "campaign.json", campaign)
    campaign_sha = analyze.sha256_file(root / "campaign.json")

    def ledger_append(record: dict[str, Any]) -> None:
        path = root / "attempt_ledger.jsonl"
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        previous = analyze.ledger_line_digest(existing[-1]) if existing else analyze.GENESIS_LEDGER_DIGEST
        line = json.dumps({**record, "previous_record_sha256": previous}, sort_keys=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    ledger_append(
        {
            "schema_version": 2,
            "attempt_id": "attempt-001",
            "record_type": "preregistration",
            "status": "preregistered",
            "campaign_id": campaign["campaign_id"],
            "campaign_sha256": campaign_sha,
            "protocol_artifacts": campaign["protocol_artifacts"],
            "source_artifacts": campaign["source_artifacts"],
            "created_at": "2026-08-05T22:30:00+08:00",
            "external_anchor": {
                "remote": "git@github.com:VectifyAI/ConDB.git",
                "branch": "agent/review-mongodb-optimization-report",
                "commit": "b" * 40,
                "pushed_at": "2026-08-05T22:29:00+08:00",
                "note": "synthetic anchor",
            },
        }
    )

    sequence = analyze.expected_output_sequence()
    start = datetime(2026, 8, 5, 23, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    completed: list[dict[str, Any]] = []
    cursor = start
    for item in sequence:
        workload = item["workload"]
        arm = item["arm"]
        factor = ARM_FACTORS[workload][arm]
        if workload == "S" and arm == "C" and scalar_cb_factor is not None:
            factor = ARM_FACTORS["S"]["B"] * scalar_cb_factor
        if workload == "P" and arm == "C":
            factor *= point_drift
        base = BASE_INSTRUCTIONS[workload] * factor
        block_effect = 1.0 + 0.002 * math.sin(item["block"] * 1.7 + hash(workload) % 7)
        rows: list[dict[str, Any]] = []
        if workload == "P":
            iterations = POINT_ITERATIONS
        else:
            iterations = 1
        if iteration_override is not None and item["process_index"] == iteration_override[0]:
            iterations = iteration_override[1]
        for repetition in range(analyze.REPETITIONS):
            jitter = 1.0 + rng.uniform(-0.0004, 0.0004)
            instructions = base * block_effect * jitter
            row_iterations = iterations
            if (
                within_process_iteration_drift is not None
                and item["process_index"] == within_process_iteration_drift[0]
                and repetition == within_process_iteration_drift[1]
            ):
                row_iterations = iterations + 1
            rows.append(
                {
                    "name": analyze.WORKLOAD_SPECS[workload]["run_name"],
                    "run_name": analyze.WORKLOAD_SPECS[workload]["run_name"],
                    "run_type": "iteration",
                    # google-benchmark emits 0 here on iteration rows; only aggregates carry the count.
                    "repetitions": 0,
                    "repetition_index": repetition,
                    "threads": 1,
                    "iterations": row_iterations,
                    "time_unit": "ns",
                    "real_time": instructions / 12.0,
                    "cpu_time": instructions / 12.0,
                    "instructions_per_iteration": instructions,
                }
            )
        for aggregate in ("mean", "median", "stddev"):
            values = [row["instructions_per_iteration"] for row in rows]
            if aggregate == "stddev":
                magnitude = max(1e-9, (max(values) - min(values)) / 4.0)
            else:
                magnitude = sum(values) / len(values)
            rows.append(
                {
                    "name": f"{analyze.WORKLOAD_SPECS[workload]['run_name']}_{aggregate}",
                    "run_name": analyze.WORKLOAD_SPECS[workload]["run_name"],
                    "run_type": "aggregate",
                    "aggregate_name": aggregate,
                    "repetitions": analyze.REPETITIONS,
                    "threads": 1,
                    "iterations": analyze.REPETITIONS,
                    "time_unit": "ns",
                    "real_time": magnitude / 12.0,
                    "cpu_time": magnitude / 12.0,
                    "instructions_per_iteration": magnitude,
                }
            )
        context_date = cursor.replace(microsecond=0).isoformat()
        _write_json(
            root / item["raw"],
            {
                "context": {
                    "date": context_date,
                    "host_name": HOST_NAME,
                    "executable": analyze.ARM_BINARY_PATHS[arm],
                    "num_cpus": 96,
                    "mhz_per_cpu": 4000,
                    "cpu_scaling_enabled": False,
                    "library_build_type": "release",
                },
                "benchmarks": rows,
            },
        )
        log_path = root / item["log"]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"{analyze.WORKLOAD_SPECS[workload]['run_name']} synthetic run\n", encoding="utf-8")

        started_at = cursor
        exited_at = cursor + timedelta(seconds=6)
        finished_at = cursor + timedelta(seconds=7)
        temporary_raw = (root / item["raw"]).with_name(f".{Path(item['raw']).name}.incomplete")
        sibling_busy = 0.01
        if sibling_busy_override is not None and item["process_index"] == sibling_busy_override[0]:
            sibling_busy = sibling_busy_override[1]
        completed.append(
            {
                **item,
                "pid": 1000 + item["process_index"],
                "returncode": 0,
                "governor": "performance",
                "started_at": started_at.isoformat(),
                "process_exited_at": exited_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "command": analyze.expected_benchmark_command(campaign, item, temporary_raw),
                "raw_sha256": analyze.sha256_file(root / item["raw"]),
                "log_sha256": analyze.sha256_file(root / item["log"]),
                "selected_cpu_busy_fraction": 0.99,
                "sibling_cpu_busy_fraction": sibling_busy,
                "loadavg_before": [1.0, 1.0, 1.0],
                "loadavg_after": [1.0, 1.0, 1.0],
                "cpu_khz_before": {"0": 3900000, "48": 3900000},
                "cpu_khz_after": {"0": 3900000, "48": 3900000},
            }
        )
        cursor = finished_at + timedelta(seconds=1)
    finished = cursor

    run_record = {
        "schema_version": 3,
        "status": "complete",
        "campaign_id": campaign["campaign_id"],
        "attempt_id": "attempt-001",
        "started_at": start.isoformat(),
        "finished_at": finished.isoformat(),
        "host": {
            "name": HOST_NAME,
            "kernel": "synthetic",
            "machine": "x86_64",
            "python": "3.12.0",
            "cpu_governor": "performance",
        },
        "runtime_provenance": {
            "cpu_model": "Intel(R) Xeon(R) Gold 6418H",
            "microcode": "0x2b000639",
            "kernel": "synthetic",
            "libc": "ldd synthetic",
            "bazel_version": "bazel 7.5.0-synthetic",
            "compiler_version": "GCC synthetic",
            "python_executable": "/usr/bin/python3",
            "python_version": "3.12.0",
            "selected_cpu": analyze.SELECTED_CPU,
            "smt_sibling_cpu": analyze.SIBLING_CPU,
            "selected_cpu_thread_siblings": [analyze.SELECTED_CPU, analyze.SIBLING_CPU],
            "numa_topology": "online=0-1 node0=0-23,48-71 node1=24-47,72-95",
            "turbo_state": "intel_pstate.no_turbo=0 cpufreq.boost=NA",
            "governors": {str(analyze.SELECTED_CPU): "performance", str(analyze.SIBLING_CPU): "performance"},
            "binary_shared_libraries": {arm: "linux-vdso.so.1" for arm in analyze.ARMS},
            "environment": {"PATH": "/usr/bin"},
            "scheduler_policy": "SCHED policy 0, nice 0",
            "build_attestation_sha256": analyze.sha256_file(root / "build_attestation.json"),
        },
        "idle_preflight": {
            "cooldown_seconds": analyze.COOLDOWN_SECONDS,
            "sample_seconds": analyze.PREFLIGHT_SAMPLE_SECONDS,
            "selected_cpu_busy_fraction": 0.01,
            "sibling_cpu_busy_fraction": 0.005,
            "loadavg": [1.0, 1.0, 1.0],
            "loadavg_after_sample": [1.0, 1.0, 1.0],
            "measured_at": start.isoformat(),
        },
        "cpu_affinity": analyze.SELECTED_CPU,
        "taskset_command": ["taskset", "-c", str(analyze.SELECTED_CPU)],
        "process_count": analyze.PROCESS_COUNT,
        "repetitions_per_process": repetitions_override or analyze.REPETITIONS,
        "campaign_sha256_preflight": campaign_sha,
        "campaign_sha256_postflight": campaign_sha,
        "protocol_sha256_preflight": campaign["protocol_artifacts"],
        "protocol_sha256_postflight": campaign["protocol_artifacts"],
        "source_artifact_sha256_preflight": campaign["source_artifacts"],
        "source_artifact_sha256_postflight": campaign["source_artifacts"],
        "binary_identity_preflight": {
            arm: {
                "path": analyze.ARM_BINARY_PATHS[arm],
                "sha256": binary_sha[arm],
                "build_id": build_id[arm],
            }
            for arm in analyze.ARMS
        },
        "binary_identity_postflight": {
            arm: {
                "path": analyze.ARM_BINARY_PATHS[arm],
                "sha256": binary_sha[arm],
                "build_id": build_id[arm],
            }
            for arm in analyze.ARMS
        },
        "binary_stat_preflight": {
            arm: {"device": 1, "inode": 2 + index, "size": 1000, "mtime_ns": 1, "ctime_ns": 1}
            for index, arm in enumerate(analyze.ARMS)
        },
        "binary_stat_postflight": {
            arm: {"device": 1, "inode": 2 + index, "size": 1000, "mtime_ns": 1, "ctime_ns": 1}
            for index, arm in enumerate(analyze.ARMS)
        },
        "completed_processes": completed,
    }
    _write_json(root / "campaign_run.json", run_record)
    _write_json(
        root / "campaign_partial.json",
        {
            "schema_version": 2,
            "status": "complete",
            "campaign_id": campaign["campaign_id"],
            "attempt_id": "attempt-001",
            "campaign_sha256": campaign_sha,
            "started_at": start.isoformat(),
            "finished_at": finished.isoformat(),
            "expected_process_count": analyze.PROCESS_COUNT,
            "last_completed_process_index": analyze.PROCESS_COUNT,
            "current_process": None,
            "completed_processes": completed,
            "run_record_sha256": analyze.sha256_file(root / "campaign_run.json"),
        },
    )
    ledger_append(
        {
            "schema_version": 2,
            "attempt_id": "attempt-001",
            "record_type": "started",
            "status": "started",
            "campaign_id": campaign["campaign_id"],
            "campaign_sha256": campaign_sha,
            "pid": 4242,
            "boot_id": "synthetic-boot-id",
            "started_at": start.isoformat(),
            "created_at": start.isoformat(),
        }
    )
    ledger_append(
        {
            "schema_version": 2,
            "attempt_id": "attempt-001",
            "record_type": "outcome",
            "status": "succeeded",
            "campaign_id": campaign["campaign_id"],
            "campaign_sha256": campaign_sha,
            "started_at": start.isoformat(),
            "finished_at": finished.isoformat(),
            "completed_process_count": analyze.PROCESS_COUNT,
            "run_record_sha256": analyze.sha256_file(root / "campaign_run.json"),
            "created_at": finished.isoformat(),
        }
    )


def _with_synthetic_bundle(action: Callable[[Path], None], **kwargs: Any) -> None:
    original_samples = analyze.BOOTSTRAP_SAMPLES
    analyze.BOOTSTRAP_SAMPLES = 400
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _retarget(root)
        try:
            _build_synthetic_bundle(root, **kwargs)
            action(root)
        finally:
            _restore_target()
            analyze.BOOTSTRAP_SAMPLES = original_samples


def test_synthetic_campaign_passes_every_gate() -> None:
    def run(root: Path) -> None:
        summary = analyze.analyze_campaign(emit=False)
        gates = summary["adoption_gates"]
        check(gates["interval_family"] == "stratified_log_ratio_welch_t", "gates must use the Welch-t family")
        check(gates["overall_adoption_gate_passed"] is True, f"synthetic campaign should pass: {gates}")
        check(
            all(v == "faster" for v in gates["C_over_B_count_family"]["wording_by_workload"].values()),
            "synthetic C/B endpoints should all read faster",
        )
        instructions = summary["results"]["C_over_A"]["S"]["instructions_per_iteration"]
        check(instructions["gate_interval"]["method"] == "stratified_log_ratio_welch_t", "wrong interval method")
        check(
            "sensitivity_bootstrap_never_used_for_gates" in instructions,
            "the sensitivity bootstrap must still be reported",
        )
        check(0.94 < instructions["ratio_geomean"] < 0.97, f"unexpected point estimate {instructions}")

    _with_synthetic_bundle(run)


def test_synthetic_campaign_noninferiority_states_end_to_end() -> None:
    def expect(state: str, passes: bool) -> Callable[[Path], None]:
        def run(root: Path) -> None:
            summary = analyze.analyze_campaign(emit=False)
            gates = summary["adoption_gates"]
            actual = gates["C_over_B_count_family"]["wording_by_workload"]["S"]
            check(actual == state, f"expected scalar state {state}, got {gates['C_over_B_count_family']}")
            check(
                gates["complexity_tradeoff_versus_rejected_implementation_passed"] is passes,
                f"expected complexity trade-off pass={passes}",
            )
            check(
                gates["overall_adoption_gate_passed"] is True,
                "C/B never vetoes adoption regardless of its own outcome",
            )

        return run

    # Inside the 1% margin: the previous implementation is marginally ahead, which is exactly the
    # expected outcome and must not fail the gate.
    _with_synthetic_bundle(expect("noninferior_only", True), scalar_cb_factor=1.005)
    # Beyond the margin: the previous implementation's extra machinery bought enough to matter.
    _with_synthetic_bundle(expect("noninferiority_failed", False), scalar_cb_factor=1.03)


def test_synthetic_campaign_point_control_drift_fails() -> None:
    def run(root: Path) -> None:
        summary = analyze.analyze_campaign(emit=False)
        gates = summary["adoption_gates"]
        check(gates["point_controls"]["passed"] is False, "a drifting point control must fail its gate")
        check(gates["overall_adoption_gate_passed"] is False, "a failed point control must fail the overall gate")

    _with_synthetic_bundle(run, point_drift=1.03)


def test_synthetic_campaign_rejects_sibling_contention() -> None:
    def run(root: Path) -> None:
        assert_fails(lambda: analyze.analyze_campaign(emit=False), "above the pre-frozen contention gate")

    _with_synthetic_bundle(run, sibling_busy_override=(42, 0.4))


def test_synthetic_campaign_rejects_wrong_repetition_count() -> None:
    def run(root: Path) -> None:
        assert_fails(lambda: analyze.analyze_campaign(emit=False), "run record repetitions per process")

    _with_synthetic_bundle(run, repetitions_override=4)


def test_synthetic_campaign_rejects_multi_iteration_count_workload() -> None:
    def run(root: Path) -> None:
        assert_fails(lambda: analyze.analyze_campaign(emit=False), "exactly one benchmark iteration")

    _with_synthetic_bundle(run, iteration_override=(_first_process_index("S"), 2))


def test_synthetic_campaign_records_but_does_not_gate_cross_arm_point_iterations() -> None:
    """Unequal point-control iteration counts across arms must be recorded, never gated.

    google-benchmark auto-calibrates that count from a noisy timing, and the three arms are least
    likely to agree exactly when they genuinely differ in speed. Gating on agreement would make
    attempt retention conditional on the measured effect, i.e. selection on the outcome. Both
    reported metrics are already per-iteration, so unequal counts do not bias the ratio.
    """

    def run(root: Path) -> None:
        summary = analyze.analyze_campaign(emit=False)
        recorded = summary["point_control_iterations_by_block"]
        check(recorded, "the point control iteration counts must be recorded")
        differing = [block for block, counts in recorded.items() if len(set(counts.values())) != 1]
        check(bool(differing), "this fixture deliberately makes one block's arms disagree")
        check(summary["valid_complete_campaign"] is True, "unequal cross-arm counts must not invalidate the campaign")

    _with_synthetic_bundle(run, iteration_override=(_first_process_index("P"), POINT_ITERATIONS + 1))


def test_synthetic_campaign_still_requires_equal_iterations_within_a_process() -> None:
    def run(root: Path) -> None:
        assert_fails(
            lambda: analyze.analyze_campaign(emit=False),
            "equal positive iteration counts within a process",
        )

    _with_synthetic_bundle(run, within_process_iteration_drift=(_first_process_index("P"), 1))


def _write_chained_ledger(path: Path, records: list[dict[str, Any]]) -> None:
    previous = analyze.GENESIS_LEDGER_DIGEST
    lines = []
    for record in records:
        line = json.dumps({**record, "previous_record_sha256": previous}, sort_keys=True)
        lines.append(line)
        previous = analyze.ledger_line_digest(line)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def test_ledger_binding_is_enforced() -> None:
    def run(root: Path) -> None:
        ledger_path = root / "attempt_ledger.jsonl"
        campaign = analyze.load_json(root / "campaign.json")
        campaign_sha = analyze.sha256_file(root / "campaign.json")
        original = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
        prereg, started, outcome = original

        analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="outcome")
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, "0" * 64, expect_state="outcome"),
            "campaign SHA-256 binding",
        )

        def restore() -> None:
            _write_chained_ledger(ledger_path, [prereg, started, outcome])

        # Preregistration alone is a clean, ready-to-run attempt.
        _write_chained_ledger(ledger_path, [prereg])
        analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted")
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="outcome"),
            "no start record",
        )

        # A start with no outcome is an interrupted run: it must NOT silently become runnable
        # again under the same preregistration.
        _write_chained_ledger(ledger_path, [prereg, started])
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted"),
            "already started but never closed",
        )

        restore()
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted"),
            "already has a terminal outcome",
        )

        # A tampered line must break the hash chain.
        tampered = dict(prereg)
        tampered["campaign_sha256"] = "0" * 64
        broken = ledger_path.read_text(encoding="utf-8").splitlines()
        broken[0] = json.dumps({**tampered, "previous_record_sha256": analyze.GENESIS_LEDGER_DIGEST}, sort_keys=True)
        ledger_path.write_text("".join(line + "\n" for line in broken), encoding="utf-8")
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="outcome"),
            "breaks the hash chain",
        )

        # Later attempts must also be created later; the ledger is append-only.
        third = {**prereg, "attempt_id": "attempt-003", "created_at": "2026-08-06T02:00:00+08:00"}
        closed_first = {**outcome, "status": "failed"}
        _write_chained_ledger(ledger_path, [prereg, started, closed_first, third])
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted"),
            "attempt ledger preregistration sequence",
        )

        # An earlier attempt that was never closed must be caught rather than ignored.
        second = {**prereg, "attempt_id": "attempt-002", "created_at": "2026-08-06T01:00:00+08:00"}
        _write_chained_ledger(ledger_path, [prereg, second])
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted"),
            "never closed",
        )

        # A legitimate second attempt after a recorded failure is accepted.
        _write_chained_ledger(ledger_path, [prereg, started, closed_first, second])
        result = analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted")
        check(result["attempt_id"] == "attempt-002", "the active attempt must be the newest preregistration")
        check(result["attempt_count"] == 2, "the summary must expose the full attempt count")

        malformed = {**prereg, "attempt_id": "attempt-1"}
        _write_chained_ledger(ledger_path, [malformed])
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted"),
            "malformed attempt ID",
        )

        # The external anchor is the only defence against wholesale local deletion, so it must be
        # a structured, checkable reference rather than free prose.
        no_anchor = {key: value for key, value in prereg.items() if key != "external_anchor"}
        _write_chained_ledger(ledger_path, [no_anchor])
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted"),
            "external anchor is not an object",
        )
        prose_anchor = {**prereg, "external_anchor": "pushed to git, trust me"}
        _write_chained_ledger(ledger_path, [prose_anchor])
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted"),
            "external anchor is not an object",
        )
        bad_commit = {**prereg, "external_anchor": {**prereg["external_anchor"], "commit": "not-a-sha"}}
        _write_chained_ledger(ledger_path, [bad_commit])
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted"),
            "anchor commit has an invalid format",
        )

        ledger_path.write_text("", encoding="utf-8")
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted"),
            "attempt ledger is empty",
        )
        restore()

    _with_synthetic_bundle(run)


def test_runner_can_analyze_its_own_open_attempt() -> None:
    """The runner analyses BEFORE writing its outcome, so the ledger must accept a started attempt.

    Folding that state into the pre-launch gate would make the runner refuse its own mid-attempt
    ledger after all 288 processes had already run.
    """

    def run(root: Path) -> None:
        ledger_path = root / "attempt_ledger.jsonl"
        records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
        prereg, started, _outcome = records

        # Exactly the state at run_campaign.py's in-line analysis call.
        _write_chained_ledger(ledger_path, [prereg, started])
        summary = analyze.analyze_campaign(emit=False, ledger_state="started")
        check(summary["valid_complete_campaign"] is True, "the runner must be able to analyze its own attempt")

        # The same state must still be refused by the pre-launch gate and by post-hoc analysis.
        campaign = analyze.load_json(root / "campaign.json")
        campaign_sha = analyze.sha256_file(root / "campaign.json")
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="unstarted"),
            "already started but never closed",
        )
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="outcome"),
            "exactly one terminal outcome",
        )
        # And "started" must reject a ledger that has not actually started.
        _write_chained_ledger(ledger_path, [prereg])
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="started"),
            "has no start record",
        )
        assert_fails(
            lambda: analyze.validate_attempt_ledger(campaign, campaign_sha, expect_state="nonsense"),
            "unknown ledger expectation",
        )

    _with_synthetic_bundle(run)


def test_patch_parser_accepts_a_real_no_newline_diff() -> None:
    """`\\ No newline at end of file` usually terminates a hunk, outside the counted lines."""
    import subprocess

    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        target = repo / "f.txt"
        target.write_text("line one\nline two", encoding="utf-8")  # no trailing newline
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        target.write_text("line one\nline changed", encoding="utf-8")
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "--full-index"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        check("No newline at end of file" in diff, "the fixture must actually produce the marker")
        check(_parse_patch_text(diff) == {"f.txt"}, "a real no-newline diff must parse, not fail closed")


def test_patch_parser_gates_creation_by_dev_null_source() -> None:
    """`new file mode` is optional, so the creation check cannot rely on that header alone."""
    without_header = (
        "diff --git a/.bazelrc.local b/.bazelrc.local\n"
        "--- /dev/null\n"
        "+++ b/.bazelrc.local\n"
        "@@ -0,0 +1 @@\n"
        "+build --copt=-O0\n"
    )
    assert_fails(lambda: _parse_patch_text(without_header), "not a declared new file")
    deletion = (
        f"diff --git a/{PRODUCTION_PATH} b/{PRODUCTION_PATH}\n"
        f"--- a/{PRODUCTION_PATH}\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-gone\n"
    )
    assert_fails(lambda: _parse_patch_text(deletion), "deletions are not permitted")


def test_block_execution_order_matches_its_declared_derivation() -> None:
    order = list(range(1, analyze.BLOCK_COUNT + 1))
    random.Random(analyze.BLOCK_EXECUTION_SEED).shuffle(order)
    check(
        tuple(order) == analyze.BLOCK_EXECUTION_ORDER,
        "BLOCK_EXECUTION_ORDER must be reproducible from its declared seed and rule",
    )
    check(
        sorted(analyze.BLOCK_EXECUTION_ORDER) == list(range(1, analyze.BLOCK_COUNT + 1)),
        "the execution order must be a permutation of every block exactly once",
    )
    # Each stratum must no longer sit at a fixed period in execution time.
    positions = {block: index for index, block in enumerate(analyze.BLOCK_EXECUTION_ORDER)}
    for permutation, blocks in analyze.STRATUM_BLOCKS.items():
        spacing = sorted(positions[block] for block in blocks)
        gaps = {second - first for first, second in zip(spacing, spacing[1:])}
        check(len(gaps) > 1, f"stratum {permutation} is still evenly spaced in time: {spacing}")


def test_runtime_provenance_rejects_sentinels_and_foreign_hosts() -> None:
    def mutate_and_expect(mutation: Callable[[dict[str, Any]], None], fragment: str) -> Callable[[Path], None]:
        def run(root: Path) -> None:
            record = analyze.load_json(root / "campaign_run.json")
            mutation(record)
            _write_json(root / "campaign_run.json", record)
            journal = analyze.load_json(root / "campaign_partial.json")
            journal["run_record_sha256"] = analyze.sha256_file(root / "campaign_run.json")
            _write_json(root / "campaign_partial.json", journal)
            assert_fails(lambda: analyze.analyze_campaign(emit=False), fragment)

        return run

    _with_synthetic_bundle(
        mutate_and_expect(lambda r: r["runtime_provenance"].__setitem__("cpu_model", "unknown"), "placeholder")
    )
    _with_synthetic_bundle(
        mutate_and_expect(
            lambda r: r["runtime_provenance"].__setitem__("turbo_state", "intel_pstate.no_turbo=NA cpufreq.boost=NA"),
            "turbo state was not captured",
        )
    )
    _with_synthetic_bundle(
        mutate_and_expect(
            lambda r: r["runtime_provenance"]["binary_shared_libraries"].__setitem__("A", "static or unreadable"),
            "shared library listing was not captured",
        )
    )
    _with_synthetic_bundle(
        mutate_and_expect(
            lambda r: r["runtime_provenance"]["environment"].__setitem__("LD_PRELOAD", "/tmp/evil.so"),
            "LD_PRELOAD was set",
        )
    )
    _with_synthetic_bundle(
        mutate_and_expect(
            lambda r: r["host"].__setitem__("name", "some-other-machine"),
            "attested build host must be the measurement host",
        )
    )


def test_build_attestation_requires_identical_c1_and_c2() -> None:
    def run(root: Path) -> None:
        attestation = analyze.load_json(root / "build_attestation.json")
        attestation["builds"]["C2"]["output_sha256"] = "f" * 64
        attestation["reproducibility_check"]["c2_output_sha256"] = "f" * 64
        _write_json(root / "build_attestation.json", attestation)
        campaign = analyze.load_json(root / "campaign.json")
        campaign["source_artifacts"]["build_attestation.json"] = analyze.sha256_file(root / "build_attestation.json")
        _write_json(root / "campaign.json", campaign)
        assert_fails(
            lambda: analyze.validate_campaign(analyze.load_json(root / "campaign.json"), allow_placeholders=False),
            "C1/C2 output SHA-256",
        )

    _with_synthetic_bundle(run)


def test_build_attestation_binds_campaign_binaries() -> None:
    def run(root: Path) -> None:
        campaign = analyze.load_json(root / "campaign.json")
        campaign["arms"]["B"]["sha256"] = "e" * 64
        _write_json(root / "campaign.json", campaign)
        assert_fails(
            lambda: analyze.validate_campaign(analyze.load_json(root / "campaign.json"), allow_placeholders=False),
            "must equal attested build",
        )

    _with_synthetic_bundle(run)


def main() -> int:
    global PASSED
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        name = test.__name__
        try:
            print(f"[ RUN  ] {name}")
            test()
        # SystemExit too: analyze.fail raises it, so an unexpected fail-closed exit must be
        # recorded as one test failure rather than aborting the whole suite.
        except (Exception, SystemExit):  # noqa: BLE001 - the suite reports every failure itself
            FAILURES.append(name)
            print(f"[ FAIL ] {name}")
            traceback.print_exc()
        else:
            PASSED += 1
            print(f"[  OK  ] {name}")
    print(f"\n{PASSED} passed, {len(FAILURES)} failed out of {len(tests)}")
    if FAILURES:
        print("failed: " + ", ".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
