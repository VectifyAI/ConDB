#!/usr/bin/env python3
"""Server-side CPU per operation, measured the same way for MongoDB and PostgreSQL.

The unit is CPU nanoseconds burned inside the server by the thread (MongoDB) or
process (PostgreSQL) that serves one client connection, divided by the number of
client operations, with a same-length idle baseline subtracted.  Wall time per
operation is recorded from the same run so the two can be compared directly.

MongoDB
    mongod runs one thread per connection, named ``conn<N>``.  Every thread of
    the mongod host process is snapshotted before and after each arm from
    ``/proc/<pid>/task/<tid>/schedstat`` field 1 (nanoseconds on CPU) and from
    ``/proc/<pid>/task/<tid>/stat`` fields 14+15 (utime+stime in clock ticks).
    The two counters are independent kernel accounting paths and are both
    retained so they can be checked against each other.  Per-thread deltas are
    kept in the artifact so the attribution to ``conn*`` threads is auditable
    rather than asserted.

PostgreSQL
    postgres forks one backend per connection and the postmaster reaps it, so
    the postmaster's ``cutime+cstime`` (``/proc/<pid>/stat`` fields 16+17)
    increases by exactly the CPU that backend consumed over its whole life.
    Each arm is measured at two iteration counts and the per-operation cost is
    taken as the slope, which cancels connection setup, backend fork and the
    prologue.  psycopg is not installed on this host, so psql is driven through
    ``docker exec``; the client's own cost is outside the measured counter.

Nothing in here writes to layout2_view or layout_shared_text, and no index on
either collection is created or dropped.  The index-count ablation uses
purpose-built ``bottleneck_probe_*`` clone collections.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from pymongo import MongoClient

MONGO_URI = "mongodb://localhost:57017/?directConnection=true"
MONGO_DB = "bench"
NODES = "layout2_view"
TEXT = "layout_shared_text"
NODE_INDEX = "allops_tree_node"
CHILD_INDEX = "allops_tree_parent_path"
COVER_INDEX = "layout2_rootcause_exact_cover"
TREE_ID = "base"

PROBE_PREFIX = "bottleneck_probe"
PROBE_DOCS = 100_000

CLK_TCK = os.sysconf("SC_CLK_TCK")

NODE_PROJECTION = {
    "_id": 0, "node_id": 1, "parent_id": 1, "depth": 1, "title": 1,
    "summary": 1, "start_index": 1, "end_index": 1,
}
CHILD_PROJECTION = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}


def log(message: str) -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------
# CPU accounting
# --------------------------------------------------------------------------

def _read_stat_fields(path: Path) -> list[str] | None:
    """Split /proc stat safely: comm can contain spaces and parentheses."""
    try:
        raw = path.read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    open_paren = raw.find("(")
    close_paren = raw.rfind(")")
    if open_paren < 0 or close_paren < 0:
        return None
    comm = raw[open_paren + 1:close_paren]
    rest = raw[close_paren + 2:].split()
    # index 0 == pid, index 1 == comm, index 2.. == state onwards
    return [raw[:open_paren].strip(), comm] + rest


def thread_snapshot(pid: int) -> dict[int, dict[str, Any]]:
    """CPU counters for every thread of ``pid``, from two kernel sources."""
    out: dict[int, dict[str, Any]] = {}
    task_dir = Path(f"/proc/{pid}/task")
    try:
        tids = os.listdir(task_dir)
    except (FileNotFoundError, PermissionError):
        return out
    for tid_name in tids:
        try:
            tid = int(tid_name)
        except ValueError:
            continue
        fields = _read_stat_fields(task_dir / tid_name / "stat")
        if fields is None or len(fields) < 15:
            continue
        # fields[0]=pid fields[1]=comm fields[2]=state -> utime is field 14 (1-based)
        utime = int(fields[13])
        stime = int(fields[14])
        sched_ns = None
        try:
            sched_ns = int((task_dir / tid_name / "schedstat").read_text().split()[0])
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            pass
        out[tid] = {
            "comm": fields[1],
            "ticks": utime + stime,
            "sched_ns": sched_ns,
        }
    return out


def thread_delta(before: dict[int, dict[str, Any]],
                 after: dict[int, dict[str, Any]]) -> dict[str, Any]:
    per_thread = []
    for tid, post in after.items():
        pre = before.get(tid)
        if pre is None:
            pre = {"comm": post["comm"], "ticks": 0, "sched_ns": 0}
        d_ticks = post["ticks"] - pre["ticks"]
        d_sched = None
        if post["sched_ns"] is not None and pre.get("sched_ns") is not None:
            d_sched = post["sched_ns"] - pre["sched_ns"]
        if d_ticks or (d_sched or 0):
            per_thread.append({
                "tid": tid, "comm": post["comm"],
                "ticks": d_ticks,
                "sched_ns": d_sched,
            })
    per_thread.sort(key=lambda x: -(x["sched_ns"] or 0))
    conn = [t for t in per_thread if t["comm"].startswith("conn")]
    return {
        "per_thread": per_thread,
        "conn_sched_ns": sum(t["sched_ns"] or 0 for t in conn),
        "conn_ticks": sum(t["ticks"] for t in conn),
        "total_sched_ns": sum(t["sched_ns"] or 0 for t in per_thread),
        "total_ticks": sum(t["ticks"] for t in per_thread),
        "conn_threads": [t["comm"] for t in conn],
    }


def mongod_host_pid() -> int:
    """Host pid of the mongod backing port 57017 (the condb_mongo container)."""
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Pid}}", "condb_mongo"],
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


# --------------------------------------------------------------------------
# MongoDB arms
# --------------------------------------------------------------------------

def build_mongo_arms(db: Any, ids: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = db[NODES]
    text = db[TEXT]
    admin = db.client["admin"]
    hit_node = ids["node_id"]
    miss_node = ids["miss_node_id"]
    hit_entity = ids["entity_id"]
    miss_entity = ids["miss_entity_id"]
    parent = ids["parent_id"]
    lower, upper = ids["subtree_lower"], ids["subtree_upper"]
    empty = db[f"{PROBE_PREFIX}_empty"]
    idx2 = db[f"{PROBE_PREFIX}_idx2"]
    idx5 = db[f"{PROBE_PREFIX}_idx5"]
    idx8 = db[f"{PROBE_PREFIX}_idx8"]
    probe_node = ids["probe_node_id"]

    def find_one(coll, flt, proj, hint=None):
        return lambda: coll.find_one(flt, proj, hint=hint)

    def drain(cursor_factory):
        return lambda: sum(1 for _ in cursor_factory())

    arms: dict[str, dict[str, Any]] = {}

    def add(name, fn, note, group):
        arms[name] = {"fn": fn, "note": note, "group": group}

    # ---- floor probes -------------------------------------------------
    add("ping", lambda: admin.command("ping"),
        "admin ping: wire protocol receive, IDL parse of a trivial command, "
        "dispatch, reply. No namespace, no collection acquisition, no plan.",
        "floor")
    add("find_empty_collection", lambda: empty.find_one({}),
        "find on an empty probe collection, no filter, no projection, no hint. "
        "Adds namespace resolution, collection acquisition, canonicalization of "
        "an empty filter, planning of a COLLSCAN, and executor teardown.",
        "floor")

    # ---- get_node ladder ----------------------------------------------
    add("get_node_hit",
        find_one(nodes, {"tree_id": TREE_ID, "node_id": hit_node}, NODE_PROJECTION, NODE_INDEX),
        "report shape: hinted find_one on (tree_id,node_id) with 7-field projection.",
        "get_node")
    add("get_node_miss",
        find_one(nodes, {"tree_id": TREE_ID, "node_id": miss_node}, NODE_PROJECTION, NODE_INDEX),
        "same shape, filter matches nothing: everything except the FETCH of a "
        "real document and the projection transform.",
        "get_node")
    add("get_node_hit_noproj",
        find_one(nodes, {"tree_id": TREE_ID, "node_id": hit_node}, None, NODE_INDEX),
        "hit, no projection: isolates projection AST parse plus transform.",
        "get_node")
    add("get_node_miss_noproj",
        find_one(nodes, {"tree_id": TREE_ID, "node_id": miss_node}, None, NODE_INDEX),
        "miss, no projection: with get_node_miss isolates projection AST parse "
        "and analysis alone, since no document is ever transformed.",
        "get_node")
    add("get_node_hit_proj1",
        find_one(nodes, {"tree_id": TREE_ID, "node_id": hit_node}, {"_id": 0, "node_id": 1}, NODE_INDEX),
        "hit, 1-field projection: per-field cost against the 7-field arm.",
        "get_node")
    add("get_node_hit_nohint",
        find_one(nodes, {"tree_id": TREE_ID, "node_id": hit_node}, NODE_PROJECTION, None),
        "hit, unhinted: this shape has more than one candidate plan, so it is "
        "eligible for the plan cache. Not a like-for-like arm; kept to show the "
        "cost of the path the report does NOT take.",
        "get_node")

    # ---- get_children --------------------------------------------------
    add("get_children_hit",
        drain(lambda: nodes.find({"tree_id": TREE_ID, "parent_id": parent}, CHILD_PROJECTION)
              .sort([("path", 1), ("node_id", 1)]).hint(CHILD_INDEX)),
        "report shape: hinted find on (tree_id,parent_id), 3-field projection, "
        "sort (path,node_id), cursor fully drained.",
        "get_children")
    add("get_children_miss",
        drain(lambda: nodes.find({"tree_id": TREE_ID, "parent_id": miss_node}, CHILD_PROJECTION)
              .sort([("path", 1), ("node_id", 1)]).hint(CHILD_INDEX)),
        "same shape returning zero children: the fixed cost of the shape.",
        "get_children")

    # ---- get_entity ----------------------------------------------------
    add("get_entity_hit",
        find_one(text, {"_id": hit_entity}, {"_id": 1, "text": 1}),
        "report shape: unhinted _id equality on the text collection. On 7.0.34 "
        "this is the IDHACK fast path, not the 8.0+ express path.",
        "get_entity")
    add("get_entity_miss",
        find_one(text, {"_id": miss_entity}, {"_id": 1, "text": 1}),
        "same shape, no such _id: IDHACK setup with no document returned.",
        "get_entity")
    add("get_entity_hit_noproj",
        find_one(text, {"_id": hit_entity}, None),
        "hit without the projection.",
        "get_entity")

    # ---- get_subtree ---------------------------------------------------
    add("get_subtree_root",
        find_one(nodes, {"tree_id": TREE_ID, "node_id": hit_node}, {"_id": 0, "path": 1}, NODE_INDEX),
        "the root lookup half of get_subtree, on its own.",
        "get_subtree")
    add("get_subtree_scan",
        drain(lambda: nodes.find({"path": {"$gte": lower, "$lt": upper}}, CHILD_PROJECTION)
              .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX)),
        "the covered range scan half, cursor fully drained through every "
        "getMore batch, on the chosen subtree.",
        "get_subtree")
    add("get_subtree_full",
        lambda: _subtree_full(nodes, hit_node, lower, upper),
        "the whole report operation: root lookup then drained covered scan.",
        "get_subtree")
    add("get_subtree_scan_count",
        lambda: nodes.count_documents({"path": {"$gte": lower, "$lt": upper}}, hint=COVER_INDEX),
        "same index range, count only: no key decode into BSON, no projection, "
        "no cursor batches. Bounds the pure index-walk cost of the range.",
        "get_subtree")

    # ---- index-count ablation ------------------------------------------
    for label, coll in (("idx2", idx2), ("idx5", idx5), ("idx8", idx8)):
        add(f"probe_{label}_hit",
            find_one(coll, {"tree_id": TREE_ID, "node_id": probe_node}, NODE_PROJECTION, NODE_INDEX),
            f"identical documents and identical hinted query on a {PROBE_DOCS}-doc "
            f"clone carrying {label[3:]} indexes. Differences between the three "
            f"arms are per-index planner-parameter construction.",
            "index_count")

    return arms


def _subtree_full(nodes: Any, node_id: str, lower: str, upper: str) -> int:
    root = nodes.find_one({"tree_id": TREE_ID, "node_id": node_id},
                          {"_id": 0, "path": 1}, hint=NODE_INDEX)
    if root is None:
        return 0
    cursor = (nodes.find({"path": {"$gte": lower, "$lt": upper}}, CHILD_PROJECTION)
              .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX))
    return sum(1 for _ in cursor)


def measure_mongo_arm(pid: int, fn: Callable[[], Any], iterations: int) -> dict[str, Any]:
    fn()  # touch once so the arm is warm and the connection thread exists
    before = thread_snapshot(pid)
    wall_started = time.perf_counter()
    latencies = []
    result = None
    for _ in range(iterations):
        t0 = time.perf_counter()
        result = fn()
        latencies.append(time.perf_counter() - t0)
    wall = time.perf_counter() - wall_started
    after = thread_snapshot(pid)
    delta = thread_delta(before, after)
    rows = result if isinstance(result, int) else (1 if result else 0)
    return {
        "iterations": iterations,
        "rows_last": rows,
        "wall_s": round(wall, 4),
        "wall_us_per_op": round(wall / iterations * 1e6, 3),
        "wall_p50_us": round(statistics.median(latencies) * 1e6, 3),
        "conn_cpu_ns_per_op": round(delta["conn_sched_ns"] / iterations, 1),
        "conn_cpu_us_per_op": round(delta["conn_sched_ns"] / iterations / 1000, 3),
        "conn_ticks_us_per_op": round(delta["conn_ticks"] / CLK_TCK * 1e6 / iterations, 3),
        "total_cpu_us_per_op": round(delta["total_sched_ns"] / iterations / 1000, 3),
        "conn_threads": delta["conn_threads"],
        "per_thread_delta": delta["per_thread"][:12],
    }


def measure_mongo_idle(pid: int, seconds: float) -> dict[str, Any]:
    before = thread_snapshot(pid)
    time.sleep(seconds)
    after = thread_snapshot(pid)
    delta = thread_delta(before, after)
    return {
        "seconds": seconds,
        "conn_cpu_ns_per_s": round(delta["conn_sched_ns"] / seconds, 1),
        "total_cpu_ns_per_s": round(delta["total_sched_ns"] / seconds, 1),
        "per_thread_delta": delta["per_thread"][:12],
    }


# --------------------------------------------------------------------------
# probe collection setup
# --------------------------------------------------------------------------

def setup_probes(db: Any) -> dict[str, Any]:
    """Build clone collections that differ only in how many indexes they carry."""
    info: dict[str, Any] = {"docs": PROBE_DOCS}
    db[f"{PROBE_PREFIX}_empty"].drop()
    db.create_collection(f"{PROBE_PREFIX}_empty")

    source = list(db[NODES].find({"tree_id": TREE_ID}, {"_id": 0}).limit(PROBE_DOCS))
    info["source_docs"] = len(source)
    for label in ("idx2", "idx5", "idx8"):
        name = f"{PROBE_PREFIX}_{label}"
        db[name].drop()
        db[name].insert_many([dict(d) for d in source], ordered=False)

    # idx2: _id_ plus the hinted index == 2 indexes total.
    db[f"{PROBE_PREFIX}_idx2"].create_index(
        [("tree_id", 1), ("node_id", 1)], name=NODE_INDEX)

    # idx5: the same five indexes layout2_view carries.
    five = db[f"{PROBE_PREFIX}_idx5"]
    five.create_index([("tree_id", 1), ("node_id", 1)], name=NODE_INDEX)
    five.create_index([("path", 1), ("node_id", 1)], name="path_1_node_id_1")
    five.create_index([("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)],
                      name=COVER_INDEX)
    five.create_index([("tree_id", 1), ("parent_id", 1), ("path", 1), ("node_id", 1)],
                      name=CHILD_INDEX)

    # idx8: the same five plus three more, to test linearity in index count.
    eight = db[f"{PROBE_PREFIX}_idx8"]
    eight.create_index([("tree_id", 1), ("node_id", 1)], name=NODE_INDEX)
    eight.create_index([("path", 1), ("node_id", 1)], name="path_1_node_id_1")
    eight.create_index([("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)],
                       name=COVER_INDEX)
    eight.create_index([("tree_id", 1), ("parent_id", 1), ("path", 1), ("node_id", 1)],
                       name=CHILD_INDEX)
    eight.create_index([("depth", 1)], name="probe_extra_depth")
    eight.create_index([("start_index", 1)], name="probe_extra_start")
    eight.create_index([("end_index", 1)], name="probe_extra_end")

    for label in ("idx2", "idx5", "idx8"):
        name = f"{PROBE_PREFIX}_{label}"
        info[name] = {
            "count": db[name].count_documents({}),
            "indexes": [ix["name"] for ix in db[name].list_indexes()],
        }
    return info


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def pick_ids(db: Any, subtree_rows_target: int) -> dict[str, Any]:
    nodes = db[NODES]
    cohort = json.loads(Path(
        "bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json"
    ).read_text())["samples"][:200]
    chosen = min(cohort, key=lambda s: abs(s["rows"] - subtree_rows_target))
    path = chosen["path"]
    node_id = path.rsplit("/", 1)[-1]
    parent_doc = nodes.find_one({"tree_id": TREE_ID, "node_id": node_id},
                                {"_id": 0, "node_id": 1, "path": 1})
    entity = db[TEXT].find_one({}, {"_id": 1})
    probe_doc = db[f"{PROBE_PREFIX}_idx5"].find_one({}, {"_id": 0, "node_id": 1})
    return {
        "node_id": node_id,
        "miss_node_id": "zzzz-no-such-node",
        "parent_id": node_id,
        "entity_id": entity["_id"],
        "miss_entity_id": "zzzz-no-such-entity",
        "subtree_path": path,
        "subtree_rows_expected": chosen["rows"],
        "subtree_lower": parent_doc["path"] + "/",
        "subtree_upper": parent_doc["path"] + "0",
        "probe_node_id": probe_doc["node_id"] if probe_doc else None,
        "cohort_source": "bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench/db/runs/bottleneck_20260806/mongo_cpu_arms.json")
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--subtree-iterations", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--subtree-rows", type=int, default=11686)
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--only", default=None, help="comma-separated arm names")
    parser.add_argument("--slowms", type=int, default=None,
                        help="temporarily set slowms for the run and restore afterwards; "
                             "this server ships with slowms=0, which logs a JSON line for "
                             "every operation and costs ~38-40%% of the CPU of the short "
                             "shapes (see slowlog_cost.json)")
    args = parser.parse_args()

    client = MongoClient(MONGO_URI, maxPoolSize=1)
    db = client[MONGO_DB]
    pid = mongod_host_pid()
    log(f"mongod host pid {pid}, server {client.server_info()['version']}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    output: dict[str, Any] = {
        "run": {
            "generated_unix_s": time.time(),
            "iterations": args.iterations,
            "subtree_iterations": args.subtree_iterations,
            "repeats": args.repeats,
            "mongod_host_pid": pid,
            "mongodb_version": client.server_info()["version"],
            "mongodb_git": client.server_info()["gitVersion"],
            "clk_tck": CLK_TCK,
        },
        "contract": {
            "unit": "CPU nanoseconds on the mongod connection thread per client "
                    "operation, from /proc/<pid>/task/<tid>/schedstat field 1",
            "cross_check": "conn_ticks_us_per_op is the same quantity from "
                           "utime+stime clock ticks, an independent counter",
            "idle_baseline": "measured separately; subtract before quoting",
            "profiler": "system profiler is off for these runs",
        },
    }

    if args.setup:
        log(f"building probe clone collections ({PROBE_DOCS} docs each)")
        output["probe_setup"] = setup_probes(db)
        log(json.dumps(output["probe_setup"], indent=2, default=str))

    ids = pick_ids(db, args.subtree_rows)
    output["inputs"] = json.loads(json.dumps(ids, default=str))
    log(f"subtree target: path={ids['subtree_path']} expected rows={ids['subtree_rows_expected']}")

    original_profile = db.command("profile", -1)
    profile_level = original_profile["was"]
    output["run"]["profile_setting_at_start"] = original_profile
    if profile_level != 0:
        raise SystemExit(f"system profiler is on (level {profile_level}); refusing to time")
    if args.slowms is not None:
        db.command("profile", profile_level, slowms=args.slowms)
        output["run"]["slowms_for_this_run"] = db.command("profile", -1)
        log(f"slowms set to {args.slowms} for this run "
            f"(was {original_profile.get('slowms')}); will be restored")

    arms = build_mongo_arms(db, ids)
    if args.only:
        wanted = set(args.only.split(","))
        arms = {k: v for k, v in arms.items() if k in wanted}

    log("idle baseline")
    output["idle_baseline"] = [measure_mongo_idle(pid, 3.0) for _ in range(2)]
    log(json.dumps({k: v for k, v in output["idle_baseline"][0].items()
                    if k != "per_thread_delta"}))

    output["arms"] = {}
    for name, arm in arms.items():
        iterations = args.subtree_iterations if arm["group"] == "get_subtree" and \
            name != "get_subtree_root" else args.iterations
        reps = []
        for rep in range(args.repeats):
            m = measure_mongo_arm(pid, arm["fn"], iterations)
            reps.append(m)
            log(f"  {name} rep{rep+1}: cpu {m['conn_cpu_us_per_op']} us/op "
                f"(ticks {m['conn_ticks_us_per_op']}), wall {m['wall_us_per_op']} us/op, "
                f"rows {m['rows_last']}")
        output["arms"][name] = {
            "note": arm["note"],
            "group": arm["group"],
            "iterations": iterations,
            "repeats": reps,
            "cpu_us_per_op_median": round(statistics.median(
                r["conn_cpu_us_per_op"] for r in reps), 3),
            "cpu_us_per_op_ticks_median": round(statistics.median(
                r["conn_ticks_us_per_op"] for r in reps), 3),
            "wall_us_per_op_median": round(statistics.median(
                r["wall_us_per_op"] for r in reps), 3),
            "wall_p50_us_median": round(statistics.median(
                r["wall_p50_us"] for r in reps), 3),
            "rows": reps[0]["rows_last"],
        }
        out_path.write_text(json.dumps(output, indent=2, default=str))

    output["idle_baseline_after"] = measure_mongo_idle(pid, 3.0)
    if args.slowms is not None:
        db.command("profile", profile_level, slowms=original_profile.get("slowms", 100))
        output["run"]["slowms_restored_to"] = db.command("profile", -1)
        log(f"slowms restored: {output['run']['slowms_restored_to']}")
    output["run"]["status"] = "complete"
    out_path.write_text(json.dumps(output, indent=2, default=str))

    log("\n=== summary: CPU us/op on the mongod connection thread ===")
    log("%-26s %10s %10s %10s %8s" % ("arm", "cpu_us", "cpu_us_tk", "wall_us", "rows"))
    for name, a in output["arms"].items():
        log("%-26s %10.3f %10.3f %10.3f %8s" % (
            name, a["cpu_us_per_op_median"], a["cpu_us_per_op_ticks_median"],
            a["wall_us_per_op_median"], a["rows"]))
    log(f"\nwritten to {out_path}")
    client.close()


if __name__ == "__main__":
    main()
