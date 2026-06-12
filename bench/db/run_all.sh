#!/usr/bin/env bash
# Run the full database benchmark suite from scratch.
# Each benchmark drops and rebuilds its own tables/collections, so this is a
# clean run; the generated datasets under data/ are reused as-is.
#
#   bash bench/db/run_all.sh            # read(medium,large) + write + operators + concurrency
#   bash bench/db/run_all.sh quick      # skip the large (10M) read pass

set -u
cd "$(dirname "$0")/../.."
PY=bench/db/.venv/bin/python
OUT=bench/db/runs
mkdir -p "$OUT"
MODE="${1:-full}"

run() {
    echo ">>> $*"
    "$@" || echo "!!! failed: $*"
}

echo "=== read benchmark ==="
run "$PY" bench/db/bench_databases.py --doc bench/db/data/medium.json --out "$OUT/read_medium.json"
if [ "$MODE" != "quick" ]; then
    run "$PY" bench/db/bench_databases.py --doc bench/db/data/large.json --out "$OUT/read_large.json"
fi

echo "=== write benchmark ==="
run "$PY" bench/db/bench_writes.py --doc bench/db/data/medium.json --updates 5000 --inserts 2000 --out "$OUT/write_medium.json"

echo "=== operator benchmark ==="
run "$PY" bench/db/bench_operators.py --doc bench/db/data/medium.json --out "$OUT/operators_medium.json"

echo "=== concurrency benchmark ==="
run "$PY" bench/db/bench_concurrency.py --doc bench/db/data/medium.json --duration 5 \
    --concurrency 1 2 4 8 16 32 64 --out "$OUT/concurrency_medium.json"

echo "=== done ==="
