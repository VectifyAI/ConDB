#!/usr/bin/env python3
"""A/B the hinted-plan memo probe on ``get_children``, against a locally built mongod.

The measured quantity is **server CPU on the arm's own connection thread**
(``/proc/<pid>/task/<tid>/schedstat``, nanoseconds), never wall clock and never
retired instructions.  Client wall is recorded alongside it but the two are
never combined into one figure.

Why three servers.  The probe is gated by an environment variable that is read
once at process start, so one process cannot serve both arms.  Each arm is
therefore its own mongod, started from the *same binary* against a
byte-identical copy of one dbpath:

    baseline   gate off    the stock code path
    probe      gate on     the change under test
    control    gate off    identical to baseline in every respect

``control`` is what licenses reading the ``probe`` number.  Two processes from
the same binary on the same data still differ; ``control - baseline`` is that
difference measured, and any ``probe`` effect smaller than it is noise.

Arms rotate within each block and the reported effect is the median over blocks
of the per-block paired delta.  Every block first verifies that all three arms
return element-wise identical rows for every input; a single mismatch aborts
the run.  Each arm holds one pinned connection (``maxPoolSize=minPoolSize=1``)
for the whole run, because fresh connections have been measured 14-26% apart in
P50 on this workload.

Block min/max is the observed spread over blocks.  It is not a confidence
interval and must not be reported as one.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from pymongo import MongoClient

SOURCE_URI = "mongodb://localhost:57017/?directConnection=true"
DB_NAME = "bench"
NODES = "layout2_view"
TREE_ID = "base"

NODE_INDEX = "allops_tree_node"
CHILD_INDEX = "allops_tree_parent_path"
COVER_INDEX = "layout2_rootcause_exact_cover"

CHILD_PROJECTION = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}
CHILD_SORT = [("path", 1), ("node_id", 1)]

DEFAULT_GATE_ENV = "MONGO_PROBE_HINTED_PLAN_MEMO"
GATE_ENV = DEFAULT_GATE_ENV  # overridden by --gate-env

ARM_NAMES = ("baseline", "probe", "control")
ARM_GATES = {"baseline": "0", "probe": "1", "control": "0"}
PORTS = (57021, 57022, 57023)


def arm_ports(rotation: int) -> dict[str, int]:
    """Which physical port (and therefore which dbpath copy) each arm runs on.

    Rotating this is how a server-specific bias is told apart from a real effect: if the
    effect follows the gate rather than the port, it is the change and not the machine.
    """
    return {name: PORTS[(i + rotation) % len(PORTS)] for i, name in enumerate(ARM_NAMES)}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ------------------------------------------------------------ server CPU tap

def thread_nanos(pid: int, comm: str) -> int:
    """schedstat nanoseconds for the threads of ``pid`` whose comm is ``comm``."""
    total = 0
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return 0
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/comm") as handle:
                if handle.read().strip() != comm:
                    continue
            with open(f"{task_dir}/{tid}/schedstat") as handle:
                total += int(handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            continue
    return total


# --------------------------------------------------------------- the server

def start_mongod(binary: Path, dbpath: Path, logpath: Path, port: int,
                 cache_gb: int, gate: str | None) -> subprocess.Popen:
    dbpath.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if gate is not None:
        env[GATE_ENV] = gate
    else:
        env.pop(GATE_ENV, None)
    proc = subprocess.Popen(
        [str(binary), "--port", str(port), "--dbpath", str(dbpath),
         "--bind_ip", "127.0.0.1", "--wiredTigerCacheSizeGB", str(cache_gb),
         "--logpath", str(logpath),
         "--setParameter", "diagnosticDataCollectionEnabled=false"],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env,
    )
    uri = f"mongodb://localhost:{port}/?directConnection=true"
    for _ in range(240):
        try:
            MongoClient(uri, serverSelectionTimeoutMS=500).admin.command("ping")
            log(f"  mongod up on {port} (pid {proc.pid}, {GATE_ENV}={gate})")
            return proc
        except Exception:
            if proc.poll() is not None:
                raise SystemExit(f"mongod on {port} exited early; see {logpath}")
            time.sleep(0.5)
    raise SystemExit(f"mongod on {port} did not become ready")


def stop_mongod(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=180)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=60)


# ------------------------------------------------------------------ dataset

def populate(dst_db, doc_limit: int) -> dict[str, Any]:
    """Copy a prefix of the real tree, then build the same indexes as layout2_view."""
    src_db = MongoClient(SOURCE_URI, maxPoolSize=1)[DB_NAME]
    src_nodes, dst_nodes = src_db[NODES], dst_db[NODES]
    dst_nodes.drop()

    copied, batch = 0, []
    for doc in src_nodes.find({"tree_id": TREE_ID}).limit(doc_limit):
        batch.append(doc)
        if len(batch) >= 5000:
            dst_nodes.insert_many(batch, ordered=False)
            copied += len(batch)
            batch = []
    if batch:
        dst_nodes.insert_many(batch, ordered=False)
        copied += len(batch)
    log(f"  nodes copied: {copied}")

    dst_nodes.create_index([("tree_id", 1), ("node_id", 1)], name=NODE_INDEX, unique=True)
    dst_nodes.create_index([("path", 1), ("node_id", 1)], name="path_1_node_id_1")
    dst_nodes.create_index([("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)],
                           name=COVER_INDEX)
    dst_nodes.create_index([("tree_id", 1), ("parent_id", 1), ("path", 1), ("node_id", 1)],
                           name=CHILD_INDEX)
    log("  indexes built")
    return {"nodes": copied, "indexes": [ix["name"] for ix in dst_nodes.list_indexes()]}


def choose_cohort(dst_db, wanted: int, lo: int, hi: int) -> dict[str, Any]:
    """Pick parents whose fan-out in the clone equals their fan-out in the real data.

    A parent whose children were truncated by the copy limit would have a
    smaller fan-out here than on the real server, which would quietly change
    the shape being measured.  Those are dropped rather than corrected.
    """
    src_nodes = MongoClient(SOURCE_URI, maxPoolSize=1)[DB_NAME][NODES]
    dst_nodes = dst_db[NODES]
    candidates = list(dst_nodes.aggregate([
        {"$match": {"tree_id": TREE_ID, "parent_id": {"$ne": None}}},
        {"$group": {"_id": "$parent_id", "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": lo, "$lte": hi}}},
        {"$sort": {"_id": 1}},
        {"$limit": wanted * 4},
    ], allowDiskUse=True))

    cohort, fanouts, dropped = [], [], 0
    for row in candidates:
        if len(cohort) >= wanted:
            break
        parent = row["_id"]
        real = src_nodes.count_documents({"tree_id": TREE_ID, "parent_id": parent})
        if real != row["n"]:
            dropped += 1
            continue
        cohort.append(parent)
        fanouts.append(row["n"])

    if len(cohort) < wanted:
        raise SystemExit(f"only {len(cohort)} verified parents, wanted {wanted}")
    log(f"  cohort: {len(cohort)} parents, fan-out {min(fanouts)}-{max(fanouts)}, "
        f"median {statistics.median(fanouts)}, {dropped} dropped as truncated")
    return {"parents": cohort, "fanouts": fanouts, "dropped_truncated": dropped}


# --------------------------------------------------------------------- arms

def build_arm(port: int) -> dict[str, Any]:
    uri = f"mongodb://localhost:{port}/?directConnection=true"
    client = MongoClient(uri, maxPoolSize=1, minPoolSize=1)
    db = client[DB_NAME]
    connection_id = db.command("hello")["connectionId"]
    nodes = db[NODES]

    def reader(parent: str) -> list[tuple]:
        cursor = (nodes.find({"tree_id": TREE_ID, "parent_id": parent}, CHILD_PROJECTION)
                  .sort(CHILD_SORT).hint(CHILD_INDEX))
        return [(d.get("node_id"), d.get("title"), d.get("summary")) for d in cursor]

    return {"client": client, "db": db, "reader": reader,
            "comm": f"conn{connection_id}", "connection_id": connection_id, "port": port}


def plan_cache_counters(db) -> dict[str, int]:
    try:
        classic = db.command("serverStatus")["metrics"]["query"]["planCache"]["classic"]
        return {k: int(classic.get(k, 0)) for k in ("hits", "misses", "skipped")}
    except Exception:
        return {}


def verify_equality(arms: dict[str, dict[str, Any]], parents: list[str]) -> int:
    elements = 0
    others = [n for n in arms if n != "baseline"]
    for parent in parents:
        expected = arms["baseline"]["reader"](parent)
        if not expected:
            raise SystemExit(f"baseline returned no children for parent {parent!r}")
        for name in others:
            got = arms[name]["reader"](parent)
            if got != expected:
                raise SystemExit(
                    f"OUTPUT MISMATCH arm {name} parent {parent!r}:\n"
                    f"  got      {got!r}\n  expected {expected!r}")
            elements += len(got)
    return elements


def measure_arm(pid: int, comm: str, reader: Callable[[str], Any],
                parents: list[str], sweeps: int) -> dict[str, float]:
    before = thread_nanos(pid, comm)
    t0 = time.perf_counter()
    rows = 0
    for _ in range(sweeps):
        for parent in parents:
            rows += len(reader(parent))
    wall = time.perf_counter() - t0
    cpu_ns = thread_nanos(pid, comm) - before
    ops = sweeps * len(parents)
    return {"server_cpu_us": cpu_ns / ops / 1000.0,
            "client_wall_us": wall / ops * 1e6,
            "ops": ops, "rows": rows}


# --------------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--scratch", default="/tmp/mongo-getchildren-bench")
    parser.add_argument("--doc-limit", type=int, default=1_500_000)
    parser.add_argument("--cohort", type=int, default=64)
    parser.add_argument("--fanout-lo", type=int, default=6)
    parser.add_argument("--fanout-hi", type=int, default=14)
    parser.add_argument("--blocks", type=int, default=14)
    parser.add_argument("--sweeps", type=int, default=25)
    parser.add_argument("--warmup-sweeps", type=int, default=6)
    parser.add_argument("--cache-gb", type=int, default=8)
    parser.add_argument("--out", required=True)
    parser.add_argument("--reuse-data", action="store_true",
                        help="skip populate; reuse the prepared dbpath copies")
    parser.add_argument("--gate-env", default=DEFAULT_GATE_ENV,
                        help="startup environment variable that switches the change under test")
    parser.add_argument("--port-rotation", type=int, default=0, choices=(0, 1, 2),
                        help="rotate which physical port and dbpath each arm runs on, to "
                             "separate a server-specific bias from a real effect")
    args = parser.parse_args()

    global GATE_ENV
    GATE_ENV = args.gate_env

    binary = Path(args.binary).resolve()
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    base_dbpath = scratch / "dbpath_base"

    out: dict[str, Any] = {
        "run": {"generated_unix_s": time.time(), "binary": str(binary),
                "doc_limit": args.doc_limit, "blocks": args.blocks,
                "sweeps_per_block": args.sweeps, "cache_gb": args.cache_gb,
                "gate_env": args.gate_env,
                "port_rotation": args.port_rotation,
                "arm_ports": arm_ports(args.port_rotation)},
        "units": {"server_cpu_us": "mongod CPU on the arm's own connection thread "
                                   "(schedstat ns / ops), microseconds",
                  "client_wall_us": "client wall clock per op, microseconds",
                  "note": "server CPU and client wall are separate quantities and are "
                          "never combined"},
    }

    procs: dict[str, subprocess.Popen] = {}
    try:
        if not args.reuse_data:
            log("preparing dataset (one server, then copied byte-for-byte per arm)")
            if base_dbpath.exists():
                shutil.rmtree(base_dbpath)
            prep = start_mongod(binary, base_dbpath, scratch / "prepare.log",
                                57020, args.cache_gb, gate=None)
            try:
                prep_db = MongoClient("mongodb://localhost:57020/?directConnection=true",
                                      maxPoolSize=1)[DB_NAME]
                out["dataset"] = populate(prep_db, args.doc_limit)
                out["cohort"] = choose_cohort(prep_db, args.cohort,
                                              args.fanout_lo, args.fanout_hi)
                prep_db.client.admin.command("fsync")
            finally:
                stop_mongod(prep)
            log("  base server stopped; copying dbpath per arm")
            for port in PORTS:
                target = scratch / f"dbpath_{port}"
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(base_dbpath, target)
            (scratch / "cohort.json").write_text(json.dumps(out["cohort"]))
        else:
            out["cohort"] = json.loads((scratch / "cohort.json").read_text())
            out["dataset"] = {"note": "reused from a previous prepare"}

        parents = out["cohort"]["parents"]

        ports = arm_ports(args.port_rotation)
        log(f"starting one server per arm from the same binary "
            f"(port rotation {args.port_rotation}: {ports})")
        for name in ARM_NAMES:
            port = ports[name]
            procs[name] = start_mongod(binary, scratch / f"dbpath_{port}",
                                       scratch / f"{name}.log", port, args.cache_gb,
                                       ARM_GATES[name])

        arms = {name: build_arm(ports[name]) for name in ARM_NAMES}
        for name, arm in arms.items():
            log(f"  arm {name:<9} port {arm['port']} connectionId {arm['connection_id']}")

        out["plan_cache_before"] = {n: plan_cache_counters(a["db"]) for n, a in arms.items()}

        log(f"verifying element-wise output equality over {len(parents)} inputs")
        elements = verify_equality(arms, parents)
        log(f"  {elements} elements compared, all equal")

        log(f"warming up ({args.warmup_sweeps} sweeps per arm)")
        for name, arm in arms.items():
            measure_arm(procs[name].pid, arm["comm"], arm["reader"],
                        parents, args.warmup_sweeps)

        order = list(ARM_NAMES)
        blocks: list[dict[str, Any]] = []
        log(f"measuring {args.blocks} blocks x {args.sweeps} sweeps x "
            f"{len(parents)} inputs per arm")
        for block in range(args.blocks):
            rotated = order[block % len(order):] + order[:block % len(order)]
            block_elements = verify_equality(arms, parents)
            row: dict[str, Any] = {"block": block, "order": rotated,
                                   "elements_compared": block_elements}
            for name in rotated:
                row[name] = measure_arm(procs[name].pid, arms[name]["comm"],
                                        arms[name]["reader"], parents, args.sweeps)
            base_cpu = row["baseline"]["server_cpu_us"]
            for name in order:
                if name == "baseline":
                    continue
                row[f"{name}_vs_baseline_pct"] = (
                    100.0 * (row[name]["server_cpu_us"] - base_cpu) / base_cpu)
            blocks.append(row)
            log("  block %2d  baseline %7.2f  probe %7.2f (%+6.2f%%)  "
                "control %7.2f (%+6.2f%%)"
                % (block, base_cpu, row["probe"]["server_cpu_us"],
                   row["probe_vs_baseline_pct"], row["control"]["server_cpu_us"],
                   row["control_vs_baseline_pct"]))

        out["blocks"] = blocks
        out["plan_cache_after"] = {n: plan_cache_counters(a["db"]) for n, a in arms.items()}
        out["plan_cache_delta"] = {
            n: {k: out["plan_cache_after"][n].get(k, 0) - out["plan_cache_before"][n].get(k, 0)
                for k in out["plan_cache_after"].get(n, {})}
            for n in arms}

        summary: dict[str, Any] = {}
        for name in order:
            if name == "baseline":
                continue
            deltas = [b[f"{name}_vs_baseline_pct"] for b in blocks]
            summary[name] = {
                "median_paired_delta_pct": statistics.median(deltas),
                "block_min_pct": min(deltas),
                "block_max_pct": max(deltas),
                "blocks_improved": sum(1 for d in deltas if d < 0),
                "blocks": len(deltas),
            }
        out["summary"] = summary

        log("")
        log("%-10s %22s %10s %10s %14s" % ("arm", "median paired delta", "block min",
                                           "block max", "blocks better"))
        for name, s in summary.items():
            log("%-10s %21.2f%% %9.2f%% %9.2f%% %10d/%d"
                % (name, s["median_paired_delta_pct"], s["block_min_pct"],
                   s["block_max_pct"], s["blocks_improved"], s["blocks"]))
        log("")
        log(f"plan cache counter deltas: {json.dumps(out['plan_cache_delta'])}")

        out["run"]["status"] = "complete"
    finally:
        for proc in procs.values():
            stop_mongod(proc)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))
        log(f"written to {args.out}")


if __name__ == "__main__":
    main()
