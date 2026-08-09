#!/usr/bin/env python3
"""Validation checks for the CPU-per-operation method in bench_bottleneck_cpu.py.

Three things have to be true before any number from that harness means what it
claims to mean.

1.  The connection thread must not burn CPU while waiting for the next request.
    If it spun or polled, CPU per operation would depend on how fast the client
    drives it, and every number would be an artefact of client pacing.  Measured
    by running the same arm at several inter-request delays.
2.  The two kernel counters (schedstat nanoseconds and utime+stime clock ticks)
    must agree once the sample is long enough to make tick quantisation small.
3.  The harness's whole-thread CPU must reconcile with mongod's own per-command
    CPU accounting (system.profile cpuNanos).  The difference between them is
    the part of the cost that lives outside the command timer: socket receive,
    dispatch and reply.  That difference is itself a result, so it is recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient

MONGO_URI = "mongodb://localhost:57017/?directConnection=true"
CLK_TCK = os.sysconf("SC_CLK_TCK")

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_bottleneck_cpu import (  # noqa: E402
    thread_snapshot, thread_delta, mongod_host_pid, NODE_PROJECTION,
    NODES, TEXT, NODE_INDEX, TREE_ID,
)


def log(m: str) -> None:
    print(m, flush=True)


def timed(pid: int, fn, iterations: int, delay_s: float = 0.0) -> dict[str, Any]:
    fn()
    before = thread_snapshot(pid)
    t0 = time.perf_counter()
    for _ in range(iterations):
        fn()
        if delay_s:
            time.sleep(delay_s)
    wall = time.perf_counter() - t0
    delta = thread_delta(before, thread_snapshot(pid))
    return {
        "iterations": iterations,
        "delay_s": delay_s,
        "wall_s": round(wall, 4),
        "cpu_us_per_op": round(delta["conn_sched_ns"] / iterations / 1000, 3),
        "cpu_us_per_op_ticks": round(delta["conn_ticks"] / CLK_TCK * 1e6 / iterations, 3),
        "conn_sched_ns_total": delta["conn_sched_ns"],
        "conn_ticks_total": delta["conn_ticks"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench/db/runs/bottleneck_20260806/method_validation.json")
    parser.add_argument("--iterations", type=int, default=4000)
    args = parser.parse_args()

    client = MongoClient(MONGO_URI, maxPoolSize=1)
    db = client["bench"]
    pid = mongod_host_pid()
    nodes = db[NODES]
    text = db[TEXT]
    node = nodes.find_one({"tree_id": TREE_ID}, {"_id": 0, "node_id": 1})
    entity = text.find_one({}, {"_id": 1})

    shapes = {
        "ping": lambda: db.client["admin"].command("ping"),
        "get_node_hit": lambda: nodes.find_one(
            {"tree_id": TREE_ID, "node_id": node["node_id"]}, NODE_PROJECTION, hint=NODE_INDEX),
        "get_entity_hit": lambda: text.find_one({"_id": entity["_id"]}, {"_id": 1, "text": 1}),
    }

    out: dict[str, Any] = {
        "run": {"generated_unix_s": time.time(), "mongod_host_pid": pid,
                "mongodb_version": client.server_info()["version"],
                "iterations": args.iterations, "clk_tck": CLK_TCK},
        "purpose": {
            "check_1": "CPU per op must not depend on inter-request delay",
            "check_2": "schedstat ns and utime+stime ticks must agree",
            "check_3": "whole-thread CPU vs system.profile cpuNanos; the gap is "
                       "receive+dispatch+reply outside the command timer",
        },
    }

    # ---- check 1 and 2 --------------------------------------------------
    log("=== check 1/2: pacing independence and counter agreement ===")
    out["pacing"] = {}
    for name, fn in shapes.items():
        rows = []
        for delay in (0.0, 0.0005, 0.002):
            iters = args.iterations if delay == 0.0 else max(600, args.iterations // 5)
            r = timed(pid, fn, iters, delay)
            rows.append(r)
            log("  %-14s delay=%6.4fs -> cpu %7.3f us/op (ticks %7.3f) over %d ops"
                % (name, delay, r["cpu_us_per_op"], r["cpu_us_per_op_ticks"], iters))
        base = rows[0]["cpu_us_per_op"]
        spread = max(abs(r["cpu_us_per_op"] - base) / base for r in rows)
        out["pacing"][name] = {
            "rows": rows,
            "max_relative_deviation_from_zero_delay": round(spread, 4),
        }
        log("  %-14s max relative deviation across pacings: %.1f%%" % (name, spread * 100))

    # ---- check 3 --------------------------------------------------------
    log("\n=== check 3: whole-thread CPU vs system.profile cpuNanos ===")
    out["profiler_reconciliation"] = {}
    prof_iters = 2000
    for name, fn in shapes.items():
        if name == "ping":
            continue  # ping is not profiled as a namespaced op
        # whole-thread cost, profiler off
        db.command("profile", 0)
        whole = timed(pid, fn, prof_iters)

        # profiler on: mongod's own per-command CPU, and the thread cost with
        # the profiler's own overhead included (recorded, not used for the gap)
        db["system.profile"].drop()
        db.command("profile", 2, slowms=0, sampleRate=1.0)
        with_prof = timed(pid, fn, prof_iters)
        db.command("profile", 0)

        docs = list(db["system.profile"].find(
            {"op": {"$in": ["query", "command", "getmore"]}},
            {"cpuNanos": 1, "durationMillis": 1, "millis": 1, "op": 1, "ns": 1, "nreturned": 1}))
        cpus = [d["cpuNanos"] / 1000 for d in docs if "cpuNanos" in d]
        db["system.profile"].drop()

        entry = {
            "profiler_off_thread_cpu_us_per_op": whole["cpu_us_per_op"],
            "profiler_on_thread_cpu_us_per_op": with_prof["cpu_us_per_op"],
            "profile_docs": len(docs),
            "profile_cpu_us_p50": round(statistics.median(cpus), 3) if cpus else None,
            "profile_cpu_us_mean": round(statistics.mean(cpus), 3) if cpus else None,
            "gap_us_per_op": round(whole["cpu_us_per_op"] - statistics.median(cpus), 3)
            if cpus else None,
            "gap_note": "whole connection-thread CPU minus mongod's own cpuNanos for "
                        "the command; this is socket receive, command dispatch and "
                        "reply serialisation outside the profiled window, plus any "
                        "measurement bias",
        }
        out["profiler_reconciliation"][name] = entry
        log("  %-14s thread %7.3f us/op | profile cpuNanos p50 %7.3f us | gap %7.3f us"
            % (name, entry["profiler_off_thread_cpu_us_per_op"],
               entry["profile_cpu_us_p50"] or -1, entry["gap_us_per_op"] or -1))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    log(f"\nwritten to {args.out}")
    client.close()


if __name__ == "__main__":
    main()
