#!/usr/bin/env bash
# Minimal sharded cluster, purely to answer one question: does an exhaust cursor stream through
# mongos? The report treats exhaust as unavailable behind a router, which excludes every sharded
# deployment from the ~40% overlap it buys -- but that restriction is imposed by the driver, so no
# experiment through PyMongo can tell "mongos cannot" from "the driver will not ask".
#
# Small on purpose: the question is about the wire protocol, not about throughput. Enough documents
# to need several batches is all that is required.
#
# Ports 57020 config / 57021 shard / 57022 mongos, all bound to 127.0.0.1.
set -euo pipefail

BIN=/tmp/mongo-subtree-stream/bazel-bin/src/mongo
ROOT=/tmp/subtree-sharded
DOCS=${DOCS:-20000}

echo "=== tearing down any previous cluster ==="
for p in 57022 57021 57020; do
  pkill -u "$(id -u)" -f "port $p" 2>/dev/null || true
done
sleep 2
rm -rf "$ROOT"
mkdir -p "$ROOT"/{cfg,shard0,log}

echo "=== config server (57020) ==="
"$BIN/db/mongod" --configsvr --replSet cfg --port 57020 --dbpath "$ROOT/cfg" \
  --bind_ip 127.0.0.1 --wiredTigerCacheSizeGB 1 --logpath "$ROOT/log/cfg.log" \
  --setParameter diagnosticDataCollectionEnabled=false --fork >/dev/null

echo "=== shard (57021) ==="
"$BIN/db/mongod" --shardsvr --replSet shard0 --port 57021 --dbpath "$ROOT/shard0" \
  --bind_ip 127.0.0.1 --wiredTigerCacheSizeGB 4 --logpath "$ROOT/log/shard0.log" \
  --setParameter diagnosticDataCollectionEnabled=false --fork >/dev/null

echo "=== initiating replica sets ==="
python3 - <<'PY'
import time
from pymongo import MongoClient
for port, name in ((57020, "cfg"), (57021, "shard0")):
    for _ in range(60):
        try:
            c = MongoClient(f"mongodb://127.0.0.1:{port}/?directConnection=true",
                            serverSelectionTimeoutMS=500)
            c.admin.command("ping")
            break
        except Exception:
            time.sleep(1)
    else:
        raise SystemExit(f"mongod on {port} never came up")
    try:
        c.admin.command({"replSetInitiate": {
            "_id": name, "members": [{"_id": 0, "host": f"127.0.0.1:{port}"}]}})
        print(f"  initiated {name}")
    except Exception as exc:
        print(f"  {name}: {exc}")
    # Wait for PRIMARY before moving on.
    for _ in range(60):
        st = c.admin.command("replSetGetStatus")
        if st["members"][0]["stateStr"] == "PRIMARY":
            print(f"  {name} PRIMARY")
            break
        time.sleep(1)
    else:
        raise SystemExit(f"{name} never reached PRIMARY")
PY

echo "=== mongos (57022) ==="
"$BIN/s/mongos" --configdb cfg/127.0.0.1:57020 --port 57022 --bind_ip 127.0.0.1 \
  --logpath "$ROOT/log/mongos.log" --fork >/dev/null

echo "=== adding shard and loading $DOCS documents ==="
DOCS=$DOCS python3 - <<'PY'
import os, time
from pymongo import MongoClient
s = None
for _ in range(60):
    try:
        s = MongoClient("mongodb://127.0.0.1:57022", serverSelectionTimeoutMS=500)
        s.admin.command("ping")
        break
    except Exception:
        time.sleep(1)
if s is None:
    raise SystemExit("mongos never came up")
print("  mongos hello msg:", s.admin.command("hello").get("msg"))
try:
    print("  addShard:", s.admin.command({"addShard": "shard0/127.0.0.1:57021"})["ok"])
except Exception as exc:
    print("  addShard:", exc)

db = s["bench"]
db["layout2_view"].drop()
n = int(os.environ["DOCS"])
batch, total = [], 0
for i in range(n):
    batch.append({
        "path": f"/000006/000075/000773/{i:08d}",
        "node_id": f"n{i}",
        "title": f"title-{i}",
        "summary": "summary text for node %d %s" % (i, " " * (i % 17)),
    })
    if len(batch) >= 2000:
        db["layout2_view"].insert_many(batch, ordered=False); total += len(batch); batch = []
if batch:
    db["layout2_view"].insert_many(batch, ordered=False); total += len(batch)
db["layout2_view"].create_index(
    [("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)],
    name="layout2_rootcause_exact_cover")
print(f"  loaded {total} docs, index built")
print("  shard status:", [sh["_id"] for sh in s["config"]["shards"].find()])
PY

echo "=== cluster up: mongos on 57022 ==="
