"""What a hinted point query pays for re-planning, in server instructions.

`shouldCacheQuery` returns false whenever the find command carries a hint
(classic_plan_cache.cpp), on the grounds that a hinted query is not
multi-planned and so has nothing to cache.  That reasoning covers only half of
what the cache does: a cache hit also skips `QueryPlanner::plan()` itself --
index entry fill-out, enumeration, tagging, solution building and
`QueryPlannerAnalysis::analyzeDataAccess`.  A hinted query re-runs all of it on
every call.

This measures the size of that.  Three arms, one mongod, one binary, one
collection, one predicate:

    hinted    find(q, proj).hint(IDX)   -> shouldCacheQuery false, replans
    cached    find(q, proj), express off -> planner + plan cache hit
    express   find(q, proj), express on  -> EXPRESS_IXSCAN, no planner at all

`hinted` and `cached` execute the identical plan -- the hint names the index the
unhinted planner picks on its own -- and the script refuses to report unless
`explain` confirms it, so their difference is planning and nothing else.
`express` is the same predicate answered by the fast path, which is where a
hinted query could land if a hint naming the index express would use no longer
disqualified it.

The express arms are switched with `internalQueryDisableCompoundFieldExpressExecutor`
on the running server, so all three arms come from one binary and code layout is
held fixed.  Build-to-build layout noise on this workload measures 13%, larger
than anything worth reporting, which is why nothing here compares two binaries.

The metric is retired user-space instructions on the mongod process, not wall
time: three sibling agents build on this box and cycles swing with them, while
retired instructions do not.  Arms are interleaved block by block so any drift
in the box hits both equally, and the interval is a paired bootstrap over the
per-block differences.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import statistics
import string
import subprocess
import sys
import time
from pathlib import Path

import pymongo

MONGOD = "/home/junyao/code/mongo/bazel-bin/src/mongo/db/mongod"
PORT = 57030
DBPATH = Path("/tmp/gn-hintcache/db")
LOGPATH = Path("/tmp/gn-hintcache/mongod.log")

DB = "condb_probe"
COLL = "nodes"
IDX = "probe_tree_node"
TREE_ID = "t1"
NDOCS = 200_000
BLOB_BYTES = 400

PROJECTION = {
    "_id": 0,
    "node_id": 1,
    "title": 1,
    "path": 1,
    "parent_id": 1,
    "depth": 1,
    "kind": 1,
    "updated_at": 1,
}


def start_mongod() -> subprocess.Popen:
    if DBPATH.exists():
        shutil.rmtree(DBPATH.parent, ignore_errors=True)
    DBPATH.mkdir(parents=True)
    proc = subprocess.Popen(
        [
            MONGOD,
            "--port", str(PORT),
            "--dbpath", str(DBPATH),
            "--bind_ip", "127.0.0.1",
            "--wiredTigerCacheSizeGB", "4",
            "--logpath", str(LOGPATH),
            "--setParameter", "diagnosticDataCollectionEnabled=false",
            # Nothing should be logged during the measured window: logging every
            # operation is worth ~38-40% of server CPU on reads this short.
            "--slowms", "10000",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(120):
        try:
            pymongo.MongoClient(f"mongodb://127.0.0.1:{PORT}", serverSelectionTimeoutMS=500).admin.command("ping")
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError(f"mongod did not come up; see {LOGPATH}")


def load(client: pymongo.MongoClient) -> None:
    coll = client[DB][COLL]
    coll.drop()
    rng = random.Random(20260810)
    alpha = string.ascii_lowercase
    batch = []
    for i in range(NDOCS):
        batch.append(
            {
                "tree_id": TREE_ID,
                "node_id": f"n{i:07d}",
                "parent_id": f"n{i // 8:07d}",
                "path": f"/{i // 8:07d}/{i:07d}",
                "title": f"node {i}",
                "depth": 1 + (i % 6),
                "kind": "section" if i % 3 else "leaf",
                "updated_at": 1_700_000_000 + i,
                "blob": "".join(rng.choice(alpha) for _ in range(BLOB_BYTES)),
            }
        )
        if len(batch) == 5000:
            coll.insert_many(batch, ordered=False)
            batch.clear()
    if batch:
        coll.insert_many(batch, ordered=False)

    coll.create_index([("tree_id", 1), ("node_id", 1)], name=IDX, unique=True)
    # Single-field unique index, for the shipped-express positive control.
    coll.create_index([("node_id", 1)], name="probe_node", unique=True)
    # A second index on a field of the predicate, so the unhinted query has more
    # than one candidate solution and therefore gets multi-planned and cached.
    # Without it the unhinted arm is a single-solution plan, which is never
    # written to the cache, and the two arms would both replan.
    coll.create_index([("tree_id", 1), ("depth", 1)], name="probe_tree_depth")
    coll.create_index([("parent_id", 1)], name="probe_parent")


COMPOUND_KNOB = "internalQueryDisableCompoundFieldExpressExecutor"
SINGLE_KNOB = "internalQueryDisableSingleFieldExpressExecutor"


def set_express(client: pymongo.MongoClient, enabled: bool) -> None:
    # The compound path is reached through the single-field eligibility check, so
    # disabling the single-field executor disables both. Set them together.
    client.admin.command("setParameter", **{COMPOUND_KNOB: not enabled})
    client.admin.command("setParameter", **{SINGLE_KNOB: not enabled})


def plan_of(coll, flt, hint) -> dict:
    ex = coll.find(flt, PROJECTION, hint=hint).explain() if hint else coll.find(flt, PROJECTION).explain()
    win = ex["queryPlanner"]["winningPlan"]
    stages, index = [], None
    node = win.get("queryPlan", win)
    while node:
        stages.append(node["stage"])
        if node["stage"] in ("IXSCAN", "EXPRESS_IXSCAN"):
            index = node.get("indexName") or node.get("keyPattern")
        node = node.get("inputStage")
    return {"stages": stages, "index": index}


def plan_cache_counters(client: pymongo.MongoClient) -> dict:
    m = client.admin.command("serverStatus")["metrics"]["query"]["planCache"]["classic"]
    return {k: m[k] for k in ("hits", "misses", "skipped") if k in m}


class Perf:
    """Retired user-space instructions on a pid, sampled between marks."""

    def __init__(self, pid: int):
        self.pid = pid
        self.proc = subprocess.Popen(
            ["perf", "stat", "-e", "instructions:u", "-p", str(pid), "-I", "100", "-x", ","],
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
        )
        self.samples: list[tuple[float, int]] = []
        import threading

        def pump():
            for line in self.proc.stderr:
                parts = line.strip().split(",")
                if len(parts) < 2:
                    continue
                try:
                    self.samples.append((float(parts[0]), int(parts[1])))
                except ValueError:
                    continue

        self.thread = threading.Thread(target=pump, daemon=True)
        self.thread.start()
        time.sleep(0.5)

    def total(self) -> int:
        return sum(v for _, v in self.samples)

    def close(self) -> None:
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.thread.join(timeout=2)


# name -> (predicate kind, hint, express enabled)
#
# The `single_*` pair is a positive control. Single-field express is a shipped
# MongoDB feature with a published win; if this harness cannot reproduce that
# win, its measurement of the compound pair means nothing.
ARMS = {
    "hinted": ("compound", IDX, False),
    "cached": ("compound", None, False),
    "express": ("compound", None, True),
    "single_off": ("single", None, False),
    "single_on": ("single", None, True),
}


def run_block(client, coll, filters, arm: str, ops: int) -> None:
    kind, hint, express = ARMS[arm]
    set_express(client, express)
    flt = filters[kind]
    if hint:
        for _ in range(ops):
            next(coll.find(flt, PROJECTION, hint=hint).limit(1), None)
    else:
        for _ in range(ops):
            next(coll.find(flt, PROJECTION).limit(1), None)


def bootstrap_ci(diffs: list[float], iters: int = 20000, seed: int = 7) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(diffs)
    means = []
    for _ in range(iters):
        means.append(statistics.fmean(diffs[rng.randrange(n)] for _ in range(n)))
    means.sort()
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=12)
    ap.add_argument("--ops", type=int, default=20000)
    ap.add_argument("--out", default="runs/hint_plancache_instr")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"starting mongod on {PORT} from {MONGOD}", flush=True)
    proc = start_mongod()
    try:
        client = pymongo.MongoClient(f"mongodb://127.0.0.1:{PORT}")
        server_pid = proc.pid
        print(f"mongod pid {server_pid}; loading {NDOCS} docs", flush=True)
        load(client)
        coll = client[DB][COLL]

        hit = f"n{NDOCS // 2:07d}"
        filters = {
            "compound": {"tree_id": TREE_ID, "node_id": hit},
            "single": {"node_id": hit},
        }

        # Warm the cache for the unhinted arms and record the plan each arm runs.
        plans = {}
        for arm in ARMS:
            kind, hint, express = ARMS[arm]
            set_express(client, express)
            run_block(client, coll, filters, arm, 200)
            plans[arm] = plan_of(coll, filters[kind], hint)
            print(f"{arm} plan: {plans[arm]}", flush=True)

        # `hinted` and `cached` must be the same plan or their difference is not
        # attributable to planning. The express arms are expected to differ --
        # that is the point of them.
        if plans["hinted"] != plans["cached"]:
            print("ABORT: hinted and cached do not execute the same plan", flush=True)
            return 2
        for arm in ("express", "single_on"):
            if plans[arm]["stages"] != ["EXPRESS_IXSCAN"]:
                print(f"ABORT: {arm} did not take the express path", flush=True)
                return 2
        for arm in ("cached", "single_off"):
            if "EXPRESS_IXSCAN" in plans[arm]["stages"]:
                print(f"ABORT: {arm} took the express path; knob did not hold", flush=True)
                return 2
        if plans["express"]["index"] != plans["cached"]["index"]:
            print("ABORT: express and cached use different indexes", flush=True)
            return 2
        if plans["single_on"]["index"] != plans["single_off"]["index"]:
            print("ABORT: single_on and single_off use different indexes", flush=True)
            return 2

        cache_evidence = {}
        for arm in ARMS:
            before = plan_cache_counters(client)
            run_block(client, coll, filters, arm, 500)
            after = plan_cache_counters(client)
            cache_evidence[arm] = {k: after[k] - before[k] for k in after}
        print(f"plan cache counters per 500 ops: {json.dumps(cache_evidence)}", flush=True)

        # Warm every arm so nothing in the measured window is first-touch.
        for arm in ARMS:
            run_block(client, coll, filters, arm, 3000)

        names = list(ARMS)
        rows = []
        for b in range(args.blocks):
            # Rotate arm order so any within-block drift does not favour one arm.
            order = names[b % len(names):] + names[: b % len(names)]
            block = {"block": b, "order": order}
            for name in order:
                perf = Perf(server_pid)
                t0 = time.perf_counter()
                run_block(client, coll, filters, name, args.ops)
                t1 = time.perf_counter()
                time.sleep(0.2)
                perf.close()
                instr = perf.total()
                block[name] = {
                    "instructions": instr,
                    "per_op": instr / args.ops,
                    "wall_s": t1 - t0,
                    "us_per_op": (t1 - t0) * 1e6 / args.ops,
                }
            rows.append(block)
            print(
                "block {}: ".format(b)
                + "  ".join(f"{n} {block[n]['per_op']:.0f}" for n in names),
                flush=True,
            )

        def compare(a: str, b: str) -> dict:
            pcts = [100.0 * (r[a]["per_op"] - r[b]["per_op"]) / r[b]["per_op"] for r in rows]
            lo, hi = bootstrap_ci(pcts)
            return {
                "mean_delta_pct": statistics.fmean(pcts),
                "median_delta_pct": statistics.median(pcts),
                "ci95": [lo, hi],
                "blocks_positive": sum(1 for p in pcts if p > 0),
                "blocks": len(pcts),
            }

        summary = {
            "per_op_instructions": {
                n: statistics.fmean(r[n]["per_op"] for r in rows) for n in names
            },
            "us_per_op": {n: statistics.fmean(r[n]["us_per_op"] for r in rows) for n in names},
            "hinted_vs_cached": compare("hinted", "cached"),
            "hinted_vs_express": compare("hinted", "express"),
            "cached_vs_express": compare("cached", "express"),
            "single_off_vs_single_on": compare("single_off", "single_on"),
        }
        print(json.dumps(summary, indent=2), flush=True)

        (outdir / "result.json").write_text(
            json.dumps(
                {
                    "mongod": MONGOD,
                    "ndocs": NDOCS,
                    "ops_per_block": args.ops,
                    "plans": plans,
                    "plan_cache_counters": cache_evidence,
                    "blocks": rows,
                    "summary": summary,
                },
                indent=2,
            )
        )
        print(f"wrote {outdir / 'result.json'}", flush=True)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
