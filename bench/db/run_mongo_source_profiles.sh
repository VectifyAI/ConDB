#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/../.." && pwd)"
if (($# < 1)); then
    echo "usage: $0 NEW_OUTPUT_DIR [ARM ...]" >&2
    exit 2
fi
output_dir="$1"
duration_s="${PROFILE_DURATION_S:-15}"
record_s="${PROFILE_RECORD_S:-19}"
frequency="${PROFILE_FREQUENCY:-499}"

mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"
if find "$output_dir" -mindepth 1 -print -quit | grep -q .; then
    echo "output directory must be empty: $output_dir" >&2
    exit 2
fi
mongo_pid="$(docker inspect -f '{{.State.Pid}}' condb_mongo)"
docker exec condb_mongo mongod --version >"$output_dir/runtime-version.txt"
symroot="$(mktemp -d /tmp/condb-mongo-symfs.XXXXXX)"
mkdir -p "$symroot/usr/bin"
docker cp condb_mongo:/usr/bin/mongod "$symroot/usr/bin/mongod"

profile_before="$(
    docker exec condb_mongo mongosh --quiet bench --eval \
        'print(JSON.stringify(db.getProfilingStatus()))'
)"
profile_level="$(
    python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["was"])' \
        <<<"$profile_before"
)"
profile_slowms="$(
    python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["slowms"])' \
        <<<"$profile_before"
)"
profile_sample_rate="$(
    python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["sampleRate"])' \
        <<<"$profile_before"
)"
profile_restored=0

restore_profile() {
    if [[ "$profile_restored" -eq 0 ]]; then
        docker exec condb_mongo mongosh --quiet bench --eval \
            "db.setProfilingLevel($profile_level,{slowms:$profile_slowms,sampleRate:$profile_sample_rate})" \
            >/dev/null
        profile_restored=1
    fi
}

cleanup() {
    restore_profile
    gio trash "$symroot"
}
trap cleanup EXIT

printf '%s\n' "$profile_before" >"$output_dir/profiling-before.json"
docker exec condb_mongo mongosh --quiet bench --eval \
    'db.setProfilingLevel(0,{slowms:100,sampleRate:1})' \
    >/dev/null
docker exec condb_mongo mongosh --quiet bench --eval \
    'print(JSON.stringify(db.getProfilingStatus()))' \
    >"$output_dir/profiling-during.json"

if (($# > 1)); then
    arms=("${@:2}")
else
    arms=(
        node_miss
        node_hit
        entity_miss
        entity_hit
        children_empty
        children_covered128
        children_noncovered128
    )
fi

for arm in "${arms[@]}"; do
    ready_file="$output_dir/$arm.ready"
    go_file="$output_dir/$arm.go"
    python3 "$script_dir/bench_mongo_source_hotloop.py" \
        "$arm" \
        --duration "$duration_s" \
        --ready-file "$ready_file" \
        --go-file "$go_file" \
        >"$output_dir/$arm.hotloop.json" &
    workload_pid=$!

    for _ in {1..1200}; do
        if [[ -f "$ready_file" ]]; then
            break
        fi
        if ! kill -0 "$workload_pid" 2>/dev/null; then
            wait "$workload_pid"
        fi
        sleep 0.05
    done
    if [[ ! -f "$ready_file" ]]; then
        echo "hot loop did not become ready: $arm" >&2
        exit 1
    fi

    docker run --rm --privileged --pid=host \
        -v /:/host:ro \
        -v "$output_dir:/host$output_dir" \
        ubuntu:22.04 \
        chroot /host /usr/bin/perf record \
        -F "$frequency" \
        -g \
        --call-graph dwarf,16384 \
        -p "$mongo_pid" \
        -o "$output_dir/$arm.perf.data" \
        -- /usr/bin/sleep "$record_s" \
        >"$output_dir/$arm.perf-record.log" 2>&1 &
    perf_pid=$!

    sleep 1
    touch "$go_file"
    wait "$perf_pid"
    wait "$workload_pid"

    docker run --rm --privileged \
        -v /:/host:ro \
        -v "$output_dir:/host$output_dir" \
        ubuntu:22.04 \
        chroot /host /bin/chown \
        "$(id -u):$(id -g)" \
        "$output_dir/$arm.perf.data"

    perf report \
        -i "$output_dir/$arm.perf.data" \
        --symfs "$symroot" \
        --stdio \
        --no-children \
        -g none \
        --sort comm,dso,symbol \
        >"$output_dir/$arm.perf-flat.txt"

    perf report \
        -i "$output_dir/$arm.perf.data" \
        --symfs "$symroot" \
        --stdio \
        --children \
        -g graph,0.5,caller \
        --sort comm,dso,symbol \
        >"$output_dir/$arm.perf-caller.txt"

    perf report \
        -i "$output_dir/$arm.perf.data" \
        --stdio \
        -g none \
        --sort comm \
        >"$output_dir/$arm.perf-comm.txt"

    query_comm="$(
        awk '$1 ~ /^[0-9.]+%$/ && $3 ~ /^conn[0-9]+$/ {print $3; exit}' \
            "$output_dir/$arm.perf-comm.txt"
    )"
    if [[ -z "$query_comm" ]]; then
        echo "could not identify query worker: $arm" >&2
        exit 1
    fi

    perf report \
        -i "$output_dir/$arm.perf.data" \
        --symfs "$symroot" \
        --stdio \
        --comms "$query_comm" \
        --no-children \
        -g none \
        --sort dso,symbol \
        >"$output_dir/$arm.perf-query.txt"

    perf report \
        -i "$output_dir/$arm.perf.data" \
        --symfs "$symroot" \
        --stdio \
        --comms "$query_comm" \
        --children \
        -g graph,0.5,caller \
        --sort dso,symbol \
        >"$output_dir/$arm.perf-query-inclusive.txt"
done

restore_profile
docker exec condb_mongo mongosh --quiet bench --eval \
    'print(JSON.stringify(db.getProfilingStatus()))' \
    >"$output_dir/profiling-after.json"

python3 - "$output_dir" "$mongo_pid" "$duration_s" "$record_s" "$frequency" <<'PY'
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
manifest = {
    "status": "complete",
    "mongodb_pid": int(sys.argv[2]),
    "workload_duration_s": float(sys.argv[3]),
    "record_duration_s": float(sys.argv[4]),
    "sample_frequency_hz": int(sys.argv[5]),
    "arms": sorted(
        path.name.removesuffix(".hotloop.json")
        for path in output.glob("*.hotloop.json")
    ),
}
(output / "manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n"
)
PY
