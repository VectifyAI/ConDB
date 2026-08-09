#!/usr/bin/env python3
"""Benchmark SQL-friendly subtree layouts on the existing 10M-node corpus.

This benchmark complements (and does not replace) the matched-schema engine
comparison.  It fixes the public API contract and lets PostgreSQL and SQLite
use relational execution and a read-optimized hierarchy encoding:

* ``path_colocated``: typed materialized path, metadata colocated, covering
  range access.
* ``path_client``: the report's client-mediated separated layout, retained as
  a control.
* ``path_join``: the same separated schema, but joined inside SQL.
* ``preorder_colocated``: DFS preorder interval, metadata colocated.
* ``preorder_join``: DFS preorder interval plus an in-database metadata join.

Every arm accepts ``(tree_id, root_node_id)`` and returns the same ordered
``(node_id, title, summary)`` descendants, excluding the root and with no depth
limit.  Results are fully consumed, fingerprinted, and checked against the
existing 10M report artifact.

The build phase intentionally reuses the already-ingested 10M source tables:

PostgreSQL
  layout2_pg_view, layout3_pg_struct, layout3_pg_meta

SQLite
  bench/db/runs/_sqlite.db:nodes

This avoids parsing the 14 GB source JSON again.  All newly created PostgreSQL
relations use the ``sql_native_`` prefix; SQLite writes a separate database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

PG_SOURCE_VIEW = "layout2_pg_view"
PG_SOURCE_STRUCT = "layout3_pg_struct"
PG_SOURCE_META = "layout3_pg_meta"
PG_NATIVE_NODES = "sql_native_nodes"
PG_NATIVE_STRUCT = "sql_native_struct"

SQLITE_PATH_NODES = "path_nodes"
SQLITE_PATH_STRUCT = "path_struct"
SQLITE_META = "node_meta"
SQLITE_NATIVE_NODES = "preorder_nodes"
SQLITE_NATIVE_STRUCT = "preorder_struct"

ARMS = (
    "path_colocated",
    "path_client",
    "path_join",
    "preorder_colocated",
    "preorder_join",
)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def percentile(values: Sequence[float], pct: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return round(ordered[index], 3)


def fingerprint(rows: Sequence[tuple[str, str, str]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        for value in row:
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def normalized(rows: Iterable[Sequence[Any]]) -> list[tuple[str, str, str]]:
    return [
        (str(row[0]), row[1] or "", row[2] or "")
        for row in rows
    ]


def expected_samples(path: Path, max_paths: int) -> list[dict[str, Any]]:
    document = json.loads(path.read_text())
    samples = document["samples"][:max_paths]
    for sample in samples:
        sample["node_id"] = sample["path"].rsplit("/", 1)[-1]
    return samples


def summarize(samples: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    per_path_ms = [statistics.median(sample["repeats_ms"][arm]) for sample in samples]
    return {
        "paths": len(samples),
        "repeats": len(samples[0]["repeats_ms"][arm]) if samples else 0,
        "p50_ms": percentile(per_path_ms, 50),
        "p95_ms": percentile(per_path_ms, 95),
        "p99_ms": percentile(per_path_ms, 99),
        "mean_ms": round(statistics.mean(per_path_ms), 3) if per_path_ms else 0.0,
        "avg_rows": round(statistics.mean(sample["rows"] for sample in samples), 1)
        if samples else 0.0,
    }


def preorder_rows(rows: Iterable[Sequence[Any]]) -> Iterator[tuple[Any, ...]]:
    """Convert path-ordered rows to half-open DFS preorder intervals.

    Only the active root-to-current stack is retained.  A node's interval ends
    when the stream reaches the first later row at the same or a shallower
    depth.  Emission order is therefore not preorder, but the stored preorder
    values are; bulk loaders and indexes do not require input ordering.
    """

    stack: list[tuple[int, tuple[Any, ...]]] = []
    count = 0
    for count, row in enumerate(rows, start=1):
        preorder = count - 1
        tree_id, node_id, parent_id, depth = row[:4]
        while stack and stack[-1][0] >= depth:
            _, pending = stack.pop()
            yield (*pending, preorder)
        pending = (
            tree_id,
            node_id,
            parent_id,
            depth,
            preorder,
            *row[4:],
        )
        stack.append((depth, pending))
    while stack:
        _, pending = stack.pop()
        yield (*pending, count)


def batched(rows: Iterable[tuple[Any, ...]], size: int) -> Iterator[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def pg_relation_bytes(connection: Any, relation: str) -> int:
    return connection.execute(
        "SELECT pg_total_relation_size(%s::regclass)", (relation,)
    ).fetchone()[0]


def pg_index_bytes(connection: Any, relation: str) -> int:
    return connection.execute(
        "SELECT pg_indexes_size(%s::regclass)", (relation,)
    ).fetchone()[0]


def require_pg_sources(connection: Any) -> None:
    for table in (PG_SOURCE_VIEW, PG_SOURCE_STRUCT, PG_SOURCE_META):
        if connection.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
            raise SystemExit(
                f"missing PostgreSQL source table {table}; run the 10M layout benchmark with --keep"
            )


def build_postgres(args: argparse.Namespace, output: dict[str, Any]) -> None:
    import psycopg

    read_connection = psycopg.connect(args.pg_dsn)
    write_connection = psycopg.connect(args.pg_dsn, autocommit=True)
    require_pg_sources(write_connection)

    started = time.perf_counter()
    log("[postgres] dropping only sql_native_* relations and indexes ...")
    write_connection.execute(f"DROP TABLE IF EXISTS {PG_NATIVE_STRUCT}")
    write_connection.execute(f"DROP TABLE IF EXISTS {PG_NATIVE_NODES}")
    for index in (
        "sql_native_path_view_root",
        "sql_native_path_view_cover",
        "sql_native_path_struct_root",
        "sql_native_path_struct_scan",
    ):
        write_connection.execute(f"DROP INDEX IF EXISTS {index}")

    log("[postgres] adding SQL-friendly path lookup/covering indexes ...")
    index_started = time.perf_counter()
    write_connection.execute(
        f"CREATE UNIQUE INDEX sql_native_path_view_root ON {PG_SOURCE_VIEW} "
        "(tree_id, node_id) INCLUDE (path)"
    )
    write_connection.execute(
        f"CREATE INDEX sql_native_path_view_cover ON {PG_SOURCE_VIEW} "
        "(tree_id, path, node_id) INCLUDE (title, summary)"
    )
    write_connection.execute(
        f"CREATE UNIQUE INDEX sql_native_path_struct_root ON {PG_SOURCE_STRUCT} "
        "(tree_id, node_id) INCLUDE (path)"
    )
    write_connection.execute(
        f"CREATE INDEX sql_native_path_struct_scan ON {PG_SOURCE_STRUCT} "
        "(tree_id, path, node_id)"
    )
    output["build"]["path_index_s"] = round(time.perf_counter() - index_started, 1)

    log("[postgres] streaming 10M path rows into preorder intervals ...")
    write_connection.execute(f"""
        CREATE TABLE {PG_NATIVE_NODES} (
            tree_id TEXT COLLATE "C" NOT NULL,
            node_id TEXT COLLATE "C" NOT NULL,
            parent_id TEXT COLLATE "C",
            depth INTEGER NOT NULL,
            preorder BIGINT NOT NULL,
            title TEXT,
            summary TEXT,
            start_index INTEGER,
            end_index INTEGER,
            subtree_end BIGINT NOT NULL
        )
    """)
    stream_started = time.perf_counter()
    with read_connection.cursor(name="sql_native_source") as source:
        source.itersize = args.fetch_batch
        source.execute(f"""
            SELECT tree_id,node_id,parent_id,depth,
                   title,summary,start_index,end_index
            FROM {PG_SOURCE_VIEW}
            ORDER BY path,node_id
        """)
        with write_connection.cursor().copy(
            f"COPY {PG_NATIVE_NODES} "
            "(tree_id,node_id,parent_id,depth,preorder,title,summary,"
            "start_index,end_index,subtree_end) FROM STDIN"
        ) as copy:
            for count, row in enumerate(preorder_rows(source), start=1):
                copy.write_row(row)
                if count % 1_000_000 == 0:
                    log(f"      ... {count:,} preorder rows")
    output["build"]["preorder_copy_s"] = round(time.perf_counter() - stream_started, 1)

    log("[postgres] building preorder indexes and separated structure ...")
    native_index_started = time.perf_counter()
    write_connection.execute(
        f"CREATE UNIQUE INDEX sql_native_nodes_root ON {PG_NATIVE_NODES} "
        "(tree_id,node_id) INCLUDE (preorder,subtree_end)"
    )
    write_connection.execute(
        f"CREATE INDEX sql_native_nodes_scan ON {PG_NATIVE_NODES} "
        "(tree_id,preorder) INCLUDE (node_id,title,summary)"
    )
    write_connection.execute(f"""
        CREATE TABLE {PG_NATIVE_STRUCT} AS
        SELECT tree_id,node_id,parent_id,depth,preorder,subtree_end
        FROM {PG_NATIVE_NODES}
    """)
    write_connection.execute(
        f"CREATE UNIQUE INDEX sql_native_struct_root ON {PG_NATIVE_STRUCT} "
        "(tree_id,node_id) INCLUDE (preorder,subtree_end)"
    )
    write_connection.execute(
        f"CREATE INDEX sql_native_struct_scan ON {PG_NATIVE_STRUCT} "
        "(tree_id,preorder) INCLUDE (node_id)"
    )
    for table in (PG_SOURCE_VIEW, PG_SOURCE_STRUCT, PG_SOURCE_META,
                  PG_NATIVE_NODES, PG_NATIVE_STRUCT):
        write_connection.execute(f"VACUUM (ANALYZE, PARALLEL 0) {table}")
    output["build"]["native_index_and_struct_s"] = round(
        time.perf_counter() - native_index_started, 1
    )
    output["build"]["total_s"] = round(time.perf_counter() - started, 1)
    output["storage"] = {
        relation: {
            "total_bytes": pg_relation_bytes(write_connection, relation),
            "index_bytes": pg_index_bytes(write_connection, relation),
        }
        for relation in (
            PG_SOURCE_VIEW,
            PG_SOURCE_STRUCT,
            PG_SOURCE_META,
            PG_NATIVE_NODES,
            PG_NATIVE_STRUCT,
        )
    }
    output["build"]["status"] = "complete"
    read_connection.close()
    write_connection.close()


def remove_sqlite_database(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(f"{path}{suffix}").unlink()
        except FileNotFoundError:
            pass


def sqlite_table_bytes(connection: sqlite3.Connection, table: str) -> int:
    try:
        return connection.execute(
            "SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name=?", (table,)
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def sqlite_storage(connection: sqlite3.Connection, path: Path) -> dict[str, Any]:
    objects = connection.execute("""
        SELECT type,name,tbl_name
        FROM sqlite_master
        WHERE type IN ('table','index')
          AND name NOT LIKE 'sqlite_stat%'
        ORDER BY type,name
    """).fetchall()
    return {
        "database_bytes": path.stat().st_size,
        "objects": {
            name: {
                "type": object_type,
                "table": table,
                "bytes": sqlite_table_bytes(connection, name),
            }
            for object_type, name, table in objects
        },
    }


def build_sqlite(args: argparse.Namespace, output: dict[str, Any]) -> None:
    source_path = Path(args.sqlite_source)
    target_path = Path(args.sqlite_path)
    if not source_path.exists():
        raise SystemExit(f"missing SQLite source database: {source_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    remove_sqlite_database(target_path)

    started = time.perf_counter()
    connection = sqlite3.connect(target_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.executescript(f"""
        CREATE TABLE {SQLITE_PATH_NODES} (
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
        CREATE TABLE {SQLITE_PATH_STRUCT} (
            tree_id TEXT COLLATE BINARY NOT NULL,
            node_id TEXT COLLATE BINARY NOT NULL,
            parent_id TEXT COLLATE BINARY,
            depth INTEGER NOT NULL,
            path TEXT COLLATE BINARY NOT NULL
        );
        CREATE TABLE {SQLITE_META} (
            node_id TEXT COLLATE BINARY PRIMARY KEY,
            title TEXT,
            summary TEXT,
            start_index INTEGER,
            end_index INTEGER
        ) WITHOUT ROWID;
        CREATE TABLE {SQLITE_NATIVE_NODES} (
            tree_id TEXT COLLATE BINARY NOT NULL,
            node_id TEXT COLLATE BINARY NOT NULL,
            parent_id TEXT COLLATE BINARY,
            depth INTEGER NOT NULL,
            preorder INTEGER NOT NULL,
            title TEXT,
            summary TEXT,
            start_index INTEGER,
            end_index INTEGER,
            subtree_end INTEGER NOT NULL,
            PRIMARY KEY (tree_id, preorder),
            UNIQUE (tree_id, node_id)
        ) WITHOUT ROWID;
        CREATE TABLE {SQLITE_NATIVE_STRUCT} (
            tree_id TEXT COLLATE BINARY NOT NULL,
            node_id TEXT COLLATE BINARY NOT NULL,
            parent_id TEXT COLLATE BINARY,
            depth INTEGER NOT NULL,
            preorder INTEGER NOT NULL,
            subtree_end INTEGER NOT NULL,
            PRIMARY KEY (tree_id, preorder),
            UNIQUE (tree_id, node_id)
        ) WITHOUT ROWID;
    """)

    log("[sqlite] copying path and metadata layouts from the 10M source ...")
    copy_started = time.perf_counter()
    connection.execute("ATTACH DATABASE ? AS source", (str(source_path.resolve()),))
    connection.execute(f"""
        INSERT INTO {SQLITE_PATH_NODES}
        SELECT tree_id,node_id,parent_id,depth,path,title,summary,start_index,end_index
        FROM source.nodes
    """)
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute(f"""
        INSERT INTO {SQLITE_PATH_STRUCT}
        SELECT tree_id,node_id,parent_id,depth,path FROM source.nodes
    """)
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute(f"""
        INSERT INTO {SQLITE_META}
        SELECT node_id,title,summary,start_index,end_index FROM source.nodes
    """)
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("DETACH DATABASE source")
    output["build"]["path_and_meta_copy_s"] = round(time.perf_counter() - copy_started, 1)

    log("[sqlite] streaming 10M path rows into preorder intervals ...")
    stream_started = time.perf_counter()
    source = sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    source_rows = source.execute("""
        SELECT tree_id,node_id,parent_id,depth,title,summary,start_index,end_index
        FROM nodes INDEXED BY ix_path
        ORDER BY path
    """)
    insert_sql = f"""
        INSERT INTO {SQLITE_NATIVE_NODES}
        (tree_id,node_id,parent_id,depth,preorder,title,summary,
         start_index,end_index,subtree_end)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """
    count = 0
    for batch in batched(preorder_rows(source_rows), args.insert_batch):
        connection.executemany(insert_sql, batch)
        connection.commit()
        count += len(batch)
        if count % 1_000_000 == 0:
            log(f"      ... {count:,} preorder rows")
    source.close()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    output["build"]["preorder_copy_s"] = round(time.perf_counter() - stream_started, 1)

    log("[sqlite] building SQL-friendly path indexes and separated preorder table ...")
    index_started = time.perf_counter()
    connection.executescript(f"""
        CREATE UNIQUE INDEX sql_native_path_nodes_root
          ON {SQLITE_PATH_NODES}(tree_id,node_id);
        CREATE INDEX sql_native_path_nodes_cover
          ON {SQLITE_PATH_NODES}(tree_id,path,node_id,title,summary);
        CREATE UNIQUE INDEX sql_native_path_struct_root
          ON {SQLITE_PATH_STRUCT}(tree_id,node_id);
        CREATE INDEX sql_native_path_struct_scan
          ON {SQLITE_PATH_STRUCT}(tree_id,path,node_id);
        INSERT INTO {SQLITE_NATIVE_STRUCT}
          SELECT tree_id,node_id,parent_id,depth,preorder,subtree_end
          FROM {SQLITE_NATIVE_NODES};
        ANALYZE;
    """)
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    output["build"]["index_and_struct_s"] = round(time.perf_counter() - index_started, 1)
    output["build"]["total_s"] = round(time.perf_counter() - started, 1)
    output["storage"] = sqlite_storage(connection, target_path)
    output["build"]["status"] = "complete"
    connection.close()


def pg_queries(connection: Any, chunk: int) -> dict[str, Callable[[str, str], list[tuple[str, str, str]]]]:
    path_colocated_sql = f"""
        SELECT child.node_id,child.title,child.summary
        FROM {PG_SOURCE_VIEW} child
        WHERE child.tree_id=%s AND child.path>=%s AND child.path<%s
        ORDER BY child.path,child.node_id
    """
    path_join_sql = f"""
        SELECT child.node_id,meta.title,meta.summary
        FROM {PG_SOURCE_STRUCT} child
        JOIN {PG_SOURCE_META} meta ON meta.node_id=child.node_id
        WHERE child.tree_id=%s AND child.path>=%s AND child.path<%s
        ORDER BY child.path,child.node_id
    """
    preorder_colocated_sql = f"""
        SELECT child.node_id,child.title,child.summary
        FROM {PG_NATIVE_NODES} child
        WHERE child.tree_id=%s AND child.preorder>%s AND child.preorder<%s
        ORDER BY child.preorder
    """
    preorder_join_sql = f"""
        SELECT child.node_id,meta.title,meta.summary
        FROM {PG_NATIVE_STRUCT} child
        JOIN {PG_SOURCE_META} meta ON meta.node_id=child.node_id
        WHERE child.tree_id=%s AND child.preorder>%s AND child.preorder<%s
        ORDER BY child.preorder
    """

    def path_bounds(table: str, tree_id: str, node_id: str) -> tuple[str, str]:
        path = connection.execute(
            f"SELECT path FROM {table} WHERE tree_id=%s AND node_id=%s",
            (tree_id, node_id),
        ).fetchone()[0]
        return path + "/", path + "0"

    def preorder_bounds(table: str, tree_id: str, node_id: str) -> tuple[int, int]:
        return connection.execute(
            f"SELECT preorder,subtree_end FROM {table} "
            "WHERE tree_id=%s AND node_id=%s",
            (tree_id, node_id),
        ).fetchone()

    def path_direct(
        table: str, sql: str
    ) -> Callable[[str, str], list[tuple[str, str, str]]]:
        def query(tree_id: str, node_id: str) -> list[tuple[str, str, str]]:
            lower, upper = path_bounds(table, tree_id, node_id)
            return normalized(connection.execute(sql, (tree_id, lower, upper)).fetchall())

        return query

    def preorder_direct(
        table: str, sql: str
    ) -> Callable[[str, str], list[tuple[str, str, str]]]:
        def query(tree_id: str, node_id: str) -> list[tuple[str, str, str]]:
            lower, upper = preorder_bounds(table, tree_id, node_id)
            return normalized(connection.execute(sql, (tree_id, lower, upper)).fetchall())

        return query

    def path_client(tree_id: str, node_id: str) -> list[tuple[str, str, str]]:
        root_path = connection.execute(
            f"SELECT path FROM {PG_SOURCE_STRUCT} WHERE tree_id=%s AND node_id=%s",
            (tree_id, node_id),
        ).fetchone()[0]
        ids = [
            row[0]
            for row in connection.execute(
                f"SELECT node_id FROM {PG_SOURCE_STRUCT} "
                "WHERE tree_id=%s AND path>=%s AND path<%s ORDER BY path,node_id",
                (tree_id, root_path + "/", root_path + "0"),
            ).fetchall()
        ]
        metadata: dict[str, tuple[str, str]] = {}
        for start in range(0, len(ids), chunk):
            part = ids[start:start + chunk]
            for found_id, title, summary in connection.execute(
                f"SELECT node_id,title,summary FROM {PG_SOURCE_META} "
                "WHERE node_id=ANY(%s::text[])",
                (part,),
            ).fetchall():
                metadata[found_id] = (title or "", summary or "")
        return [(found_id, *metadata[found_id]) for found_id in ids]

    return {
        "path_colocated": path_direct(PG_SOURCE_VIEW, path_colocated_sql),
        "path_client": path_client,
        "path_join": path_direct(PG_SOURCE_STRUCT, path_join_sql),
        "preorder_colocated": preorder_direct(PG_NATIVE_NODES, preorder_colocated_sql),
        "preorder_join": preorder_direct(PG_NATIVE_STRUCT, preorder_join_sql),
    }


def sqlite_queries(
    connection: sqlite3.Connection, chunk: int
) -> dict[str, Callable[[str, str], list[tuple[str, str, str]]]]:
    path_colocated_sql = f"""
        SELECT child.node_id,child.title,child.summary
        FROM {SQLITE_PATH_NODES} child
        WHERE child.tree_id=? AND child.path>=? AND child.path<?
        ORDER BY child.path,child.node_id
    """
    path_join_sql = f"""
        SELECT child.node_id,meta.title,meta.summary
        FROM {SQLITE_PATH_STRUCT} child
        JOIN {SQLITE_META} meta ON meta.node_id=child.node_id
        WHERE child.tree_id=? AND child.path>=? AND child.path<?
        ORDER BY child.path,child.node_id
    """
    preorder_colocated_sql = f"""
        SELECT child.node_id,child.title,child.summary
        FROM {SQLITE_NATIVE_NODES} child
        WHERE child.tree_id=? AND child.preorder>? AND child.preorder<?
        ORDER BY child.preorder
    """
    preorder_join_sql = f"""
        SELECT child.node_id,meta.title,meta.summary
        FROM {SQLITE_NATIVE_STRUCT} child
        JOIN {SQLITE_META} meta ON meta.node_id=child.node_id
        WHERE child.tree_id=? AND child.preorder>? AND child.preorder<?
        ORDER BY child.preorder
    """

    def path_bounds(table: str, tree_id: str, node_id: str) -> tuple[str, str]:
        path = connection.execute(
            f"SELECT path FROM {table} WHERE tree_id=? AND node_id=?",
            (tree_id, node_id),
        ).fetchone()[0]
        return path + "/", path + "0"

    def preorder_bounds(table: str, tree_id: str, node_id: str) -> tuple[int, int]:
        return connection.execute(
            f"SELECT preorder,subtree_end FROM {table} WHERE tree_id=? AND node_id=?",
            (tree_id, node_id),
        ).fetchone()

    def path_direct(
        table: str, sql: str
    ) -> Callable[[str, str], list[tuple[str, str, str]]]:
        def query(tree_id: str, node_id: str) -> list[tuple[str, str, str]]:
            lower, upper = path_bounds(table, tree_id, node_id)
            return normalized(connection.execute(sql, (tree_id, lower, upper)).fetchall())

        return query

    def preorder_direct(
        table: str, sql: str
    ) -> Callable[[str, str], list[tuple[str, str, str]]]:
        def query(tree_id: str, node_id: str) -> list[tuple[str, str, str]]:
            lower, upper = preorder_bounds(table, tree_id, node_id)
            return normalized(connection.execute(sql, (tree_id, lower, upper)).fetchall())

        return query

    def metadata_sql(count: int) -> str:
        return (
            f"SELECT node_id,title,summary FROM {SQLITE_META} WHERE node_id IN ("
            + ",".join("?" for _ in range(count))
            + ")"
        )

    def path_client(tree_id: str, node_id: str) -> list[tuple[str, str, str]]:
        root_path = connection.execute(
            f"SELECT path FROM {SQLITE_PATH_STRUCT} WHERE tree_id=? AND node_id=?",
            (tree_id, node_id),
        ).fetchone()[0]
        ids = [
            row[0]
            for row in connection.execute(
                f"SELECT node_id FROM {SQLITE_PATH_STRUCT} "
                "WHERE tree_id=? AND path>=? AND path<? ORDER BY path,node_id",
                (tree_id, root_path + "/", root_path + "0"),
            ).fetchall()
        ]
        metadata: dict[str, tuple[str, str]] = {}
        for start in range(0, len(ids), chunk):
            part = ids[start:start + chunk]
            for found_id, title, summary in connection.execute(metadata_sql(len(part)), part):
                metadata[found_id] = (title or "", summary or "")
        return [(found_id, *metadata[found_id]) for found_id in ids]

    return {
        "path_colocated": path_direct(SQLITE_PATH_NODES, path_colocated_sql),
        "path_client": path_client,
        "path_join": path_direct(SQLITE_PATH_STRUCT, path_join_sql),
        "preorder_colocated": preorder_direct(SQLITE_NATIVE_NODES, preorder_colocated_sql),
        "preorder_join": preorder_direct(SQLITE_NATIVE_STRUCT, preorder_join_sql),
    }


def pg_plans(connection: Any, tree_id: str, node_id: str) -> dict[str, Any]:
    path = connection.execute(
        f"SELECT path FROM {PG_SOURCE_VIEW} WHERE tree_id=%s AND node_id=%s",
        (tree_id, node_id),
    ).fetchone()[0]
    preorder, subtree_end = connection.execute(
        f"SELECT preorder,subtree_end FROM {PG_NATIVE_NODES} "
        "WHERE tree_id=%s AND node_id=%s",
        (tree_id, node_id),
    ).fetchone()
    queries = {
        "path_colocated": (f"""
            SELECT child.node_id,child.title,child.summary FROM {PG_SOURCE_VIEW} child
            WHERE child.tree_id=%s AND child.path>=%s AND child.path<%s
            ORDER BY child.path,child.node_id
        """, (tree_id, path + "/", path + "0")),
        "path_join": (f"""
            SELECT child.node_id,meta.title,meta.summary FROM {PG_SOURCE_STRUCT} child
            JOIN {PG_SOURCE_META} meta ON meta.node_id=child.node_id
            WHERE child.tree_id=%s AND child.path>=%s AND child.path<%s
            ORDER BY child.path,child.node_id
        """, (tree_id, path + "/", path + "0")),
        "preorder_colocated": (f"""
            SELECT child.node_id,child.title,child.summary FROM {PG_NATIVE_NODES} child
            WHERE child.tree_id=%s AND child.preorder>%s AND child.preorder<%s
            ORDER BY child.preorder
        """, (tree_id, preorder, subtree_end)),
        "preorder_join": (f"""
            SELECT child.node_id,meta.title,meta.summary FROM {PG_NATIVE_STRUCT} child
            JOIN {PG_SOURCE_META} meta ON meta.node_id=child.node_id
            WHERE child.tree_id=%s AND child.preorder>%s AND child.preorder<%s
            ORDER BY child.preorder
        """, (tree_id, preorder, subtree_end)),
    }
    plans = {}
    for name, (sql, parameters) in queries.items():
        raw = connection.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
            parameters,
        ).fetchone()[0]
        plans[name] = raw if not isinstance(raw, str) else json.loads(raw)
    plans["root_lookup"] = {
        "path": [
            row[0] for row in connection.execute(
                f"EXPLAIN SELECT path FROM {PG_SOURCE_VIEW} "
                "WHERE tree_id=%s AND node_id=%s",
                (tree_id, node_id),
            ).fetchall()
        ],
        "preorder": [
            row[0] for row in connection.execute(
                f"EXPLAIN SELECT preorder,subtree_end FROM {PG_NATIVE_NODES} "
                "WHERE tree_id=%s AND node_id=%s",
                (tree_id, node_id),
            ).fetchall()
        ],
    }
    return plans


def sqlite_plans(connection: sqlite3.Connection, tree_id: str, node_id: str) -> dict[str, Any]:
    path = connection.execute(
        f"SELECT path FROM {SQLITE_PATH_NODES} WHERE tree_id=? AND node_id=?",
        (tree_id, node_id),
    ).fetchone()[0]
    preorder, subtree_end = connection.execute(
        f"SELECT preorder,subtree_end FROM {SQLITE_NATIVE_NODES} "
        "WHERE tree_id=? AND node_id=?",
        (tree_id, node_id),
    ).fetchone()
    queries = {
        "path_colocated": (f"""
            SELECT child.node_id,child.title,child.summary FROM {SQLITE_PATH_NODES} child
            WHERE child.tree_id=? AND child.path>=? AND child.path<?
            ORDER BY child.path,child.node_id
        """, (tree_id, path + "/", path + "0")),
        "path_join": (f"""
            SELECT child.node_id,meta.title,meta.summary FROM {SQLITE_PATH_STRUCT} child
            JOIN {SQLITE_META} meta ON meta.node_id=child.node_id
            WHERE child.tree_id=? AND child.path>=? AND child.path<?
            ORDER BY child.path,child.node_id
        """, (tree_id, path + "/", path + "0")),
        "preorder_colocated": (f"""
            SELECT child.node_id,child.title,child.summary FROM {SQLITE_NATIVE_NODES} child
            WHERE child.tree_id=? AND child.preorder>? AND child.preorder<?
            ORDER BY child.preorder
        """, (tree_id, preorder, subtree_end)),
        "preorder_join": (f"""
            SELECT child.node_id,meta.title,meta.summary FROM {SQLITE_NATIVE_STRUCT} child
            JOIN {SQLITE_META} meta ON meta.node_id=child.node_id
            WHERE child.tree_id=? AND child.preorder>? AND child.preorder<?
            ORDER BY child.preorder
        """, (tree_id, preorder, subtree_end)),
    }
    plans = {
        name: [tuple(row) for row in connection.execute(
            "EXPLAIN QUERY PLAN " + sql, parameters
        ).fetchall()]
        for name, (sql, parameters) in queries.items()
    }
    plans["root_lookup"] = {
        "path": [tuple(row) for row in connection.execute(
            f"EXPLAIN QUERY PLAN SELECT path FROM {SQLITE_PATH_NODES} "
            "WHERE tree_id=? AND node_id=?", (tree_id, node_id)
        ).fetchall()],
        "preorder": [tuple(row) for row in connection.execute(
            f"EXPLAIN QUERY PLAN SELECT preorder,subtree_end FROM {SQLITE_NATIVE_NODES} "
            "WHERE tree_id=? AND node_id=?", (tree_id, node_id)
        ).fetchall()],
    }
    return plans


def benchmark_queries(
    args: argparse.Namespace,
    output: dict[str, Any],
    queries: dict[str, Callable[[str, str], list[tuple[str, str, str]]]],
    plan_reader: Callable[[str, str], dict[str, Any]],
) -> None:
    expected = expected_samples(Path(args.expected), args.max_paths)
    output["contract"] = {
        "input": ["tree_id", "root_node_id"],
        "output": ["descendant_node_id", "title", "summary"],
        "root_excluded": True,
        "depth_limit": None,
        "order": "DFS preorder (equivalent to binary materialized-path order)",
        "validation": "row count and SHA-256 fingerprint against the existing 10M artifact",
    }
    output["arms"] = list(ARMS)
    output["samples"] = []
    output["run"] = {"status": "running", "repeats": args.repeats}

    log("warming all arms on the first three roots ...")
    for sample in expected[:3]:
        for arm in ARMS:
            rows = queries[arm](args.tree_id, sample["node_id"])
            if len(rows) != sample["rows"] or fingerprint(rows) != sample["fingerprint"]:
                raise RuntimeError(f"warm-up validation failed: {arm} {sample['path']}")

    log(f"timing {len(expected)} roots x {args.repeats} repeats x {len(ARMS)} arms ...")
    by_path: dict[str, dict[str, Any]] = {
        sample["path"]: {
            "path": sample["path"],
            "node_id": sample["node_id"],
            "rows": sample["rows"],
            "fingerprint": sample["fingerprint"],
            "repeats_ms": {arm: [] for arm in ARMS},
        }
        for sample in expected
    }
    for repeat in range(args.repeats):
        for path_index, expected_sample in enumerate(expected):
            rotation = (repeat + path_index) % len(ARMS)
            arm_order = ARMS[rotation:] + ARMS[:rotation]
            sample = by_path[expected_sample["path"]]
            for arm in arm_order:
                started = time.perf_counter()
                rows = queries[arm](args.tree_id, expected_sample["node_id"])
                elapsed_ms = (time.perf_counter() - started) * 1_000
                actual_fingerprint = fingerprint(rows)
                if len(rows) != expected_sample["rows"]:
                    raise RuntimeError(
                        f"row-count mismatch {arm} {expected_sample['path']}: "
                        f"{len(rows)} != {expected_sample['rows']}"
                    )
                if actual_fingerprint != expected_sample["fingerprint"]:
                    raise RuntimeError(
                        f"fingerprint mismatch {arm} {expected_sample['path']}: "
                        f"{actual_fingerprint} != {expected_sample['fingerprint']}"
                    )
                sample["repeats_ms"][arm].append(round(elapsed_ms, 6))
                del rows
            if (path_index + 1) % 20 == 0:
                log(
                    f"      repeat {repeat + 1}/{args.repeats}: "
                    f"{path_index + 1}/{len(expected)} roots"
                )
        output["samples"] = list(by_path.values())
        Path(args.out).write_text(json.dumps(output, indent=2))

    output["summaries"] = {
        arm: summarize(list(by_path.values()), arm) for arm in ARMS
    }
    first = expected[0]
    output["plans"] = plan_reader(args.tree_id, first["node_id"])
    output["validation"] = {
        "expected_artifact": args.expected,
        "paths": len(expected),
        "arms": len(ARMS),
        "repeats": args.repeats,
        "all_row_counts_and_fingerprints_match": True,
    }
    output["samples"] = list(by_path.values())
    output["run"]["status"] = "complete"


def run_postgres(args: argparse.Namespace, output: dict[str, Any]) -> None:
    import psycopg

    connection = psycopg.connect(args.pg_dsn, autocommit=True)
    require_pg_sources(connection)
    for table in (PG_NATIVE_NODES, PG_NATIVE_STRUCT):
        if connection.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
            raise SystemExit(f"missing {table}; run --phase build first")
    queries = pg_queries(connection, args.chunk)
    benchmark_queries(
        args,
        output,
        queries,
        lambda tree_id, node_id: pg_plans(connection, tree_id, node_id),
    )
    output["postgres_version"] = connection.execute("SHOW server_version").fetchone()[0]
    connection.close()


def run_sqlite(args: argparse.Namespace, output: dict[str, Any]) -> None:
    connection = sqlite3.connect(args.sqlite_path)
    connection.execute("PRAGMA query_only=ON")
    variable_limit = connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    chunk = min(args.chunk, variable_limit)
    queries = sqlite_queries(connection, chunk)
    benchmark_queries(
        args,
        output,
        queries,
        lambda tree_id, node_id: sqlite_plans(connection, tree_id, node_id),
    )
    output["sqlite_version"] = sqlite3.sqlite_version
    output["sqlite_variable_limit"] = variable_limit
    output["effective_chunk"] = chunk
    output["storage"] = sqlite_storage(connection, Path(args.sqlite_path))
    connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, choices=("postgres", "sqlite"))
    parser.add_argument("--phase", choices=("build", "run", "all"), default="all")
    parser.add_argument(
        "--pg-dsn",
        default="host=localhost port=55432 dbname=bench user=postgres password=bench",
    )
    parser.add_argument("--sqlite-source", default="bench/db/runs/_sqlite.db")
    parser.add_argument("--sqlite-path", default="bench/db/runs/_sql_native_10m.sqlite")
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--expected",
        default="bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json",
    )
    parser.add_argument(
        "--tree-id",
        help="source tree identifier (defaults to base for PostgreSQL and large for SQLite)",
    )
    parser.add_argument("--max-paths", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--chunk", type=int, default=1_000)
    parser.add_argument("--fetch-batch", type=int, default=20_000)
    parser.add_argument("--insert-batch", type=int, default=50_000)
    args = parser.parse_args()
    if args.tree_id is None:
        args.tree_id = "base" if args.engine == "postgres" else "large"
    if min(args.max_paths, args.repeats, args.chunk, args.fetch_batch, args.insert_batch) <= 0:
        parser.error("path/repeat/batch arguments must be positive")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output: dict[str, Any] = {
        "engine": args.engine,
        "nodes": 10_000_000,
        "source": {
            "expected": args.expected,
            "postgres_tables": [PG_SOURCE_VIEW, PG_SOURCE_STRUCT, PG_SOURCE_META],
            "sqlite_database": args.sqlite_source,
        },
        "build": {"status": "not_run"},
    }
    if out_path.exists():
        previous = json.loads(out_path.read_text())
        if args.phase == "run" and previous.get("build", {}).get("status") == "complete":
            output["build"] = previous["build"]
            output["storage"] = previous.get("storage", {})

    if args.phase in ("build", "all"):
        if args.engine == "postgres":
            build_postgres(args, output)
        else:
            build_sqlite(args, output)
        out_path.write_text(json.dumps(output, indent=2))
        log(f"wrote build artifact {out_path}")

    if args.phase in ("run", "all"):
        if args.engine == "postgres":
            run_postgres(args, output)
        else:
            run_sqlite(args, output)
        out_path.write_text(json.dumps(output, indent=2))
        print(f"\n{args.engine} SQL-native subtree benchmark")
        for arm in ARMS:
            stats = output["summaries"][arm]
            print(
                f"  {arm:21s} p50={stats['p50_ms']:9.3f} ms "
                f"p95={stats['p95_ms']:9.3f} ms p99={stats['p99_ms']:9.3f} ms"
            )
        log(f"wrote complete artifact {out_path}")


if __name__ == "__main__":
    main()
