#!/usr/bin/env python3
"""Instrument PostgreSQL subtree latency with MongoDB-matched stage boundaries.

The script uses the same authoritative path list, ordered output, 1,000-ID
Metadata batches, warm-up protocol, alternating layout order, result checks,
and timer boundaries as ``bench_layout_2v3_mongo_breakdown.py``.  Driver-native
rows are fully materialized inside the fetch stage; Metadata-map construction
and the ordered merge are timed separately.  Results are diagnostic only and
do not update the report.
"""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
import sys
import time
from pathlib import Path

from bench_layout_2v3_mongo_breakdown import (
    aggregate,
    fingerprint,
    output_bytes,
    parse_indices,
    stats,
)
from bench_layout_2v3_postgres import explain


VIEW = "layout2_pg_view"
STRUCT = "layout3_pg_struct"
META = "layout3_pg_meta"
TEXT = "layout_shared_pg_text"
TABLES = (VIEW, STRUCT, META, TEXT)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-result",
        default=(
            "bench/db/runs/report_3eng_20260716/"
            "layout_2v3_postgres_10m_final.json"
        ),
        help="completed clean PostgreSQL result whose path order is authoritative",
    )
    parser.add_argument(
        "--pg-dsn",
        default="host=localhost port=55432 dbname=bench user=postgres password=bench",
    )
    parser.add_argument(
        "--out",
        default=(
            "bench/db/runs/report_3eng_20260716/"
            "layout_2v3_postgres_breakdown.json"
        ),
    )
    parser.add_argument(
        "--peer-result",
        default=(
            "bench/db/runs/report_3eng_20260716/"
            "layout_2v3_mongo_10m_final.json"
        ),
        help="completed MongoDB result used to verify identical inputs/outputs",
    )
    parser.add_argument(
        "--indices",
        default="all",
        help="zero-based sample indices: all, 25, or 25-26,53-54",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "independently rebuild retained tables by running "
            "bench_layout_2v3_postgres.py --keep before instrumentation"
        ),
    )
    parser.add_argument(
        "--rebuild-result",
        default=(
            "bench/db/runs/report_3eng_20260716/"
            "layout_2v3_postgres_breakdown_seed.json"
        ),
        help="throwaway benchmark JSON written by --rebuild",
    )
    parser.add_argument(
        "--keep-tables",
        action="store_true",
        help="do not drop tables owned by an independent --rebuild run",
    )
    args = parser.parse_args()

    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")

    import psycopg

    source = json.loads(Path(args.source_result).read_text())
    if source.get("status") != "complete":
        raise SystemExit(f"source result is not complete: {source.get('status')!r}")
    if source.get("chunk") != 1_000:
        raise SystemExit(f"source result chunk is not 1000: {source.get('chunk')!r}")
    if len(source.get("samples", [])) != source.get("paths"):
        raise SystemExit("source sample count does not match source path count")

    peer = json.loads(Path(args.peer_result).read_text())
    if peer.get("status") != "complete":
        raise SystemExit(f"peer result is not complete: {peer.get('status')!r}")
    source_signature = [
        (sample["path"], sample["rows"], sample["fingerprint"])
        for sample in source["samples"]
    ]
    peer_signature = [
        (sample["path"], sample["rows"], sample["fingerprint"])
        for sample in peer.get("samples", [])
    ]
    if source_signature != peer_signature:
        raise SystemExit("PostgreSQL and MongoDB source paths/outputs differ")

    indices = parse_indices(args.indices, len(source["samples"]))
    rebuilt = False
    rebuilt_result = None
    if args.rebuild:
        benchmark = Path(__file__).with_name("bench_layout_2v3_postgres.py")
        rebuild_command = [
            sys.executable,
            str(benchmark),
            "--doc",
            source["doc"],
            "--pg-dsn",
            args.pg_dsn,
            "--out",
            args.rebuild_result,
            "--chunk",
            str(source["chunk"]),
            "--max-paths",
            str(source["paths"]),
            "--keep",
        ]
        log("rebuilding PostgreSQL layout tables independently ...")
        try:
            subprocess.run(rebuild_command, check=True)
        except BaseException:
            cleanup = psycopg.connect(args.pg_dsn, autocommit=True)
            try:
                for table in TABLES:
                    cleanup.execute(f"DROP TABLE IF EXISTS {table}")
            finally:
                cleanup.close()
            raise
        rebuilt = True

        rebuilt_result = json.loads(Path(args.rebuild_result).read_text())
        if rebuilt_result.get("status") != "complete":
            raise SystemExit("independent table rebuild did not complete")
        rebuilt_samples = rebuilt_result.get("samples", [])
        if [sample["path"] for sample in rebuilt_samples] != [
            sample["path"] for sample in source["samples"]
        ]:
            raise SystemExit("independent rebuild selected different paths")
        if [sample["fingerprint"] for sample in rebuilt_samples] != [
            sample["fingerprint"] for sample in source["samples"]
        ]:
            raise SystemExit("independent rebuild produced different outputs")

    conn = psycopg.connect(args.pg_dsn, autocommit=True)
    try:
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            ).fetchall()
        }
        missing = sorted(set(TABLES) - existing)
        if missing:
            raise SystemExit(
                "missing retained tables: "
                + ", ".join(missing)
                + "; run bench_layout_2v3_postgres.py --keep first"
            )

        if rebuilt_result is not None:
            seed_tables = rebuilt_result.get("tables", {})
            counts = {
                VIEW: seed_tables.get("two_view", {}).get("count"),
                STRUCT: seed_tables.get("three_struct", {}).get("count"),
                META: seed_tables.get("three_meta", {}).get("count"),
                TEXT: seed_tables.get("shared_text", {}).get("count"),
            }
        else:
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in TABLES
            }
        expected_nodes = source["nodes"]
        for table in (VIEW, STRUCT, META):
            if counts[table] != expected_nodes:
                raise SystemExit(
                    f"table {table} has {counts[table]:,} rows, expected "
                    f"{expected_nodes:,}"
                )

        range_sql = (
            "SELECT node_id, title, summary "
            f"FROM {VIEW} WHERE path >= %s AND path < %s "
            "ORDER BY path, node_id"
        )
        structure_sql = (
            f"SELECT node_id FROM {STRUCT} "
            "WHERE path >= %s AND path < %s ORDER BY path, node_id"
        )
        metadata_sql = (
            f"SELECT node_id, title, summary FROM {META} "
            "WHERE node_id = ANY(%s::text[])"
        )
        chunk_size = source["chunk"]

        def bounds(path: str) -> tuple[str, str]:
            return path + "/", path + "0"

        def two_tables(path: str) -> list[tuple[str, str, str]]:
            return [
                (node_id, title or "", summary_text or "")
                for node_id, title, summary_text in conn.execute(
                    range_sql, bounds(path)
                ).fetchall()
            ]

        def three_staged(path: str) -> tuple[list[tuple[str, str, str]], dict]:
            started = time.perf_counter()
            ids = [
                row[0]
                for row in conn.execute(structure_sql, bounds(path)).fetchall()
            ]
            structure_ms = (time.perf_counter() - started) * 1_000

            metadata: dict[str, tuple[str, str]] = {}
            fetch_call_ms: list[float] = []
            metadata_map_ms = 0.0
            metadata_batch_cleanup_ms = 0.0
            for offset in range(0, len(ids), chunk_size):
                chunk = ids[offset : offset + chunk_size]

                started = time.perf_counter()
                rows = conn.execute(metadata_sql, (chunk,)).fetchall()
                fetch_call_ms.append((time.perf_counter() - started) * 1_000)

                started = time.perf_counter()
                for node_id, title, summary_text in rows:
                    metadata[node_id] = (title or "", summary_text or "")
                metadata_map_ms += (time.perf_counter() - started) * 1_000

                started = time.perf_counter()
                del rows
                metadata_batch_cleanup_ms += (
                    time.perf_counter() - started
                ) * 1_000

            if len(metadata) != len(ids):
                raise RuntimeError(
                    f"metadata mismatch for {path}: {len(metadata):,}/{len(ids):,}"
                )

            started = time.perf_counter()
            result = [(node_id, *metadata[node_id]) for node_id in ids]
            ordered_merge_ms = (time.perf_counter() - started) * 1_000
            return result, {
                "structure_ms": structure_ms,
                "metadata_fetch_ms": sum(fetch_call_ms),
                "metadata_map_ms": metadata_map_ms,
                "metadata_batch_cleanup_ms": metadata_batch_cleanup_ms,
                "ordered_merge_ms": ordered_merge_ms,
                "metadata_calls": len(fetch_call_ms),
                "metadata_fetch_call_ms": stats(fetch_call_ms),
            }

        log("warming retained PostgreSQL layouts ...")
        for source_sample in source["samples"][:3]:
            left = two_tables(source_sample["path"])
            right, _ = three_staged(source_sample["path"])
            if left != right:
                raise RuntimeError(f"warm-up mismatch for {source_sample['path']}")
            del left, right
        gc.collect()

        output = {
            "engine": "postgres",
            "source_result": args.source_result,
            "nodes": expected_nodes,
            "source_paths": source["paths"],
            "chunk": chunk_size,
            "indices": indices,
            "repeats": args.repeats,
            "tables": counts,
            "environment": {
                "postgres_server_version": conn.info.server_version,
                "psycopg_version": psycopg.__version__,
                "autocommit": conn.autocommit,
                "prepare_threshold": conn.prepare_threshold,
                "client_encoding": conn.execute("SHOW client_encoding").fetchone()[0],
                "transaction_isolation": conn.execute(
                    "SHOW default_transaction_isolation"
                ).fetchone()[0],
            },
            "status": "running",
            "plans": {},
            "samples": [],
        }
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        log(f"instrumenting {len(indices)} paths x {args.repeats} repeat(s) ...")
        for repeat in range(args.repeats):
            for position, index in enumerate(indices):
                source_sample = source["samples"][index]
                path = source_sample["path"]
                two_first = (index + repeat) % 2 == 0

                gc.disable()
                try:
                    if two_first:
                        started = time.perf_counter()
                        two_rows = two_tables(path)
                        two_total_ms = (time.perf_counter() - started) * 1_000

                        started = time.perf_counter()
                        three_rows, stages = three_staged(path)
                        three_total_ms = (time.perf_counter() - started) * 1_000
                    else:
                        started = time.perf_counter()
                        three_rows, stages = three_staged(path)
                        three_total_ms = (time.perf_counter() - started) * 1_000

                        started = time.perf_counter()
                        two_rows = two_tables(path)
                        two_total_ms = (time.perf_counter() - started) * 1_000
                finally:
                    gc.enable()

                if two_rows != three_rows:
                    raise RuntimeError(f"timed output mismatch for {path}")
                if len(two_rows) != source_sample["rows"]:
                    raise RuntimeError(
                        f"row-count mismatch for {path}: "
                        f"{len(two_rows):,}/{source_sample['rows']:,}"
                    )
                digest = fingerprint(two_rows)
                if digest != source_sample["fingerprint"]:
                    raise RuntimeError(f"fingerprint mismatch for {path}")

                bytes_returned = output_bytes(two_rows)
                attributed = sum(
                    stages[key]
                    for key in (
                        "structure_ms",
                        "metadata_fetch_ms",
                        "metadata_map_ms",
                        "metadata_batch_cleanup_ms",
                        "ordered_merge_ms",
                    )
                )

                started = time.perf_counter()
                del two_rows
                two_release_ms = (time.perf_counter() - started) * 1_000
                started = time.perf_counter()
                del three_rows
                three_release_ms = (time.perf_counter() - started) * 1_000
                started = time.perf_counter()
                gc.collect()
                gc_collect_ms = (time.perf_counter() - started) * 1_000

                sample = {
                    "repeat": repeat,
                    "source_index": index,
                    "path": path,
                    "order": "two_first" if two_first else "three_first",
                    "rows": source_sample["rows"],
                    "output_utf8_bytes": bytes_returned,
                    "bytes_per_row": round(bytes_returned / source_sample["rows"], 3),
                    "two_total_ms": round(two_total_ms, 6),
                    "two_release_ms": round(two_release_ms, 6),
                    "three_total_ms": round(three_total_ms, 6),
                    "structure_ms": round(stages["structure_ms"], 6),
                    "metadata_fetch_ms": round(stages["metadata_fetch_ms"], 6),
                    "metadata_map_ms": round(stages["metadata_map_ms"], 6),
                    "metadata_batch_cleanup_ms": round(
                        stages["metadata_batch_cleanup_ms"], 6
                    ),
                    "ordered_merge_ms": round(stages["ordered_merge_ms"], 6),
                    "three_unattributed_ms": round(three_total_ms - attributed, 6),
                    "metadata_calls": stages["metadata_calls"],
                    "metadata_fetch_call_ms": stages["metadata_fetch_call_ms"],
                    "three_release_ms": round(three_release_ms, 6),
                    "gc_collect_ms": round(gc_collect_ms, 6),
                    "fingerprint": digest,
                }
                output["samples"].append(sample)

                done = repeat * len(indices) + position + 1
                total = args.repeats * len(indices)
                if done % 10 == 0 or done == total:
                    log(f"      ... {done}/{total}")
                    output_path.write_text(json.dumps(output, indent=2))

        # Collect plans only after timing so EXPLAIN ANALYZE cannot warm the
        # measured workload.  The Metadata plan uses one real 1,000-ID batch.
        plan_path = source["samples"][0]["path"]
        plan_ids = [
            row[0]
            for row in conn.execute(structure_sql, bounds(plan_path)).fetchall()
        ]
        output["plans"] = {
            "two_tables": explain(conn, range_sql, bounds(plan_path)),
            "three_structure": explain(conn, structure_sql, bounds(plan_path)),
            "three_metadata_1000": explain(
                conn, metadata_sql, (plan_ids[:chunk_size],)
            ),
        }
        structure_nodes = output["plans"]["three_structure"]["nodes"]
        if not any(
            node.get("node_type") == "Index Only Scan"
            and node.get("heap_fetches", 0) == 0
            for node in structure_nodes
        ):
            raise RuntimeError("Structure plan is not a zero-heap-fetch Index Only Scan")
        for label in ("two_tables", "three_metadata_1000"):
            if any(
                node.get("node_type") == "Seq Scan"
                for node in output["plans"][label]["nodes"]
            ):
                raise RuntimeError(f"{label} unexpectedly uses a sequential scan")

        output["aggregate"] = aggregate(output["samples"])
        output["status"] = "complete"
        output_path.write_text(json.dumps(output, indent=2))
        log(f"wrote {args.out}")

        share = output["aggregate"]["three_stage_share_of_total"]
        print("PostgreSQL three-table subtree breakdown")
        print(
            f"  paths={len(indices)} repeats={args.repeats} "
            f"samples={len(output['samples'])}"
        )
        print(
            "  aggregate three-layout time share: "
            f"structure={share['structure_ms']:.1%}, "
            f"metadata fetch={share['metadata_fetch_ms']:.1%}, "
            f"metadata map={share['metadata_map_ms']:.1%}, "
            f"ordered merge={share['ordered_merge_ms']:.1%}, "
            f"other={share['three_unattributed_ms']:.1%}"
        )
    finally:
        if rebuilt and not args.keep_tables:
            for table in TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            log("cleaned up independently rebuilt PostgreSQL layout tables")
        conn.close()


if __name__ == "__main__":
    main()
