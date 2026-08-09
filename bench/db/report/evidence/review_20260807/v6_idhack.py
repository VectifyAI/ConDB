#!/usr/bin/env python3
"""Claims B and D.

Real collection (layout2_view), published docker port unless marked _ip:
  base_pm          find_one {tree_id,node_id} + hint          baseline
  idhack_pm        find_one {_id: ObjectId}                   the arm Claim B measured
  idhack_dbcmd     Database.command find {_id: ObjectId}      adoptable A'+B stack
  idhack_raw       raw OP_MSG {_id: ObjectId}                 transport-matched to PostgreSQL
  idhack_raw_ip    raw OP_MSG {_id: ObjectId}, container IP   reproduces the claimed 60.3 us

Key-shape probes, 10M docs each, identical 7-field payload, published port, raw client:
  probe_oid        _id = ObjectId          (what was measured)
  probe_str        _id = "base|<node_id>"  (the string natural key)
  probe_sub        _id = {tree_id,node_id} (the sub-document natural key)
"""
from __future__ import annotations
import gc, json, statistics, sys, time
from pathlib import Path

sys.path.insert(0, "/home/junyao/code/pageindex/ConDB/bench/db")
from pymongo import MongoClient
from bench_opwin_20260807 import (NODES, TEXT, NODE_PROJ, RawMongo, canon_node,
                                  thread_table, mongod_pid)

BLOCKS = int(sys.argv[1]); OUT = Path(sys.argv[2])
ITERS, INPUTS = 300, 64
kw = dict(maxPoolSize=1, minPoolSize=1, directConnection=True)
PUB, CON = "mongodb://localhost:57017", "mongodb://172.17.0.3:27017"
cli_a, cli_b = MongoClient(PUB, **kw), MongoClient(PUB, **kw)
db_a, db_b = cli_a["bench"], cli_b["bench"]
conn_a = db_a.command("hello")["connectionId"]
conn_b = db_b.command("hello")["connectionId"]
r_pub = RawMongo("localhost", 57017)
r_con = RawMongo("172.17.0.3", 27017)
p_oid, p_str, p_sub = (RawMongo("localhost", 57017) for _ in range(3))
pid = mongod_pid("condb_mongo")

node_ids = [r["_id"] for r in db_a[TEXT].find({}, {"_id": 1}).sort([("_id", 1)]).limit(INPUTS)]
oids = [db_a[NODES].find_one({"tree_id": "base", "node_id": n}, {"_id": 1})["_id"] for n in node_ids]
p_oids = [db_a["zz_rev_oid"].find_one({"node_id": n}, {"_id": 1})["_id"] for n in node_ids]
for c in ("zz_rev_oid", "zz_rev_str", "zz_rev_sub"):
    st = db_a.command("collStats", c)
    print(f"{c}: {st['count']} docs avgObjSize {st['avgObjSize']} "
          f"storage {st['storageSize']/1e6:.0f}MB _id_ {st['indexSizes']['_id_']/1e6:.1f}MB")


def raw_find(raw, coll, flt):
    r = raw.command({"find": coll, "filter": flt, "projection": NODE_PROJ,
                     "limit": 1, "singleBatch": True, "$db": "bench"})
    assert r["ok"] == 1.0 and r["cursor"]["id"] == 0
    b = r["cursor"]["firstBatch"]
    return canon_node(b[0]) if b else ()


ARMS = {
    "base_pm": (lambda i: canon_node(db_a[NODES].find_one(
        {"tree_id": "base", "node_id": node_ids[i]}, NODE_PROJ,
        hint="allops_tree_node")), f"conn{conn_a}"),
    "idhack_pm": (lambda i: canon_node(db_b[NODES].find_one({"_id": oids[i]}, NODE_PROJ)),
                  f"conn{conn_b}"),
    "idhack_dbcmd": (lambda i: canon_node(db_b.command(
        {"find": NODES, "filter": {"_id": oids[i]}, "projection": NODE_PROJ,
         "limit": 1, "singleBatch": True})["cursor"]["firstBatch"][0]), f"conn{conn_b}"),
    "idhack_raw": (lambda i: raw_find(r_pub, NODES, {"_id": oids[i]}),
                   f"conn{r_pub.connection_id}"),
    "idhack_raw_ip": (lambda i: raw_find(r_con, NODES, {"_id": oids[i]}),
                      f"conn{r_con.connection_id}"),
    "probe_oid": (lambda i: raw_find(p_oid, "zz_rev_oid", {"_id": p_oids[i]}),
                  f"conn{p_oid.connection_id}"),
    "probe_str": (lambda i: raw_find(p_str, "zz_rev_str", {"_id": f"base|{node_ids[i]}"}),
                  f"conn{p_str.connection_id}"),
    "probe_sub": (lambda i: raw_find(p_sub, "zz_rev_sub",
                                     {"_id": {"tree_id": "base", "node_id": node_ids[i]}}),
                  f"conn{p_sub.connection_id}"),
}
names = list(ARMS)
for i in range(INPUTS):
    exp = ARMS["base_pm"][0](i)
    assert len(exp) == 7, exp
    for n in names:
        got = ARMS[n][0](i)
        assert got == exp, (n, i, got, exp)
print(f"equality: {len(names)} arms x {INPUTS} inputs x 7 fields, all equal")

# plan check for every arm shape
for label, coll, flt in (("real base+hint", NODES, {"tree_id": "base", "node_id": node_ids[0]}),
                         ("real _id oid", NODES, {"_id": oids[0]}),
                         ("probe oid", "zz_rev_oid", {"_id": p_oids[0]}),
                         ("probe str", "zz_rev_str", {"_id": f"base|{node_ids[0]}"}),
                         ("probe sub", "zz_rev_sub", {"_id": {"tree_id": "base", "node_id": node_ids[0]}})):
    cmd = {"find": coll, "filter": flt, "projection": NODE_PROJ, "limit": 1, "singleBatch": True}
    if label == "real base+hint":
        cmd["hint"] = "allops_tree_node"
    ex = db_a.command({"explain": cmd, "verbosity": "executionStats"})
    print(f"  {label:16} {json.dumps(ex['queryPlanner']['winningPlan'])[:110]} "
          f"nRet={ex['executionStats']['nReturned']}")

orig = cli_a["admin"].command("profile", -1)
results = {n: [] for n in names}
try:
    for d in ("admin", "bench"):
        cli_a[d].command("profile", 0, slowms=100)
    for n in names:
        for i in range(300):
            ARMS[n][0](i % INPUTS)
    for blk in range(BLOCKS):
        order = names[blk % len(names):] + names[:blk % len(names)]
        for n in order:
            fn, comm = ARMS[n]
            before = thread_table(pid); gc.disable()
            t0 = time.perf_counter(); c0 = time.process_time()
            for k in range(ITERS):
                fn(k % INPUTS)
            c1 = time.process_time(); t1 = time.perf_counter()
            gc.enable(); after = thread_table(pid)
            results[n].append({"wall_us": (t1-t0)*1e6/ITERS, "ccpu_us": (c1-c0)*1e6/ITERS,
                               "scpu_us": (after.get(comm, 0)-before.get(comm, 0))/1e3/ITERS})
        print(f"block {blk+1}/{BLOCKS}", flush=True)
finally:
    for d in ("admin", "bench"):
        cli_a[d].command("profile", 0, slowms=orig["slowms"])
    now = cli_a["admin"].command("profile", -1)
    print("profile restored:", now)
    assert now["slowms"] == orig["slowms"]

summary = {n: {k: round(statistics.median(r[k] for r in results[n]), 2)
               for k in ("wall_us", "ccpu_us", "scpu_us")} for n in names}
for n in names:
    ws = [r["wall_us"] for r in results[n]]
    summary[n]["wall_range"] = [round(min(ws), 1), round(max(ws), 1)]


def pair(a, b):
    e = {}
    for k in ("wall_us", "scpu_us"):
        d = [results[a][i][k] - results[b][i][k] for i in range(BLOCKS)]
        p = [100*x/results[b][i][k] for i, x in enumerate(d)]
        e[k] = {"med_us": round(statistics.median(d), 2), "med_pct": round(statistics.median(p), 2),
                "min_pct": round(min(p), 2), "max_pct": round(max(p), 2),
                "n_neg": sum(1 for x in d if x < 0)}
    return e


paired = {f"{a}_vs_{b}": pair(a, b) for a, b in (
    ("idhack_pm", "base_pm"), ("idhack_dbcmd", "base_pm"), ("idhack_raw", "base_pm"),
    ("idhack_raw_ip", "base_pm"), ("idhack_raw_ip", "idhack_raw"),
    ("probe_str", "probe_oid"), ("probe_sub", "probe_oid"), ("probe_sub", "probe_str"))}
OUT.write_text(json.dumps({"blocks": BLOCKS, "summary": summary, "paired": paired,
                           "raw": results}, indent=2))
print(f"\n{'arm':<16}{'wall':>9}{'ccpu':>9}{'scpu':>9}   wall range")
for n in names:
    s = summary[n]
    print(f"{n:<16}{s['wall_us']:>9.1f}{s['ccpu_us']:>9.1f}{s['scpu_us']:>9.1f}   {s['wall_range']}")
print()
for k, p in paired.items():
    print(f"{k:<32} wall {p['wall_us']['med_pct']:>7.2f}% ({p['wall_us']['med_us']:+.1f}) "
          f"[{p['wall_us']['min_pct']:.1f},{p['wall_us']['max_pct']:.1f}] {p['wall_us']['n_neg']}/{BLOCKS} | "
          f"scpu {p['scpu_us']['med_pct']:>7.2f}% ({p['scpu_us']['med_us']:+.1f}) "
          f"[{p['scpu_us']['min_pct']:.1f},{p['scpu_us']['max_pct']:.1f}] {p['scpu_us']['n_neg']}/{BLOCKS}")
for r in (r_pub, r_con, p_oid, p_str, p_sub):
    r.close()
