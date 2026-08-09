#!/usr/bin/env python3
"""get_subtree head to head: MongoDB baseline, MongoDB pipelined, PostgreSQL.

All three arms run in one process, interleaved within each block, against the
same subtree, and each fully materializes the ordered (node_id,title,summary)
list.  Output is compared element-wise across arms before timing starts.

Needs pymongo and psycopg in the same interpreter:
    PYTHONPATH=<scratch>/libs /usr/bin/python3 bench/db/bench_subtree_headtohead.py
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import psycopg
import pymongo
from pymongo import CursorType

MONGO_URI = "mongodb://localhost:57017"
MONGO_NODES = "layout2_view"
COVER_INDEX = "layout2_rootcause_exact_cover"
PG_DSN = "host=localhost port=55432 dbname=bench user=postgres password=bench"
PG_NODES = "layout2_pg_view"
TREE_ID = "base"
PROJECTION = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}
COHORT = "bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json"


def log(m: str) -> None:
    print(m, flush=True)


def fingerprint(rows) -> str:
    d = hashlib.sha256()
    for r in rows:
        for v in r:
            e = str(v).encode()
            d.update(len(e).to_bytes(4, "big"))
            d.update(e)
    return d.hexdigest()


def cgroup_cpu_path(container: str) -> Path:
    """cpu.stat of a container's cgroup.  PostgreSQL forks a backend per
    connection, so per-thread /proc accounting on the postmaster misses it;
    the cgroup covers every process in the container for both engines."""
    cid = os.popen(f"docker inspect -f '{{{{.Id}}}}' {container}").read().strip()
    root = Path("/sys/fs/cgroup/system.slice") / f"docker-{cid}.scope"
    return root / "cpu.stat"


def cgroup_cpu_us(path: Path) -> int:
    for line in path.read_text().splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=11686)
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--iters", type=int, default=2)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    s = min(json.loads(Path(COHORT).read_text())["samples"][:200],
            key=lambda x: abs(x["rows"] - args.rows))
    path, lower, upper = s["path"], s["path"] + "/", s["path"] + "0"
    log(f"input path={path} rows={s['rows']} batch={args.batch}")

    client = pymongo.MongoClient(MONGO_URI)
    nodes = client["bench"][MONGO_NODES]
    flt = {"path": {"$gte": lower, "$lt": upper}}
    sort_spec = [("path", 1), ("node_id", 1)]

    def mongo_baseline():
        cur = nodes.find(flt, PROJECTION).sort(sort_spec).hint(COVER_INDEX)
        return [(r["node_id"], r["title"], r["summary"]) for r in cur]

    def mongo_pipelined():
        cur = (nodes.find(flt, PROJECTION, cursor_type=CursorType.EXHAUST)
               .sort(sort_spec).hint(COVER_INDEX).batch_size(args.batch))
        return [(r["node_id"], r["title"], r["summary"]) for r in cur]

    conn = psycopg.connect(PG_DSN, autocommit=True)
    SQL = (f"SELECT node_id,title,summary FROM {PG_NODES} "
           f"WHERE tree_id=%s AND path>=%s AND path<%s ORDER BY path,node_id")

    def pg_prepared():
        return conn.execute(SQL, (TREE_ID, lower, upper)).fetchall()

    arms = {"mongo_baseline": mongo_baseline,
            "mongo_pipelined": mongo_pipelined,
            "pg_prepared": pg_prepared}
    cpu_paths = {"mongo_baseline": cgroup_cpu_path("condb_mongo"),
                 "mongo_pipelined": cgroup_cpu_path("condb_mongo"),
                 "pg_prepared": cgroup_cpu_path("condb_pg")}

    # equality + psycopg prepare_threshold warmup (default 5)
    fps = {}
    for n, fn in arms.items():
        for _ in range(6):
            r = fn()
        fps[n] = fingerprint(r)
        log(f"  {n}: rows={len(r)} fp={fps[n][:16]}")
    assert len(set(fps.values())) == 1, f"arms disagree: {fps}"
    log("  element-wise output identical across all three arms")

    names = list(arms)
    wall = {n: [] for n in names}
    cpu = {n: [] for n in names}
    for b in range(args.blocks):
        order = names if b % 2 == 0 else list(reversed(names))
        for n in order:
            arms[n]()
            gc.collect()
            gc.disable()
            c0 = cgroup_cpu_us(cpu_paths[n])
            t0 = time.perf_counter()
            for _ in range(args.iters):
                r = arms[n]()
                del r
            dt = (time.perf_counter() - t0) * 1e6 / args.iters
            c1 = cgroup_cpu_us(cpu_paths[n])
            gc.enable()
            wall[n].append(dt)
            cpu[n].append((c1 - c0) / args.iters)
        log("  block %d: " % b + "  ".join(
            f"{n}={wall[n][-1]/1000:.2f}ms/{cpu[n][-1]/1000:.2f}cpu" for n in names))

    out = {"input": {"path": path, "rows": s["rows"]}, "batch": args.batch,
           "blocks": args.blocks, "iters": args.iters,
           "pymongo": pymongo.version, "psycopg": psycopg.__version__, "arms": {}}
    log("\nsummary:")
    for n in names:
        out["arms"][n] = {"wall_us": [round(x, 1) for x in wall[n]],
                          "server_cpu_us": [round(x, 1) for x in cpu[n]],
                          "wall_p50_us": round(statistics.median(wall[n]), 1),
                          "cpu_p50_us": round(statistics.median(cpu[n]), 1)}
        log(f"  {n:18s} wall {statistics.median(wall[n])/1000:9.2f} ms   "
            f"server cpu {statistics.median(cpu[n])/1000:8.2f} ms")
    pgw = wall["pg_prepared"]
    for n in ("mongo_baseline", "mongo_pipelined"):
        pp = [round(100 * (wall[n][i] - pgw[i]) / pgw[i], 2) for i in range(args.blocks)]
        out["arms"][n]["paired_pct_vs_pg"] = pp
        log(f"  {n:18s} vs PostgreSQL, paired per block: median {statistics.median(pp):+6.2f}%"
            f"  range [{min(pp):+.1f},{max(pp):+.1f}]")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=1))
        log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
