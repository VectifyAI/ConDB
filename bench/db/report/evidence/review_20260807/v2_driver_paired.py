#!/usr/bin/env python3
"""Independent paired re-measurement of Claim A.

Arms, each on its OWN pooled connection so mongod-side CPU is attributable:
  A_find_one     pymongo Collection.find_one            (baseline)
  C_db_command   pymongo Database.command, byte-identical wire command
  F_raw          hand-written OP_MSG, no lsid
  G_raw_lsid     hand-written OP_MSG, WITH an lsid  -> prices the lsid alone
  H_raw_checked  hand-written OP_MSG, no lsid, but checks ok + cursor id
Sets slowms=100 for the window and restores it in a finally.
"""
from __future__ import annotations
import gc, json, statistics, sys, time, uuid
from pathlib import Path

sys.path.insert(0, "/home/junyao/code/pageindex/ConDB/bench/db")
import bson
from bson.binary import Binary, UUID_SUBTYPE
from pymongo import MongoClient
from bench_opwin_20260807 import (NODES, TEXT, NODE_PROJ, RawMongo, canon_node,
                                  thread_table, mongod_pid)

BLOCKS = int(sys.argv[1]) if len(sys.argv) > 1 else 16
ITERS = 300
INPUTS = 64
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/v2_driver_paired.json")

URI = "mongodb://localhost:57017"
kw = dict(maxPoolSize=1, minPoolSize=1, directConnection=True)
cli_a = MongoClient(URI, **kw)
cli_c = MongoClient(URI, **kw)
adm = cli_a["admin"]
pid = mongod_pid("condb_mongo")

db_a, db_c = cli_a["bench"], cli_c["bench"]
conn_a = db_a.command("hello")["connectionId"]
conn_c = db_c.command("hello")["connectionId"]
raw = RawMongo("localhost", 57017)
raw_l = RawMongo("localhost", 57017)
raw_h = RawMongo("localhost", 57017)
print("conn ids", conn_a, conn_c, raw.connection_id, raw_l.connection_id, raw_h.connection_id)

node_ids = [r["_id"] for r in db_a[TEXT].find({}, {"_id": 1})
            .sort([("_id", 1)]).limit(INPUTS)]
coll_a = db_a[NODES]

LSID = {"id": Binary(uuid.uuid4().bytes, UUID_SUBTYPE)}


def cmd(i, db=True):
    c = {"find": NODES, "filter": {"tree_id": "base", "node_id": node_ids[i]},
         "hint": "allops_tree_node", "projection": NODE_PROJ,
         "limit": 1, "singleBatch": True}
    if db:
        c["$db"] = "bench"
    return c


def a(i):
    return canon_node(coll_a.find_one(
        {"tree_id": "base", "node_id": node_ids[i]}, NODE_PROJ,
        hint="allops_tree_node"))


def c(i):
    return canon_node(db_c.command(cmd(i, db=False))["cursor"]["firstBatch"][0])


def f(i):
    return canon_node(raw.command(cmd(i))["cursor"]["firstBatch"][0])


def g(i):
    return canon_node(raw_l.command(dict(cmd(i), lsid=LSID))["cursor"]["firstBatch"][0])


def h(i):
    r = raw_h.command(cmd(i))
    if r.get("ok") != 1.0:
        raise RuntimeError(r)
    cur = r["cursor"]
    if cur["id"] != 0:
        raise RuntimeError("leaked cursor")
    b = cur["firstBatch"]
    return canon_node(b[0]) if b else ()


ARMS = {"A_find_one": (a, f"conn{conn_a}"),
        "C_db_command": (c, f"conn{conn_c}"),
        "F_raw": (f, f"conn{raw.connection_id}"),
        "G_raw_lsid": (g, f"conn{raw_l.connection_id}"),
        "H_raw_checked": (h, f"conn{raw_h.connection_id}")}
names = list(ARMS)

# byte-size proof
print("encoded command bytes: raw(no lsid)", len(bson.encode(cmd(0))),
      "raw(with lsid)", len(bson.encode(dict(cmd(0), lsid=LSID))))

for i in range(len(node_ids)):
    exp = a(i)
    for n in names:
        got = ARMS[n][0](i)
        assert got == exp, (n, i, got, exp)
print("equality verified over", len(node_ids), "inputs x", len(names), "arms")

orig = adm.command("profile", -1)
print("profile before:", orig)
results = {n: [] for n in names}
try:
    for dbn in ("admin", "bench"):
        MongoClient(URI, **kw)[dbn].command("profile", 0, slowms=100)
    print("slowms set to 100 (logging off) for the window")
    for n in names:
        for i in range(300):
            ARMS[n][0](i % len(node_ids))
    for blk in range(BLOCKS):
        order = names[blk % len(names):] + names[:blk % len(names)]
        for n in order:
            fn, comm = ARMS[n]
            before = thread_table(pid)
            gc.disable()
            t0 = time.perf_counter(); c0 = time.process_time()
            for k in range(ITERS):
                fn(k % len(node_ids))
            c1 = time.process_time(); t1 = time.perf_counter()
            gc.enable()
            after = thread_table(pid)
            results[n].append({
                "wall_us": (t1 - t0) * 1e6 / ITERS,
                "ccpu_us": (c1 - c0) * 1e6 / ITERS,
                "scpu_us": (after.get(comm, 0) - before.get(comm, 0)) / 1e3 / ITERS,
                "other_us": sum(after.get(x, 0) - before.get(x, 0) for x in after
                                if x.startswith("conn") and x != comm) / 1e3 / ITERS,
            })
        print(f"block {blk+1}/{BLOCKS}", flush=True)
finally:
    for dbn in ("admin", "bench"):
        MongoClient(URI, **kw)[dbn].command("profile", 0, slowms=orig["slowms"])
    now = MongoClient(URI, **kw)["admin"].command("profile", -1)
    print("profile restored:", now)
    assert now["slowms"] == orig["slowms"] and now["was"] == orig["was"], "RESTORE FAILED"

summary = {n: {k: round(statistics.median(r[k] for r in results[n]), 2)
               for k in ("wall_us", "ccpu_us", "scpu_us", "other_us")} for n in names}
for n in names:
    ws = [r["wall_us"] for r in results[n]]
    summary[n]["wall_min"] = round(min(ws), 2)
    summary[n]["wall_max"] = round(max(ws), 2)
paired = {}
for n in names:
    if n == "A_find_one":
        continue
    e = {}
    for k in ("wall_us", "ccpu_us", "scpu_us"):
        d = [results[n][b][k] - results["A_find_one"][b][k] for b in range(BLOCKS)]
        p = [100 * dd / results["A_find_one"][b][k] for b, dd in enumerate(d)]
        e[k] = {"med_us": round(statistics.median(d), 2),
                "med_pct": round(statistics.median(p), 2),
                "min_pct": round(min(p), 2), "max_pct": round(max(p), 2),
                "n_favour": sum(1 for x in d if x < 0)}
    paired[n] = e
OUT.write_text(json.dumps({"blocks": BLOCKS, "iters": ITERS,
                           "summary": summary, "paired": paired,
                           "raw": results}, indent=2))
print()
print(f"{'arm':<16}{'wall':>9}{'ccpu':>9}{'scpu':>9}{'other':>8}   wall range")
for n in names:
    s = summary[n]
    print(f"{n:<16}{s['wall_us']:>9.1f}{s['ccpu_us']:>9.1f}{s['scpu_us']:>9.1f}"
          f"{s['other_us']:>8.2f}   [{s['wall_min']},{s['wall_max']}]")
print()
for n, p in paired.items():
    print(f"{n:<16} wall {p['wall_us']['med_pct']:>7.2f}% ({p['wall_us']['med_us']:+.1f}us) "
          f"[{p['wall_us']['min_pct']:.1f},{p['wall_us']['max_pct']:.1f}] {p['wall_us']['n_favour']}/{BLOCKS} | "
          f"ccpu {p['ccpu_us']['med_pct']:>7.2f}% ({p['ccpu_us']['med_us']:+.1f}us) | "
          f"scpu {p['scpu_us']['med_pct']:>7.2f}% ({p['scpu_us']['med_us']:+.1f}us)")
raw.close(); raw_l.close(); raw_h.close()
