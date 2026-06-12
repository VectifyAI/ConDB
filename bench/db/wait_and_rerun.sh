#!/usr/bin/env bash
# Wait until the shared host is idle (the competing CPU job is gone and load has
# settled), then re-run the full benchmark suite so the numbers are not degraded
# by contention. Logs progress to runs/wait_rerun.log.
set -u
cd "$(dirname "$0")/../.."
LOG=bench/db/runs/wait_rerun.log
CORES=$(nproc)
mkdir -p bench/db/runs
echo "$(date '+%F %T') armed: cores=$CORES, waiting for bench_rebuilt to finish" > "$LOG"

# Require the competitor to be absent for several consecutive checks, so we do
# not start during a short gap between its configs.
absent=0
while [ "$absent" -lt 5 ]; do
    if pgrep -x bench_rebuilt >/dev/null 2>&1; then
        absent=0
    else
        absent=$((absent + 1))
    fi
    load=$(cut -d' ' -f1 /proc/loadavg)
    echo "$(date '+%F %T') load1=$load absent_streak=$absent" >> "$LOG"
    sleep 90
done

# Also wait for the 1-minute load to fall below half the core count.
thr=$(awk "BEGIN{printf \"%.1f\", $CORES*0.5}")
while awk -v t="$thr" '{exit !($1+0 > t+0)}' /proc/loadavg; do
    echo "$(date '+%F %T') waiting for load < $thr (now $(cut -d' ' -f1 /proc/loadavg))" >> "$LOG"
    sleep 60
done
sleep 30

echo "$(date '+%F %T') host idle, ensuring containers" >> "$LOG"
docker start condb_pg condb_mongo >/dev/null 2>&1
sleep 5

echo "$(date '+%F %T') starting full benchmark suite" >> "$LOG"
bash bench/db/run_all.sh full >> "$LOG" 2>&1
echo "$(date '+%F %T') DONE" >> "$LOG"
