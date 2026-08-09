#!/usr/bin/env python3
"""Server-side CPU per operation for PostgreSQL, in the same unit as MongoDB.

PostgreSQL forks one backend per connection and the postmaster reaps it, so the
postmaster's cutime+cstime (/proc/<pid>/stat fields 16 and 17) grows by exactly
the CPU that backend burned over its whole life.  Each arm is run at two
iteration counts and the per-operation cost is the slope between them, which
cancels backend fork, connection setup, the prologue and psql teardown.

Two arms are run for every operation because they are two different claims:

  unprepared  every execution re-parses, re-rewrites and re-plans.  This is the
              honest counterpart to MongoDB, which was measured re-planning on
              every call for all four shapes.
  prepared    PREPARE once, then EXECUTE.  This is what the report's benchmark
              actually measured, because it drives PostgreSQL through psycopg3
              whose prepare_threshold defaults to 5, so an identical query text
              is server-prepared after its fifth execution and the plan is
              reused for the rest of the run.

psycopg is not installed on this host, so psql is driven through docker exec.
The client's cost sits outside the counter being read, which is the backend's.
The prepared arm here sends "EXECUTE q(...)" as simple-protocol text, so it
still pays a small parse per call that psycopg3's Bind/Execute would avoid;
that makes this arm a slight overstatement of PostgreSQL's cost, not an
understatement.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

CONTAINER = "condb_pg"
DB = "bench"
USER = "postgres"
NODES = "layout2_pg_view"
TEXT = "layout_shared_pg_text"
TREE_ID = "base"
CLK_TCK = 100  # verified below against os.sysconf on the host


def log(m: str) -> None:
    print(m, flush=True)


def postmaster_pid() -> int:
    out = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", CONTAINER],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def reaped_child_cpu_ticks(pid: int) -> int:
    """cutime+cstime: CPU of children this process has reaped."""
    raw = Path(f"/proc/{pid}/stat").read_text()
    rest = raw[raw.rfind(")") + 2:].split()
    # rest[0]=state, so field N (1-based) is rest[N-3]; cutime=16, cstime=17
    return int(rest[13]) + int(rest[14])


def run_psql(sql: str) -> tuple[str, str]:
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", USER, "-d", DB,
         "-qAt", "-v", "ON_ERROR_STOP=1"],
        input=sql, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed: {proc.stderr[:2000]}")
    return proc.stdout, proc.stderr


def measure(pid: int, sql_builder, n: int) -> dict[str, Any]:
    sql = sql_builder(n)
    before = reaped_child_cpu_ticks(pid)
    t0 = time.perf_counter()
    out, _ = run_psql(sql)
    wall = time.perf_counter() - t0
    after = reaped_child_cpu_ticks(pid)
    return {
        "iterations": n,
        "backend_cpu_ticks": after - before,
        "backend_cpu_s": (after - before) / CLK_TCK,
        "wall_s": round(wall, 4),
        "output_lines": out.count("\n"),
    }


def slope(pid: int, sql_builder, n_small: int, n_large: int, repeats: int) -> dict[str, Any]:
    """CPU per operation as the slope between two iteration counts."""
    small = [measure(pid, sql_builder, n_small) for _ in range(repeats)]
    large = [measure(pid, sql_builder, n_large) for _ in range(repeats)]
    per_op = []
    for a, b in zip(small, large):
        per_op.append((b["backend_cpu_ticks"] - a["backend_cpu_ticks"])
                      / CLK_TCK / (n_large - n_small) * 1e6)
    return {
        "n_small": n_small, "n_large": n_large,
        "small": small, "large": large,
        "cpu_us_per_op_each": [round(x, 3) for x in per_op],
        "cpu_us_per_op": round(statistics.median(per_op), 3),
        "wall_us_per_op": round(
            statistics.median((b["wall_s"] - a["wall_s"]) / (n_large - n_small) * 1e6
                              for a, b in zip(small, large)), 3),
        "rows_per_op": round(statistics.median(
            (b["output_lines"] - a["output_lines"]) / (n_large - n_small) for a, b in zip(small, large)), 2),
    }


# --------------------------------------------------------------------------
# arms
# --------------------------------------------------------------------------

def build_arms(ids: dict[str, str]) -> dict[str, dict[str, Any]]:
    node = ids["node_id"]
    parent = ids["parent_id"]
    entity = ids["entity_id"]
    lower, upper = ids["subtree_lower"], ids["subtree_upper"]

    sql_node = (f"SELECT node_id,parent_id,depth,title,summary,start_index,end_index "
                f"FROM {NODES} WHERE tree_id='{TREE_ID}' AND node_id='{node}';")
    sql_children = (f"SELECT node_id,title,summary FROM {NODES} "
                    f"WHERE tree_id='{TREE_ID}' AND parent_id='{parent}' ORDER BY path,node_id;")
    sql_entity = f"SELECT node_id,text FROM {TEXT} WHERE node_id='{entity}';"
    sql_subtree_root = f"SELECT path FROM {NODES} WHERE tree_id='{TREE_ID}' AND node_id='{node}';"
    sql_subtree_scan = (f"SELECT node_id,title,summary FROM {NODES} "
                        f"WHERE tree_id='{TREE_ID}' AND path>='{lower}' AND path<'{upper}' "
                        f"ORDER BY path,node_id;")

    prep = {
        "pg_select_1": ("PREPARE q AS SELECT 1;", "EXECUTE q;"),
        "pg_get_node": (
            f"PREPARE q(text,text) AS SELECT node_id,parent_id,depth,title,summary,"
            f"start_index,end_index FROM {NODES} WHERE tree_id=$1 AND node_id=$2;",
            f"EXECUTE q('{TREE_ID}','{node}');"),
        "pg_get_children": (
            f"PREPARE q(text,text) AS SELECT node_id,title,summary FROM {NODES} "
            f"WHERE tree_id=$1 AND parent_id=$2 ORDER BY path,node_id;",
            f"EXECUTE q('{TREE_ID}','{parent}');"),
        "pg_get_entity": (
            f"PREPARE q(text) AS SELECT node_id,text FROM {TEXT} WHERE node_id=$1;",
            f"EXECUTE q('{entity}');"),
        "pg_get_subtree_scan": (
            f"PREPARE q(text,text,text) AS SELECT node_id,title,summary FROM {NODES} "
            f"WHERE tree_id=$1 AND path>=$2 AND path<$3 ORDER BY path,node_id;",
            f"EXECUTE q('{TREE_ID}','{lower}','{upper}');"),
        "pg_get_subtree_full": (
            f"PREPARE qr(text,text) AS SELECT path FROM {NODES} WHERE tree_id=$1 AND node_id=$2;\n"
            f"PREPARE q(text,text,text) AS SELECT node_id,title,summary FROM {NODES} "
            f"WHERE tree_id=$1 AND path>=$2 AND path<$3 ORDER BY path,node_id;",
            f"EXECUTE qr('{TREE_ID}','{node}');\nEXECUTE q('{TREE_ID}','{lower}','{upper}');"),
    }
    unprep = {
        "pg_select_1": "SELECT 1;",
        "pg_get_node": sql_node,
        "pg_get_children": sql_children,
        "pg_get_entity": sql_entity,
        "pg_get_subtree_scan": sql_subtree_scan,
        "pg_get_subtree_full": sql_subtree_root + "\n" + sql_subtree_scan,
    }

    arms: dict[str, dict[str, Any]] = {}
    for name in unprep:
        stmt = unprep[name]
        arms[f"{name}__unprepared"] = {
            "builder": (lambda s: (lambda n: "\\o /dev/null\n" + (s + "\n") * n))(stmt),
            "mode": "unprepared",
            "note": "simple protocol, full parse + rewrite + plan on every execution",
        }
        setup, exec_stmt = prep[name]
        arms[f"{name}__prepared"] = {
            "builder": (lambda su, ex: (lambda n: "\\o /dev/null\n" + su + "\n" + (ex + "\n") * n))(setup, exec_stmt),
            "mode": "prepared",
            "note": "PREPARE once then EXECUTE; the plan is reused. This is the mode "
                    "the report's psycopg3 harness ends up in after 5 executions.",
        }
    return arms


# --------------------------------------------------------------------------
# per-phase rusage split
# --------------------------------------------------------------------------

STAT_RE = re.compile(r"^!\s+([\d.]+) s user, ([\d.]+) s system, ([\d.]+) s elapsed")


def phase_split(sql_stmt: str, iterations: int, prepared_setup: str | None = None,
                exec_stmt: str | None = None) -> dict[str, Any]:
    """Per-phase CPU from PostgreSQL's own getrusage instrumentation."""
    body = (exec_stmt if prepared_setup else sql_stmt)
    prologue = (prepared_setup + "\n") if prepared_setup else ""
    sql = ("\\o /dev/null\n" + prologue +
           "SET client_min_messages=log;\n"
           "SET log_parser_stats=on;\nSET log_planner_stats=on;\nSET log_executor_stats=on;\n"
           + (body + "\n") * iterations)
    _, err = run_psql(sql)

    phases: dict[str, list[float]] = {}
    current = None
    for line in err.splitlines():
        line = line.strip()
        if line.startswith("LOG:") and line.endswith("STATISTICS"):
            current = line[len("LOG:"):].strip().replace(" STATISTICS", "")
            continue
        m = STAT_RE.match(line)
        if m and current:
            phases.setdefault(current, []).append(
                (float(m.group(1)) + float(m.group(2))) * 1e6)
            current = None
    return {
        "iterations": iterations,
        "phases_us": {k: {"n": len(v), "median": round(statistics.median(v), 2),
                          "mean": round(statistics.mean(v), 2), "sum": round(sum(v), 1)}
                      for k, v in phases.items()},
        "note": "user+system CPU microseconds per phase from PostgreSQL's own "
                "getrusage instrumentation (log_parser_stats/log_planner_stats/"
                "log_executor_stats). The elog of each record adds overhead that "
                "is NOT inside these phase timers, but the timers themselves are "
                "the phase boundaries PostgreSQL defines.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench/db/runs/bottleneck_20260806/pg_cpu_arms.json")
    parser.add_argument("--n-small", type=int, default=2000)
    parser.add_argument("--n-large", type=int, default=10000)
    parser.add_argument("--subtree-n-small", type=int, default=20)
    parser.add_argument("--subtree-n-large", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--inputs", default="bench/db/runs/bottleneck_20260806/mongo_cpu_arms.json")
    args = parser.parse_args()

    import os
    assert os.sysconf("SC_CLK_TCK") == CLK_TCK, "clock tick assumption is wrong"

    mongo_inputs = json.loads(Path(args.inputs).read_text())["inputs"]
    ids = {
        "node_id": mongo_inputs["node_id"],
        "parent_id": mongo_inputs["parent_id"],
        "entity_id": mongo_inputs["entity_id"],
        "subtree_lower": mongo_inputs["subtree_lower"],
        "subtree_upper": mongo_inputs["subtree_upper"],
    }
    log(f"inputs mirrored from the MongoDB run: {json.dumps(ids)}")

    pid = postmaster_pid()
    log(f"condb_pg postmaster host pid {pid}")

    out: dict[str, Any] = {
        "run": {"generated_unix_s": time.time(), "postmaster_host_pid": pid,
                "repeats": args.repeats, "clk_tck": CLK_TCK},
        "inputs": ids,
        "contract": {
            "unit": "CPU microseconds (user+system) burned by the PostgreSQL backend "
                    "per client operation, from the postmaster's cutime+cstime for "
                    "the reaped backend, taken as a two-point slope",
            "comparability": "same quantity as the MongoDB harness's connection-thread "
                             "CPU: server-side CPU per client operation, client cost "
                             "excluded on both sides",
        },
        "arms": {},
    }

    arms = build_arms(ids)
    for name, arm in arms.items():
        is_subtree = "subtree" in name
        ns, nl = ((args.subtree_n_small, args.subtree_n_large) if is_subtree
                  else (args.n_small, args.n_large))
        r = slope(pid, arm["builder"], ns, nl, args.repeats)
        r["mode"] = arm["mode"]
        r["note"] = arm["note"]
        out["arms"][name] = r
        log("  %-34s %10.3f us cpu/op  (%s)  wall %9.3f us/op  rows %s"
            % (name, r["cpu_us_per_op"], ",".join(str(x) for x in r["cpu_us_per_op_each"]),
               r["wall_us_per_op"], r["rows_per_op"]))
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))

    # ---- per-phase split -------------------------------------------------
    log("\n=== PostgreSQL per-phase CPU (its own getrusage instrumentation) ===")
    node = ids["node_id"]
    splits = {
        "pg_get_node__unprepared": (
            f"SELECT node_id,parent_id,depth,title,summary,start_index,end_index "
            f"FROM {NODES} WHERE tree_id='{TREE_ID}' AND node_id='{node}';", None, None),
        "pg_get_node__prepared": (
            None,
            f"PREPARE q(text,text) AS SELECT node_id,parent_id,depth,title,summary,"
            f"start_index,end_index FROM {NODES} WHERE tree_id=$1 AND node_id=$2;",
            f"EXECUTE q('{TREE_ID}','{node}');"),
        "pg_get_entity__unprepared": (
            f"SELECT node_id,text FROM {TEXT} WHERE node_id='{ids['entity_id']}';", None, None),
        "pg_get_children__unprepared": (
            f"SELECT node_id,title,summary FROM {NODES} WHERE tree_id='{TREE_ID}' "
            f"AND parent_id='{ids['parent_id']}' ORDER BY path,node_id;", None, None),
    }
    out["phase_split"] = {}
    for name, (stmt, setup, ex) in splits.items():
        r = phase_split(stmt, 400, setup, ex)
        out["phase_split"][name] = r
        log(f"  {name}:")
        for phase, v in sorted(r["phases_us"].items()):
            log("     %-28s n=%4d median %7.2f us  mean %7.2f us" % (phase, v["n"], v["median"], v["mean"]))
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))

    out["run"]["status"] = "complete"
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    log(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
