#!/usr/bin/env python3
"""Post-hoc analysis of the two control workloads. NOT part of the pre-registered protocol.

`analyze.py` is the frozen analyzer: it was hashed into the attempt ledger before execution and it
decides the adoption gate. This script is different in kind. It was written *after* the results
were known, to explain why both controls fell outside their pre-registered bands, and it exists so
that the numbers the report quotes for that explanation are reproducible rather than asserted.

Nothing here can change the adoption gate. `analyze.py` reports
`overall_adoption_gate_passed: false` and that outcome stands.

Two of the quantities below condition on a post-treatment variable — which of two instruction
states a process landed in — and the incidence of that state differs by arm, so the conditioned
estimates are not protected by randomisation. They are reported alongside the pre-registered
marginal estimates, never in place of them. Section 3 prints both so the difference is visible.

    python3 analyze_controls_posthoc.py

Reads only `raw/*.json`. Writes nothing. No third-party package required.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import statistics as st
import sys

RAW_GLOB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw", "*.json")
NAME_RE = re.compile(r"^block(\d+)_([SMWPX])_([ABC])_")

# Counted documents and index keys scanned per iteration, from campaign.json's guard contracts.
# The guards say "400001 works/keys examined" and so on; the +1 is the work that returns EOF.
DOCUMENTS = {"S": 400_000, "M": 200_000, "W": 200_000, "X": 200_000}
KEYS = {"S": 400_000, "M": 400_000, "W": 200_000}

ARM_LABEL = {"A": "pinned base", "B": "rejected implementation", "C": "candidate"}


def load():
    """(block, workload, arm) -> (mean instructions, mean cpu ns, [per-repetition instructions])."""
    cells = {}
    for path in sorted(glob.glob(RAW_GLOB)):
        m = NAME_RE.match(os.path.basename(path))
        if not m:
            continue
        with open(path) as fh:
            doc = json.load(fh)
        rows = [b for b in doc["benchmarks"] if b.get("run_type") == "iteration"]
        instr = [r["instructions_per_iteration"] for r in rows]
        cpu = [r["cpu_time"] for r in rows]
        cells[(int(m.group(1)), m.group(2), m.group(3))] = (
            sum(instr) / len(instr),
            sum(cpu) / len(cpu),
            instr,
        )
    return cells


def load_arm_positions():
    """(block, workload, arm) -> which of the three arms ran first, second or third."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "campaign_run.json")
    with open(path) as fh:
        run = json.load(fh)
    return {
        (p["block"], p["workload"], p["arm"]): p["arm_position"]
        for p in run["completed_processes"]
    }


def split_states(values):
    """Threshold rule: cut at the single widest relative gap in the sorted process means.

    Stated as a rule rather than a number so it is checkable. It is only meaningful because the gap
    it finds is two orders of magnitude wider than every other gap in the sample; `describe_states`
    prints the runner-up so a reader can see that for themselves.
    """
    ordered = sorted(values)
    gaps = [((ordered[i + 1] - ordered[i]) / ordered[i], i) for i in range(len(ordered) - 1)]
    widest, index = max(gaps)
    runner_up = max(g for g, i in gaps if i != index)
    return (ordered[index] + ordered[index + 1]) / 2.0, widest, runner_up


def pct(x):
    return f"{100.0 * x:+.4f}%"


def rel_range(xs):
    return (max(xs) - min(xs)) / min(xs)


def main():
    cells = load()
    if not cells:
        sys.exit(f"no raw process files matched {RAW_GLOB}")
    blocks = sorted({k[0] for k in cells})
    arms = "ABC"

    print("=" * 78)
    print("POST-HOC CONTROL ANALYSIS -- not pre-registered, cannot change the adoption gate")
    print(f"{len(blocks)} blocks, {len(cells)} processes")
    print("=" * 78)

    # ---------------------------------------------------------------- 1. point control (P)
    print("\n1. POINT-QUERY CONTROL (P)  --  the optimization cannot execute on this plan\n")
    for num, den in (("C", "A"), ("B", "A")):
        ir = [cells[(b, "P", num)][0] / cells[(b, "P", den)][0] for b in blocks]
        cr = [cells[(b, "P", num)][1] / cells[(b, "P", den)][1] for b in blocks]
        print(
            f"   {num}/{den}   instructions {st.mean(ir):.6f}"
            f"   cpu time {st.mean(cr):.6f}  (sd {st.stdev(cr):.4f})"
        )
    print("\n   CPU time per arm, averaged over blocks -- the offset follows the binary:")
    for arm in arms:
        xs = [cells[(b, "P", arm)][1] for b in blocks]
        print(f"      arm {arm} ({ARM_LABEL[arm]:<24}) {st.mean(xs):9.1f} ns")
    print("\n   CPU time per execution position, the same processes regrouped:")
    positions = load_arm_positions()
    for pos in sorted({p for p in positions.values()}):
        xs = [
            cells[(b, "P", a)][1]
            for b in blocks
            for a in arms
            if positions.get((b, "P", a)) == pos
        ]
        print(f"      position {pos} within its block           {st.mean(xs):9.1f} ns  (n={len(xs)})")
    print(
        "\n   The instruction ratio is flat and the CPU ratio is not, so this is a property of the\n"
        "   binaries, not of the optimization. The report makes no CPU-time claim because of it."
    )

    # ---------------------------------------------------------- 2. non-intrusion control (X)
    print("\n2. NON-INTRUSION CONTROL (X)  --  COUNT -> FETCH -> IXSCAN, optimization cannot fire\n")
    means = {(b, a): cells[(b, "X", a)][0] for b in blocks for a in arms}
    threshold, widest, runner_up = split_states(means.values())
    print(
        f"   widest relative gap {100 * widest:.4f}%, next widest {100 * runner_up:.4f}%"
        f"  -> threshold {threshold:.0f}"
    )
    state = {k: (0 if v < threshold else 1) for k, v in means.items()}

    for s in (0, 1):
        xs = [v for k, v in means.items() if state[k] == s]
        print(
            f"   state {s}: n={len(xs):3d}  mean {st.mean(xs):.0f}"
            f"  spread over all arms {100 * rel_range(xs):.4f}%"
        )
    lo = st.mean([v for k, v in means.items() if state[k] == 0])
    hi = st.mean([v for k, v in means.items() if state[k] == 1])
    print(f"   separation between states: {100 * (hi / lo - 1):.4f}%")

    straddlers = 0
    for k, (_, _, reps) in ((k, cells[(k[0], "X", k[1])]) for k in means):
        if len({0 if r < threshold else 1 for r in reps}) > 1:
            straddlers += 1
    print(f"   processes whose repetitions straddle the threshold: {straddlers}")

    print("\n   Within a state AND within an arm the process-to-process spread collapses:")
    for arm in arms:
        for s in (0, 1):
            xs = [means[(b, arm)] for b in blocks if state[(b, arm)] == s]
            if len(xs) > 1:
                print(
                    f"      arm {arm} state {s}: n={len(xs):2d}"
                    f"  cv {100 * st.stdev(xs) / st.mean(xs):.4f}%"
                    f"  spread {100 * rel_range(xs):.4f}%"
                )
    print("\n   High-state incidence differs by arm, which is why conditioning is post-treatment:")
    for arm in arms:
        n = sum(1 for b in blocks if state[(b, arm)] == 1)
        print(f"      arm {arm} ({ARM_LABEL[arm]:<24}) {n}/{len(blocks)}")

    print("\n3. X CONTROL: PRE-REGISTERED MARGINAL ESTIMATE vs POST-HOC CONDITIONED ESTIMATE\n")
    for num, den in (("C", "A"), ("B", "A"), ("B", "C")):
        marginal = [means[(b, num)] / means[(b, den)] for b in blocks]
        print(
            f"   {num}/{den}  marginal over all {len(blocks)} blocks: {st.mean(marginal):.6f}"
            f"   (block sd {st.stdev(marginal):.4e})"
        )
        for s in (0, 1):
            paired = [b for b in blocks if state[(b, num)] == s and state[(b, den)] == s]
            if len(paired) < 2:
                print(f"      state {s}: n={len(paired)} -- too few concordant blocks")
                continue
            ratios = [means[(b, num)] / means[(b, den)] for b in paired]
            diffs = [means[(b, num)] - means[(b, den)] for b in paired]
            print(
                f"      state {s}: n={len(paired):2d}  ratio {st.mean(ratios):.6f}"
                f"  (sd {st.stdev(ratios):.2e})"
                f"  {st.mean(diffs) / DOCUMENTS['X']:+.2f} instructions per fetched document"
            )
    print(
        "\n   The marginal B/A is below 1 while both conditioned B/A are above it. The pre-registered\n"
        "   estimator inverts the sign here because arm B landed in the high state 7 times out of 30\n"
        "   against arm A's 13, and averaging over blocks absorbs the difference. That is a property\n"
        "   of this control workload; it does not arise on S, M or W, where no such state exists."
    )

    blocks_needed = None
    marginal = [means[(b, "C")] / means[(b, "A")] for b in blocks]
    sd = st.stdev(marginal)
    if sd > 0:
        blocks_needed = math.ceil((1.96 * sd / 0.002) ** 2)
    print(
        f"\n   The pre-registered band was +/-0.2%. At the observed per-block sd of {100 * sd:.2f}%\n"
        f"   a 95% interval reaches that half-width at roughly {blocks_needed} blocks, not 30, so the\n"
        "   band was unattainable as written."
    )

    # ------------------------------------------------------------- 4. endpoint normalisation
    print("\n4. COUNT ENDPOINTS: IS THE SAVING PER DOCUMENT OR PER STAGE ITERATION?\n")
    print(f"   {'':<8}{'ratio':>10}{'per counted doc':>18}{'per stage iteration':>22}")
    for wl in "SMW":
        for num, den in (("C", "A"), ("B", "A"), ("B", "C")):
            ratios = [cells[(b, wl, num)][0] / cells[(b, wl, den)][0] for b in blocks]
            saved = st.mean([cells[(b, wl, den)][0] - cells[(b, wl, num)][0] for b in blocks])
            print(
                f"   {wl} {num}/{den}{st.mean(ratios):10.6f}"
                f"{saved / DOCUMENTS[wl]:18.2f}{saved / KEYS[wl]:22.3f}"
            )
    print(
        "\n   Only M discriminates: it scans 400,000 keys while counting 200,000 documents, so on S\n"
        "   and W the two columns are identical by construction. On M the candidate's saving holds\n"
        "   per counted document (116.05, against 118.00 and 117.06 on S and W) and the rejected\n"
        "   arm's extra saving over the candidate holds per stage iteration (8.999, against 11.004\n"
        "   and 9.976). The rejected arm's remaining advantage is devirtualisation of the two\n"
        "   per-iteration calls between CountStage and CountScan, which is per iteration; the\n"
        "   candidate's saving is one working-set member, which is per counted document."
    )
    print()


if __name__ == "__main__":
    main()
