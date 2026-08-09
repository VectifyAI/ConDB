#!/usr/bin/env python3
"""Decompose PyMongo's per-operation client CPU for get_node / get_children.

The main harness showed the client half is ~89 us of Python CPU per operation
while a minimal OP_MSG client needs ~18 us for the identical wire exchange.
This script asks *which* PyMongo layers hold the difference, using arms that a
deployment could actually choose, plus a cProfile attribution.

Every arm sends a byte-identical or near-identical command; only the client
code path changes.  Output equality is checked element-wise first.
"""

from __future__ import annotations

import argparse
import cProfile
import gc
import io
import json
import pstats
import statistics
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_opwin_20260807 import (  # noqa: E402
    NODES, TEXT, NODE_PROJ, CHILD_PROJ, RawMongo, canon_node, canon_children,
)


def log(m: str) -> None:
    print(m, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="mongodb://localhost:57017")
    ap.add_argument("--db", default="bench")
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--iters", type=int, default=300)
    ap.add_argument("--inputs", type=int, default=64)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    host_port = args.uri.split("//")[1]
    host, port = host_port.split(":")

    base = MongoClient(args.uri, maxPoolSize=1, minPoolSize=1,
                       directConnection=True)
    noretry = MongoClient(args.uri, maxPoolSize=1, minPoolSize=1,
                          directConnection=True, retryReads=False)
    db, db_nr = base[args.db], noretry[args.db]
    coll, coll_nr = db[NODES], db_nr[NODES]
    raw = RawMongo(host, int(port))

    node_ids = [r["_id"] for r in base[args.db][TEXT].find({}, {"_id": 1})
                .sort([("_id", 1)]).limit(args.inputs)]
    samples = json.loads(
        Path("bench/db/runs/report_3eng_20260716/"
             "layout_2v3_postgres_10m_final.json").read_text())["samples"]
    parent_ids = [s["path"].rsplit("/", 1)[-1] for s in samples[:args.inputs]]

    def cmd_node(i):
        return {"find": NODES,
                "filter": {"tree_id": "base", "node_id": node_ids[i]},
                "hint": "allops_tree_node", "projection": NODE_PROJ,
                "limit": 1, "singleBatch": True}

    ARMS: dict[str, Any] = {}

    def add(name, fn, note):
        ARMS[name] = {"fn": fn, "note": note, "family": "node"}

    add("A_find_one",
        lambda i: canon_node(coll.find_one(
            {"tree_id": "base", "node_id": node_ids[i]}, NODE_PROJ,
            hint="allops_tree_node")),
        "BASELINE pymongo Collection.find_one")
    add("B_find_next",
        lambda i: canon_node(next(iter(coll.find(
            {"tree_id": "base", "node_id": node_ids[i]}, NODE_PROJ,
            hint="allops_tree_node").limit(1)), None)),
        "Collection.find(...).limit(1) iterated directly")
    add("C_db_command",
        lambda i: canon_node(db.command(cmd_node(i))["cursor"]["firstBatch"][0]),
        "Database.command: skips the Cursor object entirely")
    add("D_find_one_noretry",
        lambda i: canon_node(coll_nr.find_one(
            {"tree_id": "base", "node_id": node_ids[i]}, NODE_PROJ,
            hint="allops_tree_node")),
        "find_one on a client with retryReads=False")
    add("E_db_command_noretry",
        lambda i: canon_node(
            db_nr.command(cmd_node(i))["cursor"]["firstBatch"][0]),
        "Database.command with retryReads=False")
    add("F_raw_opmsg",
        lambda i: canon_node(raw.command(
            dict(cmd_node(i), **{"$db": args.db}))["cursor"]["firstBatch"][0]),
        "FLOOR raw OP_MSG, same C BSON codec, no lsid/session/topology")

    names = list(ARMS)

    log("verifying output equality element-wise")
    elems = 0
    for i in range(len(node_ids)):
        expect = ARMS["A_find_one"]["fn"](i)
        elems += len(expect)
        for n in names:
            got = ARMS[n]["fn"](i)
            if got != expect:
                raise SystemExit(f"MISMATCH {n} input {i}")
    log(f"  node: {len(names)} arms x {len(node_ids)} inputs, {elems} elements equal")

    for n in names:
        for i in range(200):
            ARMS[n]["fn"](i % len(node_ids))

    results = {n: [] for n in names}
    for block in range(args.blocks):
        order = names[block % len(names):] + names[:block % len(names)]
        for n in order:
            fn = ARMS[n]["fn"]
            gc.disable()
            t0 = time.perf_counter(); c0 = time.process_time()
            for k in range(args.iters):
                fn(k % len(node_ids))
            c1 = time.process_time(); t1 = time.perf_counter()
            gc.enable()
            results[n].append({
                "wall_us": (t1 - t0) * 1e6 / args.iters,
                "ccpu_us": (c1 - c0) * 1e6 / args.iters,
            })
        log(f"block {block+1}/{args.blocks}")

    summary = {
        n: {"note": ARMS[n]["note"],
            "wall_us": round(statistics.median(r["wall_us"] for r in results[n]), 2),
            "ccpu_us": round(statistics.median(r["ccpu_us"] for r in results[n]), 2)}
        for n in names
    }
    paired = {}
    for n in names:
        if n == "A_find_one":
            continue
        e = {}
        for key in ("wall_us", "ccpu_us"):
            d = [results[n][b][key] - results["A_find_one"][b][key]
                 for b in range(args.blocks)]
            p = [100 * dd / results["A_find_one"][b][key]
                 for b, dd in enumerate(d)]
            e[key] = {"median_delta_us": round(statistics.median(d), 2),
                      "median_pct": round(statistics.median(p), 2),
                      "min_pct": round(min(p), 2), "max_pct": round(max(p), 2)}
        paired[n] = e

    # ---- cProfile attribution of the baseline ----------------------------
    prof = cProfile.Profile()
    prof.enable()
    for k in range(3000):
        ARMS["A_find_one"]["fn"](k % len(node_ids))
    prof.disable()
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(28)
    profile_text = buf.getvalue()

    prof2 = cProfile.Profile()
    prof2.enable()
    for k in range(3000):
        ARMS["F_raw_opmsg"]["fn"](k % len(node_ids))
    prof2.disable()
    buf2 = io.StringIO()
    pstats.Stats(prof2, stream=buf2).sort_stats("tottime").print_stats(15)
    profile_raw_text = buf2.getvalue()

    out = {
        "run": {"generated_unix_s": time.time(), "blocks": args.blocks,
                "iters_per_block": args.iters, "inputs": len(node_ids),
                "uri": args.uri, "pymongo": __import__("pymongo").version,
                "python": __import__("sys").version},
        "contract": {"wall_us": "client wall per op",
                     "ccpu_us": "client process CPU (user+sys) per op",
                     "note": "server work is identical across arms except that "
                             "raw omits lsid; all arms verified output-equal"},
        "summary": summary, "paired": paired, "raw": results,
        "cprofile_find_one": profile_text,
        "cprofile_raw_opmsg": profile_raw_text,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2))

    log("")
    log(f"{'arm':<24}{'wall':>9}{'ccpu':>9}   note")
    for n in names:
        s = summary[n]
        log(f"{n:<24}{s['wall_us']:>9.1f}{s['ccpu_us']:>9.1f}   {s['note'][:52]}")
    log("")
    for n, p in paired.items():
        log(f"{n:<24} wall {p['wall_us']['median_pct']:>7.2f}% "
            f"[{p['wall_us']['min_pct']:.1f},{p['wall_us']['max_pct']:.1f}]  "
            f"ccpu {p['ccpu_us']['median_pct']:>7.2f}% "
            f"({p['ccpu_us']['median_delta_us']:+.1f} us)")
    log("")
    log(profile_text[:4000])


if __name__ == "__main__":
    main()
