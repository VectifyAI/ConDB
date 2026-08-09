#!/usr/bin/env python3
"""Two loose ends:
 1. Does trySbeEngine change anything for an _id equality?  (Claim D's premise
    that B and C are mutually exclusive.)
 2. Was driver.json a logging-ON run?  Measure find_one wall at slowms=0 and
    slowms=100 back to back, paired.
"""
import gc, json, statistics, sys, time
sys.path.insert(0, "/home/junyao/code/pageindex/ConDB/bench/db")
from pymongo import MongoClient
from bench_opwin_20260807 import NODES, TEXT, NODE_PROJ, canon_node

kw = dict(maxPoolSize=1, minPoolSize=1, directConnection=True)
cli = MongoClient("mongodb://localhost:57017", **kw)
db, adm = cli["bench"], cli["admin"]
node_ids = [r["_id"] for r in db[TEXT].find({}, {"_id": 1}).sort([("_id", 1)]).limit(64)]
oid = db[NODES].find_one({"tree_id": "base", "node_id": node_ids[0]}, {"_id": 1})["_id"]

orig_fw = adm.command("getParameter", 1, internalQueryFrameworkControl=1)["internalQueryFrameworkControl"]
orig_pr = adm.command("profile", -1)
print("start:", orig_fw, orig_pr)
try:
    for s in ("forceClassicEngine", "trySbeEngine"):
        adm.command("setParameter", 1, internalQueryFrameworkControl=s)
        for label, cmd in (("_id equality", {"find": NODES, "filter": {"_id": oid}}),
                           ("_id equality + proj", {"find": NODES, "filter": {"_id": oid},
                                                    "projection": NODE_PROJ}),
                           ("_id equality + hint", {"find": NODES, "filter": {"_id": oid},
                                                    "hint": "_id_"})):
            ex = db.command({"explain": cmd, "verbosity": "queryPlanner"})
            print(f"  {s:<20}{label:<22} explainVersion={ex.get('explainVersion')} "
                  f"{json.dumps(ex['queryPlanner']['winningPlan'])[:120]}")
finally:
    adm.command("setParameter", 1, internalQueryFrameworkControl=orig_fw)
    got = adm.command("getParameter", 1, internalQueryFrameworkControl=1)["internalQueryFrameworkControl"]
    print("framework restored:", got)
    assert got == orig_fw

print("\nlogging on/off, paired, pymongo find_one over the published port")
coll = db[NODES]


def fn(i):
    return canon_node(coll.find_one({"tree_id": "base", "node_id": node_ids[i]},
                                    NODE_PROJ, hint="allops_tree_node"))


def block():
    gc.disable(); t0 = time.perf_counter()
    for k in range(300):
        fn(k % 64)
    t1 = time.perf_counter(); gc.enable()
    return (t1 - t0) * 1e6 / 300


res = {0: [], 100: []}
try:
    for b in range(10):
        for slow in ((0, 100) if b % 2 == 0 else (100, 0)):
            for d in ("admin", "bench"):
                cli[d].command("profile", 0, slowms=slow)
            for i in range(200):
                fn(i % 64)
            res[slow].append(block())
finally:
    for d in ("admin", "bench"):
        cli[d].command("profile", 0, slowms=orig_pr["slowms"])
    now = adm.command("profile", -1)
    print("profile restored:", now)
    assert now["slowms"] == orig_pr["slowms"] and now["was"] == orig_pr["was"]

m0, m1 = statistics.median(res[0]), statistics.median(res[100])
d = [res[0][i] - res[100][i] for i in range(10)]
p = [100 * d[i] / res[100][i] for i in range(10)]
print(f"slowms=0 (logging ON)  wall {m0:.1f} us   {[round(x,1) for x in res[0]]}")
print(f"slowms=100 (logging OFF) wall {m1:.1f} us {[round(x,1) for x in res[100]]}")
print(f"paired: logging costs {statistics.median(d):+.1f} us = {statistics.median(p):+.1f}% "
      f"[{min(p):.1f},{max(p):.1f}] {sum(1 for x in d if x>0)}/10 blocks")
