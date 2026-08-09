#!/usr/bin/env python3
"""Compare SQLite two-table and three-table subtree layouts.

The logical layouts differ only in whether structure and metadata share a
table.  Both layouts store the same fields and share one leaf-text table:

  two tables
    view = structure + metadata (everything except text)
    text = leaf text keyed by node_id

  three tables
    struct = tree_id, node_id, parent_id, depth, path
    meta = title, summary, start_index, end_index keyed by node_id
    text = the same text table used by the two-table layout

Both timed paths return the same ordered list of
``(node_id, title, summary)``.  The two-table path performs one path-range
query.  The three-table path performs a covered structure scan, metadata
lookups in fixed-size ``IN`` batches, a dictionary build, and an ordered
client-side merge.  Text is stored once and is not read by either query.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import statistics
import sys
import time
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from bench_databases import flatten


VIEW = "layout2_view"
STRUCT = "layout3_struct"
META = "layout3_meta"
TEXT = "layout_shared_text"

STRUCT_FIELDS = ("tree_id", "node_id", "parent_id", "depth", "path")
META_FIELDS = ("title", "summary", "start_index", "end_index")

TWO_SQL = f"""
    SELECT node_id, title, summary
    FROM {VIEW}
    WHERE path >= ? COLLATE BINARY AND path < ? COLLATE BINARY
    ORDER BY path COLLATE BINARY, node_id COLLATE BINARY
"""

THREE_STRUCT_SQL = f"""
    SELECT node_id
    FROM {STRUCT}
    WHERE path >= ? COLLATE BINARY AND path < ? COLLATE BINARY
    ORDER BY path COLLATE BINARY, node_id COLLATE BINARY
"""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def free_gb(path: str = "/") -> float:
    return shutil.disk_usage(path).free / 1e9


def remove_database(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(f"{path}{suffix}").unlink()
        except FileNotFoundError:
            pass


def batched(values: Iterable[tuple], size: int) -> Iterator[list[tuple]]:
    iterator = iter(values)
    while batch := list(islice(iterator, size)):
        yield batch


def ingest(
    connection: sqlite3.Connection,
    statement: str,
    values: Iterable[tuple],
    batch_size: int = 10_000,
) -> tuple[float, int]:
    started = time.perf_counter()
    count = 0
    connection.execute("BEGIN")
    try:
        for batch in batched(values, batch_size):
            connection.executemany(statement, batch)
            count += len(batch)
            if count % 2_000_000 == 0:
                log(f"      ... {count:,}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return time.perf_counter() - started, count


def percentile(values: Sequence[float], p: int) -> float:
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
    for row in rows:
        for value in row:
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def explain(
    connection: sqlite3.Connection,
    statement: str,
    parameters: Sequence[str],
) -> list[dict]:
    rows = connection.execute(f"EXPLAIN QUERY PLAN {statement}", parameters)
    return [
        {"id": row[0], "parent": row[1], "notused": row[2], "detail": row[3]}
        for row in rows.fetchall()
    ]


def sqlite_variable_limit(connection: sqlite3.Connection) -> int:
    if hasattr(connection, "getlimit") and hasattr(
        sqlite3, "SQLITE_LIMIT_VARIABLE_NUMBER"
    ):
        return connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)

    for (option,) in connection.execute("PRAGMA compile_options"):
        prefix = "MAX_VARIABLE_NUMBER="
        if option.startswith(prefix):
            return int(option[len(prefix) :])
    return 999


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        f"""
        CREATE TABLE {VIEW} (
            tree_id TEXT COLLATE BINARY NOT NULL,
            node_id TEXT COLLATE BINARY NOT NULL,
            parent_id TEXT COLLATE BINARY,
            depth INTEGER NOT NULL,
            path TEXT COLLATE BINARY NOT NULL,
            title TEXT,
            summary TEXT,
            start_index INTEGER,
            end_index INTEGER
        );

        CREATE TABLE {STRUCT} (
            tree_id TEXT COLLATE BINARY NOT NULL,
            node_id TEXT COLLATE BINARY NOT NULL,
            parent_id TEXT COLLATE BINARY,
            depth INTEGER NOT NULL,
            path TEXT COLLATE BINARY NOT NULL
        );

        CREATE TABLE {META} (
            node_id TEXT COLLATE BINARY PRIMARY KEY,
            title TEXT,
            summary TEXT,
            start_index INTEGER,
            end_index INTEGER
        );

        CREATE TABLE {TEXT} (
            node_id TEXT COLLATE BINARY PRIMARY KEY,
            text TEXT NOT NULL
        );
        """
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", default="bench/db/data/large.json")
    parser.add_argument(
        "--sqlite-path", default="bench/db/runs/_layout_2v3_sqlite.db"
    )
    parser.add_argument(
        "--out", default="bench/db/runs/layout_2v3_sqlite_10m.json"
    )
    parser.add_argument("--chunk", type=int, default=1_000)
    parser.add_argument("--max-paths", type=int, default=200)
    parser.add_argument("--insert-batch", type=int, default=10_000)
    parser.add_argument("--min-free-gb", type=float, default=30.0)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if args.chunk <= 0:
        parser.error("--chunk must be positive")
    if args.max_paths <= 0:
        parser.error("--max-paths must be positive")
    if args.insert_batch <= 0:
        parser.error("--insert-batch must be positive")
    if free_gb() < args.min_free_gb:
        raise SystemExit(
            f"ABORT: free disk {free_gb():.1f}GB < {args.min_free_gb:.1f}GB"
        )

    database_path = Path(args.sqlite_path)
    output_path = Path(args.out)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.resolve() == output_path.resolve():
        parser.error("--sqlite-path and --out must be different files")

    log(f"loading {args.doc} ...")
    started = time.perf_counter()
    with open(args.doc, encoding="utf-8") as source:
        document = json.load(source)
    records = flatten(document, tree_id="base", seed=7)
    del document
    source_rows = records.rows
    paths = records.subtree_paths[: args.max_paths]
    if not paths:
        raise SystemExit("ABORT: dataset has no internal subtree paths")
    log(
        f"flattened {len(source_rows):,} nodes and selected {len(paths)} paths "
        f"in {time.perf_counter() - started:.1f}s"
    )

    unique_ids = len({row["node_id"] for row in source_rows})
    if unique_ids != len(source_rows):
        raise SystemExit(
            f"ABORT: node_id is not globally unique ({unique_ids:,}/{len(source_rows):,})"
        )

    output = {
        "doc": args.doc,
        "engine": "sqlite",
        "sqlite_version": sqlite3.sqlite_version,
        "nodes": len(source_rows),
        "paths": len(paths),
        "requested_chunk": args.chunk,
        "status": "running",
        "layout_fields": {
            "two_view": [*STRUCT_FIELDS, *META_FIELDS],
            "three_struct": list(STRUCT_FIELDS),
            "three_meta": ["node_id", *META_FIELDS],
            "shared_text": ["node_id", "text"],
            "timed_result": ["node_id", "title", "summary"],
        },
        "measurement": {
            "path_collation": "BINARY",
            "result_order": ["path", "node_id"],
            "warmup_paths": min(3, len(paths)),
            "timed_layout_order": "alternating per path",
            "equality_check": "exact ordered tuple list per path",
        },
        "tables": {},
        "plans": {},
        "samples": [],
    }

    def save() -> None:
        output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    connection: sqlite3.Connection | None = None
    remove_database(database_path)
    try:
        connection = sqlite3.connect(database_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")

        variable_limit = sqlite_variable_limit(connection)
        effective_chunk = min(args.chunk, variable_limit)
        output["sqlite_variable_limit"] = variable_limit
        output["effective_chunk"] = effective_chunk
        if effective_chunk != args.chunk:
            log(
                f"requested metadata chunk {args.chunk:,} exceeds SQLite's "
                f"{variable_limit:,}-variable limit; using {effective_chunk:,}"
            )

        create_schema(connection)

        log("ingesting two-table view (structure + full metadata, no text) ...")
        elapsed, count = ingest(
            connection,
            f"""
                INSERT INTO {VIEW}
                (tree_id,node_id,parent_id,depth,path,title,summary,start_index,end_index)
                VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                (
                    row["tree_id"],
                    row["node_id"],
                    row["parent_id"],
                    row["depth"],
                    row["path"],
                    row["title"],
                    row["summary"],
                    row["start_index"],
                    row["end_index"],
                )
                for row in source_rows
            ),
            args.insert_batch,
        )
        output["tables"]["two_view"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        log("ingesting three-table structure ...")
        elapsed, count = ingest(
            connection,
            f"""
                INSERT INTO {STRUCT} (tree_id,node_id,parent_id,depth,path)
                VALUES (?,?,?,?,?)
            """,
            (tuple(row[field] for field in STRUCT_FIELDS) for row in source_rows),
            args.insert_batch,
        )
        output["tables"]["three_struct"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        log("ingesting three-table metadata (including start/end) ...")
        elapsed, count = ingest(
            connection,
            f"""
                INSERT INTO {META}
                (node_id,title,summary,start_index,end_index)
                VALUES (?,?,?,?,?)
            """,
            (
                (
                    row["node_id"],
                    row["title"],
                    row["summary"],
                    row["start_index"],
                    row["end_index"],
                )
                for row in source_rows
            ),
            args.insert_batch,
        )
        output["tables"]["three_meta"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        log("ingesting shared leaf-text table ...")
        elapsed, count = ingest(
            connection,
            f"INSERT INTO {TEXT} (node_id,text) VALUES (?,?)",
            (
                (row["node_id"], row["text"])
                for row in source_rows
                if row["text"]
            ),
            args.insert_batch,
        )
        output["tables"]["shared_text"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        log("building binary path indexes ...")
        started = time.perf_counter()
        connection.execute(
            f"CREATE INDEX ix_layout2_view_path_node "
            f"ON {VIEW}(path COLLATE BINARY, node_id COLLATE BINARY)"
        )
        connection.execute(
            f"CREATE INDEX ix_layout3_struct_path_node "
            f"ON {STRUCT}(path COLLATE BINARY, node_id COLLATE BINARY)"
        )
        connection.commit()
        connection.execute("ANALYZE")
        connection.commit()
        output["index_build_s"] = round(time.perf_counter() - started, 1)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        save()

        # Source rows are no longer needed during the measurement window.
        source_rows = None
        records.rows.clear()

        def bounds(path: str) -> tuple[str, str]:
            return path + "/", path + "0"

        def two_tables(path: str) -> list[tuple[str, str, str]]:
            return [
                (row[0], row[1] or "", row[2] or "")
                for row in connection.execute(TWO_SQL, bounds(path))
            ]

        def metadata_statement(count: int) -> str:
            placeholders = ",".join("?" for _ in range(count))
            return (
                f"SELECT node_id,title,summary FROM {META} "
                f"WHERE node_id IN ({placeholders})"
            )

        def three_tables(path: str) -> list[tuple[str, str, str]]:
            ids = [
                row[0]
                for row in connection.execute(THREE_STRUCT_SQL, bounds(path))
            ]
            metadata: dict[str, tuple[str, str]] = {}
            for start in range(0, len(ids), effective_chunk):
                chunk = ids[start : start + effective_chunk]
                statement = metadata_statement(len(chunk))
                for row in connection.execute(statement, chunk):
                    metadata[row[0]] = (row[1] or "", row[2] or "")
            if len(metadata) != len(ids):
                raise RuntimeError(
                    f"metadata mismatch for {path}: {len(metadata):,}/{len(ids):,}"
                )
            return [(node_id, *metadata[node_id]) for node_id in ids]

        first = paths[0]
        output["plans"]["two_tables"] = explain(
            connection, TWO_SQL, bounds(first)
        )
        output["plans"]["three_structure"] = explain(
            connection, THREE_STRUCT_SQL, bounds(first)
        )
        metadata_plan_batch = min(effective_chunk, 1_000)
        output["metadata_plan_batch_size"] = metadata_plan_batch
        output["plans"]["three_metadata_batch"] = explain(
            connection,
            metadata_statement(metadata_plan_batch),
            [""] * metadata_plan_batch,
        )

        log("warming both layouts on the first three paths ...")
        for index, path in enumerate(paths[:3]):
            if index % 2 == 0:
                left = two_tables(path)
                right = three_tables(path)
            else:
                right = three_tables(path)
                left = two_tables(path)
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

            sample = {
                "path": path,
                "rows": len(two_rows),
                "two_ms": round(two_ms, 6),
                "three_ms": round(three_ms, 6),
                "three_over_two": round(three_ms / two_ms, 6) if two_ms else None,
                "fingerprint": fingerprint(two_rows),
            }
            output["samples"].append(sample)
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
            "ties": sum(
                sample["two_ms"] == sample["three_ms"]
                for sample in output["samples"]
            ),
            "ratio_p50": percentile(ratios, 50),
            "ratio_p95": percentile(ratios, 95),
        }
        output["status"] = "complete"
        save()

        print("\nSQLite two-table vs three-table subtree view")
        print(
            f"  nodes={output['nodes']:,} paths={output['paths']} "
            f"chunk={effective_chunk}"
        )
        for label in ("two_tables", "three_tables"):
            stats = output[label]
            print(
                f"  {label:16s} p50={stats['p50_ms']:9.3f} ms "
                f"p95={stats['p95_ms']:9.3f} ms "
                f"p99={stats['p99_ms']:9.3f} ms "
                f"rows~{stats['avg_rows']:,.1f}"
            )
        print(
            "  paired: "
            f"two faster on {output['paired']['two_faster_paths']}/{len(paths)} "
            f"paths; median three/two={output['paired']['ratio_p50']:.3f}x"
        )
        log(f"wrote {args.out}")
    except Exception as error:
        output["status"] = "error"
        output["error"] = repr(error)
        save()
        raise
    finally:
        if connection is not None:
            connection.close()
        if not args.keep:
            remove_database(database_path)
            log("cleaned up SQLite layout comparison database")
        save()


if __name__ == "__main__":
    main()
