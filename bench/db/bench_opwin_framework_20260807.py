#!/usr/bin/env python3
"""Two remaining server-side levers for get_node / get_children on 7.0.34.

A. internalQueryFrameworkControl.  The server runs forceClassicEngine.
   classic_plan_cache.cpp:135-142 declines to cache any hinted query that is
   not SBE-compatible, and says in the comment that the SBE plan cache *does*
   store the plan and so could serve a hinted query.  That makes trySbeEngine
   the only untested configuration lever that could remove per-execution
   planning from these shapes.  This measures it.

   The parameter is global, so arms cannot be interleaved inside a block.
   They are alternated *between* blocks instead, with a re-warm after every
   switch, and the reported effect is the per-block paired delta.

B. Dropping the explicit sort from get_children.  The index already supplies
   path,node_id order.  Measured with the raw client so PyMongo's per-command
   cost does not swamp a single-digit-microsecond server effect.

The parameter is restored in a finally block and the restored value is
recorded in the artifact.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_opwin_20260807 import (  # noqa: E402
    NODES, NODE_PROJ, CHILD_PROJ, RawMongo, thread_table, mongod_pid,
    canon_node, canon_children,
)


def log(m: str) -> None:
    print(m, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="mongodb://172.17.0.3:27017")
    ap.add_argument("--db", default="bench")
    ap.add_argument("--blocks", type=int, default=8)
    ap.add_argument("--iters", type=int, default=300)
    ap.add_argument("--inputs", type=int, default=64)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    host, port = args.uri.split("//")[1].split(":")
    cli = MongoClient(args.uri, maxPoolSize=1, minPoolSize=1,
                      directConnection=True)
    db, admin = cli[args.db], cli["admin"]
    raw = RawMongo(host, int(port))
    comm = f"conn{raw.connection_id}"
    pid = mongod_pid("condb_mongo")

    original = admin.command(
        "getParameter", 1, internalQueryFrameworkControl=1
    )["internalQueryFrameworkControl"]
    log(f"framework control at start: {original}")

    node_ids = [r["_id"] for r in db["layout_shared_text"].find({}, {"_id": 1})
                .sort([("_id", 1)]).limit(args.inputs)]
    samples = json.loads(
        Path("bench/db/runs/report_3eng_20260716/"
             "layout_2v3_postgres_10m_final.json").read_text())["samples"]
    parent_ids = [s["path"].rsplit("/", 1)[-1] for s in samples[:args.inputs]]

    def rawfind(cmd):
        r = raw.command(dict(cmd, **{"$db": args.db}))
        assert r["cursor"]["id"] == 0
        return r["cursor"]["firstBatch"]

    def node(i):
        b = rawfind({"find": NODES,
                     "filter": {"tree_id": "base", "node_id": node_ids[i]},
                     "hint": "allops_tree_node", "projection": NODE_PROJ,
                     "limit": 1, "singleBatch": True})
        return canon_node(b[0]) if b else ()

    def children(i, sort=True):
        cmd = {"find": NODES,
               "filter": {"tree_id": "base", "parent_id": parent_ids[i]},
               "hint": "allops_tree_parent_path", "projection": CHILD_PROJ}
        if sort:
            cmd["sort"] = {"path": 1, "node_id": 1}
        return canon_children(rawfind(cmd))

    OPS = {"node": node,
           "children": lambda i: children(i, True),
           "children_nosort": lambda i: children(i, False)}

    # equality: nosort must return exactly the sorted result
    log("verifying children_nosort is element-wise equal to children")
    elems = 0
    for i in range(len(parent_ids)):
        a, b = OPS["children"](i), OPS["children_nosort"](i)
        if a != b:
            raise SystemExit(f"MISMATCH at parent {parent_ids[i]}")
        elems += sum(len(r) for r in a)
    log(f"  {len(parent_ids)} parents, {elems} elements, equal")

    results: dict[str, dict[str, list]] = {
        setting: {op: [] for op in OPS}
        for setting in ("forceClassicEngine", "trySbeEngine")
    }
    plan_summaries: dict[str, dict[str, str]] = {}

    def measure(op_fn):
        before = thread_table(pid)
        gc.disable()
        t0 = time.perf_counter(); c0 = time.process_time()
        for k in range(args.iters):
            op_fn(k % len(node_ids))
        c1 = time.process_time(); t1 = time.perf_counter()
        gc.enable()
        after = thread_table(pid)
        return {"wall_us": (t1 - t0) * 1e6 / args.iters,
                "ccpu_us": (c1 - c0) * 1e6 / args.iters,
                "scpu_us": (after.get(comm, 0) - before.get(comm, 0))
                / 1e3 / args.iters}

    try:
        for block in range(args.blocks):
            settings = ("forceClassicEngine", "trySbeEngine")
            if block % 2:
                settings = settings[::-1]
            for setting in settings:
                admin.command("setParameter", 1,
                              internalQueryFrameworkControl=setting)
                for op, fn in OPS.items():
                    for i in range(300):          # re-warm after the switch
                        fn(i % len(node_ids))
                if setting not in plan_summaries:
                    plan_summaries[setting] = {}
                    for op, flt in (("node", {"tree_id": "base",
                                              "node_id": node_ids[0]}),
                                    ("children", {"tree_id": "base",
                                                  "parent_id": parent_ids[0]})):
                        hint = ("allops_tree_node" if op == "node"
                                else "allops_tree_parent_path")
                        ex = db.command({"explain": {"find": NODES,
                                                     "filter": flt,
                                                     "hint": hint},
                                         "verbosity": "queryPlanner"})
                        qp = ex["queryPlanner"]
                        plan_summaries[setting][op] = json.dumps({
                            "queryFramework": ex.get("explainVersion"),
                            "winningPlan": qp["winningPlan"]})[:300]
                for op, fn in OPS.items():
                    results[setting][op].append(measure(fn))
            log(f"block {block+1}/{args.blocks}")
    finally:
        admin.command("setParameter", 1,
                      internalQueryFrameworkControl=original)
        restored = admin.command(
            "getParameter", 1, internalQueryFrameworkControl=1
        )["internalQueryFrameworkControl"]
        log(f"framework control restored to: {restored}")

    def med(setting, op, key):
        return statistics.median(r[key] for r in results[setting][op])

    summary = {
        setting: {op: {k: round(med(setting, op, k), 2)
                       for k in ("wall_us", "ccpu_us", "scpu_us")}
                  for op in OPS}
        for setting in results
    }
    paired = {}
    for op in OPS:
        e = {}
        for key in ("wall_us", "scpu_us"):
            d = [results["trySbeEngine"][op][b][key]
                 - results["forceClassicEngine"][op][b][key]
                 for b in range(args.blocks)]
            p = [100 * dd / results["forceClassicEngine"][op][b][key]
                 for b, dd in enumerate(d)]
            e[key] = {"median_delta_us": round(statistics.median(d), 2),
                      "median_pct": round(statistics.median(p), 2),
                      "min_pct": round(min(p), 2), "max_pct": round(max(p), 2)}
        paired[op] = e

    # B: sort removal, classic engine only, paired per block
    sortd = [results["forceClassicEngine"]["children_nosort"][b]["scpu_us"]
             - results["forceClassicEngine"]["children"][b]["scpu_us"]
             for b in range(args.blocks)]
    sortp = [100 * d / results["forceClassicEngine"]["children"][b]["scpu_us"]
             for b, d in enumerate(sortd)]

    out = {
        "run": {"generated_unix_s": time.time(), "blocks": args.blocks,
                "iters": args.iters, "uri": args.uri,
                "framework_at_start": original, "framework_restored": restored,
                "profile": db.command("profile", -1)},
        "plan_summaries": plan_summaries,
        "summary": summary,
        "paired_sbe_vs_classic": paired,
        "sort_removal_classic_scpu": {
            "median_delta_us": round(statistics.median(sortd), 2),
            "median_pct": round(statistics.median(sortp), 2),
            "min_pct": round(min(sortp), 2), "max_pct": round(max(sortp), 2),
            "blocks_favouring_nosort": sum(1 for d in sortd if d < 0)},
        "raw": results,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2))

    log("")
    for setting in results:
        for op in OPS:
            s = summary[setting][op]
            log(f"{setting:<20}{op:<18} wall {s['wall_us']:>7.1f} "
                f"scpu {s['scpu_us']:>7.1f}")
    log("")
    for op, p in paired.items():
        log(f"SBE vs classic {op:<18} wall {p['wall_us']['median_pct']:>7.2f}% "
            f"scpu {p['scpu_us']['median_pct']:>7.2f}% "
            f"[{p['scpu_us']['min_pct']:.1f},{p['scpu_us']['max_pct']:.1f}]")
    s = out["sort_removal_classic_scpu"]
    log(f"sort removal (classic) scpu {s['median_pct']:>7.2f}% "
        f"({s['median_delta_us']:+.2f} us) [{s['min_pct']:.1f},{s['max_pct']:.1f}] "
        f"{s['blocks_favouring_nosort']}/{args.blocks} blocks favour nosort")
    for setting, v in plan_summaries.items():
        log(f"{setting}: {v}")
    raw.close(); cli.close()


if __name__ == "__main__":
    main()
