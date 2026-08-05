#!/bin/bash
# Usage: run_parity.sh <label> <mongod-binary> <port>
# Starts a standalone mongod under taskset with forceClassicEngine, runs the
# capture script with the HEAD-built shell, writes <label>.capture.json.
set -u
LABEL="$1"
MONGOD="$2"
PORT="$3"
OUT=/tmp/mongo-count-minimal-validation
SHELL_BIN=/home/junyao/code/mongo/bazel-out/k8-opt/bin/src/mongo/shell/mongo
DBPATH="$OUT/parity-db-$LABEL"
PIN="1-47,49-95"

rm -rf "$DBPATH"
mkdir -p "$DBPATH"

echo "[$LABEL] mongod=$MONGOD port=$PORT"
taskset -c "$PIN" "$MONGOD" \
    --dbpath "$DBPATH" --port "$PORT" --bind_ip 127.0.0.1 \
    --setParameter internalQueryFrameworkControl=forceClassicEngine \
    --logpath "$OUT/$LABEL-mongod.log" --fork >"$OUT/$LABEL-mongod-start.log" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
    echo "[$LABEL] mongod failed to start (rc=$rc)"
    cat "$OUT/$LABEL-mongod-start.log"
    exit 1
fi

for i in $(seq 1 60); do
    if taskset -c "$PIN" "$SHELL_BIN" --port "$PORT" --quiet --eval 'db.adminCommand({ping:1})' \
        >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

taskset -c "$PIN" "$SHELL_BIN" --port "$PORT" --quiet "$OUT/explain_parity.js" \
    >"$OUT/$LABEL-capture.raw" 2>&1
crc=$?
echo "[$LABEL] capture rc=$crc"

grep '^CAPTURE:' "$OUT/$LABEL-capture.raw" | sed 's/^CAPTURE://' > "$OUT/$LABEL-capture.json"
echo "[$LABEL] capture bytes: $(wc -c < "$OUT/$LABEL-capture.json")"

taskset -c "$PIN" "$SHELL_BIN" --port "$PORT" --quiet --eval \
    'db.getSiblingDB("admin").shutdownServer({force:true})' >/dev/null 2>&1
sleep 2
pkill -f "port $PORT" >/dev/null 2>&1
exit $crc
