#!/usr/bin/env python3
"""internalQueryFrameworkControl is GLOBAL.  Claim C measured only get_node and
get_children.  This measures the other two operations of the same workload
(bench_all_ops_layouts.py: get_subtree, get_entity) under both settings, paired,
with element-wise correctness checks.  Parameter restored in a finally."""
import gc, json, statistics, sys, time
from pathlib import Path
sys.path.insert(0, "/home/junyao/code/pageindex/ConDB/bench/db")
from pymongo import MongoClient
from bench_opwin_20260807 import thread_table, mongod_pid

BLOCKS = int(sys.argv[1]); OUT = Path(sys.argv[2])
kw = dict(maxPoolSize=1, minPoolSize=1, directConnection=True)
cli = MongoClient("mongodb://172.17.0.3:27017", **kw)
db, adm = cli["bench"], cli["admin"]
comm = f"conn{db.command('hello')['connectionId']}"
pid = mongod_pid("condb_mongo")
nodes, text = db["layout2_view"], db["layout_shared_text"]

samples = json.loads(Path("/home/junyao/code/pageindex/ConDB/bench/db/runs/"
                          "report_3eng_20260716/layout_2v3_postgres_10m_final.json"
                          ).read_text())["samples"]
tree_ids = [s["path"].rsplit("/", 1)[-1] for s in samples[:16]]
ent_ids = [r["_id"] for r in text.find({}, {"_id": 1}).sort([("_id", 1)]).limit(64)]


def get_subtree(i):
    nid = tree_ids[i % len(tree_ids)]
    root = nodes.find_one({"tree_id": "base", "node_id": nid}, {"_id": 0, "path": 1},
                          hint="allops_tree_node")
    if root is None:
        return ()
    lo, hi = root["path"] + "/", root["path"] + "0"
    cur = (nodes.find({"path": {"$gte": lo, "$lt": hi}},
                      {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
           .sort([("path", 1), ("node_id", 1)]).hint("layout2_rootcause_exact_cover"))
    return tuple((r.get("node_id"), r.get("title"), r.get("summary")) for r in cur)


def get_entity(i):
    r = text.find_one({"_id": ent_ids[i % len(ent_ids)]}, {"_id": 1, "text": 1})
    return (ent_ids[i % len(ent_ids)], r.get("text")) if r else ()


OPS = {"get_subtree": (get_subtree, 12), "get_entity": (get_entity, 400)}
SET = ("forceClassicEngine", "trySbeEngine")
orig = adm.command("getParameter", 1, internalQueryFrameworkControl=1)["internalQueryFrameworkControl"]
orig_pr = adm.command("profile", -1)
res = {s: {o: [] for o in OPS} for s in SET}
extra = {}
try:
    for d in ("admin", "bench"):
        cli[d].command("profile", 0, slowms=100)
    adm.command("setParameter", 1, internalQueryFrameworkControl="forceClassicEngine")
    ref = {o: [OPS[o][0](i) for i in range(8)] for o in OPS}
    adm.command("setParameter", 1, internalQueryFrameworkControl="trySbeEngine")
    got = {o: [OPS[o][0](i) for i in range(8)] for o in OPS}
    extra["correctness"] = {o: {"mismatches": sum(1 for a, b in zip(ref[o], got[o]) if a != b),
                                "rows": sum(len(a) if isinstance(a, tuple) and a and isinstance(a[0], tuple) else 1 for a in ref[o])}
                            for o in OPS}
    print("correctness:", extra["correctness"])
    for s in SET:
        adm.command("setParameter", 1, internalQueryFrameworkControl=s)
        ex = db.command({"explain": {"find": "layout2_view",
                                     "filter": {"path": {"$gte": "/000006/", "$lt": "/0000060"}},
                                     "sort": {"path": 1, "node_id": 1},
                                     "projection": {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
                                     "hint": "layout2_rootcause_exact_cover"},
                         "verbosity": "queryPlanner"})
        extra["plan_subtree_" + s] = {"explainVersion": ex.get("explainVersion"),
                                      "plan": json.dumps(ex["queryPlanner"]["winningPlan"])[:300]}
        ex2 = db.command({"explain": {"find": "layout_shared_text", "filter": {"_id": ent_ids[0]},
                                      "projection": {"_id": 1, "text": 1}},
                          "verbosity": "queryPlanner"})
        extra["plan_entity_" + s] = {"explainVersion": ex2.get("explainVersion"),
                                     "plan": json.dumps(ex2["queryPlanner"]["winningPlan"])[:200]}
        print(s, "subtree:", extra["plan_subtree_" + s]["explainVersion"],
              extra["plan_subtree_" + s]["plan"][:150])
        print(s, "entity :", extra["plan_entity_" + s]["explainVersion"],
              extra["plan_entity_" + s]["plan"][:120])
    for blk in range(BLOCKS):
        for s in (SET if blk % 2 == 0 else SET[::-1]):
            adm.command("setParameter", 1, internalQueryFrameworkControl=s)
            for o, (fn, n) in OPS.items():
                for i in range(max(4, n // 4)):
                    fn(i)
            for o, (fn, n) in OPS.items():
                before = thread_table(pid); gc.disable()
                t0 = time.perf_counter()
                for k in range(n):
                    fn(k)
                t1 = time.perf_counter(); gc.enable(); after = thread_table(pid)
                res[s][o].append({"wall_us": (t1-t0)*1e6/n,
                                  "scpu_us": (after.get(comm, 0)-before.get(comm, 0))/1e3/n})
        print(f"block {blk+1}/{BLOCKS}", flush=True)
finally:
    adm.command("setParameter", 1, internalQueryFrameworkControl=orig)
    g = adm.command("getParameter", 1, internalQueryFrameworkControl=1)["internalQueryFrameworkControl"]
    for d in ("admin", "bench"):
        cli[d].command("profile", 0, slowms=orig_pr["slowms"])
    pr = adm.command("profile", -1)
    print("RESTORED:", g, pr)
    assert g == orig and pr["slowms"] == orig_pr["slowms"]

out = {"blocks": BLOCKS, "extra": extra, "raw": res, "summary": {}, "paired": {}}
for s in SET:
    out["summary"][s] = {o: {k: round(statistics.median(r[k] for r in res[s][o]), 2)
                             for k in ("wall_us", "scpu_us")} for o in OPS}
for o in OPS:
    e = {}
    for k in ("wall_us", "scpu_us"):
        d = [res["trySbeEngine"][o][b][k] - res["forceClassicEngine"][o][b][k] for b in range(BLOCKS)]
        p = [100*x/res["forceClassicEngine"][o][b][k] for b, x in enumerate(d)]
        e[k] = {"med_us": round(statistics.median(d), 2), "med_pct": round(statistics.median(p), 2),
                "min_pct": round(min(p), 2), "max_pct": round(max(p), 2),
                "n_favour_sbe": sum(1 for x in d if x < 0)}
    out["paired"][o] = e
OUT.write_text(json.dumps(out, indent=2))
print()
for s in SET:
    for o in OPS:
        v = out["summary"][s][o]
        print(f"{s:<20}{o:<14} wall {v['wall_us']:>10.1f} scpu {v['scpu_us']:>10.1f}")
print()
for o, p in out["paired"].items():
    print(f"SBE vs classic {o:<14} wall {p['wall_us']['med_pct']:>8.2f}% "
          f"[{p['wall_us']['min_pct']:.1f},{p['wall_us']['max_pct']:.1f}] {p['wall_us']['n_favour_sbe']}/{BLOCKS} | "
          f"scpu {p['scpu_us']['med_pct']:>8.2f}% [{p['scpu_us']['min_pct']:.1f},{p['scpu_us']['max_pct']:.1f}] "
          f"{p['scpu_us']['n_favour_sbe']}/{BLOCKS}")
