#!/usr/bin/env python3
"""Final A/B of express prefix scan vs classic, same binary, three arms.

baseline/control: internalQueryEnableExpressPrefixScan=false
probe:            internalQueryEnableExpressPrefixScan=true
Cheap catalog stays at its default (false).
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_children_plancache as harness
from bench_children_cheap_catalog import populate_synthetic
from bench_children_plancache import (
    ARM_NAMES,
    PORTS,
    arm_ports,
    build_arm,
    log,
    measure_arm,
    start_mongod,
    stop_mongod,
    verify_equality,
)


def plan_kind(explain: dict) -> str:
    blob = json.dumps(explain)
    if "EXPRESS_PREFIX_IXSCAN" in blob or "EXPRESS_IXSCAN_PREFIX" in blob:
        return "EXPRESS_PREFIX"
    if "EXPRESS_IXSCAN" in blob:
        return "EXPRESS_POINT"
    if "IXSCAN" in blob:
        return "CLASSIC_IXSCAN"
    return "OTHER"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--scratch", default="/tmp/mongo-prefix-final-bench")
    parser.add_argument("--blocks", type=int, default=12)
    parser.add_argument("--sweeps", type=int, default=40)
    parser.add_argument("--warmup-sweeps", type=int, default=8)
    parser.add_argument("--cache-gb", type=int, default=2)
    parser.add_argument("--out", required=True)
    parser.add_argument("--port-rotation", type=int, default=0)
    args = parser.parse_args()

    harness.GATE_KIND = "param"
    harness.GATE_ENV = "internalQueryEnableExpressPrefixScan"

    binary = Path(args.binary).resolve()
    scratch = Path(args.scratch)
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    ports = arm_ports(args.port_rotation)
    out: dict = {
        "run": {
            "binary": str(binary),
            "blocks": args.blocks,
            "sweeps": args.sweeps,
            "port_rotation": args.port_rotation,
            "arm_ports": ports,
            "note": "final check: prefix-scan gate only; cheap catalog default off",
        },
        "units": {
            "server_cpu_us": "connection-thread schedstat ns/op, microseconds",
            "client_wall_us": "client wall us/op",
        },
        "correctness": {},
    }

    procs = {}
    try:
        log("populate synthetic 64x10")
        prep = start_mongod(binary, scratch / "dbpath_base", scratch / "prepare.log",
                            57020, args.cache_gb, gate=None)
        try:
            db = MongoClient("mongodb://localhost:57020/?directConnection=true",
                             maxPoolSize=1)["bench"]
            cohort = populate_synthetic(db)
            db.client.admin.command("fsync")
        finally:
            stop_mongod(prep)
        for port in PORTS:
            shutil.copytree(scratch / "dbpath_base", scratch / f"dbpath_{port}")

        parents = cohort["parents"]
        out["cohort"] = cohort

        for name in ARM_NAMES:
            port = ports[name]
            procs[name] = start_mongod(
                binary, scratch / f"dbpath_{port}", scratch / f"{name}.log",
                port, args.cache_gb, harness.ARM_GATES[name],
            )

        arms = {name: build_arm(ports[name]) for name in ARM_NAMES}
        kinds = {}
        for name, arm in arms.items():
            explained = arm["db"]["layout2_view"].find(
                {"tree_id": "base", "parent_id": parents[0]},
                {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
            ).sort([("path", 1), ("node_id", 1)]).hint("allops_tree_parent_path").explain()
            kinds[name] = plan_kind(explained)
            log(f"  {name} plan={kinds[name]}")
        out["correctness"]["plans"] = kinds
        if kinds["probe"] != "EXPRESS_PREFIX":
            raise SystemExit(f"probe did not take prefix scan: {kinds}")
        if kinds["baseline"] != "CLASSIC_IXSCAN" or kinds["control"] != "CLASSIC_IXSCAN":
            raise SystemExit(f"baseline/control must stay classic: {kinds}")

        n = verify_equality(arms, parents)
        out["correctness"]["elements_compared"] = n
        out["correctness"]["row_mismatch"] = False
        log(f"  element-wise equal, {n} elements")

        cheap = arms["probe"]["db"].client.admin.command(
            {"getParameter": 1, "internalQueryExpressPrefixScanCheapCatalog": 1}
        )
        out["correctness"]["cheap_catalog"] = cheap.get(
            "internalQueryExpressPrefixScanCheapCatalog"
        )
        log(f"  cheap catalog on probe = {out['correctness']['cheap_catalog']}")

        log(f"warmup {args.warmup_sweeps}")
        for name, arm in arms.items():
            measure_arm(procs[name].pid, arm["comm"], arm["reader"],
                        parents, args.warmup_sweeps)

        blocks = []
        order = list(ARM_NAMES)
        for b in range(args.blocks):
            verify_equality(arms, parents)
            row = {"block": b, "order": list(order)}
            for name in order:
                row[name] = measure_arm(
                    procs[name].pid, arms[name]["comm"],
                    arms[name]["reader"], parents, args.sweeps,
                )
            base = row["baseline"]["server_cpu_us"]
            row["probe_vs_baseline_pct"] = (
                row["probe"]["server_cpu_us"] - base) / base * 100.0
            row["control_vs_baseline_pct"] = (
                row["control"]["server_cpu_us"] - base) / base * 100.0
            blocks.append(row)
            log(f"  block {b:02d} probe {row['probe_vs_baseline_pct']:+.2f}% "
                f"control {row['control_vs_baseline_pct']:+.2f}% "
                f"({row['baseline']['server_cpu_us']:.1f} -> "
                f"{row['probe']['server_cpu_us']:.1f} us)")
            order.append(order.pop(0))

        probe = [b["probe_vs_baseline_pct"] for b in blocks]
        control = [b["control_vs_baseline_pct"] for b in blocks]
        out["blocks"] = blocks
        out["summary"] = {
            "probe_median_pct": statistics.median(probe),
            "control_median_pct": statistics.median(control),
            "probe_min_pct": min(probe),
            "probe_max_pct": max(probe),
            "baseline_median_us": statistics.median(
                b["baseline"]["server_cpu_us"] for b in blocks),
            "probe_median_us": statistics.median(
                b["probe"]["server_cpu_us"] for b in blocks),
            "blocks_probe_better": sum(1 for x in probe if x < 0),
        }
        Path(args.out).write_text(json.dumps(out, indent=2))
        log(f"wrote {args.out} probe {out['summary']['probe_median_pct']:+.2f}% "
            f"control {out['summary']['control_median_pct']:+.2f}%")
    finally:
        for proc in procs.values():
            stop_mongod(proc)


if __name__ == "__main__":
    main()
