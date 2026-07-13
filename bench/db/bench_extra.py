#!/usr/bin/env python3
"""Three operations the read/write/concurrency benchmarks do not cover, added
after a usage-pattern review of agentic tree retrieval:

  multiget   batched id-list fetch (MongoDB $in / SQL IN-list) versus the same
             ids fetched one point-lookup at a time. The LLM navigation step
             returns a list of candidate node ids per query, so the system
             fetches several nodes by id at once; this measures the batched form
             against the point-lookup loop it replaces.
  selectivity per-query tree_id filter with many trees co-resident in one
             collection/table (multi-tenant). The single-tree benchmarks never
             exercised tenant selectivity; here M trees share one store and
             every read filters by tree_id, served by a tree_id-led compound
             index, the pattern MongoDB's multi-tenant guidance prescribes.
  delete     whole-tree deletion (delete_tree) and on-disk space reclamation:
             delete one tenant's tree, then reclaim (WiredTiger compact /
             SQLite VACUUM / Postgres VACUUM FULL / DuckDB checkpoint), measuring
             storage before, after delete, and after reclaim.

This reuses the schemas, ingest paths, and storage measurement of
bench_databases.py so the numbers line up with the read benchmark. Trees are the
base document replicated under M distinct tree_ids; node_ids and paths repeat
across trees, and every query is disambiguated by tree_id (the realistic
multi-tenant access pattern).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

from bench_databases import (
    COLS,
    DuckDBBackend,
    MongoBackend,
    PostgresBackend,
    SqliteBackend,
    flatten,
    human,
)


def percentiles(lat_ms):
    lat_ms = sorted(lat_ms)

    def pct(p):
        if not lat_ms:
            return 0.0
        idx = min(len(lat_ms) - 1, int(round(p / 100 * (len(lat_ms) - 1))))
        return round(lat_ms[idx], 3)

    return {"p50_ms": pct(50), "p95_ms": pct(95), "p99_ms": pct(99),
            "mean_ms": round(statistics.mean(lat_ms), 3) if lat_ms else 0.0}


def time_calls(fn, items, warmup=3):
    items = list(items)
    for w in items[:warmup]:
        fn(w)
    lat, rows = [], 0
    for it in items:
        t0 = time.perf_counter()
        r = fn(it)
        lat.append((time.perf_counter() - t0) * 1000.0)
        if isinstance(r, int):
            rows += r
    out = percentiles(lat)
    out["n"] = len(lat)
    out["avg_rows"] = round(rows / len(lat), 1) if lat else 0.0
    return out


# --- multi-tenant subclasses: tenant-led compound indexes + the new ops -------

class SqliteMT(SqliteBackend):
    def build_indexes_mt(self):
        t0 = time.time()
        self.conn.execute("CREATE UNIQUE INDEX ix_pk ON nodes(tree_id, node_id)")
        self.conn.execute("CREATE INDEX ix_tparent ON nodes(tree_id, parent_id)")
        self.conn.execute("CREATE INDEX ix_tpath ON nodes(tree_id, path)")
        self.conn.commit()
        self.conn.execute("ANALYZE")
        return time.time() - t0

    def q_point_t(self, tid, node_id):
        return len(self.conn.execute(
            "SELECT title,summary FROM nodes WHERE tree_id=? AND node_id=?", (tid, node_id)).fetchall())

    def q_children_t(self, tid, parent_id):
        return len(self.conn.execute(
            "SELECT node_id,title FROM nodes WHERE tree_id=? AND parent_id=?", (tid, parent_id)).fetchall())

    def q_subtree_t(self, tid, path):
        return len(self.conn.execute(
            "SELECT node_id FROM nodes WHERE tree_id=? AND path>=? AND path<?",
            (tid, path + "/", path + "0")).fetchall())

    def q_multiget(self, tid, ids):
        ph = ",".join("?" * len(ids))
        return len(self.conn.execute(
            f"SELECT node_id,title,summary FROM nodes WHERE tree_id=? AND node_id IN ({ph})",
            (tid, *ids)).fetchall())

    def q_multiget_loop(self, tid, ids):
        n = 0
        for i in ids:
            n += len(self.conn.execute(
                "SELECT node_id,title,summary FROM nodes WHERE tree_id=? AND node_id=?", (tid, i)).fetchall())
        return n

    def delete_tree(self, tid):
        cur = self.conn.execute("DELETE FROM nodes WHERE tree_id=?", (tid,))
        self.conn.commit()
        return cur.rowcount

    def reclaim(self):
        self.conn.execute("VACUUM")
        self.conn.commit()


class DuckDBMT(DuckDBBackend):
    def build_indexes_mt(self):
        t0 = time.time()
        self.conn.execute("CREATE INDEX ix_pk ON nodes(tree_id, node_id)")
        self.conn.execute("CREATE INDEX ix_tparent ON nodes(tree_id, parent_id)")
        self.conn.execute("CREATE INDEX ix_tpath ON nodes(tree_id, path)")
        self.conn.execute("CHECKPOINT")
        return time.time() - t0

    def q_point_t(self, tid, node_id):
        return len(self.conn.execute(
            "SELECT title,summary FROM nodes WHERE tree_id=? AND node_id=?", [tid, node_id]).fetchall())

    def q_children_t(self, tid, parent_id):
        return len(self.conn.execute(
            "SELECT node_id,title FROM nodes WHERE tree_id=? AND parent_id=?", [tid, parent_id]).fetchall())

    def q_subtree_t(self, tid, path):
        return len(self.conn.execute(
            "SELECT node_id FROM nodes WHERE tree_id=? AND path>=? AND path<?",
            [tid, path + "/", path + "0"]).fetchall())

    def q_multiget(self, tid, ids):
        ph = ",".join("?" * len(ids))
        return len(self.conn.execute(
            f"SELECT node_id,title,summary FROM nodes WHERE tree_id=? AND node_id IN ({ph})",
            [tid, *ids]).fetchall())

    def q_multiget_loop(self, tid, ids):
        n = 0
        for i in ids:
            n += len(self.conn.execute(
                "SELECT node_id,title,summary FROM nodes WHERE tree_id=? AND node_id=?", [tid, i]).fetchall())
        return n

    def delete_tree(self, tid):
        n = self.conn.execute("SELECT count(*) FROM nodes WHERE tree_id=?", [tid]).fetchone()[0]
        self.conn.execute("DELETE FROM nodes WHERE tree_id=?", [tid])
        self.conn.execute("CHECKPOINT")
        return n

    def reclaim(self):
        self.conn.execute("CHECKPOINT")  # DuckDB has no online file shrink


class PostgresMT(PostgresBackend):
    def build_indexes_mt(self):
        t0 = time.time()
        self.conn.execute("ALTER TABLE nodes ADD PRIMARY KEY (tree_id, node_id)")
        self.conn.execute("CREATE INDEX ix_tparent ON nodes(tree_id, parent_id)")
        self.conn.execute('CREATE INDEX ix_tpath ON nodes(tree_id, path)')
        self.conn.execute("ANALYZE nodes")
        return time.time() - t0

    def q_point_t(self, tid, node_id):
        return len(self.conn.execute(
            "SELECT doc->>'title', doc->>'summary' FROM nodes WHERE tree_id=%s AND node_id=%s",
            (tid, node_id)).fetchall())

    def q_children_t(self, tid, parent_id):
        return len(self.conn.execute(
            "SELECT node_id, doc->>'title' FROM nodes WHERE tree_id=%s AND parent_id=%s",
            (tid, parent_id)).fetchall())

    def q_subtree_t(self, tid, path):
        return len(self.conn.execute(
            "SELECT node_id FROM nodes WHERE tree_id=%s AND path>=%s AND path<%s",
            (tid, path + "/", path + "0")).fetchall())

    def q_multiget(self, tid, ids):
        return len(self.conn.execute(
            "SELECT node_id, doc->>'title', doc->>'summary' FROM nodes WHERE tree_id=%s AND node_id = ANY(%s)",
            (tid, list(ids))).fetchall())

    def q_multiget_loop(self, tid, ids):
        n = 0
        for i in ids:
            n += len(self.conn.execute(
                "SELECT node_id, doc->>'title', doc->>'summary' FROM nodes WHERE tree_id=%s AND node_id=%s",
                (tid, i)).fetchall())
        return n

    def delete_tree(self, tid):
        cur = self.conn.execute("DELETE FROM nodes WHERE tree_id=%s", (tid,))
        return cur.rowcount

    def reclaim(self):
        self.conn.execute("VACUUM FULL nodes")


class MongoMT(MongoBackend):
    def storage_bytes(self):
        # WiredTiger reports storageSize in checkpoint-sized chunks, so a read
        # right after a bulk insert under-reports; force a checkpoint first.
        try:
            self.client.admin.command({"fsync": 1})
        except Exception:  # noqa: BLE001
            pass
        return super().storage_bytes()

    def build_indexes_mt(self):
        from pymongo import ASCENDING
        t0 = time.time()
        self.col.create_index([("tree_id", ASCENDING), ("node_id", ASCENDING)])
        self.col.create_index([("tree_id", ASCENDING), ("parent_id", ASCENDING)])
        self.col.create_index([("tree_id", ASCENDING), ("path", ASCENDING)])
        return time.time() - t0

    def q_point_t(self, tid, node_id):
        return len(list(self.col.find(
            {"tree_id": tid, "node_id": node_id}, {"title": 1, "summary": 1})))

    def q_children_t(self, tid, parent_id):
        return len(list(self.col.find(
            {"tree_id": tid, "parent_id": parent_id}, {"node_id": 1, "title": 1})))

    def q_subtree_t(self, tid, path):
        return len(list(self.col.find(
            {"tree_id": tid, "path": {"$gte": path + "/", "$lt": path + "0"}}, {"node_id": 1})))

    def q_multiget(self, tid, ids):
        return len(list(self.col.find(
            {"tree_id": tid, "node_id": {"$in": list(ids)}}, {"node_id": 1, "title": 1, "summary": 1})))

    def q_multiget_loop(self, tid, ids):
        n = 0
        for i in ids:
            n += len(list(self.col.find({"tree_id": tid, "node_id": i}, {"node_id": 1, "title": 1, "summary": 1})))
        return n

    def delete_tree(self, tid):
        return self.col.delete_many({"tree_id": tid}).deleted_count

    def reclaim(self):
        self.db.command("compact", "nodes")


def build(name, args):
    if name == "sqlite":
        return SqliteMT(args.sqlite_path)
    if name == "duckdb":
        return DuckDBMT(args.duckdb_path)
    if name == "postgres":
        return PostgresMT(args.pg_dsn)
    if name == "mongo":
        return MongoMT(args.mongo_uri)
    raise ValueError(name)


def ingest_multitenant(be, base_rows, tree_ids):
    """Ingest the base tree once per tree_id; returns total ingest seconds."""
    total = 0.0
    for tid in tree_ids:
        rows = [{**r, "tree_id": tid} for r in base_rows]
        total += be.ingest(rows)
    return total


def run_backend(be, base_rows, tenant_ids, samples, args):
    rng = random.Random(args.seed)
    print(f"  [{be.name}] setup + ingest {len(tenant_ids)} trees "
          f"({len(base_rows) * len(tenant_ids):,} rows)...", file=sys.stderr)
    be.setup()
    ingest_s = ingest_multitenant(be, base_rows, tenant_ids)
    index_s = be.build_indexes_mt()
    storage = be.storage_bytes()
    total_rows = len(base_rows) * len(tenant_ids)
    result = {
        "engine": be.name,
        "trees": len(tenant_ids),
        "rows_total": total_rows,
        "ingest_seconds": round(ingest_s, 2),
        "ingest_nodes_per_sec": int(total_rows / ingest_s) if ingest_s > 0 else 0,
        "index_build_seconds": round(index_s, 2),
        "storage_before": dict(storage),
        "selectivity": {},
        "multiget": {},
        "delete": {},
    }

    # --- #5 multi-tenant selectivity: each call is a random tenant + a key ----
    point_items = [(rng.choice(tenant_ids), rng.choice(samples["point"])) for _ in range(args.calls)]
    child_items = [(rng.choice(tenant_ids), rng.choice(samples["parent"])) for _ in range(args.calls)]
    sub_items = [(rng.choice(tenant_ids), rng.choice(samples["path"])) for _ in range(min(args.calls, 200))]
    for label, fn, items in [
        ("point", lambda it: be.q_point_t(it[0], it[1]), point_items),
        ("children", lambda it: be.q_children_t(it[0], it[1]), child_items),
        ("subtree", lambda it: be.q_subtree_t(it[0], it[1]), sub_items),
    ]:
        print(f"  [{be.name}] selectivity:{label} ({len(items)} calls)...", file=sys.stderr)
        result["selectivity"][label] = time_calls(fn, items)

    # --- #3 batched multiget vs point-lookup loop ----------------------------
    pool = samples["point"]
    for bsize in args.batch_sizes:
        tid = rng.choice(tenant_ids)
        batches = [rng.sample(pool, min(bsize, len(pool))) for _ in range(args.batches)]
        print(f"  [{be.name}] multiget batch={bsize} ({len(batches)} batches)...", file=sys.stderr)
        bat = time_calls(lambda ids: be.q_multiget(tid, ids), batches)
        loop = time_calls(lambda ids: be.q_multiget_loop(tid, ids), batches)
        result["multiget"][str(bsize)] = {
            "batched": {k: bat[k] for k in ("p50_ms", "p95_ms", "mean_ms")},
            "loop": {k: loop[k] for k in ("p50_ms", "p95_ms", "mean_ms")},
            "speedup_p50": round(loop["p50_ms"] / bat["p50_ms"], 2) if bat["p50_ms"] else None,
        }

    # --- #4 delete_tree + space reclamation ----------------------------------
    victim = tenant_ids[len(tenant_ids) // 2]
    t0 = time.perf_counter()
    deleted = be.delete_tree(victim)
    del_ms = (time.perf_counter() - t0) * 1000.0
    after_del = be.storage_bytes()
    t0 = time.perf_counter()
    try:
        be.reclaim()
        reclaim_ok = True
    except Exception as e:  # noqa: BLE001
        reclaim_ok = False
        print(f"  [{be.name}] reclaim failed: {e}", file=sys.stderr)
    reclaim_ms = (time.perf_counter() - t0) * 1000.0
    after_reclaim = be.storage_bytes()
    result["delete"] = {
        "deleted_rows": deleted,
        "delete_ms": round(del_ms, 1),
        "delete_ops_per_sec": int(deleted / (del_ms / 1000.0)) if del_ms else 0,
        "reclaim_ms": round(reclaim_ms, 1),
        "reclaim_ran": reclaim_ok,
        "total_before": storage["total"],
        "total_after_delete": after_del["total"],
        "total_after_reclaim": after_reclaim["total"],
        "freed_by_delete": storage["total"] - after_del["total"],
        "freed_by_reclaim": after_del["total"] - after_reclaim["total"],
    }
    result["storage_human"] = {
        "before": human(storage["total"]),
        "after_delete": human(after_del["total"]),
        "after_reclaim": human(after_reclaim["total"]),
    }
    be.teardown()
    return result


def main():
    ap = argparse.ArgumentParser(description="Batched multiget, multi-tenant selectivity, and delete_tree.")
    ap.add_argument("--doc", required=True, help="base PageIndex JSON, replicated into M trees")
    ap.add_argument("--trees", type=int, default=25, help="number of co-resident trees (tenants)")
    ap.add_argument("--engines", nargs="+", default=["sqlite", "duckdb", "postgres", "mongo"])
    ap.add_argument("--calls", type=int, default=500, help="selectivity calls per op")
    ap.add_argument("--batch-sizes", nargs="+", type=int, default=[10, 50, 200])
    ap.add_argument("--batches", type=int, default=100, help="batches per multiget size")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out")
    ap.add_argument("--pg-dsn", default="host=localhost port=55432 dbname=bench user=postgres password=bench")
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--sqlite-path", default="bench/db/runs/_sqlite_x.db")
    ap.add_argument("--duckdb-path", default="bench/db/runs/_duck_x.db")
    args = ap.parse_args()

    print(f"Loading {args.doc} ...", file=sys.stderr)
    doc = json.load(open(args.doc))
    recs = flatten(doc, tree_id="base")
    base_rows = recs.rows
    print(f"Base tree: {len(base_rows):,} nodes -> {args.trees} tenants "
          f"= {len(base_rows) * args.trees:,} rows", file=sys.stderr)
    tenant_ids = [f"t{idx:04d}" for idx in range(args.trees)]
    samples = {
        "point": recs.point_ids,
        "parent": recs.parent_ids,
        "path": recs.subtree_paths,
    }

    results = {"doc": args.doc, "base_nodes": len(base_rows), "trees": args.trees,
               "rows_total": len(base_rows) * args.trees, "engines": {}}
    for eng in args.engines:
        print(f"\n=== {eng} ===", file=sys.stderr)
        try:
            results["engines"][eng] = run_backend(build(eng, args), base_rows, tenant_ids, samples, args)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            results["engines"][eng] = {"engine": eng, "error": str(e)}

    # console summary
    print("\n--- #5 selectivity P50 (ms), M tenants in one store ---")
    print(f"{'op':10s} | " + " | ".join(f"{e:>10s}" for e in results["engines"]))
    for op in ("point", "children", "subtree"):
        cells = []
        for e in results["engines"]:
            v = results["engines"][e].get("selectivity", {}).get(op, {})
            cells.append(f"{v.get('p50_ms', 0):10.3f}" if v else f"{'-':>10s}")
        print(f"{op:10s} | " + " | ".join(cells))

    print("\n--- #3 multiget P50 ms (batched / loop) ---")
    for e in results["engines"]:
        mg = results["engines"][e].get("multiget", {})
        parts = [f"b{k}: {v['batched']['p50_ms']:.3f}/{v['loop']['p50_ms']:.3f}" for k, v in mg.items()]
        print(f"  {e:10s} " + "  ".join(parts))

    print("\n--- #4 delete_tree + reclaim ---")
    for e in results["engines"]:
        d = results["engines"][e].get("delete", {})
        sh = results["engines"][e].get("storage_human", {})
        if d:
            print(f"  {e:10s} del {d['deleted_rows']:,} rows in {d['delete_ms']:.0f} ms "
                  f"({d['delete_ops_per_sec']:,}/s); store {sh.get('before')} -> "
                  f"{sh.get('after_delete')} (del) -> {sh.get('after_reclaim')} (reclaim, "
                  f"{d['reclaim_ms']:.0f} ms)")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\nWrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
