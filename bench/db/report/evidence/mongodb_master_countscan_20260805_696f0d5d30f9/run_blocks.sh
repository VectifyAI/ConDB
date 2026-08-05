#!/usr/bin/env bash
set -euo pipefail

# This is a fixed-size paired campaign. There is no data-dependent stopping rule. An operational
# failure aborts nonzero and leaves partial outputs in place, which deliberately prevents a partial
# rerun: restart only after preserving the failed campaign and choosing a wholly fresh directory.

evidence_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
raw_dir="$evidence_dir/raw"
log_dir="$evidence_dir/logs"
campaign_file="$evidence_dir/campaign.json"
run_record="$evidence_dir/campaign_run.json"
analyzer="$evidence_dir/analyze.py"
runner="$evidence_dir/run_blocks.sh"
activation_patch="$evidence_dir/activation_disable.patch"
candidate_patch="$evidence_dir/candidate.patch"

benchmark_filter='CountQueryBenchmark/DirectNonDeduplicatingCountScan/400000/64$'
expected_run_name='CountQueryBenchmark/DirectNonDeduplicatingCountScan/400000/64'
cpu=0
repetitions=5

binary_B=/tmp/mongo-count-query-bm-696f0d5d-disabled
sha256_B=ed2bdc05a6188a0ebb6433923391417c183e3471df78516eac3523c2f825bebc
build_id_B=d14aea10c20ca862734eb72e930225a1e5ea263e
binary_C=/tmp/mongo-count-query-bm-696f0d5d-enabled
sha256_C=02628346e4357ab9a48d5c0dea0de68df4c0b2921ded3fb44c4f643eb5c043be
build_id_C=482d4815330592895592815012509a756b70ccf8

orders=(
    BC CB CB BC BC CB BC CB BC CB
    CB BC BC CB CB BC CB BC CB BC
)

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

for command_name in taskset sha256sum readelf awk find python3 date uname hostname; do
    command -v "$command_name" >/dev/null 2>&1 || fail "required command not found: $command_name"
done

[[ -f "$campaign_file" ]] || fail "missing frozen campaign: $campaign_file"
[[ -f "$analyzer" ]] || fail "missing analyzer: $analyzer"
[[ -f "$runner" ]] || fail "missing runner: $runner"
[[ -f "$activation_patch" ]] || fail "missing activation patch: $activation_patch"
[[ -f "$candidate_patch" ]] || fail "missing candidate patch: $candidate_patch"
[[ ! -e "$run_record" ]] || fail "refusing to overwrite existing run record: $run_record"
python3 "$analyzer" --validate-campaign-only

mkdir -p -- "$raw_dir" "$log_dir"
if [[ -n "$(find "$raw_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    fail "raw directory is not empty; partial reruns are forbidden: $raw_dir"
fi
if [[ -n "$(find "$log_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    fail "log directory is not empty; partial reruns are forbidden: $log_dir"
fi

campaign_sha256_before=$(sha256sum -- "$campaign_file" | awk '{print $1}')
runner_sha256_before=$(sha256sum -- "$runner" | awk '{print $1}')
analyzer_sha256_before=$(sha256sum -- "$analyzer" | awk '{print $1}')
activation_patch_sha256_before=$(sha256sum -- "$activation_patch" | awk '{print $1}')
candidate_patch_sha256_before=$(sha256sum -- "$candidate_patch" | awk '{print $1}')
started_at=$(date --iso-8601=seconds)
host_name=$(hostname)
kernel=$(uname -srmo)
governor=unavailable
if [[ -r /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]]; then
    IFS= read -r governor </sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
fi

verify_identity() {
    local phase=$1
    local arm=$2
    local path=$3
    local expected_sha256=$4
    local expected_build_id=$5
    local actual_sha256
    local actual_build_id

    [[ -x "$path" ]] || fail "$phase arm $arm binary is missing or not executable: $path"
    actual_sha256=$(sha256sum -- "$path" | awk '{print $1}')
    [[ "$actual_sha256" == "$expected_sha256" ]] ||
        fail "$phase arm $arm SHA-256 mismatch: expected $expected_sha256, got $actual_sha256"
    # Consume the complete readelf stream: exiting awk early can turn a valid read into SIGPIPE
    # under pipefail on binaries with many ELF notes.
    actual_build_id=$(readelf -n -- "$path" 2>/dev/null | awk '/Build ID:/ && !found {id=$3; found=1} END {print id}')
    [[ -n "$actual_build_id" ]] || fail "$phase arm $arm has no readable GNU Build ID: $path"
    [[ "$actual_build_id" == "$expected_build_id" ]] ||
        fail "$phase arm $arm Build ID mismatch: expected $expected_build_id, got $actual_build_id"
    printf 'verified %s identity for arm %s\n' "$phase" "$arm"
}

verify_identity preflight B "$binary_B" "$sha256_B" "$build_id_B"
verify_identity preflight C "$binary_C" "$sha256_C" "$build_id_C"
taskset -c "$cpu" true >/dev/null 2>&1 || fail "CPU $cpu is not available to taskset"

run_arm() {
    local pair=$1
    local arm=$2
    local position=$3
    local binary
    local label

    case "$arm" in
        B)
            binary=$binary_B
            label=disabled_control
            ;;
        C)
            binary=$binary_C
            label=enabled_candidate
            ;;
        *)
            fail "unknown arm: $arm"
            ;;
    esac

    local stem
    stem=$(printf 'pair%02d_%s_%s' "$pair" "$arm" "$label")
    local raw_path="$raw_dir/$stem.json"
    local log_path="$log_dir/$stem.log"
    [[ ! -e "$raw_path" && ! -e "$log_path" ]] || fail "refusing to overwrite block $stem"

    printf 'starting pair=%02d position=%d arm=%s (%s)\n' "$pair" "$position" "$arm" "$label"
    taskset -c "$cpu" "$binary" \
        --benchmark_filter="$benchmark_filter" \
        --benchmark_min_time=0.01 \
        --benchmark_repetitions="$repetitions" \
        --benchmark_report_aggregates_only=false \
        --benchmark_out="$raw_path" \
        --benchmark_out_format=json \
        >"$log_path" 2>&1

    [[ -s "$raw_path" ]] || fail "benchmark produced no JSON for $stem"
    [[ -s "$log_path" ]] || fail "benchmark produced no log for $stem"
    printf 'completed pair=%02d position=%d arm=%s\n' "$pair" "$position" "$arm"
}

for index in "${!orders[@]}"; do
    pair=$((index + 1))
    order=${orders[$index]}
    first=${order:0:1}
    second=${order:1:1}
    run_arm "$pair" "$first" 1
    run_arm "$pair" "$second" 2
done

raw_count=$(find "$raw_dir" -mindepth 1 -maxdepth 1 -type f -name '*.json' -print | awk 'END {print NR + 0}')
log_count=$(find "$log_dir" -mindepth 1 -maxdepth 1 -type f -name '*.log' -print | awk 'END {print NR + 0}')
[[ "$raw_count" -eq 40 ]] || fail "expected 40 JSON blocks, found $raw_count"
[[ "$log_count" -eq 40 ]] || fail "expected 40 log blocks, found $log_count"

verify_identity postflight B "$binary_B" "$sha256_B" "$build_id_B"
verify_identity postflight C "$binary_C" "$sha256_C" "$build_id_C"
campaign_sha256_after=$(sha256sum -- "$campaign_file" | awk '{print $1}')
[[ "$campaign_sha256_after" == "$campaign_sha256_before" ]] ||
    fail "campaign.json changed during execution"
runner_sha256_after=$(sha256sum -- "$runner" | awk '{print $1}')
analyzer_sha256_after=$(sha256sum -- "$analyzer" | awk '{print $1}')
activation_patch_sha256_after=$(sha256sum -- "$activation_patch" | awk '{print $1}')
candidate_patch_sha256_after=$(sha256sum -- "$candidate_patch" | awk '{print $1}')
[[ "$runner_sha256_after" == "$runner_sha256_before" ]] ||
    fail "run_blocks.sh changed during execution"
[[ "$analyzer_sha256_after" == "$analyzer_sha256_before" ]] ||
    fail "analyze.py changed during execution"
[[ "$activation_patch_sha256_after" == "$activation_patch_sha256_before" ]] ||
    fail "activation_disable.patch changed during execution"
[[ "$candidate_patch_sha256_after" == "$candidate_patch_sha256_before" ]] ||
    fail "candidate.patch changed during execution"
finished_at=$(date --iso-8601=seconds)

record_tmp="$run_record.tmp"
[[ ! -e "$record_tmp" ]] || fail "temporary run record already exists: $record_tmp"
EVIDENCE_STARTED_AT="$started_at" \
EVIDENCE_FINISHED_AT="$finished_at" \
EVIDENCE_HOST_NAME="$host_name" \
EVIDENCE_KERNEL="$kernel" \
EVIDENCE_GOVERNOR="$governor" \
EVIDENCE_CAMPAIGN_SHA="$campaign_sha256_before" \
EVIDENCE_RUNNER_SHA="$runner_sha256_before" \
EVIDENCE_ANALYZER_SHA="$analyzer_sha256_before" \
EVIDENCE_ACTIVATION_PATCH_SHA="$activation_patch_sha256_before" \
EVIDENCE_CANDIDATE_PATCH_SHA="$candidate_patch_sha256_before" \
python3 - "$record_tmp" <<'PY'
import json
import os
import sys
from pathlib import Path

output = Path(sys.argv[1])
orders = (
    "BC", "CB", "CB", "BC", "BC", "CB", "BC", "CB", "BC", "CB",
    "CB", "BC", "BC", "CB", "CB", "BC", "CB", "BC", "CB", "BC",
)
labels = {"B": "disabled_control", "C": "enabled_candidate"}
sequence = []
for pair, order in enumerate(orders, start=1):
    for position, arm in enumerate(order, start=1):
        stem = f"pair{pair:02d}_{arm}_{labels[arm]}"
        sequence.append(
            {
                "pair": pair,
                "position": position,
                "arm": arm,
                "raw": f"raw/{stem}.json",
                "log": f"logs/{stem}.log",
            }
        )

identities = {
    "B": {
        "path": "/tmp/mongo-count-query-bm-696f0d5d-disabled",
        "sha256": "ed2bdc05a6188a0ebb6433923391417c183e3471df78516eac3523c2f825bebc",
        "build_id": "d14aea10c20ca862734eb72e930225a1e5ea263e",
    },
    "C": {
        "path": "/tmp/mongo-count-query-bm-696f0d5d-enabled",
        "sha256": "02628346e4357ab9a48d5c0dea0de68df4c0b2921ded3fb44c4f643eb5c043be",
        "build_id": "482d4815330592895592815012509a756b70ccf8",
    },
}
payload = {
    "schema_version": 1,
    "status": "complete",
    "started_at": os.environ["EVIDENCE_STARTED_AT"],
    "finished_at": os.environ["EVIDENCE_FINISHED_AT"],
    "host": {
        "name": os.environ["EVIDENCE_HOST_NAME"],
        "kernel": os.environ["EVIDENCE_KERNEL"],
        "cpu_governor": os.environ["EVIDENCE_GOVERNOR"],
    },
    "cpu_affinity": 0,
    "taskset_command": ["taskset", "-c", "0"],
    "benchmark_filter": "CountQueryBenchmark/DirectNonDeduplicatingCountScan/400000/64$",
    "campaign_sha256_preflight": os.environ["EVIDENCE_CAMPAIGN_SHA"],
    "campaign_sha256_postflight": os.environ["EVIDENCE_CAMPAIGN_SHA"],
    "harness_sha256_preflight": {
        "run_blocks.sh": os.environ["EVIDENCE_RUNNER_SHA"],
        "analyze.py": os.environ["EVIDENCE_ANALYZER_SHA"],
        "activation_disable.patch": os.environ["EVIDENCE_ACTIVATION_PATCH_SHA"],
        "candidate.patch": os.environ["EVIDENCE_CANDIDATE_PATCH_SHA"],
    },
    "harness_sha256_postflight": {
        "run_blocks.sh": os.environ["EVIDENCE_RUNNER_SHA"],
        "analyze.py": os.environ["EVIDENCE_ANALYZER_SHA"],
        "activation_disable.patch": os.environ["EVIDENCE_ACTIVATION_PATCH_SHA"],
        "candidate.patch": os.environ["EVIDENCE_CANDIDATE_PATCH_SHA"],
    },
    "binary_identity_preflight": identities,
    "binary_identity_postflight": identities,
    "output_sequence": sequence,
}
output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
mv -- "$record_tmp" "$run_record"

# Analysis happens only after all forty fixed blocks and all postflight identity checks complete.
python3 "$analyzer"
printf 'campaign complete: %s\n' "$evidence_dir"
