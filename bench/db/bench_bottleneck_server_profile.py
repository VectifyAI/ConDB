#!/usr/bin/env python3
"""mongod's own per-operation accounting for all four operations, get_subtree included.

Captures system.profile level 2 for each shape and reports, per operation:

  cpuNanos             CPU nanoseconds mongod attributes to the operation, from
                       CurOp's OperationCPUTimer (curop.cpp:388-390, started
                       lazily in CurOp::startTime, read in calculateCpuTime)
  planningTimeMicros   WALL microseconds between beginQueryPlanningTimer()
                       (find_cmd.cpp:413, immediately after the command BSON has
                       been parsed into a FindCommandRequest) and
                       stopQueryPlanningTimer() at PrepareExecutionHelper::prepare()
                       (curop.h:826-846).  This is a tick-source reading, i.e.
                       wall time, NOT CPU, and the window contains collection
                       acquisition and canonicalisation as well as the planner.
  durationMillis       wall duration of the operation

get_subtree runs as a find plus one or more getMore commands against the same
cursor, so this script groups getMores back onto their originating find by
cursorid and reports the whole operation as well as the first batch alone.  The
existing short_ops_server_profile artifact omitted get_subtree entirely.

The profiler roughly doubles connection-thread CPU (see method_validation.json),
so cpuNanos captured here should be compared with other cpuNanos, not with the
profiler-off CPU numbers in mongo_cpu_arms.json.  The two are reconciled in
method_validation.json instead.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_bottleneck_cpu import (  # noqa: E402
    MONGO_URI, MONGO_DB, NODES, TEXT, NODE_INDEX, CHILD_INDEX, COVER_INDEX,
    TREE_ID, NODE_PROJECTION, CHILD_PROJECTION,
)

PROFILE_SIZE_BYTES = 256 * 1024 * 1024


def log(m: str) -> None:
    print(m, flush=True)


def pct(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))], 3)


def reset_profile(db) -> None:
    db.command("profile", 0)
    db["system.profile"].drop()
    db.create_collection("system.profile", capped=True, size=PROFILE_SIZE_BYTES)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench/db/runs/bottleneck_20260806/server_profile.json")
    parser.add_argument("--inputs", default="bench/db/runs/bottleneck_20260806/mongo_cpu_arms.json")
    parser.add_argument("--iterations", type=int, default=400)
    parser.add_argument("--subtree-iterations", type=int, default=40)
    args = parser.parse_args()

    ids = json.loads(Path(args.inputs).read_text())["inputs"]
    client = MongoClient(MONGO_URI, maxPoolSize=1)
    db = client[MONGO_DB]
    nodes, text = db[NODES], db[TEXT]
    node, parent = ids["node_id"], ids["parent_id"]
    entity = ids["entity_id"]
    lower, upper = ids["subtree_lower"], ids["subtree_upper"]

    def get_node():
        return nodes.find_one({"tree_id": TREE_ID, "node_id": node},
                              NODE_PROJECTION, hint=NODE_INDEX)

    def get_children():
        return sum(1 for _ in nodes.find({"tree_id": TREE_ID, "parent_id": parent},
                                         CHILD_PROJECTION)
                   .sort([("path", 1), ("node_id", 1)]).hint(CHILD_INDEX))

    def get_entity():
        return text.find_one({"_id": entity}, {"_id": 1, "text": 1})

    def get_subtree():
        root = nodes.find_one({"tree_id": TREE_ID, "node_id": node},
                              {"_id": 0, "path": 1}, hint=NODE_INDEX)
        return sum(1 for _ in nodes.find({"path": {"$gte": lower, "$lt": upper}},
                                         CHILD_PROJECTION)
                   .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX))

    shapes = {
        "get_node": (get_node, args.iterations),
        "get_children": (get_children, args.iterations),
        "get_entity": (get_entity, args.iterations),
        "get_subtree": (get_subtree, args.subtree_iterations),
    }

    out: dict[str, Any] = {
        "run": {"generated_unix_s": time.time(),
                "mongodb_version": client.server_info()["version"],
                "profile_capped_size_bytes": PROFILE_SIZE_BYTES},
        "inputs": ids,
        "contract": {
            "cpuNanos": "CPU, from CurOp's OperationCPUTimer",
            "planningTimeMicros": "WALL microseconds from just after command parse to "
                                  "PrepareExecutionHelper::prepare(); includes collection "
                                  "acquisition and canonicalisation, excludes command parse",
            "durationMillis": "wall duration of the operation",
            "warning": "cpuNanos is CPU and planningTimeMicros is wall. A ratio of the "
                       "two is not a fraction of anything. They are reported side by "
                       "side here because that is how mongod reports them, not because "
                       "they are commensurable.",
        },
        "shapes": {},
    }

    for name, (fn, iters) in shapes.items():
        log(f"=== {name}: {iters} iterations with profiler level 2 ===")
        reset_profile(db)
        fn()
        db.command("profile", 2, slowms=0, sampleRate=1.0)
        for _ in range(iters):
            fn()
        db.command("profile", 0)

        docs = list(db["system.profile"].find(
            {"ns": {"$in": [f"{MONGO_DB}.{NODES}", f"{MONGO_DB}.{TEXT}"]}}).sort("ts", 1))
        db["system.profile"].drop()

        # Retain every profile document, so any number below can be rechecked.
        raw_path = Path(args.out).parent / f"raw_profile_{name}.jsonl"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with raw_path.open("w") as fh:
            for d in docs:
                fh.write(json.dumps(d, default=str) + "\n")
        log(f"  retained {len(docs)} raw profile documents in {raw_path.name}")

        finds = [d for d in docs if d.get("op") == "query"]
        getmores = [d for d in docs if d.get("op") == "getmore"]
        by_cursor: dict[Any, list] = defaultdict(list)
        for d in getmores:
            by_cursor[d.get("cursorid")].append(d)

        # A client operation may issue more than one find (get_subtree does a root
        # lookup and then a range scan).  Group finds by planSummary so the two are
        # reported separately instead of being averaged into one meaningless median,
        # then build the whole-operation total as the sum of one of each component.
        by_plan: dict[str, list] = defaultdict(list)
        for f in finds:
            by_plan[f.get("planSummary", "?")].append(f)

        components = {}
        whole_cpu_per_op = 0.0
        for plan, group in by_plan.items():
            cpus = [d.get("cpuNanos", 0) / 1000 for d in group]
            components[f"find[{plan}]"] = {
                "n": len(group),
                "cpu_us_p50": pct(cpus, 50),
                "planning_wall_us_p50": pct([d.get("planningTimeMicros", 0) for d in group], 50),
                "nreturned_p50": pct([d.get("nreturned", 0) for d in group], 50),
                "keysExamined_p50": pct([d.get("keysExamined", 0) for d in group], 50),
                "docsExamined_p50": pct([d.get("docsExamined", 0) for d in group], 50),
            }
            whole_cpu_per_op += (pct(cpus, 50) or 0) * (len(group) / iters)
        if getmores:
            gcpus = [d.get("cpuNanos", 0) / 1000 for d in getmores]
            components["getmore"] = {
                "n": len(getmores),
                "cpu_us_p50": pct(gcpus, 50),
                "planning_wall_us_p50": pct([d.get("planningTimeMicros", 0) for d in getmores], 50),
                "nreturned_p50": pct([d.get("nreturned", 0) for d in getmores], 50),
            }
            whole_cpu_per_op += (pct(gcpus, 50) or 0) * (len(getmores) / iters)

        batches = []
        for f in finds:
            cid = f.get("cursorid")
            batches.append(1 + len(by_cursor.get(cid, []) if cid else []))

        entry = {
            "iterations": iters,
            "find_docs": len(finds),
            "getmore_docs": len(getmores),
            "raw_profile_file": raw_path.name,
            "components": components,
            "whole_operation_cpu_us_from_components": round(whole_cpu_per_op, 3),
            "whole_operation_note":
                "sum over components of (component cpu p50 x components per client "
                "operation); for get_subtree that is one root find + one covered-scan "
                "find + one getMore",
            "batches_per_op_median": statistics.median(batches) if batches else None,
            "first_batch": {
                "cpu_us_p50": pct([d.get("cpuNanos", 0) / 1000 for d in finds], 50),
                "cpu_us_p95": pct([d.get("cpuNanos", 0) / 1000 for d in finds], 95),
                "planning_wall_us_p50": pct([d.get("planningTimeMicros", 0) for d in finds], 50),
                "planning_wall_us_p95": pct([d.get("planningTimeMicros", 0) for d in finds], 95),
                "duration_ms_p50": pct([d.get("durationMillis", 0) for d in finds], 50),
                "nreturned_p50": pct([d.get("nreturned", 0) for d in finds], 50),
                "keysExamined_p50": pct([d.get("keysExamined", 0) for d in finds], 50),
                "docsExamined_p50": pct([d.get("docsExamined", 0) for d in finds], 50),
            },
            "getmore": {
                "cpu_us_p50": pct([d.get("cpuNanos", 0) / 1000 for d in getmores], 50),
                "planning_wall_us_p50": pct([d.get("planningTimeMicros", 0) for d in getmores], 50),
                "nreturned_p50": pct([d.get("nreturned", 0) for d in getmores], 50),
            } if getmores else None,
            "planning_share_of_wall_note":
                "planningTimeMicros / (durationMillis*1000) is wall over wall and is "
                "the only ratio these two fields support",
        }
        if finds:
            d0 = finds[0]
            entry["planning_share_of_first_batch_wall_p50"] = (
                round(entry["first_batch"]["planning_wall_us_p50"] /
                      max(entry["first_batch"]["duration_ms_p50"] * 1000, 1e-9), 4)
                if entry["first_batch"]["duration_ms_p50"] else None)
            entry["example_plan_summary"] = d0.get("planSummary")
            entry["example_keys"] = sorted(k for k in d0.keys())
        out["shapes"][name] = entry
        log(json.dumps({k: v for k, v in entry.items() if k != "example_keys"},
                       indent=2, default=str))
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))

    db.command("profile", 0)
    db["system.profile"].drop()
    out["run"]["status"] = "complete"
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    log(f"\nwritten to {args.out}")
    client.close()


if __name__ == "__main__":
    main()
