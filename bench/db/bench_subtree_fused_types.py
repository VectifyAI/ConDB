#!/usr/bin/env python3
"""Differential correctness for the fused covered decode, on values the real dataset lacks.

The A/B and control harnesses compare output element-wise, but only over layout2_view, whose
indexed values are all ordinary strings. A KeyString decoder breaks on the things that are not
ordinary: embedded NUL bytes take readCStringWithNuls' scratch path, descending components are
byte-inverted, and each BSON type has its own CType branch and its own TypeBits reads. Those are
the cases where "decode the wanted components and throw the rest away" could silently desynchronise
the TypeBits stream and produce plausible, wrong output.

Every query runs with the knob off and on, and the two result sets must be identical in order.

This is the same coverage as jstests/noPassthrough/query/fused_covered_projection.js, run through
PyMongo so it does not need the mongo shell built.

Usage:
    bench_subtree_fused_types.py [--port 57018]
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

from bson import Decimal128, Int64, MaxKey, MinKey
from pymongo import ASCENDING, DESCENDING, MongoClient

KNOB = "internalQueryEnableFusedCoveredProjection"
DB = "fused_types_check"


def stage_names(plan: Any, acc: list[str] | None = None) -> list[str]:
    acc = [] if acc is None else acc
    if isinstance(plan, dict):
        if isinstance(plan.get("stage"), str):
            acc.append(plan["stage"])
        for v in plan.values():
            stage_names(v, acc)
    elif isinstance(plan, list):
        for v in plan:
            stage_names(v, acc)
    return acc


def ixscan_fused(plan: Any) -> bool:
    """True when an IXSCAN in this plan reports a folded-in covered projection.

    The projection stage stays in the tree when the fold happens -- removing it would desynchronise
    the plan-stage tree from the QuerySolution -- so its presence says nothing. The IXSCAN's own
    'coveredProjection' flag is the activation signal, and it is only emitted when true.
    """
    found = False

    def walk(node: Any) -> None:
        nonlocal found
        if isinstance(node, dict):
            if node.get("stage") == "IXSCAN" and node.get("coveredProjection"):
                found = True
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    client = MongoClient(f"mongodb://localhost:{args.port}/?directConnection=true")
    db = client[DB]
    failures: list[str] = []
    report: list[dict[str, Any]] = []

    def set_arm(enabled: bool) -> None:
        client.admin.command({"setParameter": 1, KNOB: enabled})

    def differential(name: str, coll: Any, filt: dict, proj: dict, hint: str,
                     sort: list | None, expect_fused: bool) -> None:
        def run() -> list[dict]:
            cur = coll.find(filt, proj).hint(hint)
            if sort:
                cur = cur.sort(sort)
            return list(cur)

        def explain() -> dict:
            cur = coll.find(filt, proj).hint(hint)
            if sort:
                cur = cur.sort(sort)
            return cur.explain().get("queryPlanner", {}).get("winningPlan", {})

        set_arm(False)
        db.command({"planCacheClear": coll.name})
        base_rows, base_wp = run(), explain()
        set_arm(True)
        db.command({"planCacheClear": coll.name})
        fused_rows, fused_wp = run(), explain()
        base_plan, fused_plan = stage_names(base_wp), stage_names(fused_wp)

        fused_actually = ixscan_fused(fused_wp)
        if ixscan_fused(base_wp):
            failures.append(f"{name}: the base arm reported a fused scan")
        # The stage tree must be identical in both arms: the fold keeps PROJECTION_COVERED in
        # place so that the plan-stage tree still matches the QuerySolution one to one.
        if base_plan != fused_plan:
            failures.append(f"{name}: plan shape changed: {base_plan} -> {fused_plan}")
        ok = base_rows == fused_rows
        if not ok:
            failures.append(f"{name}: output differs")
            for i, (a, b) in enumerate(zip(base_rows, fused_rows)):
                if a != b:
                    failures.append(f"  first diff at row {i}: base={a!r} fused={b!r}")
                    break
            if len(base_rows) != len(fused_rows):
                failures.append(f"  lengths {len(base_rows)} vs {len(fused_rows)}")
        if fused_actually != expect_fused:
            failures.append(
                f"{name}: expected fused={expect_fused}, plan was {fused_plan}")
        if not base_rows:
            failures.append(f"{name}: query returned no rows, comparison is vacuous")

        keys_base = base_wp.get("executionStats")  # not requested; kept for shape parity
        report.append({"case": name, "rows": len(base_rows), "identical": ok,
                       "base_plan": base_plan, "fused_plan": fused_plan,
                       "fused": fused_actually, "_unused": keys_base is None})
        print(f"{'ok  ' if ok and fused_actually == expect_fused else 'FAIL'}  {name}: "
              f"{len(base_rows)} rows, fused={fused_actually}, identical={ok}", flush=True)

    # -- values that stress the decoder -------------------------------------------------
    types = db.types
    types.drop()
    types.insert_many([
        {"k": 1, "s": "plain", "v": 1},
        {"k": 2, "s": "with\x00nul", "v": Int64(9007199254740993)},
        {"k": 3, "s": "two\x00nuls\x00here", "v": Decimal128("1.0000000000000000000000001")},
        {"k": 4, "s": "", "v": None},
        {"k": 5, "s": "trailing\x00", "v": True},
        {"k": 6, "s": "\x00leading", "v": datetime.datetime(1970, 1, 1)},
        {"k": 7, "s": "unicode é中", "v": -0.0},
        {"k": 8, "s": "x" * 500, "v": MinKey()},
        {"k": 9, "s": "y", "v": MaxKey()},
        {"k": 10, "s": "\x00", "v": 3.5},
        {"k": 11, "s": "a\x00\x00b", "v": Int64(-1)},
    ])
    types.create_index([("k", ASCENDING), ("s", ASCENDING), ("v", ASCENDING)], name="kv")

    differential("types/skip-leading", types, {"k": {"$gte": 0}},
                 {"_id": 0, "s": 1, "v": 1}, "kv", [("k", 1)], True)
    differential("types/skip-trailing", types, {"k": {"$gte": 0}},
                 {"_id": 0, "k": 1}, "kv", [("k", 1)], True)
    differential("types/skip-both-sides", types, {"k": {"$gte": 0}},
                 {"_id": 0, "s": 1}, "kv", [("k", 1)], True)
    differential("types/all-components", types, {"k": {"$gte": 0}},
                 {"_id": 0, "k": 1, "s": 1, "v": 1}, "kv", [("k", 1)], True)

    # -- descending components invert the encoding --------------------------------------
    desc = db.desc
    desc.drop()
    desc.insert_many([{"a": i % 5, "b": f"b{i}\x00tail" if i % 3 else f"b{i}", "c": i}
                      for i in range(50)])
    desc.create_index([("a", ASCENDING), ("b", DESCENDING), ("c", ASCENDING)], name="mixed")
    differential("descending/skip-leading", desc, {"a": {"$gte": 0}},
                 {"_id": 0, "b": 1, "c": 1}, "mixed", [("a", 1), ("b", -1)], True)
    differential("descending/skip-middle", desc, {"a": {"$gte": 0}},
                 {"_id": 0, "a": 1, "c": 1}, "mixed", [("a", 1), ("b", -1)], True)

    # -- shapes that must be refused ----------------------------------------------------
    filt = db.filtered
    filt.drop()
    filt.insert_many([{"a": i, "b": i % 7, "c": f"c{i}"} for i in range(100)])
    filt.create_index([("a", ASCENDING), ("b", ASCENDING), ("c", ASCENDING)], name="abc")
    # A non-anchored regex stays a residual filter; an equality would become index bounds.
    differential("refused/residual-filter", filt, {"a": {"$gte": 0}, "c": {"$regex": "9"}},
                 {"_id": 0, "a": 1, "c": 1}, "abc", [("a", 1)], False)

    multi = db.multikey
    multi.drop()
    multi.insert_many([{"a": [1, 2, 3], "b": "x"}, {"a": [2, 3, 4], "b": "y"},
                       {"a": [5], "b": "z"}])
    multi.create_index([("a", ASCENDING), ("b", ASCENDING)], name="ab")
    # An index that is multikey overall can still fully provide a field whose own path is not
    # multikey, so PROJECTION_COVERED over a DEDUPLICATING IXSCAN is reachable -- with no sort, so
    # nothing forces a FETCH. This is the guard that matters: fusing here would emit one row per
    # index entry instead of one per document. {a:[1,2,3]} must yield exactly one {b:"x"}.
    differential("refused/multikey-dedup", multi, {"a": {"$gte": 0}},
                 {"_id": 0, "b": 1}, "ab", None, False)

    # -- a multi-interval scan builds an IndexBoundsChecker, which forces every key onto the
    # -- fused stage's materialised-key fallback rather than its fast path ---------------
    differential("checker/in-list", filt, {"a": {"$in": [1, 5, 9, 42, 77]}},
                 {"_id": 0, "a": 1, "c": 1}, "abc", [("a", 1)], True)

    # -- backward scan: the cursor walks in reverse, the decode must not care -----------
    differential("backward-scan", filt, {"a": {"$gte": 0}},
                 {"_id": 0, "a": 1, "c": 1}, "abc", [("a", -1)], True)

    # -- _id as an included key component ------------------------------------------------
    ids = db.idcover
    ids.drop()
    ids.insert_many([{"_id": i, "a": f"a{i}"} for i in range(20)])
    ids.create_index([("_id", ASCENDING), ("a", ASCENDING)], name="ida")
    differential("id-included", ids, {"_id": {"$gte": 0}},
                 {"_id": 1, "a": 1}, "ida", [("_id", 1)], True)

    # -- single-key result takes the seek path, not the fast path -----------------------
    single = db.single
    single.drop()
    single.insert_one({"a": 1, "b": "only", "c": 7})
    single.create_index([("a", ASCENDING), ("b", ASCENDING), ("c", ASCENDING)], name="abc1")
    differential("single-key-seek-path", single, {"a": 1},
                 {"_id": 0, "b": 1, "c": 1}, "abc1", None, True)

    # -- yielding on every work() -------------------------------------------------------
    prev = client.admin.command(
        {"getParameter": 1, "internalQueryExecYieldIterations": 1})[
            "internalQueryExecYieldIterations"]
    client.admin.command({"setParameter": 1, "internalQueryExecYieldIterations": 1})
    try:
        y = db.yielded
        y.drop()
        y.insert_many([{"path": f"/y/{i:06d}", "node_id": f"n{i}",
                        "title": f"t{i}\x00z" if i % 5 == 0 else f"t{i}"}
                       for i in range(2000)])
        y.create_index([("path", ASCENDING), ("node_id", ASCENDING), ("title", ASCENDING)],
                       name="ycover")
        differential("yield-every-work", y, {"path": {"$gte": "/y/", "$lt": "/y0"}},
                     {"_id": 0, "node_id": 1, "title": 1}, "ycover",
                     [("path", 1), ("node_id", 1)], True)
    finally:
        client.admin.command({"setParameter": 1, "internalQueryExecYieldIterations": prev})

    set_arm(False)
    client.drop_database(DB)

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"cases": report, "failures": failures}, indent=2, default=str))

    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" ", f)
        return 1
    print(f"all {len(report)} differential cases identical, plan shapes as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
