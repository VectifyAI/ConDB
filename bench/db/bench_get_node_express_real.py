"""What compound-equality express is worth on ConDB's real get_node, measured.

`report/ops/get_node.md` prices get_node at 71.7 us of server CPU against
PostgreSQL's 46.5 us unprepared and 20.5 us prepared, and attributes 15.46 us
(21.6%) to `getExecutorFind` -- work MongoDB redoes on every call, because a
hinted single-solution query never reaches the plan cache.

The compound express change removes that work, but only for an unhinted query:
a hint disqualifies express outright. This measures the three arms that matter,
on the real collection shape, in one binary:

    hinted    find(q, proj).hint("allops_tree_node")  -- what ConDB sends today
    planner   find(q, proj), express off              -- what stock master does
    express   find(q, proj), express on               -- the change

The real dataset lives on a MongoDB 7.0.34 server, which predates express
entirely, so it cannot host this comparison. The collection is copied verbatim
into a server built from the patched fork -- every document, every index -- and
all three arms then come from that one binary with the feature switched by
`internalQueryDisableCompoundFieldExpressExecutor`. Nothing here compares two
builds: layout noise between builds measures 13% on this workload, larger than
most effects worth reporting.

The headline metric is the *ratio* between arms, not the absolute microseconds:
this harness is not the perf-based phase decomposition that produced the 71.7 us
figure, so its absolute CPU is not comparable to that number, while a ratio
measured on the same collection shape is. Server retired instructions are the
primary metric because three sibling agents build on this box and CPU time
swings with them; server CPU from /proc and client wall time are reported
alongside as a cross-check.

A pair of random-node arms guards against the fixed hot document being the whole
story.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymongo

MONGOD = "/home/junyao/code/mongo/bazel-bin/src/mongo/db/mongod"
SRC_URI = "mongodb://127.0.0.1:57017/?directConnection=true"
PORT = 57033
DBPATH = Path("/tmp/gn-real/db")
LOGPATH = Path("/tmp/gn-real/mongod.log")

DB = "bench"
COLL = "layout2_view"
NODE_INDEX = "allops_tree_node"
TREE_ID = "base"

NODE_PROJECTION = {
    "_id": 0, "node_id": 1, "parent_id": 1, "depth": 1, "title": 1,
    "summary": 1, "start_index": 1, "end_index": 1,
}

# Every index the real collection carries. The non-express arms pay for index
# selection over all of them, so dropping any would flatter those arms.
INDEXES = [
    {"key": [("path", 1), ("node_id", 1)], "name": "path_1_node_id_1"},
    {"key": [("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)],
     "name": "layout2_rootcause_exact_cover"},
    {"key": [("tree_id", 1), ("node_id", 1)], "name": NODE_INDEX, "unique": True},
    {"key": [("tree_id", 1), ("parent_id", 1), ("path", 1), ("node_id", 1)],
     "name": "allops_tree_parent_path"},
]

COMPOUND_KNOB = "internalQueryDisableCompoundFieldExpressExecutor"
SINGLE_KNOB = "internalQueryDisableSingleFieldExpressExecutor"
CLK_TCK = os.sysconf("SC_CLK_TCK")


def log(m: str) -> None:
    print(m, flush=True)


def start_mongod(cache_gb: int) -> subprocess.Popen:
    shutil.rmtree(DBPATH.parent, ignore_errors=True)
    DBPATH.mkdir(parents=True)
    proc = subprocess.Popen(
        [MONGOD, "--port", str(PORT), "--dbpath", str(DBPATH), "--bind_ip", "127.0.0.1",
         "--wiredTigerCacheSizeGB", str(cache_gb), "--logpath", str(LOGPATH),
         "--setParameter", "diagnosticDataCollectionEnabled=false",
         # condb_mongo logs every operation; slowms=0 is worth ~40% of server CPU
         # on reads this short, so nothing may be logged in the measured window.
         "--slowms", "10000"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(240):
        try:
            pymongo.MongoClient(f"mongodb://127.0.0.1:{PORT}",
                                serverSelectionTimeoutMS=500).admin.command("ping")
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError(f"mongod did not start; see {LOGPATH}")


def copy_collection(limit: int | None) -> int:
    src = pymongo.MongoClient(SRC_URI)[DB][COLL]
    dst_client = pymongo.MongoClient(f"mongodb://127.0.0.1:{PORT}", w=1)
    dst = dst_client[DB][COLL]
    dst.drop()

    total = src.estimated_document_count() if limit is None else limit
    log(f"copying {total:,} documents from 57017")

    written = [0]
    lock = threading.Lock()
    t0 = time.time()

    def write(batch):
        dst.insert_many(batch, ordered=False)
        with lock:
            written[0] += len(batch)
            if written[0] % 500_000 < len(batch):
                el = time.time() - t0
                log(f"  {written[0]:,} / {total:,}  ({el:.0f}s, {written[0]/max(el,1):,.0f} doc/s)")

    cursor = src.find({}, no_cursor_timeout=True).batch_size(5000)
    if limit:
        cursor = cursor.limit(limit)
    with ThreadPoolExecutor(max_workers=6) as pool:
        batch, futures = [], []
        for doc in cursor:
            batch.append(doc)
            if len(batch) == 5000:
                futures.append(pool.submit(write, batch))
                batch = []
                futures = [f for f in futures if not f.done()]
                while len(futures) > 12:
                    time.sleep(0.02)
                    futures = [f for f in futures if not f.done()]
        if batch:
            futures.append(pool.submit(write, batch))
    cursor.close()

    log(f"copied {written[0]:,} documents in {time.time()-t0:.0f}s; building {len(INDEXES)} indexes")
    t1 = time.time()
    dst.create_indexes([
        pymongo.IndexModel(ix["key"], **{k: v for k, v in ix.items() if k != "key"})
        for ix in INDEXES
    ])
    log(f"indexes built in {time.time()-t1:.0f}s")
    return written[0]


def set_express(client, enabled: bool) -> None:
    client.admin.command("setParameter", **{COMPOUND_KNOB: not enabled})
    client.admin.command("setParameter", **{SINGLE_KNOB: not enabled})


# name -> (hint, express enabled, random node)
ARMS = {
    "hinted":      (NODE_INDEX, False, False),
    "planner":     (None,       False, False),
    "express":     (None,       True,  False),
    "hinted_rand": (NODE_INDEX, False, True),
    "express_rand": (None,      True,  True),
}


def run_block(client, coll, arm: str, ops: int, hit: str, pool: list[str], rng) -> None:
    hint, express, rand = ARMS[arm]
    set_express(client, express)
    if rand:
        if hint:
            for _ in range(ops):
                coll.find_one({"tree_id": TREE_ID, "node_id": rng.choice(pool)},
                              NODE_PROJECTION, hint=hint)
        else:
            for _ in range(ops):
                coll.find_one({"tree_id": TREE_ID, "node_id": rng.choice(pool)}, NODE_PROJECTION)
    else:
        flt = {"tree_id": TREE_ID, "node_id": hit}
        if hint:
            for _ in range(ops):
                coll.find_one(flt, NODE_PROJECTION, hint=hint)
        else:
            for _ in range(ops):
                coll.find_one(flt, NODE_PROJECTION)


def stages_of(coll, flt, hint):
    cur = coll.find(flt, NODE_PROJECTION).limit(1)
    if hint:
        cur = cur.hint(hint)
    ex = cur.explain()
    w = ex["queryPlanner"]["winningPlan"]
    n = w.get("queryPlan", w)
    out, index = [], None
    while n:
        out.append(n["stage"])
        if n["stage"] in ("IXSCAN", "EXPRESS_IXSCAN"):
            index = n.get("indexName")
        n = n.get("inputStage")
    return {"stages": out, "index": index}


def proc_cpu_us(pid: int) -> float:
    raw = Path(f"/proc/{pid}/stat").read_text()
    fields = raw[raw.rindex(")") + 2:].split()
    return (int(fields[11]) + int(fields[12])) * 1e6 / CLK_TCK


class Perf:
    def __init__(self, pid: int):
        self.proc = subprocess.Popen(
            ["perf", "stat", "-e", "instructions:u", "-p", str(pid), "-I", "100", "-x", ","],
            stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        self.samples: list[int] = []

        def pump():
            for line in self.proc.stderr:
                p = line.strip().split(",")
                if len(p) >= 2:
                    try:
                        self.samples.append(int(p[1]))
                    except ValueError:
                        pass

        self.thread = threading.Thread(target=pump, daemon=True)
        self.thread.start()
        time.sleep(0.5)

    def total(self) -> int:
        return sum(self.samples)

    def close(self) -> None:
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.thread.join(timeout=2)


def bootstrap_ci(vals, iters=20000, seed=11):
    rng = random.Random(seed)
    n = len(vals)
    means = sorted(statistics.fmean(vals[rng.randrange(n)] for _ in range(n)) for _ in range(iters))
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=12)
    ap.add_argument("--ops", type=int, default=10000)
    ap.add_argument("--cache-gb", type=int, default=16)
    ap.add_argument("--limit", type=int, default=None, help="copy only N docs (debug)")
    ap.add_argument("--out", default="runs/get_node_express_real")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    proc = start_mongod(args.cache_gb)
    try:
        ndocs = copy_collection(args.limit)
        client = pymongo.MongoClient(f"mongodb://127.0.0.1:{PORT}")
        coll = client[DB][COLL]

        sample = list(coll.aggregate([{"$sample": {"size": 5000}},
                                      {"$project": {"_id": 0, "node_id": 1}}]))
        pool = [d["node_id"] for d in sample]
        hit = pool[0]
        rng = random.Random(20260810)
        flt = {"tree_id": TREE_ID, "node_id": hit}

        # Warm every arm, then record the plan each one runs.
        plans = {}
        for arm in ARMS:
            run_block(client, coll, arm, 200, hit, pool, rng)
            hint, express, _ = ARMS[arm]
            plans[arm] = stages_of(coll, flt, hint)
            log(f"{arm:13s} {plans[arm]}")

        if "EXPRESS_IXSCAN" not in plans["express"]["stages"]:
            log("ABORT: express arm did not take the express path")
            return 2
        if "EXPRESS_IXSCAN" in plans["planner"]["stages"]:
            log("ABORT: planner arm took express; knob did not hold")
            return 2
        if plans["planner"]["index"] != NODE_INDEX or plans["hinted"]["index"] != NODE_INDEX:
            log("ABORT: a non-express arm chose a different index")
            return 2

        for arm in ARMS:
            run_block(client, coll, arm, 3000, hit, pool, rng)

        names = list(ARMS)
        rows = []
        for b in range(args.blocks):
            order = names[b % len(names):] + names[: b % len(names)]
            block = {"block": b, "order": order}
            for name in order:
                perf = Perf(proc.pid)
                c0 = proc_cpu_us(proc.pid)
                t0 = time.perf_counter()
                run_block(client, coll, name, args.ops, hit, pool, rng)
                t1 = time.perf_counter()
                c1 = proc_cpu_us(proc.pid)
                time.sleep(0.2)
                perf.close()
                block[name] = {
                    "instr_per_op": perf.total() / args.ops,
                    "server_cpu_us_per_op": (c1 - c0) / args.ops,
                    "wall_us_per_op": (t1 - t0) * 1e6 / args.ops,
                }
            rows.append(block)
            log("block {}: ".format(b) + "  ".join(
                f"{n} {block[n]['instr_per_op']:.0f}" for n in names))

        def compare(a, b, key="instr_per_op"):
            # Reduction of `a` relative to `b`, in percent: how much of b's cost a removes.
            pcts = [100.0 * (r[b][key] - r[a][key]) / r[b][key] for r in rows]
            lo, hi = bootstrap_ci(pcts)
            return {"mean_reduction_pct": statistics.fmean(pcts),
                    "median_reduction_pct": statistics.median(pcts),
                    "ci95": [lo, hi],
                    "blocks_positive": sum(1 for p in pcts if p > 0),
                    "blocks": len(pcts)}

        summary = {
            "ndocs": ndocs,
            "instr_per_op": {n: statistics.fmean(r[n]["instr_per_op"] for r in rows) for n in names},
            "server_cpu_us_per_op": {n: statistics.fmean(r[n]["server_cpu_us_per_op"] for r in rows) for n in names},
            "wall_us_per_op": {n: statistics.fmean(r[n]["wall_us_per_op"] for r in rows) for n in names},
            "express_vs_hinted_instr": compare("express", "hinted"),
            "express_vs_planner_instr": compare("express", "planner"),
            "express_vs_hinted_cpu": compare("express", "hinted", "server_cpu_us_per_op"),
            "express_rand_vs_hinted_rand_instr": compare("express_rand", "hinted_rand"),
        }

        # What the report's 71.7 us becomes if express removes the same share of
        # server work there as it does here.
        r = summary["express_vs_hinted_instr"]["mean_reduction_pct"] / 100.0
        summary["projected_get_node_server_cpu_us"] = {
            "report_baseline_us": 71.7,
            "reduction_applied": r,
            "projected_us": 71.7 * (1 - r),
            "postgres_unprepared_us": 46.5,
            "postgres_prepared_us": 20.5,
        }
        log(json.dumps(summary, indent=2))

        (outdir / "result.json").write_text(json.dumps(
            {"mongod": MONGOD, "source": SRC_URI, "ops_per_block": args.ops,
             "plans": plans, "blocks": rows, "summary": summary}, indent=2))
        log(f"wrote {outdir / 'result.json'}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
