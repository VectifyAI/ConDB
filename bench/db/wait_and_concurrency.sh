#!/usr/bin/env bash
# Wait for a sustained idle window, then run only the concurrency benchmark
# (it is short and the most sensitive to whole-host contention). Records load
# before and after so we can confirm the window stayed clean.
set -u
cd "$(dirname "$0")/../.."
LOG=bench/db/runs/wait_concurrency.log
CORES=$(nproc)
thr=$(awk "BEGIN{printf \"%.1f\", $CORES*0.5}")
echo "$(date '+%F %T') armed: cores=$CORES thr=$thr" > "$LOG"

# Require the competitor absent and load below threshold for a sustained streak.
stable=0
while [ "$stable" -lt 6 ]; do
    if pgrep -x bench_rebuilt >/dev/null 2>&1; then
        stable=0
    else
        l=$(cut -d' ' -f1 /proc/loadavg)
        if awk -v t="$thr" -v l="$l" 'BEGIN{exit !(l+0 < t+0)}'; then
            stable=$((stable + 1))
        else
            stable=0
        fi
    fi
    echo "$(date '+%F %T') load1=$(cut -d' ' -f1 /proc/loadavg) stable=$stable" >> "$LOG"
    sleep 45
done

docker start condb_pg condb_mongo >/dev/null 2>&1
sleep 5
echo "$(date '+%F %T') idle confirmed (load $(cut -d' ' -f1 /proc/loadavg)), running concurrency" >> "$LOG"
bench/db/.venv/bin/python bench/db/bench_concurrency.py --doc bench/db/data/medium.json \
    --duration 5 --concurrency 1 2 4 8 16 32 64 \
    --out bench/db/runs/concurrency_medium.json >> "$LOG" 2>&1
echo "$(date '+%F %T') post-run load1=$(cut -d' ' -f1 /proc/loadavg) bench_rebuilt=$(pgrep -x bench_rebuilt >/dev/null && echo present || echo absent)" >> "$LOG"
echo "$(date '+%F %T') DONE" >> "$LOG"
