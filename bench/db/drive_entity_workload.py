"""Drive the get_entity shape at one server for a fixed duration, for profiling.

Runs find_one on _id over a warmed working set on a single connection, so the
server has exactly one ``conn`` thread to attach perf to, and holds that
connection open for the whole run. Prints the server-side thread id of that
connection before starting the timed phase, so the caller can point
``perf record --tid`` at it rather than capturing the whole process.

Read-only; refuses port 57017.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

from pymongo import MongoClient

PROJ = {"_id": 1, "text": 1}


def conn_tids(pid: int) -> set[int]:
    out = set()
    task = Path(f"/proc/{pid}/task")
    for entry in task.iterdir():
        try:
            if entry.joinpath("comm").read_text().strip().startswith("conn"):
                out.add(int(entry.name))
        except (OSError, ValueError):
            continue
    return out


def sched_ns(pid: int, tid: int) -> int:
    try:
        return int(Path(f"/proc/{pid}/task/{tid}/schedstat").read_text().split()[0])
    except (OSError, IndexError, ValueError):
        return 0


def busiest_conn(pid, candidates, coll, workset, rng):
    """The connection thread that burns CPU while queries are running."""
    before = {t: sched_ns(pid, t) for t in candidates}
    n = len(workset)
    for _ in range(3000):
        coll.find_one({"_id": workset[rng.randrange(n)]}, PROJ)
    deltas = {t: sched_ns(pid, t) - before[t] for t in candidates}
    ranked = sorted(deltas.items(), key=lambda kv: -kv[1])
    if len(ranked) > 1 and ranked[0][1] < ranked[1][1] * 4:
        raise SystemExit(
            f"no clearly busiest conn thread: {ranked}; something else is "
            "talking to this server"
        )
    return ranked[0][0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", required=True)
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--db", default="bench")
    ap.add_argument("--coll", default="layout_shared_text")
    ap.add_argument("--lo", type=int, default=1_000_000)
    ap.add_argument("--hi", type=int, default=10_000_000)
    ap.add_argument("--workset", type=int, default=40000)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--seed", type=int, default=20260809)
    ap.add_argument("--tid-file", help="write the connection's server thread id here")
    args = ap.parse_args()

    if ":57017" in args.uri:
        raise SystemExit("refusing to drive port 57017: shared dataset")

    before = conn_tids(args.pid)
    # maxPoolSize 1 so the whole run uses one server-side connection thread.
    client = MongoClient(args.uri, maxPoolSize=1)
    coll = client[args.db][args.coll]
    rng = random.Random(args.seed)
    workset = [str(rng.randrange(args.lo, args.hi)) for _ in range(args.workset)]

    for doc_id in workset:  # open the connection and warm the working set
        coll.find_one({"_id": doc_id}, PROJ)
    for doc_id in workset:
        coll.find_one({"_id": doc_id}, PROJ)

    # The client opens more than one server-side connection: the topology
    # monitor keeps its own, and so does the RTT sampler. Identify the one
    # serving queries by which conn thread actually burns CPU while the
    # workload runs, rather than by assuming there is only one.
    mine = conn_tids(args.pid) - before
    if not mine:
        raise SystemExit("no new conn thread appeared on the server")
    tid = busiest_conn(args.pid, mine, coll, workset, rng)
    if args.tid_file:
        Path(args.tid_file).write_text(str(tid))
    print(f"conn tid {tid}", flush=True)

    ops = 0
    deadline = time.perf_counter() + args.seconds
    n = len(workset)
    while time.perf_counter() < deadline:
        for _ in range(500):
            coll.find_one({"_id": workset[rng.randrange(n)]}, PROJ)
        ops += 500
    print(f"{ops} operations in {args.seconds}s", flush=True)


if __name__ == "__main__":
    main()
