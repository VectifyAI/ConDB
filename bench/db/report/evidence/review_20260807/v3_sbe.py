#!/usr/bin/env python3
"""Independent re-measurement of Claim C (internalQueryFrameworkControl=trySbeEngine).

Adds three things the original harness did not do:
  * element-wise correctness comparison SBE vs classic
  * per-iteration latency samples -> p50/p95/p99 tail
  * cold-plan-cache cost: the first executions after planCacheClear under each setting
Restores the parameter in a finally and verifies the restore.
"""
from __future__ import annotations
import gc, json, statistics, sys, time
from pathlib import Path

sys.path.insert(0, "/home/junyao/code/pageindex/ConDB/bench/db")
from pymongo import MongoClient
from bench_opwin_20260807 import (NODES, NODE_PROJ, CHILD_PROJ, RawMongo,
                                  thread_table, mongod_pid, canon_node, canon_children)

BLOCKS = int(sys.argv[1]) if len(sys.argv) > 1 else 12
ITERS = 300
INPUTS = 64
OUT = Path(sys.argv[2])
URI = "mongodb://172.17.0.3:27017"

cli = MongoClient(URI, maxPoolSize=1, minPoolSize=1, directConnection=True)
db, adm = cli["bench"], cli["admin"]
raw = RawMongo("172.17.0.3", 27017)
comm = f"conn{raw.connection_id}"
pid = mongod_pid("condb_mongo")
orig = adm.command("getParameter", 1, internalQueryFrameworkControl=1)["internalQueryFrameworkControl"]
orig_prof = adm.command("profile", -1)
print("framework at start:", orig, "profile:", orig_prof)

node_ids = [r["_id"] for r in db["layout_shared_text"].find({}, {"_id": 1})
            .sort([("_id", 1)]).limit(INPUTS)]
samples = json.loads(Path("/home/junyao/code/pageindex/ConDB/bench/db/runs/"
                          "report_3eng_20260716/layout_2v3_postgres_10m_final.json"
                          ).read_text())["samples"]
parent_ids = [s["path"].rsplit("/", 1)[-1] for s in samples[:INPUTS]]


def rawfind(cmd):
    r = raw.command(dict(cmd, **{"$db": "bench"}))
    assert r["ok"] == 1.0, r
    assert r["cursor"]["id"] == 0
    return r["cursor"]["firstBatch"]


def node(i):
    b = rawfind({"find": NODES, "filter": {"tree_id": "base", "node_id": node_ids[i]},
                 "hint": "allops_tree_node", "projection": NODE_PROJ,
                 "limit": 1, "singleBatch": True})
    return canon_node(b[0]) if b else ()


def children(i):
    return canon_children(rawfind({
        "find": NODES, "filter": {"tree_id": "base", "parent_id": parent_ids[i]},
        "sort": {"path": 1, "node_id": 1}, "hint": "allops_tree_parent_path",
        "projection": CHILD_PROJ}))


OPS = {"node": node, "children": children}
SET = ("forceClassicEngine", "trySbeEngine")


def setfw(v):
    adm.command("setParameter", 1, internalQueryFrameworkControl=v)


def pc_stats():
    m = adm.command("serverStatus")["metrics"]["query"]["planCache"]
    return json.loads(json.dumps(m, default=str))


def measure(fn):
    lat = []
    before = thread_table(pid)
    gc.disable()
    t0 = time.perf_counter(); c0 = time.process_time()
    for k in range(ITERS):
        a = time.perf_counter()
        fn(k % INPUTS)
        lat.append((time.perf_counter() - a) * 1e6)
    c1 = time.process_time(); t1 = time.perf_counter()
    gc.enable()
    after = thread_table(pid)
    lat.sort()
    return {"wall_us": (t1 - t0) * 1e6 / ITERS,
            "ccpu_us": (c1 - c0) * 1e6 / ITERS,
            "scpu_us": (after.get(comm, 0) - before.get(comm, 0)) / 1e3 / ITERS,
            "p50": lat[len(lat)//2], "p95": lat[int(len(lat)*0.95)],
            "p99": lat[int(len(lat)*0.99)], "max": lat[-1]}


results = {s: {o: [] for o in OPS} for s in SET}
extra = {}
try:
    for dbn in ("admin", "bench"):
        cli[dbn].command("profile", 0, slowms=100)
    print("slowms=100 for the window")

    # ---- correctness: same rows under both settings ----
    setfw("forceClassicEngine")
    for i in range(INPUTS):
        node(i); children(i)
    ref = [(node(i), children(i)) for i in range(INPUTS)]
    setfw("trySbeEngine")
    got = [(node(i), children(i)) for i in range(INPUTS)]
    nmis = sum(1 for a, b in zip(ref, got) if a != b)
    elems = sum(len(a[0]) + sum(len(r) for r in a[1]) for a in ref)
    extra["correctness"] = {"inputs": INPUTS, "elements": elems, "mismatches": nmis}
    print(f"correctness: {INPUTS} inputs, {elems} elements, {nmis} mismatches")

    # ---- plans + plan cache under each setting ----
    for s in SET:
        setfw(s)
        db.command({"planCacheClear": NODES})
        pc0 = pc_stats()
        for i in range(200):
            children(i % INPUTS)
        pc1 = pc_stats()
        plans = {}
        for op, flt, hint in (("node", {"tree_id": "base", "node_id": node_ids[0]}, "allops_tree_node"),
                              ("children", {"tree_id": "base", "parent_id": parent_ids[0]}, "allops_tree_parent_path")):
            cmd = {"find": NODES, "filter": flt, "hint": hint}
            if op == "children":
                cmd["sort"] = {"path": 1, "node_id": 1}
                cmd["projection"] = CHILD_PROJ
            else:
                cmd["projection"] = NODE_PROJ
                cmd["limit"] = 1
                cmd["singleBatch"] = True
            ex = db.command({"explain": cmd, "verbosity": "executionStats"})
            plans[op] = {"explainVersion": ex.get("explainVersion"),
                         "winningPlan": json.dumps(ex["queryPlanner"]["winningPlan"])[:500],
                         "nReturned": ex["executionStats"]["nReturned"],
                         "keys": ex["executionStats"]["totalKeysExamined"],
                         "docs": ex["executionStats"]["totalDocsExamined"],
                         "execTimeMillis": ex["executionStats"]["executionTimeMillis"]}
        extra[f"plans_{s}"] = plans
        extra[f"plancache_{s}"] = {"before": pc0, "after": pc1}
        print(s, "planCache delta:", {k: (pc1.get(k), pc0.get(k)) for k in pc1})

    # ---- cold: first executions after planCacheClear ----
    cold = {}
    for s in SET:
        setfw(s)
        rows = []
        for rep in range(20):
            db.command({"planCacheClear": NODES})
            t = []
            for k in range(5):
                a = time.perf_counter(); children(k % INPUTS)
                t.append((time.perf_counter() - a) * 1e6)
            rows.append(t)
        cold[s] = [round(statistics.median(r[i] for r in rows), 1) for i in range(5)]
        print("cold first-5 (median over 20 reps)", s, cold[s])
    extra["cold_children_first5_us"] = cold

    # ---- paired blocks ----
    for n in OPS:
        for i in range(300):
            OPS[n](i % INPUTS)
    for blk in range(BLOCKS):
        order = SET if blk % 2 == 0 else SET[::-1]
        for s in order:
            setfw(s)
            for o, fn in OPS.items():
                for i in range(300):
                    fn(i % INPUTS)
            for o, fn in OPS.items():
                results[s][o].append(measure(fn))
        print(f"block {blk+1}/{BLOCKS}", flush=True)
finally:
    setfw(orig)
    got = adm.command("getParameter", 1, internalQueryFrameworkControl=1)["internalQueryFrameworkControl"]
    for dbn in ("admin", "bench"):
        cli[dbn].command("profile", 0, slowms=orig_prof["slowms"])
    prof = adm.command("profile", -1)
    print("RESTORED framework:", got, "profile:", prof)
    assert got == orig and prof["slowms"] == orig_prof["slowms"], "RESTORE FAILED"

summary = {s: {o: {k: round(statistics.median(r[k] for r in results[s][o]), 2)
                   for k in ("wall_us", "ccpu_us", "scpu_us", "p50", "p95", "p99", "max")}
               for o in OPS} for s in SET}
paired = {}
for o in OPS:
    e = {}
    for k in ("wall_us", "scpu_us", "p99"):
        d = [results["trySbeEngine"][o][b][k] - results["forceClassicEngine"][o][b][k]
             for b in range(BLOCKS)]
        p = [100 * dd / results["forceClassicEngine"][o][b][k] for b, dd in enumerate(d)]
        e[k] = {"med_us": round(statistics.median(d), 2), "med_pct": round(statistics.median(p), 2),
                "min_pct": round(min(p), 2), "max_pct": round(max(p), 2),
                "n_favour_sbe": sum(1 for x in d if x < 0)}
    paired[o] = e
OUT.write_text(json.dumps({"blocks": BLOCKS, "iters": ITERS, "summary": summary,
                           "paired": paired, "extra": extra, "raw": results}, indent=2))
print()
for s in SET:
    for o in OPS:
        v = summary[s][o]
        print(f"{s:<20}{o:<10} wall {v['wall_us']:>7.1f} scpu {v['scpu_us']:>7.1f} "
              f"p50 {v['p50']:>7.1f} p95 {v['p95']:>7.1f} p99 {v['p99']:>7.1f} max {v['max']:>8.1f}")
print()
for o, p in paired.items():
    print(f"SBE vs classic {o:<10} wall {p['wall_us']['med_pct']:>7.2f}% [{p['wall_us']['min_pct']:.1f},{p['wall_us']['max_pct']:.1f}] "
          f"{p['wall_us']['n_favour_sbe']}/{BLOCKS} | scpu {p['scpu_us']['med_pct']:>7.2f}% "
          f"[{p['scpu_us']['min_pct']:.1f},{p['scpu_us']['max_pct']:.1f}] {p['scpu_us']['n_favour_sbe']}/{BLOCKS} | "
          f"p99 {p['p99']['med_pct']:>7.2f}%")
raw.close()
