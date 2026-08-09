#!/usr/bin/env python3
"""Compare PostgreSQL two-table and three-table subtree layouts.

The comparison changes only where title/summary live:

  two tables
    view = structure + metadata (all node fields except text)
    text = leaf text keyed by node_id

  three tables
    struct = topology/path fields
    meta = title/summary keyed by node_id
    text = the same text table used by the two-table layout

Both measured queries return the same ordered list of
``(node_id, title, summary)``.  The two-table path performs one ordered range
query over the no-text view.  The three-table path performs an index-only
structure scan, chunked metadata lookups, and an order-preserving client
merge.  Text is loaded once because it is identical and is not read by either
subtree query.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

from bench_databases import flatten


VIEW = "layout2_pg_view"
STRUCT = "layout3_pg_struct"
META = "layout3_pg_meta"
TEXT = "layout_shared_pg_text"
TABLES = (VIEW, STRUCT, META, TEXT)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def free_gb() -> float:
    return shutil.disk_usage("/").free / 1e9


def percentile(values: list[float], p: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return round(ordered[index], 3)


def summary(samples: list[dict], latency_key: str) -> dict:
    latencies = [sample[latency_key] for sample in samples]
    rows = [sample["rows"] for sample in samples]
    return {
        "n": len(samples),
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "mean_ms": round(statistics.mean(latencies), 3) if latencies else 0.0,
        "avg_rows": round(statistics.mean(rows), 1) if rows else 0.0,
    }


def fingerprint(rows: list[tuple[str, str, str]]) -> str:
    digest = hashlib.sha256()
    for node_id, title, summary_text in rows:
        for value in (node_id, title, summary_text):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def copy_rows(conn, sql: str, rows) -> tuple[float, int]:
    started = time.perf_counter()
    count = 0
    with conn.cursor() as cursor:
        with cursor.copy(sql) as copy:
            for row in rows:
                copy.write_row(row)
                count += 1
                if count % 2_000_000 == 0:
                    log(f"      ... {count:,}")
    return time.perf_counter() - started, count


def explain(conn, sql: str, params: tuple) -> dict:
    row = conn.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
        params,
    ).fetchone()[0]
    explanation = json.loads(row) if isinstance(row, str) else row
    root = explanation[0]
    nodes = []

    def walk(plan: dict) -> None:
        item = {"node_type": plan.get("Node Type")}
        for source, target in (
            ("Index Name", "index_name"),
            ("Actual Rows", "actual_rows"),
            ("Actual Loops", "actual_loops"),
            ("Heap Fetches", "heap_fetches"),
            ("Shared Hit Blocks", "shared_hit_blocks"),
            ("Shared Read Blocks", "shared_read_blocks"),
        ):
            if source in plan:
                item[target] = plan[source]
        nodes.append(item)
        for child in plan.get("Plans", []):
            walk(child)

    walk(root["Plan"])
    return {
        "planning_ms": root.get("Planning Time"),
        "execution_ms": root.get("Execution Time"),
        "nodes": nodes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", default="bench/db/data/large.json")
    parser.add_argument(
        "--pg-dsn",
        default="host=localhost port=55432 dbname=bench user=postgres password=bench",
    )
    parser.add_argument("--out", default="bench/db/runs/layout_2v3_postgres_10m.json")
    parser.add_argument("--chunk", type=int, default=1_000)
    parser.add_argument("--max-paths", type=int, default=200)
    parser.add_argument("--min-free-gb", type=float, default=30.0)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if free_gb() < args.min_free_gb:
        raise SystemExit(
            f"ABORT: free disk {free_gb():.1f}GB < {args.min_free_gb:.1f}GB"
        )

    import psycopg

    log(f"loading {args.doc} ...")
    started = time.perf_counter()
    with open(args.doc) as source:
        document = json.load(source)
    records = flatten(document, tree_id="base", seed=7)
    del document
    rows = records.rows
    paths = records.subtree_paths[: args.max_paths]
    log(
        f"flattened {len(rows):,} nodes and selected {len(paths)} paths "
        f"in {time.perf_counter() - started:.1f}s"
    )

    unique_ids = len({row["node_id"] for row in rows})
    if unique_ids != len(rows):
        raise SystemExit(
            f"ABORT: node_id is not globally unique ({unique_ids:,}/{len(rows):,})"
        )

    conn = psycopg.connect(args.pg_dsn, autocommit=True)
    output = {
        "doc": args.doc,
        "engine": "postgres",
        "nodes": len(rows),
        "paths": len(paths),
        "chunk": args.chunk,
        "status": "running",
        "tables": {},
        "plans": {},
        "samples": [],
    }
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        output_path.write_text(json.dumps(output, indent=2))

    def drop() -> None:
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")

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

    try:
        drop()

        log("ingesting two-table view (structure + metadata, no text) ...")
        conn.execute(f"""
            CREATE TABLE {VIEW} (
                tree_id TEXT COLLATE "C", node_id TEXT COLLATE "C",
                parent_id TEXT COLLATE "C", depth INTEGER,
                path TEXT COLLATE "C", title TEXT, summary TEXT,
                start_index INTEGER, end_index INTEGER
            )
        """)
        elapsed, count = copy_rows(
            conn,
            f"COPY {VIEW} (tree_id,node_id,parent_id,depth,path,title,summary,"
            "start_index,end_index) FROM STDIN",
            (
                (
                    row["tree_id"], row["node_id"], row["parent_id"],
                    row["depth"], row["path"], row["title"], row["summary"],
                    row["start_index"], row["end_index"],
                )
                for row in rows
            ),
        )
        conn.execute(
            f"CREATE INDEX {VIEW}_path_node_idx ON {VIEW} (path, node_id)"
        )
        conn.execute(f"VACUUM (ANALYZE, PARALLEL 0) {VIEW}")
        output["tables"]["two_view"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        log("ingesting three-table structure ...")
        conn.execute(f"""
            CREATE TABLE {STRUCT} (
                tree_id TEXT COLLATE "C", node_id TEXT COLLATE "C",
                parent_id TEXT COLLATE "C", depth INTEGER,
                path TEXT COLLATE "C"
            )
        """)
        elapsed, count = copy_rows(
            conn,
            f"COPY {STRUCT} (tree_id,node_id,parent_id,depth,path) FROM STDIN",
            (
                (
                    row["tree_id"], row["node_id"], row["parent_id"],
                    row["depth"], row["path"],
                )
                for row in rows
            ),
        )
        conn.execute(
            f"CREATE INDEX {STRUCT}_path_node_idx ON {STRUCT} (path, node_id)"
        )
        # Populate the visibility map so the covered Structure read is a real
        # index-only scan rather than an index-only plan with heap fetches.
        conn.execute(f"VACUUM (ANALYZE, PARALLEL 0) {STRUCT}")
        output["tables"]["three_struct"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        log("ingesting three-table metadata ...")
        conn.execute(f"""
            CREATE TABLE {META} (
                node_id TEXT COLLATE "C" PRIMARY KEY,
                title TEXT, summary TEXT,
                start_index INTEGER, end_index INTEGER
            )
        """)
        elapsed, count = copy_rows(
            conn,
            f"COPY {META} (node_id,title,summary,start_index,end_index) FROM STDIN",
            (
                (
                    row["node_id"], row["title"], row["summary"],
                    row["start_index"], row["end_index"],
                )
                for row in rows
            ),
        )
        conn.execute(f"VACUUM (ANALYZE, PARALLEL 0) {META}")
        output["tables"]["three_meta"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        log("ingesting shared leaf-text table ...")
        conn.execute(f"""
            CREATE TABLE {TEXT} (
                node_id TEXT COLLATE "C" PRIMARY KEY,
                text TEXT
            )
        """)
        elapsed, count = copy_rows(
            conn,
            f"COPY {TEXT} (node_id,text) FROM STDIN",
            ((row["node_id"], row["text"]) for row in rows if row["text"]),
        )
        output["tables"]["shared_text"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        rows = None
        records.rows.clear()

        def bounds(path: str) -> tuple[str, str]:
            return path + "/", path + "0"

        def two_tables(path: str) -> list[tuple[str, str, str]]:
            return [
                (node_id, title or "", summary_text or "")
                for node_id, title, summary_text in conn.execute(
                    range_sql, bounds(path)
                ).fetchall()
            ]

        def three_tables(path: str) -> list[tuple[str, str, str]]:
            ids = [
                row[0]
                for row in conn.execute(structure_sql, bounds(path)).fetchall()
            ]
            metadata = {}
            for start in range(0, len(ids), args.chunk):
                chunk = ids[start : start + args.chunk]
                for node_id, title, summary_text in conn.execute(
                    metadata_sql, (chunk,)
                ).fetchall():
                    metadata[node_id] = (title or "", summary_text or "")
            if len(metadata) != len(ids):
                raise RuntimeError(
                    f"metadata mismatch for {path}: {len(metadata):,}/{len(ids):,}"
                )
            return [(node_id, *metadata[node_id]) for node_id in ids]

        first = paths[0]
        output["plans"]["two_tables"] = explain(
            conn, range_sql, bounds(first)
        )
        output["plans"]["three_structure"] = explain(
            conn, structure_sql, bounds(first)
        )

        log("warming both layouts on the first three paths ...")
        for path in paths[:3]:
            left = two_tables(path)
            right = three_tables(path)
            if left != right:
                raise RuntimeError(f"warm-up output mismatch for {path}")
        del left, right

        log("timing paired two-table vs three-table queries ...")
        for index, path in enumerate(paths):
            if index % 2 == 0:
                started = time.perf_counter()
                two_rows = two_tables(path)
                two_ms = (time.perf_counter() - started) * 1_000

                started = time.perf_counter()
                three_rows = three_tables(path)
                three_ms = (time.perf_counter() - started) * 1_000
            else:
                started = time.perf_counter()
                three_rows = three_tables(path)
                three_ms = (time.perf_counter() - started) * 1_000

                started = time.perf_counter()
                two_rows = two_tables(path)
                two_ms = (time.perf_counter() - started) * 1_000

            if two_rows != three_rows:
                raise RuntimeError(f"timed output mismatch for {path}")

            output["samples"].append({
                "path": path,
                "rows": len(two_rows),
                "two_ms": round(two_ms, 6),
                "three_ms": round(three_ms, 6),
                "three_over_two": round(three_ms / two_ms, 6) if two_ms else None,
                "fingerprint": fingerprint(two_rows),
            })
            # Keep destruction of the previous materialized results outside
            # the next path's timed assignment.
            del two_rows, three_rows
            if (index + 1) % 20 == 0:
                log(f"      ... {index + 1}/{len(paths)} paths")
                save()

        output["two_tables"] = summary(output["samples"], "two_ms")
        output["three_tables"] = summary(output["samples"], "three_ms")
        ratios = [sample["three_over_two"] for sample in output["samples"]]
        output["paired"] = {
            "three_faster_paths": sum(
                sample["three_ms"] < sample["two_ms"]
                for sample in output["samples"]
            ),
            "two_faster_paths": sum(
                sample["two_ms"] < sample["three_ms"]
                for sample in output["samples"]
            ),
            "ratio_p50": percentile(ratios, 50),
            "ratio_p95": percentile(ratios, 95),
        }
        output["status"] = "complete"
        save()

        print("\nPostgreSQL two-table vs three-table subtree view")
        print(f"  nodes={output['nodes']:,} paths={output['paths']} chunk={args.chunk}")
        for label in ("two_tables", "three_tables"):
            stats = output[label]
            print(
                f"  {label:20s} p50={stats['p50_ms']:9.3f} ms "
                f"p95={stats['p95_ms']:9.3f} ms rows~{stats['avg_rows']:,.1f}"
            )
        print(
            "  paired: "
            f"two faster on {output['paired']['two_faster_paths']}/{len(paths)} paths; "
            f"median three/two={output['paired']['ratio_p50']:.3f}x"
        )
        log(f"wrote {args.out}")
    finally:
        if not args.keep:
            drop()
            log("cleaned up layout comparison tables")
        conn.close()
        save()


if __name__ == "__main__":
    main()
