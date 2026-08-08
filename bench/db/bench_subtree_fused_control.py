#!/usr/bin/env python3
"""Non-intrusion controls for the fused covered projection.

The A/B harness shows what the change does to the query it targets. This shows what it does to
queries it should not affect, which is the other half of the claim:

  small      -- a covered projection returning a handful of rows. These take the fused stage's
                seek fallback for the first key, and small results are the overwhelming majority
                of real find traffic, so a regression here would matter more than the win.
  uncovered  -- a query with a FETCH: not a covered projection at all, so the stage builder never
                folds anything. Measures the cost of the eligibility check plus the null-pointer
                branch the fused path adds to every IndexScan::doWork.
  filtered   -- a covered index scan carrying a residual filter. Eligible in shape but refused,
                so it proves the refusal path costs nothing and still returns the right rows.

Same pairing discipline as bench_subtree_fused_ab.py: arms alternate within blocks with rotating
order, per-block deltas, element-wise output comparison, and instructions / server CPU / client
wall kept as three separate quantities.

Usage:
    bench_subtree_fused_control.py --blocks 10 --seconds 3 --instructions
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any, Callable

from pymongo import MongoClient

import bench_subtree_fused_ab as ab

DB = "bench"
NODES = "layout2_view"
COVER_INDEX = "layout2_rootcause_exact_cover"
NODE_INDEX = "allops_tree_node"
TREE_ID = "base"


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def find_small_prefix(coll: Any, lo: int = 2, hi: int = 64) -> tuple[str, str]:
    """A path range covering between 'lo' and 'hi' rows, for the small-result control.

    Walks up from a deep node's own path: the deepest prefixes cover nothing, and each step up
    multiplies the count, so the first prefix inside the band is the smallest useful one. Taking
    the first prefix with merely >= 2 rows overshoots badly -- one step too far up this tree lands
    on a subtree of over a million rows.
    """
    deep = coll.find_one({}, {"_id": 0, "path": 1, "depth": 1}, sort=[("depth", -1)])
    if deep is None:
        raise SystemExit("collection is empty")
    parts = deep["path"].strip("/").split("/")
    for depth in range(len(parts), 0, -1):
        prefix = "/" + "/".join(parts[:depth])
        lower, upper = prefix + "/", prefix + "0"
        n = coll.count_documents({"path": {"$gte": lower, "$lt": upper}}, hint=COVER_INDEX)
        if lo <= n <= hi:
            log(f"small-result control: prefix {prefix} -> {n} rows")
            return lower, upper
        if n > hi:
            raise SystemExit(
                f"no prefix of {deep['path']} lands in [{lo}, {hi}]; {prefix} already has {n}")
    raise SystemExit("could not find a small path range")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--instructions", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    uri = f"mongodb://localhost:{args.port}/?directConnection=true"
    client = MongoClient(uri)
    coll = client[DB][NODES]
    pid = ab.mongod_pid(args.port)

    small_lower, small_upper = find_small_prefix(coll)
    node = coll.find_one({"tree_id": TREE_ID}, {"_id": 0, "node_id": 1}, hint=NODE_INDEX)
    node_id = node["node_id"]

    def q_small() -> list[tuple]:
        cur = (coll.find({"path": {"$gte": small_lower, "$lt": small_upper}},
                         {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
               .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX))
        return [(d.get("node_id"), d.get("title"), d.get("summary")) for d in cur]

    def q_uncovered() -> list[tuple]:
        # Projects a field that is not in the index, so the plan needs a FETCH.
        cur = coll.find({"tree_id": TREE_ID, "node_id": node_id},
                        {"_id": 0, "node_id": 1, "depth": 1}).hint(NODE_INDEX)
        return [(d.get("node_id"), d.get("depth")) for d in cur]

    def q_filtered() -> list[tuple]:
        # Covered by the index but carrying a residual filter, so fusion must refuse. The
        # predicate has to be one the planner cannot turn into index bounds -- an equality on a
        # trailing index field becomes bounds and leaves no filter behind, which is not the case
        # under test. A non-anchored regex on an indexed string field stays a filter.
        cur = (coll.find({"path": {"$gte": small_lower, "$lt": small_upper},
                          "title": {"$regex": "e"}},
                         {"_id": 0, "node_id": 1, "title": 1})
               .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX))
        return [(d.get("node_id"), d.get("title")) for d in cur]

    controls: dict[str, Callable[[], list[tuple]]] = {
        "small": q_small, "uncovered": q_uncovered, "filtered": q_filtered,
    }

    results: dict[str, Any] = {"knob": ab.KNOB, "controls": {}}

    for name, query in controls.items():
        ab.set_arm(client, False)
        for _ in range(3):
            reference = query()
        log(f"{name}: {len(reference)} rows per query")

        blocks: list[dict[str, Any]] = []
        mismatches = 0
        for b in range(args.blocks):
            order = [False, True] if b % 2 == 0 else [True, False]
            block: dict[str, Any] = {}
            for enabled in order:
                arm = "fused" if enabled else "base"
                ab.set_arm(client, enabled)
                query()
                handle = ab.start_perf(pid, args.seconds, f"{name}{b}{arm}") \
                    if args.instructions else None
                if handle is not None:
                    time.sleep(0.20)
                ops = 0
                cpu0 = ab.proc_cpu_us(pid)
                t0 = time.perf_counter()
                while time.perf_counter() - t0 < args.seconds:
                    rows = query()
                    ops += 1
                wall = (time.perf_counter() - t0) * 1e6
                cpu = ab.proc_cpu_us(pid) - cpu0
                instr = ab.read_perf(handle)
                if rows != reference:
                    mismatches += 1
                block[arm] = {
                    "ops": ops,
                    "wall_us": wall / ops,
                    "cpu_us": cpu / ops,
                    "instructions": (instr / ops) if instr is not None else None,
                }
            for key, label in (("wall_us", "wall"), ("cpu_us", "cpu"),
                               ("instructions", "instr")):
                bv, fv = block["base"][key], block["fused"][key]
                if bv:
                    block[f"{label}_delta_pct"] = (fv / bv - 1) * 100
            blocks.append(block)

        entry: dict[str, Any] = {"rows": len(reference), "blocks": blocks,
                                 "mismatches": mismatches}
        for lbl in ("instr", "cpu", "wall"):
            ds = [b[f"{lbl}_delta_pct"] for b in blocks if f"{lbl}_delta_pct" in b]
            if ds:
                entry[f"paired_{lbl}_delta_pct"] = {
                    "median": statistics.median(ds), "min": min(ds), "max": max(ds),
                    "blocks": len(ds),
                }
                log(f"{name}: PAIRED {lbl} {statistics.median(ds):+.2f}% "
                    f"[{min(ds):+.2f}, {max(ds):+.2f}] over {len(ds)} blocks")
        log(f"{name}: output mismatches {mismatches}")
        results["controls"][name] = entry

    # Plan shapes, to show which controls were refused fusion and which never qualified.
    shapes = {}
    for name, q in (("small", q_small), ("uncovered", q_uncovered), ("filtered", q_filtered)):
        row = {}
        for enabled in (False, True):
            ab.set_arm(client, enabled)
            if name == "small":
                ex = (coll.find({"path": {"$gte": small_lower, "$lt": small_upper}},
                                {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
                      .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX)).explain()
            elif name == "uncovered":
                ex = coll.find({"tree_id": TREE_ID, "node_id": node_id},
                               {"_id": 0, "node_id": 1, "depth": 1}).hint(NODE_INDEX).explain()
            else:
                ex = (coll.find({"path": {"$gte": small_lower, "$lt": small_upper},
                                 "title": {"$regex": "e"}},
                                {"_id": 0, "node_id": 1, "title": 1})
                      .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX)).explain()
            wp = ex.get("queryPlanner", {}).get("winningPlan", {})
            row["fused" if enabled else "base"] = {
                "stages": ab.stage_names(wp), "ixscan_fused": ab.ixscan_fused(wp)}
        shapes[name] = row
        log(f"plan {name}: {row}")
    results["plan_shapes"] = shapes
    ab.set_arm(client, False)

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
