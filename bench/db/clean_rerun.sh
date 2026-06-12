#!/usr/bin/env bash
# Re-run the whole suite in a single sustained idle window, verifying the host
# stays idle for the entire run. If the competing job returns mid-run, the run
# is discarded and retried, so the final result set comes from one coherent
# contention-free run. Logs to runs/clean_rerun.log.
set -u
cd "$(dirname "$0")/../.."
LOG=bench/db/runs/clean_rerun.log
CORES=$(nproc)
thr=$(awk "BEGIN{printf \"%.1f\", $CORES*0.5}")
MARK=/tmp/condb_run_marker
CONTAM=/tmp/condb_contaminated
attempt=0
echo "$(date '+%F %T') clean-rerun armed, cores=$CORES thr=$thr" > "$LOG"

while true; do
    attempt=$((attempt + 1))

    # Wait for sustained idle: competitor absent and load below threshold.
    stable=0
    while [ "$stable" -lt 5 ]; do
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
        sleep 45
    done
    echo "$(date '+%F %T') attempt $attempt: idle (load $(cut -d' ' -f1 /proc/loadavg)), starting full run" >> "$LOG"

    docker start condb_pg condb_mongo >/dev/null 2>&1
    sleep 5
    rm -f "$CONTAM"
    touch "$MARK"

    # Sampler: flag contamination if the competitor reappears during the run.
    ( while [ -f "$MARK" ]; do
          pgrep -x bench_rebuilt >/dev/null 2>&1 && touch "$CONTAM"
          sleep 15
      done ) &
    sampler=$!

    bash bench/db/run_all.sh full >> "$LOG" 2>&1

    rm -f "$MARK"
    wait "$sampler" 2>/dev/null || true

    if [ -f "$CONTAM" ]; then
        echo "$(date '+%F %T') attempt $attempt CONTAMINATED mid-run, discarding and retrying" >> "$LOG"
        sleep 60
    else
        echo "$(date '+%F %T') attempt $attempt CLEAN full run" >> "$LOG"
        break
    fi
done
echo "$(date '+%F %T') DONE" >> "$LOG"
