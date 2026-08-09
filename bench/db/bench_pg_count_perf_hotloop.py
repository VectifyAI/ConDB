#!/usr/bin/env python3
"""Build and drive the PostgreSQL half of the scalar-count CPU profile.

The script intentionally pauses after printing the backend PID so ``perf`` can
attach to that single PostgreSQL process.  Press Enter to start the timed loop.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg


def find_node(plan: dict, node_type: str) -> dict | None:
    if plan.get("Node Type") == node_type:
        return plan
    for child in plan.get("Plans", []):
        match = find_node(child, node_type)
        if match is not None:
            return match
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn",
        default="host=localhost port=55433 dbname=bench user=postgres password=bench",
    )
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--count-rows", type=int, default=500_000)
    parser.add_argument("--seconds", type=float, default=24.0)
    parser.add_argument(
        "--queries",
        type=int,
        help="run exactly this many queries instead of stopping by elapsed time",
    )
    parser.add_argument("--out")
    parser.add_argument("--skip-seed", action="store_true")
    args = parser.parse_args()

    if not 0 < args.count_rows < args.rows:
        raise SystemExit("--count-rows must be between zero and --rows")

    conn = psycopg.connect(args.dsn, autocommit=True)
    if not args.skip_seed:
        print(f"seeding {args.rows:,} long path-like keys ...", flush=True)
        conn.execute("DROP TABLE IF EXISTS perfprobe")
        conn.execute('CREATE UNLOGGED TABLE perfprobe (k text COLLATE "C" NOT NULL)')
        conn.execute(
            """
            INSERT INTO perfprobe(k)
            SELECT '/000000/000007/000084/' || lpad(i::text, 8, '0')
            FROM generate_series(0, %s) AS g(i)
            """,
            (args.rows - 1,),
        )
        conn.execute("CREATE INDEX perfprobe_k_idx ON perfprobe(k)")
        conn.execute("VACUUM (ANALYZE, FREEZE) perfprobe")

    conn.execute("SET enable_seqscan = off")
    conn.execute("SET enable_bitmapscan = off")
    conn.execute("SET max_parallel_workers_per_gather = 0")
    conn.execute("SET jit = off")

    lower = "/000000/000007/000084/00000000"
    upper = f"/000000/000007/000084/{args.count_rows:08d}"
    query = "SELECT count(*) FROM perfprobe WHERE k >= %s AND k < %s"
    explain = conn.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, (lower, upper)
    ).fetchone()[0][0]
    scan = find_node(explain["Plan"], "Index Only Scan")
    if scan is None or scan.get("Heap Fetches") != 0:
        raise RuntimeError(f"expected zero-heap Index Only Scan, got {explain}")
    actual = conn.execute(query, (lower, upper)).fetchone()[0]
    if actual != args.count_rows:
        raise RuntimeError(f"count mismatch: expected {args.count_rows}, got {actual}")

    version = conn.execute("SELECT version()").fetchone()[0]
    backend_pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
    print(
        f"READY backend_pid={backend_pid} rows={args.rows} count_rows={args.count_rows}",
        flush=True,
    )
    input("Attach perf, then press Enter to start: ")

    latencies_ms: list[float] = []
    deadline = time.monotonic() + args.seconds
    while args.queries is None or len(latencies_ms) < args.queries:
        if args.queries is None and time.monotonic() >= deadline:
            break
        started = time.perf_counter()
        value = conn.execute(query, (lower, upper)).fetchone()[0]
        latencies_ms.append((time.perf_counter() - started) * 1_000)
        if value != args.count_rows:
            raise RuntimeError(f"count mismatch in hot loop: {value}")

    output = {
        "status": "complete",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": "postgresql",
        "version": version,
        "backend_pid": backend_pid,
        "workload": {
            "rows": args.rows,
            "count_rows": args.count_rows,
            "key_shape": "materialized-path-like long string with COLLATE C",
            "query": query,
            "plan_node": scan["Node Type"],
            "heap_fetches": scan["Heap Fetches"],
            "parallel_workers": 0,
            "jit": False,
        },
        "hot_loop": {
            "requested_seconds": args.seconds if args.queries is None else None,
            "requested_queries": args.queries,
            "queries": len(latencies_ms),
            "mean_ms": statistics.mean(latencies_ms),
            "median_ms": statistics.median(latencies_ms),
            "min_ms": min(latencies_ms),
            "max_ms": max(latencies_ms),
        },
        "explain": explain,
    }
    conn.close()
    rendered = json.dumps(output, indent=2)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n")
    print(rendered, flush=True)


if __name__ == "__main__":
    main()
