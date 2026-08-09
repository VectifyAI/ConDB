#!/usr/bin/env python3
"""Benchmark ConDB's four storage reads across five engine/layout configurations.

Configurations:

* MongoDB, materialized path, two collections (nodes + text);
* PostgreSQL, materialized path, two tables (nodes + text);
* PostgreSQL, DFS preorder interval, two tables (nodes + text);
* SQLite, materialized path, two tables (nodes + text);
* SQLite, DFS preorder interval, two tables (nodes + text).

Every operation starts from ``(tree_id, node_id)`` and fully materializes its
result.  The four operations are get_node, get_children, get_subtree, and
get_entity.  The subtree contract matches the existing 10M artifact: exclude
the root, apply no depth limit, and return ordered ``(node_id,title,summary)``.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


MONGO_NODES = "layout2_view"
MONGO_TEXT = "layout_shared_text"
MONGO_SUBTREE_INDEX = "layout2_rootcause_exact_cover"

PG_PATH_NODES = "layout2_pg_view"
PG_PREORDER_NODES = "sql_native_nodes"
PG_TEXT = "layout_shared_pg_text"

SQLITE_PATH_NODES = "path_nodes"
SQLITE_PREORDER_NODES = "preorder_nodes"
SQLITE_SOURCE_NODES = "nodes"

OPERATIONS = ("get_node", "get_children", "get_subtree", "get_entity")
CONFIGURATIONS = (
    "mongo_path",
    "postgres_path",
    "postgres_preorder",
    "sqlite_path",
    "sqlite_preorder",
)
TREE_IDS = {
    "mongo_path": "base",
    "postgres_path": "base",
    "postgres_preorder": "base",
    "sqlite_path": "large",
    "sqlite_preorder": "large",
}

Rows = list[tuple[Any, ...]]
Query = Callable[[str, str], Rows]


def log(message: str) -> None:
    print(message, flush=True)


def percentile(values: Sequence[float], pct: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return round(ordered[index], 6)


def normalize(rows: Iterable[Sequence[Any]]) -> Rows:
    return [
        tuple("" if value is None else value for value in row)
        for row in rows
    ]


def fingerprint(rows: Sequence[Sequence[Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        for value in row:
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def load_tree_samples(path: Path, limit: int) -> list[dict[str, Any]]:
    document = json.loads(path.read_text())
    samples = document["samples"][:limit]
    for sample in samples:
        sample["node_id"] = sample["path"].rsplit("/", 1)[-1]
    return samples


def prepare_indexes(mongo: Any, pg: Any, sqlite_native: Any) -> dict[str, float]:
    timings: dict[str, float] = {}

    started = time.perf_counter()
    mongo[MONGO_NODES].create_index(
        [("tree_id", 1), ("node_id", 1)],
        name="allops_tree_node",
        unique=True,
    )
    mongo[MONGO_NODES].create_index(
        [("tree_id", 1), ("parent_id", 1), ("path", 1), ("node_id", 1)],
        name="allops_tree_parent_path",
    )
    timings["mongo_s"] = round(time.perf_counter() - started, 3)

    started = time.perf_counter()
    pg.execute(
        f"CREATE INDEX IF NOT EXISTS allops_path_parent "
        f"ON {PG_PATH_NODES}(tree_id,parent_id,path,node_id)"
    )
    pg.execute(
        f"CREATE INDEX IF NOT EXISTS allops_preorder_parent "
        f"ON {PG_PREORDER_NODES}(tree_id,parent_id,preorder,node_id)"
    )
    pg.execute(f"VACUUM (ANALYZE, PARALLEL 0) {PG_PATH_NODES}")
    pg.execute(f"VACUUM (ANALYZE, PARALLEL 0) {PG_PREORDER_NODES}")
    timings["postgres_s"] = round(time.perf_counter() - started, 3)

    started = time.perf_counter()
    sqlite_native.execute(
        f"CREATE INDEX IF NOT EXISTS allops_path_parent "
        f"ON {SQLITE_PATH_NODES}(tree_id,parent_id,path,node_id)"
    )
    sqlite_native.execute(
        f"CREATE INDEX IF NOT EXISTS allops_preorder_parent "
        f"ON {SQLITE_PREORDER_NODES}(tree_id,parent_id,preorder,node_id)"
    )
    sqlite_native.execute("ANALYZE")
    sqlite_native.commit()
    timings["sqlite_s"] = round(time.perf_counter() - started, 3)
    return timings


def mongo_queries(database: Any) -> dict[str, Query]:
    nodes = database[MONGO_NODES]
    text = database[MONGO_TEXT]

    def get_node(tree_id: str, node_id: str) -> Rows:
        row = nodes.find_one(
            {"tree_id": tree_id, "node_id": node_id},
            {
                "_id": 0,
                "node_id": 1,
                "parent_id": 1,
                "depth": 1,
                "title": 1,
                "summary": 1,
                "start_index": 1,
                "end_index": 1,
            },
            hint="allops_tree_node",
        )
        if row is None:
            return []
        return normalize([(
            row.get("node_id"),
            row.get("parent_id"),
            row.get("depth"),
            row.get("title"),
            row.get("summary"),
            row.get("start_index"),
            row.get("end_index"),
        )])

    def get_children(tree_id: str, node_id: str) -> Rows:
        cursor = (
            nodes.find(
                {"tree_id": tree_id, "parent_id": node_id},
                {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
            )
            .sort([("path", 1), ("node_id", 1)])
            .hint("allops_tree_parent_path")
        )
        return normalize(
            (row.get("node_id"), row.get("title"), row.get("summary"))
            for row in cursor
        )

    def get_subtree(tree_id: str, node_id: str) -> Rows:
        root = nodes.find_one(
            {"tree_id": tree_id, "node_id": node_id},
            {"_id": 0, "path": 1},
            hint="allops_tree_node",
        )
        if root is None:
            return []
        lower, upper = root["path"] + "/", root["path"] + "0"
        cursor = (
            nodes.find(
                {"path": {"$gte": lower, "$lt": upper}},
                {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
            )
            .sort([("path", 1), ("node_id", 1)])
            .hint(MONGO_SUBTREE_INDEX)
        )
        return normalize(
            (row.get("node_id"), row.get("title"), row.get("summary"))
            for row in cursor
        )

    def get_entity(_: str, node_id: str) -> Rows:
        row = text.find_one({"_id": node_id}, {"_id": 1, "text": 1})
        return normalize([(node_id, row.get("text"))]) if row else []

    return {
        "get_node": get_node,
        "get_children": get_children,
        "get_subtree": get_subtree,
        "get_entity": get_entity,
    }


def postgres_queries(connection: Any, layout: str) -> dict[str, Query]:
    if layout == "path":
        table = PG_PATH_NODES
        order_column = "path"
    else:
        table = PG_PREORDER_NODES
        order_column = "preorder"

    def get_node(tree_id: str, node_id: str) -> Rows:
        return normalize(connection.execute(
            f"""
            SELECT node_id,parent_id,depth,title,summary,start_index,end_index
            FROM {table}
            WHERE tree_id=%s AND node_id=%s
            """,
            (tree_id, node_id),
        ).fetchall())

    def get_children(tree_id: str, node_id: str) -> Rows:
        return normalize(connection.execute(
            f"""
            SELECT node_id,title,summary
            FROM {table}
            WHERE tree_id=%s AND parent_id=%s
            ORDER BY {order_column},node_id
            """,
            (tree_id, node_id),
        ).fetchall())

    if layout == "path":
        def get_subtree(tree_id: str, node_id: str) -> Rows:
            root = connection.execute(
                f"SELECT path FROM {table} WHERE tree_id=%s AND node_id=%s",
                (tree_id, node_id),
            ).fetchone()
            if root is None:
                return []
            lower, upper = root[0] + "/", root[0] + "0"
            return normalize(connection.execute(
                f"""
                SELECT node_id,title,summary
                FROM {table}
                WHERE tree_id=%s AND path>=%s AND path<%s
                ORDER BY path,node_id
                """,
                (tree_id, lower, upper),
            ).fetchall())
    else:
        def get_subtree(tree_id: str, node_id: str) -> Rows:
            root = connection.execute(
                f"""
                SELECT preorder,subtree_end FROM {table}
                WHERE tree_id=%s AND node_id=%s
                """,
                (tree_id, node_id),
            ).fetchone()
            if root is None:
                return []
            return normalize(connection.execute(
                f"""
                SELECT node_id,title,summary
                FROM {table}
                WHERE tree_id=%s AND preorder>%s AND preorder<%s
                ORDER BY preorder
                """,
                (tree_id, root[0], root[1]),
            ).fetchall())

    def get_entity(_: str, node_id: str) -> Rows:
        return normalize(connection.execute(
            f"SELECT node_id,text FROM {PG_TEXT} WHERE node_id=%s",
            (node_id,),
        ).fetchall())

    return {
        "get_node": get_node,
        "get_children": get_children,
        "get_subtree": get_subtree,
        "get_entity": get_entity,
    }


def sqlite_queries(native: Any, source: Any, layout: str) -> dict[str, Query]:
    if layout == "path":
        table = SQLITE_PATH_NODES
        order_column = "path"
    else:
        table = SQLITE_PREORDER_NODES
        order_column = "preorder"

    def get_node(tree_id: str, node_id: str) -> Rows:
        return normalize(native.execute(
            f"""
            SELECT node_id,parent_id,depth,title,summary,start_index,end_index
            FROM {table}
            WHERE tree_id=? AND node_id=?
            """,
            (tree_id, node_id),
        ).fetchall())

    def get_children(tree_id: str, node_id: str) -> Rows:
        return normalize(native.execute(
            f"""
            SELECT node_id,title,summary
            FROM {table}
            WHERE tree_id=? AND parent_id=?
            ORDER BY {order_column},node_id
            """,
            (tree_id, node_id),
        ).fetchall())

    if layout == "path":
        def get_subtree(tree_id: str, node_id: str) -> Rows:
            root = native.execute(
                f"SELECT path FROM {table} WHERE tree_id=? AND node_id=?",
                (tree_id, node_id),
            ).fetchone()
            if root is None:
                return []
            lower, upper = root[0] + "/", root[0] + "0"
            return normalize(native.execute(
                f"""
                SELECT node_id,title,summary
                FROM {table}
                WHERE tree_id=? AND path>=? AND path<?
                ORDER BY path,node_id
                """,
                (tree_id, lower, upper),
            ).fetchall())
    else:
        def get_subtree(tree_id: str, node_id: str) -> Rows:
            root = native.execute(
                f"""
                SELECT preorder,subtree_end FROM {table}
                WHERE tree_id=? AND node_id=?
                """,
                (tree_id, node_id),
            ).fetchone()
            if root is None:
                return []
            return normalize(native.execute(
                f"""
                SELECT node_id,title,summary
                FROM {table}
                WHERE tree_id=? AND preorder>? AND preorder<?
                ORDER BY preorder
                """,
                (tree_id, root[0], root[1]),
            ).fetchall())

    def get_entity(tree_id: str, node_id: str) -> Rows:
        return normalize(source.execute(
            f"""
            SELECT node_id,text FROM {SQLITE_SOURCE_NODES}
            WHERE tree_id=? AND node_id=?
            """,
            (tree_id, node_id),
        ).fetchall())

    return {
        "get_node": get_node,
        "get_children": get_children,
        "get_subtree": get_subtree,
        "get_entity": get_entity,
    }


def summarize(samples: list[dict[str, Any]], operation: str, config: str) -> dict[str, Any]:
    elapsed = [
        statistics.median(sample["times_ms"][config])
        for sample in samples
    ]
    return {
        "inputs": len(samples),
        "repeats": len(samples[0]["times_ms"][config]) if samples else 0,
        "p50_ms": percentile(elapsed, 50),
        "p95_ms": percentile(elapsed, 95),
        "p99_ms": percentile(elapsed, 99),
        "mean_ms": round(statistics.mean(elapsed), 6) if elapsed else 0.0,
        "avg_rows": round(statistics.mean(sample["rows"] for sample in samples), 3)
        if samples else 0.0,
        "operation": operation,
    }


def benchmark(
    queries: dict[str, dict[str, Query]],
    inputs: dict[str, list[str]],
    subtree_samples: list[dict[str, Any]],
    repeats: int,
    out_path: Path,
    output: dict[str, Any],
) -> None:
    expected: dict[str, dict[str, dict[str, Any]]] = {
        operation: {} for operation in OPERATIONS
    }

    subtree_by_id = {sample["node_id"]: sample for sample in subtree_samples}
    for node_id in inputs["get_subtree"]:
        sample = subtree_by_id[node_id]
        expected["get_subtree"][node_id] = {
            "rows": sample["rows"],
            "fingerprint": sample["fingerprint"],
        }

    reference = queries["postgres_path"]
    for operation in ("get_node", "get_children", "get_entity"):
        log(f"building reference fingerprints: {operation}")
        for node_id in inputs[operation]:
            rows = reference[operation](TREE_IDS["postgres_path"], node_id)
            expected[operation][node_id] = {
                "rows": len(rows),
                "fingerprint": fingerprint(rows),
            }

    output["samples"] = {}
    for operation in OPERATIONS:
        log(f"warming {operation}")
        for node_id in inputs[operation][:3]:
            target = expected[operation][node_id]
            for config in CONFIGURATIONS:
                rows = queries[config][operation](TREE_IDS[config], node_id)
                if len(rows) != target["rows"] or fingerprint(rows) != target["fingerprint"]:
                    raise RuntimeError(f"warm-up mismatch: {operation} {config} {node_id}")

        operation_samples = [
            {
                "node_id": node_id,
                "rows": expected[operation][node_id]["rows"],
                "fingerprint": expected[operation][node_id]["fingerprint"],
                "times_ms": {config: [] for config in CONFIGURATIONS},
            }
            for node_id in inputs[operation]
        ]
        log(
            f"timing {operation}: {len(operation_samples)} inputs x "
            f"{repeats} repeats x {len(CONFIGURATIONS)} configurations"
        )
        for repeat in range(repeats):
            for input_index, sample in enumerate(operation_samples):
                rotation = (repeat + input_index) % len(CONFIGURATIONS)
                order = CONFIGURATIONS[rotation:] + CONFIGURATIONS[:rotation]
                for config in order:
                    gc.disable()
                    try:
                        started = time.perf_counter()
                        rows = queries[config][operation](
                            TREE_IDS[config], sample["node_id"]
                        )
                        elapsed_ms = (time.perf_counter() - started) * 1_000
                    finally:
                        gc.enable()
                    if len(rows) != sample["rows"]:
                        raise RuntimeError(
                            f"row mismatch: {operation} {config} {sample['node_id']} "
                            f"{len(rows)} != {sample['rows']}"
                        )
                    if fingerprint(rows) != sample["fingerprint"]:
                        raise RuntimeError(
                            f"fingerprint mismatch: {operation} {config} {sample['node_id']}"
                        )
                    sample["times_ms"][config].append(round(elapsed_ms, 6))
                    del rows
                if (input_index + 1) % 50 == 0:
                    log(
                        f"  {operation} repeat {repeat + 1}/{repeats}: "
                        f"{input_index + 1}/{len(operation_samples)}"
                    )
            output["samples"][operation] = operation_samples
            out_path.write_text(json.dumps(output, indent=2))

    output["summaries"] = {
        operation: {
            config: summarize(output["samples"][operation], operation, config)
            for config in CONFIGURATIONS
        }
        for operation in OPERATIONS
    }
    output["validation"] = {
        "all_row_counts_and_fingerprints_match": True,
        "operations": len(OPERATIONS),
        "configurations": len(CONFIGURATIONS),
        "timed_observations": sum(
            len(inputs[operation]) * repeats * len(CONFIGURATIONS)
            for operation in OPERATIONS
        ),
    }
    output["run"]["status"] = "complete"
    out_path.write_text(json.dumps(output, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mongo-uri",
        default="mongodb://localhost:57017",
    )
    parser.add_argument("--mongo-db", default="bench")
    parser.add_argument(
        "--pg-dsn",
        default="host=localhost port=55432 dbname=bench user=postgres password=bench",
    )
    parser.add_argument(
        "--sqlite-native",
        default="bench/db/runs/_sql_native_10m.sqlite",
    )
    parser.add_argument(
        "--sqlite-source",
        default="bench/db/runs/_sqlite.db",
    )
    parser.add_argument(
        "--expected",
        default="bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json",
    )
    parser.add_argument(
        "--out",
        default="bench/db/runs/all_ops_layouts_20260723/all_ops_10m.json",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--point-inputs", type=int, default=500)
    parser.add_argument("--tree-inputs", type=int, default=200)
    parser.add_argument("--skip-prepare", action="store_true")
    args = parser.parse_args()

    if min(args.repeats, args.point_inputs, args.tree_inputs) <= 0:
        parser.error("repeats and input counts must be positive")

    from pymongo import MongoClient
    import psycopg
    import sqlite3

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    mongo_client = MongoClient(args.mongo_uri)
    mongo = mongo_client[args.mongo_db]
    pg = psycopg.connect(args.pg_dsn, autocommit=True)
    sqlite_native = sqlite3.connect(args.sqlite_native)
    sqlite_source = sqlite3.connect(args.sqlite_source)
    sqlite_native.execute("PRAGMA query_only=OFF")
    sqlite_source.execute("PRAGMA query_only=ON")

    output: dict[str, Any] = {
        "run": {
            "status": "preparing",
            "repeats": args.repeats,
        },
        "contract": {
            "operations": list(OPERATIONS),
            "configurations": list(CONFIGURATIONS),
            "tree_ids": TREE_IDS,
            "input": ["tree_id", "node_id"],
            "get_node_output": [
                "node_id",
                "parent_id",
                "depth",
                "title",
                "summary",
                "start_index",
                "end_index",
            ],
            "get_children_output": ["node_id", "title", "summary"],
            "get_subtree_output": ["node_id", "title", "summary"],
            "get_entity_output": ["node_id", "text"],
            "subtree_root_excluded": True,
            "subtree_depth_limit": None,
        },
        "sources": {
            "expected": args.expected,
            "mongo_nodes": MONGO_NODES,
            "mongo_text": MONGO_TEXT,
            "postgres_path_nodes": PG_PATH_NODES,
            "postgres_preorder_nodes": PG_PREORDER_NODES,
            "postgres_text": PG_TEXT,
            "sqlite_native": args.sqlite_native,
            "sqlite_source": args.sqlite_source,
        },
    }

    if not args.skip_prepare:
        log("preparing missing point/parent indexes")
        output["index_prepare_seconds"] = prepare_indexes(mongo, pg, sqlite_native)
    else:
        output["index_prepare_seconds"] = {"skipped": True}

    tree_samples = load_tree_samples(Path(args.expected), args.tree_inputs)
    entity_ids = [
        row[0]
        for row in pg.execute(
            f"SELECT node_id FROM {PG_TEXT} ORDER BY node_id LIMIT %s",
            (args.point_inputs,),
        ).fetchall()
    ]
    if len(entity_ids) != args.point_inputs:
        raise RuntimeError(f"only found {len(entity_ids)} entity IDs")
    tree_ids = [sample["node_id"] for sample in tree_samples]
    inputs = {
        "get_node": entity_ids,
        "get_children": tree_ids,
        "get_subtree": tree_ids,
        "get_entity": entity_ids,
    }

    queries = {
        "mongo_path": mongo_queries(mongo),
        "postgres_path": postgres_queries(pg, "path"),
        "postgres_preorder": postgres_queries(pg, "preorder"),
        "sqlite_path": sqlite_queries(sqlite_native, sqlite_source, "path"),
        "sqlite_preorder": sqlite_queries(sqlite_native, sqlite_source, "preorder"),
    }
    output["run"]["status"] = "running"
    output["inputs"] = {
        operation: len(values) for operation, values in inputs.items()
    }
    out_path.write_text(json.dumps(output, indent=2))

    benchmark(
        queries,
        inputs,
        tree_samples,
        args.repeats,
        out_path,
        output,
    )

    output["versions"] = {
        "mongodb": mongo_client.server_info()["version"],
        "postgresql": pg.execute("SHOW server_version").fetchone()[0],
        "sqlite": sqlite3.sqlite_version,
    }
    out_path.write_text(json.dumps(output, indent=2))
    print(json.dumps(output["summaries"], indent=2))

    sqlite_source.close()
    sqlite_native.close()
    pg.close()
    mongo_client.close()


if __name__ == "__main__":
    main()
