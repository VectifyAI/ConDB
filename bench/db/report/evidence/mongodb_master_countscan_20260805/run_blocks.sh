#!/usr/bin/env bash
set -euo pipefail

out_dir=/tmp/mongo-count-bench/custom-final-nolog
benchmark_filter='CountQueryBenchmark/NonMultikeyCountScan/500000/64$'
baseline=/tmp/mongo-count-query-bm-nolog-disabled
candidate=/tmp/mongo-count-query-bm-nolog-enabled

mkdir -p "$out_dir"

orders=(
    "baseline candidate"
    "candidate baseline"
    "candidate baseline"
    "baseline candidate"
    "baseline candidate"
    "candidate baseline"
    "baseline candidate"
    "candidate baseline"
    "baseline candidate"
    "candidate baseline"
)

run_arm() {
    local pair=$1
    local arm=$2
    local binary
    if [[ "$arm" == baseline ]]; then
        binary=$baseline
    else
        binary=$candidate
    fi

    local stem
    stem=$(printf 'pair%02d_%s' "$pair" "$arm")
    "$binary" \
        --benchmark_filter="$benchmark_filter" \
        --benchmark_min_time=0.01 \
        --benchmark_repetitions=5 \
        --benchmark_report_aggregates_only=false \
        --benchmark_out="$out_dir/$stem.json" \
        --benchmark_out_format=json \
        >"$out_dir/$stem.log" 2>&1
    printf 'completed %s\n' "$stem"
}

for index in "${!orders[@]}"; do
    pair=$((index + 1))
    read -r first second <<<"${orders[$index]}"
    run_arm "$pair" "$first"
    run_arm "$pair" "$second"
done
