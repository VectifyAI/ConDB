#!/usr/bin/env python3
"""PostgreSQL server CPU per operation, driven through the report's own driver.

bench_bottleneck_pg_cpu.py drives psql, which can only speak the simple query
protocol, so its "prepared" arm has to send ``EXECUTE q(...)`` as utility
statement text.  For small result sets that is a fair model of psycopg3, but for
get_subtree's 11,686 rows the utility-statement row-delivery path costs about
twice what a plain SELECT costs, which is an artefact of the instrument rather
than a property of PostgreSQL.  This script removes that artefact by using
psycopg3 itself, exactly as bench_all_ops_layouts.py does.

psycopg3 binds parameters server-side (extended protocol) and, with the default
prepare_threshold of 5, promotes a query text to a named prepared statement
after its fifth execution.  Both modes are measured here:

  prepared    prepare_threshold=5, the driver default and therefore what the
              report's numbers were actually produced under
  unprepared  prepare_threshold=None, so every execution is parsed and planned

The measured quantity is the backend process's utime+stime over the timing
loop, which is the same quantity the MongoDB harness reads for the mongod
connection thread.  The backend is located by matching pg_backend_pid() against
the second field of NSpid in /proc/<hostpid>/status.

Run with the system interpreter, which has both drivers:
    PYTHONPATH=<scratch>/pglib /usr/bin/python3 bench/db/bench_bottleneck_pg_psycopg.py
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import psycopg

DSN = "host=localhost port=55432 dbname=bench user=postgres password=bench"
NODES = "layout2_pg_view"
TEXT = "layout_shared_pg_text"
TREE_ID = "base"
CLK_TCK = os.sysconf("SC_CLK_TCK")


def log(m: str) -> None:
    print(m, flush=True)


def host_pid_for_backend(container_pid: int) -> int:
    """Map a pid inside the container's namespace to the host pid."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            status = Path(f"/proc/{entry}/status").read_text()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for line in status.splitlines():
            if line.startswith("NSpid:"):
                parts = line.split()[1:]
                if len(parts) > 1 and int(parts[-1]) == container_pid:
                    return int(entry)
                break
    raise RuntimeError(f"no host pid found for container pid {container_pid}")


def proc_cpu_ticks(pid: int) -> int:
    raw = Path(f"/proc/{pid}/stat").read_text()
    rest = raw[raw.rfind(")") + 2:].split()
    return int(rest[11]) + int(rest[12])  # utime + stime


def measure(pid: int, fn, iterations: int) -> dict[str, Any]:
    fn()
    before = proc_cpu_ticks(pid)
    t0 = time.perf_counter()
    rows = 0
    for _ in range(iterations):
        rows = fn()
    wall = time.perf_counter() - t0
    ticks = proc_cpu_ticks(pid) - before
    return {
        "iterations": iterations,
        "rows": rows,
        "cpu_us_per_op": round(ticks / CLK_TCK * 1e6 / iterations, 3),
        "cpu_ticks_total": ticks,
        "wall_us_per_op": round(wall / iterations * 1e6, 3),
    }


def build_arms(conn: psycopg.Connection, ids: dict[str, str]):
    node, parent = ids["node_id"], ids["parent_id"]
    entity = ids["entity_id"]
    lower, upper = ids["subtree_lower"], ids["subtree_upper"]

    def q(sql, params):
        return lambda: len(conn.execute(sql, params).fetchall())

    def subtree_full():
        root = conn.execute(
            f"SELECT path FROM {NODES} WHERE tree_id=%s AND node_id=%s",
            (TREE_ID, node)).fetchone()
        if root is None:
            return 0
        return len(conn.execute(
            f"SELECT node_id,title,summary FROM {NODES} "
            f"WHERE tree_id=%s AND path>=%s AND path<%s ORDER BY path,node_id",
            (TREE_ID, root[0] + "/", root[0] + "0")).fetchall())

    return {
        "pg_select_1": q("SELECT %s::int", (1,)),
        "pg_get_node": q(
            f"SELECT node_id,parent_id,depth,title,summary,start_index,end_index "
            f"FROM {NODES} WHERE tree_id=%s AND node_id=%s", (TREE_ID, node)),
        "pg_get_children": q(
            f"SELECT node_id,title,summary FROM {NODES} "
            f"WHERE tree_id=%s AND parent_id=%s ORDER BY path,node_id", (TREE_ID, parent)),
        "pg_get_entity": q(
            f"SELECT node_id,text FROM {TEXT} WHERE node_id=%s", (entity,)),
        "pg_get_subtree_scan": q(
            f"SELECT node_id,title,summary FROM {NODES} "
            f"WHERE tree_id=%s AND path>=%s AND path<%s ORDER BY path,node_id",
            (TREE_ID, lower, upper)),
        "pg_get_subtree_full": subtree_full,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench/db/runs/bottleneck_20260806/pg_psycopg_cpu.json")
    parser.add_argument("--inputs", default="bench/db/runs/bottleneck_20260806/mongo_cpu_arms.json")
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--subtree-iterations", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    mongo_inputs = json.loads(Path(args.inputs).read_text())["inputs"]
    ids = {k: mongo_inputs[k] for k in
           ("node_id", "parent_id", "entity_id", "subtree_lower", "subtree_upper")}
    log(f"inputs mirrored from the MongoDB run: {json.dumps(ids)}")

    out: dict[str, Any] = {
        "run": {"generated_unix_s": time.time(), "clk_tck": CLK_TCK,
                "psycopg": psycopg.__version__, "iterations": args.iterations,
                "subtree_iterations": args.subtree_iterations, "repeats": args.repeats},
        "inputs": ids,
        "contract": {
            "unit": "CPU microseconds (utime+stime) burned by the PostgreSQL backend "
                    "process per client operation",
            "comparability": "same quantity as the MongoDB harness's connection-thread "
                             "CPU; client cost excluded on both sides",
            "driver": "psycopg3, the same driver bench_all_ops_layouts.py uses",
        },
        "arms": {},
    }

    for mode, threshold in (("prepared", 5), ("unprepared", None)):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.prepare_threshold = threshold
            backend = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            pid = host_pid_for_backend(backend)
            log(f"\n=== mode={mode} (prepare_threshold={threshold}) backend "
                f"container pid {backend} -> host pid {pid} ===")
            arms = build_arms(conn, ids)
            for name, fn in arms.items():
                iters = args.subtree_iterations if "subtree" in name else args.iterations
                reps = [measure(pid, fn, iters) for _ in range(args.repeats)]
                key = f"{name}__{mode}"
                out["arms"][key] = {
                    "mode": mode,
                    "prepare_threshold": threshold,
                    "iterations": iters,
                    "repeats": reps,
                    "cpu_us_per_op": round(statistics.median(r["cpu_us_per_op"] for r in reps), 3),
                    "wall_us_per_op": round(statistics.median(r["wall_us_per_op"] for r in reps), 3),
                    "rows": reps[0]["rows"],
                }
                a = out["arms"][key]
                log("  %-34s %10.3f us cpu/op   wall %10.3f us/op   rows %s"
                    % (key, a["cpu_us_per_op"], a["wall_us_per_op"], a["rows"]))
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_text(json.dumps(out, indent=2, default=str))

            # how many prepared statements did the driver actually create?
            n_prepared = conn.execute(
                "SELECT count(*) FROM pg_prepared_statements").fetchone()[0]
            out["arms"].setdefault("_meta", {})[mode] = {
                "server_prepared_statements": n_prepared}
            log(f"  server-side prepared statements at end of {mode}: {n_prepared}")

    out["run"]["status"] = "complete"
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    log(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
