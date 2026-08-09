#!/usr/bin/env bash
# Run MongoDB's own express jstests against a server with MONGO_EXPRESS_PREFIX_SCAN on.
#
# resmoke needs a Python environment this checkout does not have (structlog et al), so the
# tests are driven directly with the built shell instead. That loses resmoke's fixtures and
# passthrough overrides, so it is not a substitute for a real resmoke run -- but these
# particular tests are standalone-friendly, and the question being asked is narrow: does
# turning the gate on break MongoDB's existing coverage of the express path.
#
# Each test runs twice, gated and ungated, and the pass/fail is compared. A test that fails
# on BOTH is a pre-existing failure of this standalone harness, not a regression, and is
# reported as such rather than counted against the change.
set -u

BINARY=${1:?usage: run_express_jstests.sh <mongod> <mongo-shell> [scratch]}
SHELL_BIN=${2:?usage: run_express_jstests.sh <mongod> <mongo-shell> [scratch]}
SRC=${3:?usage: run_express_jstests.sh <mongod> <mongo-shell> <src-root> [scratch]}
SCRATCH=${4:-/tmp/mongo-getchildren-jstest}

TESTS=$(cd "$SRC" && ls jstests/core/index/express*.js 2>/dev/null)
[ -z "$TESTS" ] && { echo "no express jstests found under $SRC"; exit 1; }

start_mongod() {  # port, gate
  local port=$1 gate=$2 dbp="$SCRATCH/db$1"
  rm -rf "$dbp"; mkdir -p "$dbp"
  if [ "$gate" = "on" ]; then export MONGO_EXPRESS_PREFIX_SCAN=1; else unset MONGO_EXPRESS_PREFIX_SCAN; fi
  "$BINARY" --port "$port" --dbpath "$dbp" --bind_ip 127.0.0.1 --wiredTigerCacheSizeGB 2 \
            --logpath "$SCRATCH/mongod$port.log" --setParameter diagnosticDataCollectionEnabled=false &
  echo $! > "$SCRATCH/pid$port"
  for _ in $(seq 1 120); do
    "$SHELL_BIN" --port "$port" --quiet --eval 'quit(0)' >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  echo "mongod on $port did not start"; return 1
}

mkdir -p "$SCRATCH"
start_mongod 57101 on  || exit 1
start_mongod 57102 off || exit 1

pass=0; regress=0; both=0
printf '%-58s %-8s %-8s %s\n' TEST GATED UNGATED VERDICT
for t in $TESTS; do
  ( cd "$SRC" && "$SHELL_BIN" --port 57101 --quiet "$t" ) >"$SCRATCH/g.out" 2>&1; g=$?
  ( cd "$SRC" && "$SHELL_BIN" --port 57102 --quiet "$t" ) >"$SCRATCH/p.out" 2>&1; p=$?
  if [ $g -eq 0 ] && [ $p -eq 0 ]; then v=ok; pass=$((pass+1))
  elif [ $g -ne 0 ] && [ $p -eq 0 ]; then v="REGRESSION"; regress=$((regress+1))
  else v="fails both (harness, not the change)"; both=$((both+1)); fi
  printf '%-58s %-8s %-8s %s\n' "$(basename "$t")" "$g" "$p" "$v"
  [ "$v" = "REGRESSION" ] && { echo "---- gated output ----"; tail -25 "$SCRATCH/g.out"; }
done

for port in 57101 57102; do
  [ -f "$SCRATCH/pid$port" ] && kill "$(cat "$SCRATCH/pid$port")" 2>/dev/null
done
wait 2>/dev/null

echo
echo "passed both: $pass   regressions: $regress   failing on both: $both"
[ $regress -eq 0 ] || exit 1
