#!/usr/bin/env python3
"""Summarise the hinted-plan-memo A/B runs on ``get_children``.

Reads one or more artifacts written by ``bench_children_plancache.py`` and prints
the per-block paired deltas.

Three things this deliberately does *not* do:

* It never combines server CPU with client wall.  They are reported separately and
  labelled, because a break-even was once produced on this project by dividing a
  client-wall cost by a server-CPU saving.
* It never presents block min/max as a confidence interval.  It is the observed
  spread over blocks, nothing more.
* It never silently drops blocks.  Outliers are reported *both* included and
  excluded, with the excluded set named, so the reader can see what the exclusion
  is worth rather than taking it on trust.

``control`` is a second gate-off server identical to ``baseline``.  Its delta is
the process-to-process floor: an effect smaller than it has not been shown.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from pathlib import Path
from typing import Any

OUTLIER_THRESHOLD_PCT = 15.0


def pct(a: float, b: float) -> float:
    return 100.0 * (a - b) / b


def summarise(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    n = len(ordered)
    return {
        "median": st.median(values),
        "mean": st.mean(values),
        "q1": ordered[n // 4],
        "q3": ordered[3 * n // 4],
        "min": min(values),
        "max": max(values),
        "negative": sum(1 for v in values if v < 0),
        "blocks": n,
    }


def row(label: str, s: dict[str, Any]) -> str:
    return ("%-24s median %+7.2f%%  mean %+7.2f%%  IQR [%+6.2f,%+6.2f]  "
            "spread [%+7.2f,%+7.2f]  better %d/%d"
            % (label, s["median"], s["mean"], s["q1"], s["q3"], s["min"], s["max"],
               s["negative"], s["blocks"]))


def analyse(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text())
    blocks = doc["blocks"]
    rotation = doc["run"].get("port_rotation", 0)
    ports = doc["run"].get("arm_ports", {})

    def series(metric: str, a: str, b: str) -> list[float]:
        return [pct(x[a][metric], x[b][metric]) for x in blocks]

    cpu_pb = series("server_cpu_us", "probe", "baseline")
    cpu_cb = series("server_cpu_us", "control", "baseline")
    cpu_pc = series("server_cpu_us", "probe", "control")
    wall_pb = series("client_wall_us", "probe", "baseline")
    wall_cb = series("client_wall_us", "control", "baseline")

    outliers = [i for i, v in enumerate(cpu_pb) if v > OUTLIER_THRESHOLD_PCT]
    keep = [i for i in range(len(cpu_pb)) if i not in outliers]

    print(f"\n=== {path.name} — port rotation {rotation} {ports} ===")
    print(f"    activation: {json.dumps(doc.get('plan_cache_delta', {}))}")
    print(f"    absolute server CPU us/op (median over blocks): "
          f"baseline {st.median([x['baseline']['server_cpu_us'] for x in blocks]):.2f}  "
          f"probe {st.median([x['probe']['server_cpu_us'] for x in blocks]):.2f}  "
          f"control {st.median([x['control']['server_cpu_us'] for x in blocks]):.2f}")
    print("  SERVER CPU, per-block paired, all blocks")
    print("   ", row("probe   vs baseline", summarise(cpu_pb)))
    print("   ", row("control vs baseline", summarise(cpu_cb)))
    print("   ", row("probe   vs control ", summarise(cpu_pc)))
    print("  CLIENT WALL, per-block paired (separate quantity, never combined with CPU)")
    print("   ", row("probe   vs baseline", summarise(wall_pb)))
    print("   ", row("control vs baseline", summarise(wall_cb)))

    if outliers:
        print(f"  blocks where probe exceeded +{OUTLIER_THRESHOLD_PCT:.0f}%: {outliers}")
        print(f"    control in those same blocks: {[round(cpu_cb[i], 2) for i in outliers]}"
              "   (control clean there => not general box noise)")
        print(f"  EXCLUDING those {len(outliers)} blocks — shown to price the exclusion, "
              "not offered as the result")
        print("   ", row("probe   vs baseline", summarise([cpu_pb[i] for i in keep])))
        print("   ", row("control vs baseline", summarise([cpu_cb[i] for i in keep])))
        print("   ", row("probe   vs control ", summarise([cpu_pc[i] for i in keep])))

    return {"path": str(path), "rotation": rotation, "arm_ports": ports,
            "activation": doc.get("plan_cache_delta", {}),
            "cpu_probe_vs_baseline": summarise(cpu_pb),
            "cpu_control_vs_baseline": summarise(cpu_cb),
            "cpu_probe_vs_control": summarise(cpu_pc),
            "wall_probe_vs_baseline": summarise(wall_pb),
            "outlier_blocks": outliers,
            "cpu_probe_vs_baseline_ex_outliers": summarise([cpu_pb[i] for i in keep]),
            "cpu_probe_vs_control_ex_outliers": summarise([cpu_pc[i] for i in keep])}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts", nargs="+")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    results = [analyse(Path(p)) for p in args.artifacts]

    if len(results) > 1:
        print("\n=== across port rotations ===")
        print("If the effect follows the gate rather than the port, it is the change and")
        print("not the machine.  The control column is the floor below which nothing is shown.")
        print("%-10s %-34s %14s %16s %14s" % ("rotation", "probe port", "probe vs base",
                                              "control vs base", "probe vs ctrl"))
        for r in results:
            print("%-10s %-34s %13.2f%% %15.2f%% %13.2f%%"
                  % (r["rotation"], r["arm_ports"].get("probe", "?"),
                     r["cpu_probe_vs_baseline"]["median"],
                     r["cpu_control_vs_baseline"]["median"],
                     r["cpu_probe_vs_control"]["median"]))
        pooled = [r["cpu_probe_vs_baseline"]["median"] for r in results]
        pooled_ctl = [r["cpu_control_vs_baseline"]["median"] for r in results]
        print(f"\nmedian across rotations: probe vs baseline {st.median(pooled):+.2f}%, "
              f"control vs baseline {st.median(pooled_ctl):+.2f}%")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
