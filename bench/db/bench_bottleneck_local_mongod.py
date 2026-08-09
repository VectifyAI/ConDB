#!/usr/bin/env python3
"""Stand up a local mongod that perf can actually see inside, and prove it matches.

perf cannot resolve user-space symbols for the mongod in the condb_mongo
container: that process runs as uid 999, this account is uid 1014, and
/proc/<pid>/maps is therefore unreadable, so every user-space frame comes back
as [unknown].  Kernel frames resolve, user frames do not.

The fix used here is to run the *same binary* -- the 7.0.34 mongod copied out of
the container, build id 58bbaf4f510a840f -- as this account's own uid, against a
clone of the data.  perf can then read its maps and resolve every frame.

That is only worth anything if the clone reproduces the thing being measured, so
this script ends by running the same arms as bench_bottleneck_cpu.py against
both servers and printing the difference.  Any perf attribution taken from the
local server must be read together with that comparison table: where the clone
does not reproduce the real server, the attribution does not transfer.

The clone deliberately keeps the parts that drive cost:
  * the same five indexes on the nodes collection, in the same order
  * the whole target subtree, so the covered range scan walks the same number
    of index keys with the same key contents
  * the same document shape and field widths
It does not reproduce total collection size, which changes B-tree descent depth
by a couple of levels and nothing else on these access paths.
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
from typing import Any

from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_bottleneck_cpu import (  # noqa: E402
    MONGO_URI, MONGO_DB, NODES, TEXT, NODE_INDEX, CHILD_INDEX, COVER_INDEX,
    TREE_ID, NODE_PROJECTION, CHILD_PROJECTION, thread_snapshot, thread_delta,
    CLK_TCK,
)

LOCAL_PORT = 57018
LOCAL_URI = f"mongodb://localhost:{LOCAL_PORT}/?directConnection=true"
FILLER_DOCS = 200_000


def log(m: str) -> None:
    print(m, flush=True)


def start_mongod(binary: Path, dbpath: Path, logpath: Path, cache_gb: int) -> subprocess.Popen:
    dbpath.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(binary), "--port", str(LOCAL_PORT), "--dbpath", str(dbpath),
         "--bind_ip", "127.0.0.1", "--wiredTigerCacheSizeGB", str(cache_gb),
         "--logpath", str(logpath), "--nojournal" if False else "--setParameter",
         "diagnosticDataCollectionEnabled=false"],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    for _ in range(120):
        try:
            MongoClient(LOCAL_URI, serverSelectionTimeoutMS=500).admin.command("ping")
            log(f"local mongod up on {LOCAL_PORT}, pid {proc.pid}")
            return proc
        except Exception:
            if proc.poll() is not None:
                raise SystemExit(f"mongod exited early; see {logpath}")
            time.sleep(0.5)
    raise SystemExit("mongod did not become ready")


def populate(src_db, dst_db, ids: dict[str, Any]) -> dict[str, Any]:
    lower, upper = ids["subtree_lower"], ids["subtree_upper"]
    src_nodes, dst_nodes = src_db[NODES], dst_db[NODES]
    src_text, dst_text = src_db[TEXT], dst_db[TEXT]
    dst_nodes.drop()
    dst_text.drop()

    copied = 0
    batch: list[dict] = []

    def flush():
        nonlocal batch, copied
        if batch:
            dst_nodes.insert_many(batch, ordered=False)
            copied += len(batch)
            batch = []

    # the whole target subtree, plus its root, plus filler for realistic depth
    for d in src_nodes.find({"path": {"$gte": lower, "$lt": upper}}):
        batch.append(d)
        if len(batch) >= 5000:
            flush()
    flush()
    subtree_docs = copied
    root = src_nodes.find_one({"tree_id": TREE_ID, "node_id": ids["node_id"]})
    if root:
        dst_nodes.insert_one(root)
        copied += 1

    have = {d["_id"] for d in dst_nodes.find({}, {"_id": 1})}
    for d in src_nodes.find({"tree_id": TREE_ID}).limit(FILLER_DOCS):
        if d["_id"] in have:
            continue
        batch.append(d)
        if len(batch) >= 5000:
            flush()
    flush()
    log(f"  nodes copied: {copied} (subtree {subtree_docs})")

    # the same five indexes, same definitions as layout2_view
    dst_nodes.create_index([("tree_id", 1), ("node_id", 1)], name=NODE_INDEX, unique=True)
    dst_nodes.create_index([("path", 1), ("node_id", 1)], name="path_1_node_id_1")
    dst_nodes.create_index([("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)],
                           name=COVER_INDEX)
    dst_nodes.create_index([("tree_id", 1), ("parent_id", 1), ("path", 1), ("node_id", 1)],
                           name=CHILD_INDEX)

    tbatch = []
    tcopied = 0
    target = src_text.find_one({"_id": ids["entity_id"]})
    if target:
        dst_text.insert_one(target)
        tcopied += 1
    for d in src_text.find({}).limit(FILLER_DOCS):
        if d["_id"] == ids["entity_id"]:
            continue
        tbatch.append(d)
        if len(tbatch) >= 2000:
            dst_text.insert_many(tbatch, ordered=False)
            tcopied += len(tbatch)
            tbatch = []
    if tbatch:
        dst_text.insert_many(tbatch, ordered=False)
        tcopied += len(tbatch)
    log(f"  text copied: {tcopied}")

    return {
        "nodes": dst_nodes.count_documents({}),
        "subtree_rows": subtree_docs,
        "text": dst_text.count_documents({}),
        "node_indexes": [ix["name"] for ix in dst_nodes.list_indexes()],
    }


def arms_for(db, ids):
    nodes, text = db[NODES], db[TEXT]
    node, parent = ids["node_id"], ids["parent_id"]
    entity = ids["entity_id"]
    lower, upper = ids["subtree_lower"], ids["subtree_upper"]
    return {
        "ping": lambda: db.client["admin"].command("ping"),
        "get_node_hit": lambda: nodes.find_one(
            {"tree_id": TREE_ID, "node_id": node}, NODE_PROJECTION, hint=NODE_INDEX),
        "get_node_miss": lambda: nodes.find_one(
            {"tree_id": TREE_ID, "node_id": "zzzz-no-such-node"}, NODE_PROJECTION, hint=NODE_INDEX),
        "get_node_miss_noproj": lambda: nodes.find_one(
            {"tree_id": TREE_ID, "node_id": "zzzz-no-such-node"}, None, hint=NODE_INDEX),
        "get_children_hit": lambda: sum(1 for _ in nodes.find(
            {"tree_id": TREE_ID, "parent_id": parent}, CHILD_PROJECTION)
            .sort([("path", 1), ("node_id", 1)]).hint(CHILD_INDEX)),
        "get_entity_hit": lambda: text.find_one({"_id": entity}, {"_id": 1, "text": 1}),
        "get_subtree_scan": lambda: sum(1 for _ in nodes.find(
            {"path": {"$gte": lower, "$lt": upper}}, CHILD_PROJECTION)
            .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX)),
    }


def measure(pid: int, fn, iterations: int) -> dict[str, Any]:
    fn()
    before = thread_snapshot(pid)
    t0 = time.perf_counter()
    result = None
    for _ in range(iterations):
        result = fn()
    wall = time.perf_counter() - t0
    delta = thread_delta(before, thread_snapshot(pid))
    return {
        "cpu_us_per_op": round(delta["conn_sched_ns"] / iterations / 1000, 3),
        "wall_us_per_op": round(wall / iterations * 1e6, 3),
        "rows": result if isinstance(result, int) else (1 if result else 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default="bench/db/runs/bottleneck_20260806/bin/mongod")
    parser.add_argument("--dbpath", default=None)
    parser.add_argument("--cache-gb", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=8000)
    parser.add_argument("--subtree-iterations", type=int, default=120)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--inputs", default="bench/db/runs/bottleneck_20260806/mongo_cpu_arms.json")
    parser.add_argument("--out", default="bench/db/runs/bottleneck_20260806/local_vs_real.json")
    parser.add_argument("--keep-running", action="store_true",
                        help="leave the local mongod up for perf capture")
    parser.add_argument("--skip-populate", action="store_true")
    args = parser.parse_args()

    scratch = Path(os.environ.get("BOTTLENECK_SCRATCH", "/tmp/claude-1014/"
                   "-home-junyao-code-pageindex-ConDB/842657a9-47f6-4a35-8fbc-01be30deb4bd/"
                   "scratchpad"))
    dbpath = Path(args.dbpath) if args.dbpath else scratch / "local_mongod_db"
    logpath = scratch / "local_mongod.log"

    ids = json.loads(Path(args.inputs).read_text())["inputs"]
    proc = start_mongod(Path(args.binary).resolve(), dbpath, logpath, args.cache_gb)
    pidfile = scratch / "local_mongod.pid"
    pidfile.write_text(str(proc.pid))

    try:
        src = MongoClient(MONGO_URI, maxPoolSize=1)[MONGO_DB]
        dst_client = MongoClient(LOCAL_URI, maxPoolSize=1)
        dst = dst_client[MONGO_DB]

        out: dict[str, Any] = {
            "run": {"generated_unix_s": time.time(), "local_port": LOCAL_PORT,
                    "local_pid": proc.pid, "binary": str(Path(args.binary).resolve()),
                    "cache_gb": args.cache_gb, "iterations": args.iterations},
            "inputs": ids,
            "why": "perf cannot resolve user-space symbols in the containerised mongod "
                   "(uid 999 vs this account's 1014, /proc/<pid>/maps denied). This local "
                   "server runs the same binary as this account's own uid so perf can. "
                   "The comparison table below is what licenses transferring any perf "
                   "attribution from here back to the real server.",
        }

        if not args.skip_populate:
            log("populating clone")
            out["clone"] = populate(src, dst, ids)
            log(json.dumps(out["clone"], indent=2, default=str))

        real_pid = int(subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Pid}}", "condb_mongo"],
            capture_output=True, text=True, check=True).stdout.strip())

        log("\ncomparing arms: real 10M server vs local clone")
        real_arms = arms_for(MongoClient(MONGO_URI, maxPoolSize=1)[MONGO_DB], ids)
        local_arms = arms_for(dst, ids)
        out["comparison"] = {}
        log("%-24s %12s %12s %10s %10s" % ("arm", "real_cpu_us", "local_cpu_us", "ratio", "rows"))
        for name in real_arms:
            iters = args.subtree_iterations if "subtree" in name else args.iterations
            r = statistics.median(measure(real_pid, real_arms[name], iters)["cpu_us_per_op"]
                                  for _ in range(args.repeats))
            lm = [measure(proc.pid, local_arms[name], iters) for _ in range(args.repeats)]
            l = statistics.median(x["cpu_us_per_op"] for x in lm)
            out["comparison"][name] = {
                "real_cpu_us_per_op": round(r, 3),
                "local_cpu_us_per_op": round(l, 3),
                "local_over_real": round(l / r, 4) if r else None,
                "local_rows": lm[0]["rows"],
            }
            log("%-24s %12.3f %12.3f %10.3f %10s"
                % (name, r, l, l / r if r else 0, lm[0]["rows"]))

        out["run"]["status"] = "complete"
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))
        log(f"\nwritten to {args.out}")

        if args.keep_running:
            log(f"leaving local mongod running (pid {proc.pid}, port {LOCAL_PORT}); "
                f"pid file {pidfile}")
            return
    finally:
        if not args.keep_running:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=120)
            log("local mongod stopped")


if __name__ == "__main__":
    main()
