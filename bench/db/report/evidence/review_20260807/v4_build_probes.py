#!/usr/bin/env python3
"""Build three 10M-document probes that differ ONLY in the _id key shape.

zz_rev_oid  _id = the original ObjectId          (what Claim B actually measured)
zz_rev_str  _id = "base|<node_id>"               (the string natural key)
zz_rev_sub  _id = {tree_id, node_id}             (the sub-document natural key)

Payload is identical in all three: the seven fields get_node projects.
"""
import time
from pymongo import MongoClient

cli = MongoClient("mongodb://localhost:57017", directConnection=True)
db = cli["bench"]
P = {"node_id": 1, "parent_id": 1, "depth": 1, "title": 1,
     "summary": 1, "start_index": 1, "end_index": 1}

SPECS = [
    ("zz_rev_oid", dict(P)),
    ("zz_rev_str", dict(P, _id={"$concat": ["$tree_id", "|", "$node_id"]})),
    ("zz_rev_sub", dict(P, _id={"tree_id": "$tree_id", "node_id": "$node_id"})),
]
for name, proj in SPECS:
    t0 = time.time()
    db[name].drop()
    db["layout2_view"].aggregate([{"$project": proj}, {"$out": name}],
                                 allowDiskUse=True)
    st = db.command("collStats", name)
    print(f"{name}: {st['count']} docs avgObjSize {st.get('avgObjSize')} "
          f"storage {st['storageSize']/1e6:.0f}MB idIndex {st['indexSizes']['_id_']/1e6:.1f}MB "
          f"built in {time.time()-t0:.0f}s", flush=True)
print("DONE")
