#!/usr/bin/env python3
"""A/B cheap catalog vs full fillOutIndexEntries on top of express prefix scan.

All three arms enable prefix scan. Only the probe arm enables
internalQueryExpressPrefixScanCheapCatalog. Synthetic 64x10 tree so the
catalog walk is isolated from I/O. Server CPU is schedstat on the connection
thread; control is a second full-catalog process.
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


def populate_synthetic(db) -> dict:
    coll = db["layout2_view"]
    coll.drop()
    docs = []
    parents = []
    for p in range(64):
        parent = f"p{p:03d}"
        parents.append(parent)
        for c in range(10):
            docs.append({
                "tree_id": "base",
                "parent_id": parent,
                "path": f"/{p:03d}/{c:02d}",
                "node_id": f"n{p:03d}{c:02d}",
                "title": f"t{c}",
                "summary": f"s{c}" * 8,
            })
    coll.insert_many(docs)
    coll.create_index([("tree_id", 1), ("node_id", 1)], name="allops_tree_node")
    coll.create_index(
        [("tree_id", 1), ("parent_id", 1), ("path", 1), ("node_id", 1)],
        name="allops_tree_parent_path",
    )
    coll.create_index([("tree_id", 1), ("path", 1)], name="allops_tree_path")
    coll.create_index(
        [("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)],
        name="layout2_rootcause_exact_cover",
    )
    return {"parents": parents, "docs": len(docs), "indexes": 4}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--scratch", default="/tmp/mongo-cheap-catalog-bench")
    parser.add_argument("--blocks", type=int, default=12)
    parser.add_argument("--sweeps", type=int, default=40)
    parser.add_argument("--warmup-sweeps", type=int, default=8)
    parser.add_argument("--cache-gb", type=int, default=2)
    parser.add_argument("--out", required=True)
    parser.add_argument("--port-rotation", type=int, default=0)
    args = parser.parse_args()

    harness.GATE_KIND = "param"
    harness.GATE_ENV = "internalQueryExpressPrefixScanCheapCatalog"

    binary = Path(args.binary).resolve()
    scratch = Path(args.scratch)
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    extra = ["internalQueryEnableExpressPrefixScan=true"]
    ports = arm_ports(args.port_rotation)
    out: dict = {
        "run": {
            "binary": str(binary),
            "blocks": args.blocks,
            "sweeps": args.sweeps,
            "port_rotation": args.port_rotation,
            "arm_ports": ports,
            "note": "prefix scan on for all arms; cheap catalog only on probe",
        },
        "units": {
            "server_cpu_us": "connection-thread schedstat ns/op, microseconds",
            "client_wall_us": "client wall us/op",
        },
    }

    procs = {}
    try:
        log("populate synthetic tree on one server")
        prep = start_mongod(binary, scratch / "dbpath_base", scratch / "prepare.log",
                            57020, args.cache_gb, gate=None, extra_set_parameters=extra)
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
                extra_set_parameters=extra,
            )

        arms = {name: build_arm(ports[name]) for name in ARM_NAMES}
        for name, arm in arms.items():
            explained = arm["db"]["layout2_view"].find(
                {"tree_id": "base", "parent_id": parents[0]},
                {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
            ).sort([("path", 1), ("node_id", 1)]).hint("allops_tree_parent_path").explain()
            stages = json.dumps(explained)
            log(f"  {name} plan contains PREFIX={('PREFIX' in stages)} "
                f"IXSCAN={('IXSCAN' in stages)}")
            if name == "probe" and "PREFIX" not in stages and "EXPRESS" not in stages:
                raise SystemExit(f"probe did not take express prefix scan: {explained}")

        log(f"warmup {args.warmup_sweeps} sweeps")
        for name, arm in arms.items():
            measure_arm(procs[name].pid, arm["comm"], arm["reader"],
                        parents, args.warmup_sweeps)

        blocks = []
        order = list(ARM_NAMES)
        for b in range(args.blocks):
            verify_equality(arms, parents)
            row = {"block": b, "order": list(order)}
            for name in order:
                row[name] = measure_arm(procs[name].pid, arms[name]["comm"],
                                        arms[name]["reader"], parents, args.sweeps)
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
        }
        Path(args.out).write_text(json.dumps(out, indent=2))
        log(f"wrote {args.out} probe median "
            f"{out['summary']['probe_median_pct']:+.2f}% "
            f"control {out['summary']['control_median_pct']:+.2f}%")
    finally:
        for proc in procs.values():
            stop_mongod(proc)


if __name__ == "__main__":
    main()
