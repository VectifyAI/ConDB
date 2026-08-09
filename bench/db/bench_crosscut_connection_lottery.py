#!/usr/bin/env python3
"""Characterise per-connection latency dispersion against a standalone mongod.

Motivation: a null control in ``bench_crosscut_config.py`` -- six byte-identical
arms, each holding its own connection for the whole run -- produced arms that
differed by up to 20% and drifted monotonically.  Nothing in those arms differs
except the TCP connection, so the connection itself carries a persistent
latency penalty or bonus.  This script measures the size and the shape of that
distribution, because it is the noise floor for every other connection-level
comparison on this box.

Each trial opens one fresh connection, warms it, times a fixed hot loop, and
closes it.  ``--pin`` runs the client thread pinned to one NUMA node's CPUs so
that the client side of the placement is held constant.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import time
from pathlib import Path

MONGO_NODES = "layout2_view"


def percentile(values, pct):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="mongodb://localhost:57017")
    parser.add_argument("--trials", type=int, default=40)
    parser.add_argument("--ops", type=int, default=400)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--pin", type=int, default=-1,
                        help="NUMA node to pin the client thread to (-1: none)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if args.pin >= 0:
        cpus = Path(f"/sys/devices/system/node/node{args.pin}/cpulist").read_text()
        allowed: set[int] = set()
        for part in cpus.strip().split(","):
            if "-" in part:
                lo, hi = part.split("-")
                allowed.update(range(int(lo), int(hi) + 1))
            else:
                allowed.add(int(part))
        os.sched_setaffinity(0, allowed)
        print(f"pinned client to NUMA node {args.pin}: {len(allowed)} cpus")

    from pymongo import MongoClient

    seed = MongoClient(args.uri)
    ids = [
        d["node_id"] for d in
        seed["bench"][MONGO_NODES]
        .find({"tree_id": "base"}, {"node_id": 1, "_id": 0}).limit(400)
    ]
    seed.close()

    projection = {
        "_id": 0, "node_id": 1, "parent_id": 1, "depth": 1, "title": 1,
        "summary": 1, "start_index": 1, "end_index": 1,
    }

    trials = []
    for trial in range(args.trials):
        client = MongoClient(args.uri)
        nodes = client["bench"][MONGO_NODES]
        client.admin.command("ping")
        for i in range(args.warmup):
            nodes.find_one({"tree_id": "base", "node_id": ids[i % len(ids)]},
                           projection, hint="allops_tree_node")
        samples = []
        gc.disable()
        for i in range(args.ops):
            node_id = ids[i % len(ids)]
            started = time.perf_counter()
            nodes.find_one({"tree_id": "base", "node_id": node_id},
                           projection, hint="allops_tree_node")
            samples.append((time.perf_counter() - started) * 1000.0)
        gc.enable()
        trials.append({
            "trial": trial,
            "p50_ms": round(percentile(samples, 50), 6),
            "p10_ms": round(percentile(samples, 10), 6),
            "p95_ms": round(percentile(samples, 95), 6),
            "mean_ms": round(statistics.mean(samples), 6),
        })
        client.close()
        print(f"  trial {trial:3d} p50={trials[-1]['p50_ms']:.4f} "
              f"mean={trials[-1]['mean_ms']:.4f}", flush=True)

    p50s = [t["p50_ms"] for t in trials]
    summary = {
        "uri": args.uri,
        "pin_numa_node": args.pin,
        "trials": args.trials,
        "ops_per_trial": args.ops,
        "connection_p50_min": round(min(p50s), 6),
        "connection_p50_max": round(max(p50s), 6),
        "connection_p50_median": round(statistics.median(p50s), 6),
        "connection_p50_spread_pct": round(
            (max(p50s) - min(p50s)) / min(p50s) * 100, 3),
        "connection_p50_iqr_pct": round(
            (percentile(p50s, 75) - percentile(p50s, 25))
            / percentile(p50s, 50) * 100, 3),
        "connection_p50_cv_pct": round(
            statistics.stdev(p50s) / statistics.mean(p50s) * 100, 3),
        "loadavg": os.getloadavg(),
    }
    print(json.dumps(summary, indent=1))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(
        {"summary": summary, "trials": trials}, indent=1))


if __name__ == "__main__":
    main()
