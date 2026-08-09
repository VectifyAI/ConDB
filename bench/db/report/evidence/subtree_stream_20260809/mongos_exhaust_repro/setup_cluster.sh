#!/usr/bin/env bash
# Minimal sharded cluster for the exhaust-through-mongos question. See README.md.
#
#   MONGO_BIN=/path/to/build/bin ./setup_cluster.sh            # bring up
#   ./setup_cluster.sh --teardown                              # tear down
#
# Config server 57020, shard0 57021, shard1 57023, mongos 57022, all on 127.0.0.1.
# Small on purpose: the question is about the wire protocol, not about throughput.
set -euo pipefail

BIN="${MONGO_BIN:?set MONGO_BIN to the directory holding mongod and mongos}"
ROOT="${EXHAUST_REPRO_ROOT:-/tmp/mongos-exhaust-repro}"
DOCS="${DOCS:-20000}"
SHARDS="${SHARDS:-2}"
PATH_PREFIX="/000006/000075/000773"

teardown() {
  for p in 57022 57023 57021 57020; do
    pkill -u "$(id -u)" -f "port $p" 2>/dev/null || true
  done
  sleep 2
  rm -rf "$ROOT"
  echo "torn down"
}

if [[ "${1:-}" == "--teardown" ]]; then teardown; exit 0; fi

for b in mongod mongos; do
  [[ -x "$BIN/$b" ]] || { echo "not executable: $BIN/$b" >&2; exit 1; }
done

teardown >/dev/null 2>&1 || true
mkdir -p "$ROOT"/{cfg,shard0,shard1,log}

echo "config server 57020"
"$BIN/mongod" --configsvr --replSet cfg --port 57020 --dbpath "$ROOT/cfg" \
  --bind_ip 127.0.0.1 --wiredTigerCacheSizeGB 1 --logpath "$ROOT/log/cfg.log" \
  --setParameter diagnosticDataCollectionEnabled=false --fork >/dev/null

echo "shard0 57021"
"$BIN/mongod" --shardsvr --replSet shard0 --port 57021 --dbpath "$ROOT/shard0" \
  --bind_ip 127.0.0.1 --wiredTigerCacheSizeGB 2 --logpath "$ROOT/log/shard0.log" \
  --setParameter diagnosticDataCollectionEnabled=false --fork >/dev/null

if [[ "$SHARDS" -ge 2 ]]; then
  echo "shard1 57023"
  "$BIN/mongod" --shardsvr --replSet shard1 --port 57023 --dbpath "$ROOT/shard1" \
    --bind_ip 127.0.0.1 --wiredTigerCacheSizeGB 2 --logpath "$ROOT/log/shard1.log" \
    --setParameter diagnosticDataCollectionEnabled=false --fork >/dev/null
fi

SHARDS="$SHARDS" python3 - <<'PY'
import os, time
from pymongo import MongoClient

def wait(port, name):
    for _ in range(90):
        try:
            c = MongoClient(f"mongodb://127.0.0.1:{port}/?directConnection=true",
                            serverSelectionTimeoutMS=500)
            c.admin.command("ping")
            return c
        except Exception:
            time.sleep(1)
    raise SystemExit(f"{name} on {port} never came up")

members = [(57020, "cfg"), (57021, "shard0")]
if int(os.environ["SHARDS"]) >= 2:
    members.append((57023, "shard1"))

for port, name in members:
    c = wait(port, name)
    try:
        c.admin.command({"replSetInitiate": {
            "_id": name, "members": [{"_id": 0, "host": f"127.0.0.1:{port}"}]}})
    except Exception as exc:
        if "already initialized" not in str(exc):
            print(f"  {name}: {exc}")
    for _ in range(90):
        if c.admin.command("replSetGetStatus")["members"][0]["stateStr"] == "PRIMARY":
            print(f"  {name} PRIMARY")
            break
        time.sleep(1)
    else:
        raise SystemExit(f"{name} never reached PRIMARY")
PY

echo "mongos 57022"
"$BIN/mongos" --configdb cfg/127.0.0.1:57020 --port 57022 --bind_ip 127.0.0.1 \
  --logpath "$ROOT/log/mongos.log" --fork >/dev/null

DOCS="$DOCS" SHARDS="$SHARDS" PATH_PREFIX="$PATH_PREFIX" python3 - <<'PY'
import os, time
from pymongo import MongoClient

for _ in range(90):
    try:
        s = MongoClient("mongodb://127.0.0.1:57022", serverSelectionTimeoutMS=500)
        s.admin.command("ping")
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("mongos never came up")

assert s.admin.command("hello").get("msg") == "isdbgrid", "not talking to a router"
nshards = int(os.environ["SHARDS"])
for i, port in enumerate((57021, 57023)[:nshards]):
    try:
        s.admin.command({"addShard": f"shard{i}/127.0.0.1:{port}"})
    except Exception as exc:
        print(f"  addShard shard{i}: {exc}")

prefix = os.environ["PATH_PREFIX"]
db = s["bench"]
db["layout2_view"].drop()
n = int(os.environ["DOCS"])
batch = []
for i in range(n):
    batch.append({"path": f"{prefix}/{i:08d}", "node_id": f"n{i}",
                  "title": f"title-{i}",
                  "summary": "summary text for node %d %s" % (i, " " * (i % 17))})
    if len(batch) >= 2000:
        db["layout2_view"].insert_many(batch, ordered=False); batch = []
if batch:
    db["layout2_view"].insert_many(batch, ordered=False)
db["layout2_view"].create_index(
    [("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)], name="cover")

if nshards >= 2:
    # Split the range so mongos has to merge results from both shards.
    s.admin.command({"enableSharding": "bench"})
    s.admin.command({"shardCollection": "bench.layout2_view",
                     "key": {"path": 1, "node_id": 1}})
    mid = {"path": f"{prefix}/{n // 2:08d}", "node_id": f"n{n // 2}"}
    s.admin.command({"split": "bench.layout2_view", "middle": mid})
    shards = sorted(sh["_id"] for sh in s["config"]["shards"].find())
    s.admin.command({"moveChunk": "bench.layout2_view", "find": mid, "to": shards[-1]})
    import collections
    dist = collections.Counter(ch["shard"] for ch in s["config"]["chunks"].find())
    print("  chunks per shard:", dict(dist))

print(f"  {db['layout2_view'].count_documents({})} documents via mongos")
PY

echo
echo "mongos ready on 127.0.0.1:57022"
echo "  python3 exhaust_probe.py --port 57022 --filter-path $PATH_PREFIX"
echo "  python3 exhaust_probe.py --port 57022 --filter-path $PATH_PREFIX --measure"
