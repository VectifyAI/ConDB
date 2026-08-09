#!/usr/bin/env python3
"""PostgreSQL hierarchy-layout sensitivity for descendant-ID enumeration.

This is deliberately not a MongoDB/PostgreSQL ratio benchmark.  It asks a
narrower question: given the real API's root ``node_id``, which PostgreSQL
hierarchy representation is the best fit for enumerating every descendant ID?

Compared layouts:

* ``path``: materialized TEXT path, two covered B-tree probes.
* ``preorder``: root lookup plus a covered BIGINT preorder range.
* ``ltree``: PostgreSQL's hierarchy extension with a tuned GiST signature.
* ``recursive``: adjacency list traversed with ``WITH RECURSIVE``.
* ``closure``: precomputed ancestor/descendant rows (fast reads, amplified
  storage and writes).

All queries start from node_id, exclude the root itself, return the same IDs,
and are timed in cyclically rotated order.  Reported percentiles are across
the per-path median of repeated measurements, not across a mixture of paths
and repeats.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from array import array
from collections import defaultdict
from pathlib import Path

from bench_databases import flatten


TABLE = "pg_hierarchy_nodes"
CLOSURE = "pg_hierarchy_closure"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def guard(floor_gb: float = 10.0) -> None:
    free_gb = shutil.disk_usage("/").free / 1e9
    if free_gb < floor_gb:
        raise SystemExit(f"ABORT: free disk {free_gb:.1f}GB < {floor_gb}GB")


def preorder_ends(rows: list[dict]) -> array:
    ends = array("q", [len(rows)]) * len(rows)
    stack: list[tuple[int, int]] = []
    for preorder, row in enumerate(rows):
        depth = row["depth"]
        while stack and stack[-1][0] >= depth:
            _, root_preorder = stack.pop()
            ends[root_preorder] = preorder
        stack.append((depth, preorder))
    while stack:
        _, root_preorder = stack.pop()
        ends[root_preorder] = len(rows)
    return ends


def as_ltree(path: str) -> str:
    return path.strip("/").replace("/", ".")


def percentile(values: list[float], pct: int) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return round(ordered[index], 3)


def summarize(per_path: dict[str, list[float]], row_counts: dict[str, int]) -> dict:
    medians = [statistics.median(samples) for samples in per_path.values()]
    return {
        "paths": len(medians),
        "p50_ms": percentile(medians, 50),
        "p95_ms": percentile(medians, 95),
        "p99_ms": percentile(medians, 99),
        "mean_ms": round(statistics.mean(medians), 3),
        "avg_rows": round(statistics.mean(row_counts.values()), 1),
    }


def walk_plan(node: dict) -> list[dict]:
    result = [{
        "node": node.get("Node Type"),
        "index": node.get("Index Name"),
        "actual_rows": node.get("Actual Rows"),
        "actual_loops": node.get("Actual Loops"),
        "heap_fetches": node.get("Heap Fetches"),
        "rows_removed_by_recheck": node.get("Rows Removed by Index Recheck"),
        "shared_hit_blocks": node.get("Shared Hit Blocks"),
        "shared_read_blocks": node.get("Shared Read Blocks"),
    }]
    for child in node.get("Plans", []):
        result.extend(walk_plan(child))
    return result


def explain(conn, sql: str, node_id: str) -> dict:
    raw = conn.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, (node_id,)
    ).fetchone()[0]
    document = json.loads(raw) if isinstance(raw, str) else raw
    return {
        "planning_ms": document[0].get("Planning Time"),
        "execution_ms": document[0].get("Execution Time"),
        "nodes": walk_plan(document[0]["Plan"]),
    }


def relation_bytes(conn, relation: str) -> int:
    return conn.execute(
        "SELECT pg_total_relation_size(%s::regclass)", (relation,)
    ).fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", default="bench/db/data/medium.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-paths", type=int, default=200)
    parser.add_argument("--ltree-siglen", type=int, default=100)
    parser.add_argument(
        "--pg-dsn",
        default="host=localhost port=55432 dbname=bench user=postgres password=bench",
    )
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    guard()
    import psycopg

    log(f"loading {args.doc} ...")
    started = time.time()
    document = json.loads(Path(args.doc).read_text())
    records = flatten(document, tree_id="base", seed=7)
    del document
    rows = records.rows
    paths = records.subtree_paths[: args.max_paths]
    path_to_id = {row["path"]: row["node_id"] for row in rows}
    root_ids = [path_to_id[path] for path in paths]
    ends = preorder_ends(rows)
    log(f"flattened {len(rows):,} nodes and {len(paths)} paths in {time.time()-started:.1f}s")

    conn = psycopg.connect(args.pg_dsn, autocommit=True)
    output = {
        "doc": args.doc,
        "postgres_version": conn.execute("SHOW server_version").fetchone()[0],
        "nodes": len(rows),
        "paths": len(paths),
        "repeats": args.repeats,
        "ltree_siglen": args.ltree_siglen,
        "contract": "input node_id; output every descendant node_id; root excluded",
        "layouts": {},
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        out_path.write_text(json.dumps(output, indent=2))

    def drop() -> None:
        conn.execute(f"DROP TABLE IF EXISTS {CLOSURE}")
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")

    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS ltree")
        drop()
        log("building shared hierarchy table ...")
        build_started = time.time()
        conn.execute(f"""
            CREATE TABLE {TABLE} (
                node_id TEXT COLLATE "C" NOT NULL,
                parent_id TEXT COLLATE "C",
                depth INTEGER NOT NULL,
                path TEXT COLLATE "C" NOT NULL,
                lpath LTREE NOT NULL,
                preorder BIGINT NOT NULL,
                subtree_end BIGINT NOT NULL
            )
        """)
        with conn.cursor() as cursor:
            with cursor.copy(
                f"COPY {TABLE} "
                "(node_id,parent_id,depth,path,lpath,preorder,subtree_end) FROM STDIN"
            ) as copy:
                for preorder, row in enumerate(rows):
                    copy.write_row((
                        row["node_id"], row["parent_id"], row["depth"], row["path"],
                        as_ltree(row["path"]), preorder, ends[preorder],
                    ))
        del ends
        conn.execute(
            f"CREATE UNIQUE INDEX pg_hierarchy_node_cover ON {TABLE} (node_id) "
            "INCLUDE (path,lpath,preorder,subtree_end,depth)"
        )
        conn.execute(
            f"CREATE INDEX pg_hierarchy_path_cover ON {TABLE} (path) "
            "INCLUDE (node_id,depth)"
        )
        conn.execute(
            f"CREATE INDEX pg_hierarchy_preorder_cover ON {TABLE} (preorder) "
            "INCLUDE (node_id,depth)"
        )
        conn.execute(
            f"CREATE INDEX pg_hierarchy_ltree_cover ON {TABLE} USING GIST "
            f"(lpath gist_ltree_ops(siglen={args.ltree_siglen})) INCLUDE (node_id,depth)"
        )
        conn.execute(
            f"CREATE INDEX pg_hierarchy_parent_cover ON {TABLE} (parent_id) "
            "INCLUDE (node_id,depth)"
        )
        conn.execute(f"VACUUM (ANALYZE, PARALLEL 0) {TABLE}")
        output["shared_build_s"] = round(time.time() - build_started, 1)
        output["shared_storage_bytes"] = relation_bytes(conn, TABLE)
        save()

        log("building closure table ...")
        closure_started = time.time()
        conn.execute(f"""
            CREATE TABLE {CLOSURE} (
                ancestor_id TEXT COLLATE "C" NOT NULL,
                descendant_preorder BIGINT NOT NULL,
                descendant_id TEXT COLLATE "C" NOT NULL,
                relative_depth INTEGER NOT NULL
            )
        """)
        closure_rows = 0
        stack: list[tuple[int, str]] = []
        with conn.cursor() as cursor:
            with cursor.copy(
                f"COPY {CLOSURE} "
                "(ancestor_id,descendant_preorder,descendant_id,relative_depth) "
                "FROM STDIN"
            ) as copy:
                for preorder, row in enumerate(rows):
                    depth = row["depth"]
                    while stack and stack[-1][0] >= depth:
                        stack.pop()
                    for ancestor_depth, ancestor_id in stack:
                        copy.write_row((
                            ancestor_id, preorder, row["node_id"],
                            depth - ancestor_depth,
                        ))
                        closure_rows += 1
                    stack.append((depth, row["node_id"]))
        conn.execute(
            f"CREATE INDEX pg_hierarchy_closure_cover ON {CLOSURE} "
            "(ancestor_id,descendant_preorder) INCLUDE (descendant_id,relative_depth)"
        )
        conn.execute(f"VACUUM (ANALYZE, PARALLEL 0) {CLOSURE}")
        output["closure"] = {
            "rows": closure_rows,
            "rows_per_node": round(closure_rows / len(rows), 3),
            "build_s": round(time.time() - closure_started, 1),
            "storage_bytes": relation_bytes(conn, CLOSURE),
        }
        save()

        queries = {
            "path": (
                f"SELECT child.node_id FROM {TABLE} root JOIN {TABLE} child "
                "ON child.path >= root.path || '/' "
                "AND child.path < root.path || '0' WHERE root.node_id = %s"
            ),
            "preorder": (
                f"SELECT child.node_id FROM {TABLE} root JOIN {TABLE} child "
                "ON child.preorder >= root.preorder + 1 "
                "AND child.preorder < root.subtree_end WHERE root.node_id = %s"
            ),
            "ltree": (
                f"SELECT child.node_id FROM {TABLE} root JOIN {TABLE} child "
                "ON child.lpath <@ root.lpath "
                "WHERE root.node_id = %s AND child.node_id <> root.node_id"
            ),
            "recursive": (
                "WITH RECURSIVE sub(node_id,level) AS ("
                f"SELECT node_id,0 FROM {TABLE} WHERE node_id=%s UNION ALL "
                f"SELECT child.node_id,sub.level+1 FROM {TABLE} child "
                "JOIN sub ON child.parent_id=sub.node_id) "
                "SELECT node_id FROM sub WHERE level>0"
            ),
            "closure": (
                f"SELECT descendant_id FROM {CLOSURE} WHERE ancestor_id=%s"
            ),
        }

        def execute(name: str, node_id: str) -> list[str]:
            return [row[0] for row in conn.execute(queries[name], (node_id,)).fetchall()]

        log("validating every layout on every selected root ...")
        mismatches = defaultdict(list)
        row_counts = {}
        for index, node_id in enumerate(root_ids):
            reference = sorted(execute("path", node_id))
            row_counts[node_id] = len(reference)
            for name in queries:
                if name == "path":
                    continue
                if sorted(execute(name, node_id)) != reference:
                    mismatches[name].append(paths[index])
            if (index + 1) % 25 == 0:
                log(f"  validated {index + 1}/{len(root_ids)}")
        output["validation"] = {
            name: {
                "mismatches": mismatches[name][:10],
                "hard_pass": not mismatches[name],
            }
            for name in queries
        }
        save()
        if any(mismatches.values()):
            raise RuntimeError(f"layout output mismatch: {dict(mismatches)}")

        log("warming layouts ...")
        names = list(queries)
        for node_id in root_ids[:3]:
            for name in names:
                execute(name, node_id)

        log(f"timing {len(root_ids)} roots x {args.repeats} repeats x {len(names)} layouts ...")
        timings = {name: defaultdict(list) for name in names}
        for repeat in range(args.repeats):
            for index, node_id in enumerate(root_ids):
                offset = (repeat + index) % len(names)
                order = names[offset:] + names[:offset]
                for name in order:
                    call_started = time.perf_counter()
                    result = execute(name, node_id)
                    timings[name][node_id].append(
                        (time.perf_counter() - call_started) * 1000.0
                    )
                    if len(result) != row_counts[node_id]:
                        raise RuntimeError(f"timed row-count mismatch: {name}/{node_id}")
            log(f"  repeat {repeat + 1}/{args.repeats}")

        for name in names:
            output["layouts"][name] = summarize(timings[name], row_counts)
            output["layouts"][name]["plan"] = explain(conn, queries[name], root_ids[0])
        save()
    finally:
        if not args.keep:
            try:
                drop()
            except Exception as exc:
                log(f"cleanup failed: {exc!r}")
        conn.close()

    print(json.dumps({
        "nodes": output["nodes"],
        "paths": output["paths"],
        "closure": output.get("closure"),
        "layouts": {
            name: {key: value for key, value in result.items() if key != "plan"}
            for name, result in output["layouts"].items()
        },
        "validation": output.get("validation"),
    }, indent=2))


if __name__ == "__main__":
    main()
