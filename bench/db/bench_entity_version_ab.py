"""Server CPU per get_entity operation, two mongod versions, same box, paired.

The 45.354 us figure this project quotes for get_entity was measured on the
containerised 7.0.34 that holds the shared dataset. Comparing a locally built
master against it directly would compare two different transports as well as
two versions -- the container arm crosses a published docker port and this
host's endpoint-protection modules, which the origin split shows are 3.99 us of
the operation. So both arms here are host processes on loopback, serving the
same deterministically generated collection, and are alternated within blocks
so the comparison is paired against whatever else the box is doing.

The unit is the same one bench_bottleneck_cpu.py uses: CPU nanoseconds burned
by mongod's ``conn*`` threads over an arm, from
/proc/<pid>/task/<tid>/schedstat, divided by the operations in that arm, with a
same-length idle baseline subtracted. Client-side cost is outside the counter
and is not mixed into it.

Read-only against the servers under test; it never touches port 57017.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_bottleneck_cpu import thread_snapshot  # noqa: E402

from pymongo import MongoClient  # noqa: E402

PROJ = {"_id": 1, "text": 1}


def conn_cpu_ns(before: dict, after: dict) -> int:
    """CPU nanoseconds burned by connection threads between two snapshots."""
    total = 0
    for tid, post in after.items():
        if not post["comm"].startswith("conn"):
            continue
        pre = before.get(tid)
        pre_ns = pre["sched_ns"] if pre and pre["sched_ns"] is not None else 0
        if post["sched_ns"] is not None:
            total += post["sched_ns"] - pre_ns
    return total


class Arm:
    def __init__(self, label: str, uri: str, pid: int, db: str, coll: str):
        self.label = label
        self.pid = pid
        self.client = MongoClient(uri)
        self.coll = self.client[db][coll]
        self.version = self.client.server_info()["version"]

    def plan(self, doc_id: str) -> str:
        winning = self.coll.find(
            {"_id": doc_id}, PROJ
        ).explain()["queryPlanner"]["winningPlan"]
        return json.dumps(winning)

    def run(self, ids: list[str]) -> tuple[float, float, int]:
        """Returns (server CPU us/op, wall us/op, documents seen)."""
        coll = self.coll
        before = thread_snapshot(self.pid)
        t0 = time.perf_counter()
        seen = 0
        for doc_id in ids:
            if coll.find_one({"_id": doc_id}, PROJ) is not None:
                seen += 1
        wall = (time.perf_counter() - t0) / len(ids) * 1e6
        after = thread_snapshot(self.pid)
        return conn_cpu_ns(before, after) / len(ids) / 1000.0, wall, seen

    def idle(self, seconds: float) -> float:
        """Connection-thread CPU per second while nothing is asked of it."""
        before = thread_snapshot(self.pid)
        time.sleep(seconds)
        return conn_cpu_ns(before, thread_snapshot(self.pid)) / seconds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-label", required=True)
    ap.add_argument("--a-uri", required=True)
    ap.add_argument("--a-pid", type=int, required=True)
    ap.add_argument("--b-label", required=True)
    ap.add_argument("--b-uri", required=True)
    ap.add_argument("--b-pid", type=int, required=True)
    ap.add_argument("--db", default="bench")
    ap.add_argument("--coll", default="layout_shared_text")
    ap.add_argument("--lo", type=int, default=1_000_000)
    ap.add_argument("--hi", type=int, default=10_000_000)
    ap.add_argument("--blocks", type=int, default=12)
    ap.add_argument("--iters", type=int, default=2000)
    ap.add_argument("--workset", type=int, default=40000,
                    help="ids drawn once and warmed on both arms before timing")
    ap.add_argument("--seed", type=int, default=20260809)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    for uri in (args.a_uri, args.b_uri):
        if ":57017" in uri:
            raise SystemExit("refusing to measure port 57017: shared dataset")

    arms = [
        Arm(args.a_label, args.a_uri, args.a_pid, args.db, args.coll),
        Arm(args.b_label, args.b_uri, args.b_pid, args.db, args.coll),
    ]
    for a in arms:
        n = a.coll.estimated_document_count()
        print(f"  {a.label:10} mongod {a.version:10} {n:,} documents  pid {a.pid}")
        if n != args.hi - args.lo:
            raise SystemExit(f"{a.label} has {n} documents, not {args.hi - args.lo}; "
                             "the two arms would not be serving the same collection")

    probe = str(args.lo)
    plans = {a.label: a.plan(probe) for a in arms}
    for label, plan in plans.items():
        print(f"  {label:10} winning plan: {plan[:150]}")

    # A fixed working set, warmed on both arms before anything is timed.
    #
    # Drawing fresh random ids per block does not work here: 9M documents of
    # ~1 KB do not fit uncompressed in an 8 GB WiredTiger cache, so the first
    # arm to touch a block's ids pays the read and warms them for the second.
    # A null test with both arms on the same server made that visible -- the
    # arm that ran first read 90-117 us and the one that ran second read 44,
    # flipping with the block parity. Reusing a warmed working set measures the
    # cached point lookup, which is the "hit" arm the 45.354 us figure names.
    rng = random.Random(args.seed)
    workset = [str(rng.randrange(args.lo, args.hi)) for _ in range(args.workset)]
    for _ in range(2):
        for a in arms:
            a.run(workset)

    idle = {a.label: a.idle(1.0) for a in arms}
    for label, ns in idle.items():
        print(f"  {label:10} idle conn-thread CPU: {ns / 1000:.1f} us/s")

    cpu: dict[str, list[float]] = {a.label: [] for a in arms}
    wall: dict[str, list[float]] = {a.label: [] for a in arms}
    for b in range(args.blocks):
        block_ids = [workset[rng.randrange(len(workset))] for _ in range(args.iters)]
        order = arms if b % 2 == 0 else arms[::-1]
        for a in order:
            c, w, seen = a.run(block_ids)
            if seen != len(block_ids):
                raise SystemExit(f"{a.label} returned {seen} of {len(block_ids)} "
                                 "documents; the arms are not doing the same work")
            # Subtract the idle draw over the same wall time.
            c -= idle[a.label] * (w / 1e6) / 1000.0
            cpu[a.label].append(c)
            wall[a.label].append(w)
        la, lb = arms[0].label, arms[1].label
        print(f"  block {b + 1:2d}: {la} {cpu[la][-1]:6.2f} us cpu  "
              f"{lb} {cpu[lb][-1]:6.2f}  "
              f"delta {cpu[la][-1] - cpu[lb][-1]:+6.2f}  "
              f"(wall {wall[la][-1]:6.1f} vs {wall[lb][-1]:6.1f})")

    la, lb = arms[0].label, arms[1].label
    deltas = [cpu[la][i] - cpu[lb][i] for i in range(args.blocks)]
    result = {
        "blocks": args.blocks, "iters_per_block": args.iters,
        "workset": args.workset,
        "collection": args.coll, "documents": args.hi - args.lo,
        "versions": {a.label: a.version for a in arms},
        "winning_plans": plans,
        "idle_conn_cpu_ns_per_s": idle,
        "server_cpu_us_per_op": {k: v for k, v in cpu.items()},
        "wall_us_per_op": {k: v for k, v in wall.items()},
        "paired_delta_us": {
            "median": st.median(deltas), "min": min(deltas), "max": max(deltas),
            "n_b_faster": sum(1 for d in deltas if d > 0), "n": len(deltas),
        },
    }
    print(f"\n{'arm':<12}{'server CPU us':>15}{'wall us':>12}")
    for a in arms:
        print(f"{a.label:<12}{st.median(cpu[a.label]):>15.2f}"
              f"{st.median(wall[a.label]):>12.1f}")
    d = result["paired_delta_us"]
    base = st.median(cpu[la])
    print(f"\npaired {la} - {lb}: {d['median']:+.2f} us "
          f"({d['median'] / base * 100:+.1f}% of {la})  "
          f"[{d['min']:+.2f}, {d['max']:+.2f}]  {d['n_b_faster']}/{d['n']} blocks")

    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
