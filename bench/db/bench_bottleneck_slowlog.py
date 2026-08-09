#!/usr/bin/env python3
"""Quantify what per-operation slow-query logging costs on this server.

The condb_mongo server has slowms=0.  In MongoDB that means every operation
qualifies as slow, so mongod formats a JSON log line for each one and writes it
to the log.  This was already set before this session started -- the first probe
of the session recorded {'was': 0, 'slowms': 0, 'sampleRate': 1.0} -- so it is
in force for every MongoDB measurement this project has taken, including the
report's baselines.

PostgreSQL's counterpart, log_min_duration_statement, is checked here too, so
the two engines' logging configurations can be stated side by side rather than
assumed to match.

The cost is measured from both directions, which is the point:

  real server   slowms 0 -> 100 (logging off for these shapes), then restored
  local clone   slowms 100 -> 0 (logging on), then restored

If turning it off on the real server and turning it on the clone move the cost
by the same amount, the attribution is confirmed by two independent directions
rather than assumed from one.  It also tests the leading alternative explanation
for why the clone was uniformly cheaper than the real server.

The slowms change is reverted in a finally block.  Nothing else is modified.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_bottleneck_cpu import (  # noqa: E402
    MONGO_URI, MONGO_DB, NODES, TEXT, NODE_INDEX, CHILD_INDEX, COVER_INDEX,
    TREE_ID, NODE_PROJECTION, CHILD_PROJECTION, thread_snapshot, thread_delta,
)

LOCAL_URI = "mongodb://localhost:57018/?directConnection=true"


def log(m: str) -> None:
    print(m, flush=True)


def arms_for(db, ids):
    nodes, text = db[NODES], db[TEXT]
    node = ids["node_id"]
    entity = ids["entity_id"]
    lower, upper = ids["subtree_lower"], ids["subtree_upper"]
    return {
        "ping": lambda: db.client["admin"].command("ping"),
        "get_node_hit": lambda: nodes.find_one(
            {"tree_id": TREE_ID, "node_id": node}, NODE_PROJECTION, hint=NODE_INDEX),
        "get_children_hit": lambda: sum(1 for _ in nodes.find(
            {"tree_id": TREE_ID, "parent_id": node}, CHILD_PROJECTION)
            .sort([("path", 1), ("node_id", 1)]).hint(CHILD_INDEX)),
        "get_entity_hit": lambda: text.find_one({"_id": entity}, {"_id": 1, "text": 1}),
        "get_subtree_scan": lambda: sum(1 for _ in nodes.find(
            {"path": {"$gte": lower, "$lt": upper}}, CHILD_PROJECTION)
            .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX)),
    }


def measure(pid: int, fn, iterations: int) -> float:
    fn()
    before = thread_snapshot(pid)
    for _ in range(iterations):
        fn()
    delta = thread_delta(before, thread_snapshot(pid))
    return delta["conn_sched_ns"] / iterations / 1000


def sweep(uri: str, pid: int, ids: dict, slowms_values: list[int],
          iterations: int, subtree_iterations: int, repeats: int) -> dict[str, Any]:
    client = MongoClient(uri, maxPoolSize=1)
    db = client[MONGO_DB]
    original = db.command("profile", -1)
    results: dict[str, Any] = {"original_profile_setting": original, "by_slowms": {}}
    try:
        for slowms in slowms_values:
            db.command("profile", original.get("was", 0), slowms=slowms)
            got = db.command("profile", -1)
            arms = arms_for(db, ids)
            row = {}
            for name, fn in arms.items():
                iters = subtree_iterations if "subtree" in name else iterations
                row[name] = round(statistics.median(
                    measure(pid, fn, iters) for _ in range(repeats)), 3)
            results["by_slowms"][str(slowms)] = {"effective": got, "cpu_us_per_op": row}
            log(f"  slowms={slowms}: " + ", ".join(f"{k}={v}" for k, v in row.items()))
    finally:
        db.command("profile", original.get("was", 0), slowms=original.get("slowms", 100))
        restored = db.command("profile", -1)
        results["restored_to"] = restored
        log(f"  restored: {restored}")
        client.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench/db/runs/bottleneck_20260806/slowlog_cost.json")
    parser.add_argument("--inputs", default="bench/db/runs/bottleneck_20260806/mongo_cpu_arms.json")
    parser.add_argument("--iterations", type=int, default=8000)
    parser.add_argument("--subtree-iterations", type=int, default=120)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--local-pid-file", default="/tmp/claude-1014/"
                        "-home-junyao-code-pageindex-ConDB/"
                        "842657a9-47f6-4a35-8fbc-01be30deb4bd/scratchpad/local_mongod.pid")
    args = parser.parse_args()

    ids = json.loads(Path(args.inputs).read_text())["inputs"]
    real_pid = int(subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Pid}}", "condb_mongo"],
        capture_output=True, text=True, check=True).stdout.strip())

    out: dict[str, Any] = {
        "run": {"generated_unix_s": time.time()},
        "what": "CPU us/op on the mongod connection thread with per-operation slow-query "
                "logging on (slowms=0, the setting this server was already in) and off "
                "(slowms=100, so none of these shapes qualify)",
    }

    log("=== real server condb_mongo (slowms 0 -> 100 -> restored) ===")
    out["real"] = sweep(MONGO_URI, real_pid, ids, [0, 100],
                        args.iterations, args.subtree_iterations, args.repeats)

    pidfile = Path(args.local_pid_file)
    if pidfile.exists():
        try:
            local_pid = int(pidfile.read_text().strip())
            MongoClient(LOCAL_URI, serverSelectionTimeoutMS=1500).admin.command("ping")
            log("\n=== local clone (slowms 100 -> 0 -> restored) ===")
            out["local_clone"] = sweep(LOCAL_URI, local_pid, ids, [100, 0],
                                       args.iterations, args.subtree_iterations, args.repeats)
        except Exception as e:
            log(f"local clone unavailable: {str(e)[:120]}")

    # PostgreSQL's equivalent setting, for the record
    pg = subprocess.run(
        ["docker", "exec", "-i", "condb_pg", "psql", "-U", "postgres", "-d", "bench", "-qAt",
         "-c", "SELECT name||'='||setting FROM pg_settings WHERE name IN "
               "('log_min_duration_statement','log_statement','logging_collector',"
               "'log_duration','log_destination')"],
        capture_output=True, text=True)
    out["postgresql_logging_settings"] = pg.stdout.strip().splitlines()
    log("\n=== PostgreSQL logging settings ===")
    for line in out["postgresql_logging_settings"]:
        log(f"  {line}")

    # derived
    def delta(block):
        on = block["by_slowms"]["0"]["cpu_us_per_op"]
        off = block["by_slowms"]["100"]["cpu_us_per_op"]
        return {k: {"logging_on_us": on[k], "logging_off_us": off[k],
                    "logging_cost_us": round(on[k] - off[k], 3),
                    "logging_share_of_on": round((on[k] - off[k]) / on[k], 4) if on[k] else None}
                for k in on}

    out["derived"] = {"real": delta(out["real"])}
    if "local_clone" in out:
        out["derived"]["local_clone"] = delta(out["local_clone"])

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))

    log("\n=== cost of per-operation slow-query logging (CPU us/op) ===")
    log("%-20s %14s %14s %12s %10s" % ("arm", "logging_on", "logging_off", "cost_us", "share"))
    for k, v in out["derived"]["real"].items():
        log("%-20s %14.3f %14.3f %12.3f %9.1f%%"
            % (k, v["logging_on_us"], v["logging_off_us"], v["logging_cost_us"],
               (v["logging_share_of_on"] or 0) * 100))
    if "local_clone" in out["derived"]:
        log("\n--- same measurement on the local clone, opposite direction ---")
        for k, v in out["derived"]["local_clone"].items():
            log("%-20s %14.3f %14.3f %12.3f %9.1f%%"
                % (k, v["logging_on_us"], v["logging_off_us"], v["logging_cost_us"],
                   (v["logging_share_of_on"] or 0) * 100))
    log(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
