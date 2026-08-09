#!/usr/bin/env python3
"""Independent verification: what PyMongo actually puts on the wire vs the raw
client, and what the server actually plans for each _id shape."""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/junyao/code/pageindex/ConDB/bench/db")
from bson import ObjectId  # noqa
from pymongo import MongoClient, monitoring
import bson

NODES = "layout2_view"
NODE_PROJ = {"_id": 0, "node_id": 1, "parent_id": 1, "depth": 1,
             "title": 1, "summary": 1, "start_index": 1, "end_index": 1}

CAPTURED = []


class Cap(monitoring.CommandListener):
    def started(self, e):
        CAPTURED.append(("started", e.command_name, e.command))

    def succeeded(self, e):
        CAPTURED.append(("ok", e.command_name, e.reply))

    def failed(self, e):
        CAPTURED.append(("fail", e.command_name, None))


cli = MongoClient("mongodb://localhost:57017", directConnection=True,
                  maxPoolSize=1, minPoolSize=1, event_listeners=[Cap()])
db = cli["bench"]
coll = db[NODES]

nid = next(iter(db["layout_shared_text"].find({}, {"_id": 1}).sort([("_id", 1)]).limit(1)))["_id"]
oid = coll.find_one({"tree_id": "base", "node_id": nid}, {"_id": 1})["_id"]
print("sample node_id", nid, "oid", oid)

CAPTURED.clear()
r1 = coll.find_one({"tree_id": "base", "node_id": nid}, NODE_PROJ, hint="allops_tree_node")
find_one_cmd = [c for c in CAPTURED if c[0] == "started" and c[1] == "find"][-1][2]
find_one_reply = [c for c in CAPTURED if c[0] == "ok" and c[1] == "find"][-1][2]

cmd_node = {"find": NODES,
            "filter": {"tree_id": "base", "node_id": nid},
            "hint": "allops_tree_node", "projection": NODE_PROJ,
            "limit": 1, "singleBatch": True}
CAPTURED.clear()
r2 = db.command(dict(cmd_node))
db_cmd = [c for c in CAPTURED if c[0] == "started" and c[1] == "find"][-1][2]
db_cmd_reply = [c for c in CAPTURED if c[0] == "ok" and c[1] == "find"][-1][2]

raw_cmd = dict(cmd_node, **{"$db": "bench"})

def dump(label, d):
    print(f"--- {label} keys={list(d.keys())}")
    print("   ", json.dumps({k: (str(v)[:80]) for k, v in d.items()}, default=str))
    try:
        print("    encoded bytes:", len(bson.encode(dict(d))))
    except Exception as ex:
        print("    encode failed", ex)

dump("pymongo find_one  -> wire", find_one_cmd)
dump("pymongo db.command-> wire", db_cmd)
dump("raw client        -> wire", raw_cmd)
print()
print("find_one reply keys :", list(find_one_reply.keys()))
print("db.command reply keys:", list(db_cmd_reply.keys()))
print("equal docs:", r1 == r2["cursor"]["firstBatch"][0])

# ------------------------------------------------------------------ explains
print("\n================ EXPLAIN, REAL 10M COLLECTION ================")
for label, cmd in (
    ("get_node filter + hint", {"find": NODES,
                                "filter": {"tree_id": "base", "node_id": nid},
                                "hint": "allops_tree_node",
                                "projection": NODE_PROJ,
                                "limit": 1, "singleBatch": True}),
    ("get_node filter, no hint", {"find": NODES,
                                  "filter": {"tree_id": "base", "node_id": nid},
                                  "projection": NODE_PROJ,
                                  "limit": 1, "singleBatch": True}),
    ("_id: ObjectId (the measured IDHACK arm)",
     {"find": NODES, "filter": {"_id": oid}, "projection": NODE_PROJ,
      "limit": 1, "singleBatch": True}),
    ("_id: ObjectId, no projection",
     {"find": NODES, "filter": {"_id": oid}}),
):
    ex = db.command({"explain": cmd, "verbosity": "executionStats"})
    wp = ex["queryPlanner"]["winningPlan"]
    es = ex["executionStats"]
    print(f"\n{label}\n  explainVersion={ex.get('explainVersion')} "
          f"winningPlan={json.dumps(wp)[:300]}")
    print(f"  nReturned={es['nReturned']} totalKeysExamined={es['totalKeysExamined']} "
          f"totalDocsExamined={es['totalDocsExamined']} works={es.get('totalKeysExamined')}")

# ------------------------------------------------------- probe: natural key _id
print("\n================ EXPLAIN, NATURAL-KEY _id PROBE ================")
probe = db["zz_review_idhack_probe"]
probe.drop()
probe.insert_many(
    [{"_id": {"tree_id": "base", "node_id": f"{i:06d}"}, "v": i} for i in range(1000)]
    + [{"_id": f"base|{i:06d}", "v": i} for i in range(1000)])
for label, flt, hint in (
    ("subdoc _id, no hint", {"_id": {"tree_id": "base", "node_id": "000500"}}, None),
    ("subdoc _id, field order REVERSED",
     {"_id": {"node_id": "000500", "tree_id": "base"}}, None),
    ("string _id, no hint", {"_id": "base|000500"}, None),
    ("string _id, hint _id_", {"_id": "base|000500"}, "_id_"),
    ("subdoc _id, with projection", {"_id": {"tree_id": "base", "node_id": "000500"}}, None),
):
    cmd = {"find": "zz_review_idhack_probe", "filter": flt}
    if hint:
        cmd["hint"] = hint
    if "projection" in label:
        cmd["projection"] = {"_id": 0, "v": 1}
    ex = db.command({"explain": cmd, "verbosity": "executionStats"})
    wp = ex["queryPlanner"]["winningPlan"]
    es = ex["executionStats"]
    print(f"{label:38} explainVer={ex.get('explainVersion')} nReturned={es['nReturned']} "
          f"plan={json.dumps(wp)[:200]}")
# does the sub-document actually match?
print("subdoc find result:", list(probe.find({"_id": {"tree_id": "base", "node_id": "000500"}})))
print("subdoc reversed  :", list(probe.find({"_id": {"node_id": "000500", "tree_id": "base"}})))
probe.drop()
print("probe dropped:", "zz_review_idhack_probe" not in db.list_collection_names())
