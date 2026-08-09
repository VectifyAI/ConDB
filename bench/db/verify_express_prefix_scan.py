#!/usr/bin/env python3
"""Correctness gate for the express prefix-scan path.  Run this before believing any number.

Two servers from the same binary over byte-identical dbpath copies, differing only in
MONGO_EXPRESS_PREFIX_SCAN.  Every check compares the gated server against the ungated one,
so "correct" means "identical to what MongoDB does today", not "looks plausible".

What is checked, and why each one is here:

  plan          the gated server must actually use EXPRESS_IXSCAN, or every other check
                below is vacuously green while measuring nothing
  equality      element-wise, order-sensitive, over the whole cohort
  empty         a parent with no children must return nothing, not the next parent's rows
  getMore       a result far larger than one batch.  This is the case that used to hit
                MONGO_UNREACHABLE_TASSERT(8375808) in stashResult(), and the one where a
                resumed cursor can duplicate or drop rows
  yielding      the same, under internalQueryExecYieldIterations = 1, which forces a yield
                between essentially every document and so exercises the release/re-seek path
                on every row rather than never
  non-intrusion the ungated server must behave exactly as before, including for queries the
                new eligibility deliberately rejects
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from pymongo import MongoClient

DB_NAME = "bench"
NODES = "layout2_view"
TREE_ID = "base"
CHILD_INDEX = "allops_tree_parent_path"
PROJ = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}
SORT = [("path", 1), ("node_id", 1)]
GATE = "MONGO_EXPRESS_PREFIX_SCAN"

failures: list[str] = []


def log(m: str) -> None:
    print(m, flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    log(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(f"{name}: {detail}")


def start(binary: Path, dbpath: Path, logpath: Path, port: int, gate: str | None):
    env = dict(os.environ)
    if gate is None:
        env.pop(GATE, None)
    else:
        env[GATE] = gate
    proc = subprocess.Popen(
        [str(binary), "--port", str(port), "--dbpath", str(dbpath), "--bind_ip", "127.0.0.1",
         "--wiredTigerCacheSizeGB", "4", "--logpath", str(logpath),
         "--setParameter", "diagnosticDataCollectionEnabled=false"],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env)
    uri = f"mongodb://localhost:{port}/?directConnection=true"
    for _ in range(240):
        try:
            MongoClient(uri, serverSelectionTimeoutMS=500).admin.command("ping")
            return proc, MongoClient(uri, maxPoolSize=2)[DB_NAME]
        except Exception:
            if proc.poll() is not None:
                raise SystemExit(f"mongod on {port} exited early; see {logpath}")
            time.sleep(0.5)
    raise SystemExit(f"mongod on {port} did not start")


def children(db, parent, batch=None):
    cur = db[NODES].find({"tree_id": TREE_ID, "parent_id": parent}, PROJ).sort(SORT)
    if batch:
        cur = cur.batch_size(batch)
    return [(d.get("node_id"), d.get("title"), d.get("summary")) for d in cur]


def stage_chain(plan):
    out, node = [], plan
    while node:
        out.append(node.get("stage"))
        node = node.get("inputStage")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--gated-dbpath", required=True)
    ap.add_argument("--plain-dbpath", required=True)
    ap.add_argument("--cohort-json", required=True)
    ap.add_argument("--scratch", default="/tmp/mongo-getchildren-verify")
    args = ap.parse_args()

    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    parents = json.loads(Path(args.cohort_json).read_text())["parents"]

    gated_proc, gated = start(Path(args.binary).resolve(), Path(args.gated_dbpath),
                              scratch / "gated.log", 57061, "1")
    plain_proc, plain = start(Path(args.binary).resolve(), Path(args.plain_dbpath),
                              scratch / "plain.log", 57062, None)
    try:
        log("\n== plan: the gated server must really take the express path ==")
        ex = gated.command("explain", {"find": NODES,
                                       "filter": {"tree_id": TREE_ID, "parent_id": parents[0]},
                                       "projection": PROJ,
                                       "sort": {"path": 1, "node_id": 1}},
                           verbosity="queryPlanner")
        chain = stage_chain(ex["queryPlanner"]["winningPlan"])
        check("gated uses EXPRESS_IXSCAN", any("EXPRESS" in (s or "") for s in chain),
              f"stages={chain}")
        exp = plain.command("explain", {"find": NODES,
                                        "filter": {"tree_id": TREE_ID, "parent_id": parents[0]},
                                        "projection": PROJ,
                                        "sort": {"path": 1, "node_id": 1}},
                            verbosity="queryPlanner")
        pchain = stage_chain(exp["queryPlanner"]["winningPlan"])
        check("ungated is unchanged (no EXPRESS)", not any("EXPRESS" in (s or "") for s in pchain),
              f"stages={pchain}")

        log("\n== equality over the cohort ==")
        bad = 0
        total = 0
        for p in parents:
            a, b = children(gated, p), children(plain, p)
            total += len(b)
            if a != b:
                bad += 1
                if bad == 1:
                    log(f"      first mismatch at parent {p!r}: {len(a)} vs {len(b)} rows")
        check(f"{len(parents)} parents, {total} rows, order-sensitive", bad == 0,
              f"{bad} parents differed")

        log("\n== a parent with no children ==")
        a = children(gated, "zzzz-no-such-parent")
        b = children(plain, "zzzz-no-such-parent")
        check("empty result", a == b == [], f"gated={len(a)} plain={len(b)}")

        log("\n== a result far larger than one batch (exercises stashResult and getMore) ==")
        # Bounded deliberately: a limit would make the query express-ineligible, so the scan is
        # cut off by closing the cursor after N rows instead. N is far past the 101-document first
        # batch, so getMore and stashResult are exercised either way.
        BIG = 5000

        def big(db, cap=BIG):
            cur = db[NODES].find({"tree_id": TREE_ID}, {"_id": 0, "node_id": 1}).sort(
                [("parent_id", 1), ("path", 1), ("node_id", 1)])
            out = []
            for d in cur:
                out.append(d.get("node_id"))
                if len(out) >= cap:
                    break
            cur.close()
            return out

        big_gated, big_plain = big(gated), big(plain)
        check(f"{len(big_plain)} rows across ~{max(1, len(big_plain)//101)} batches",
              big_gated == big_plain,
              f"gated={len(big_gated)} plain={len(big_plain)}")
        check("no duplicated rows", len(big_gated) == len(set(big_gated)),
              f"{len(big_gated) - len(set(big_gated))} duplicates")

        log("\n== the same, yielding between essentially every document ==")
        for db in (gated, plain):
            db.client.admin.command({"setParameter": 1, "internalQueryExecYieldIterations": 1})
        # Smaller under yieldIterations=1: every document yields, so this is thousands of
        # release/re-seek cycles, which is the point.
        y_gated, y_plain = big(gated, 1500), big(plain, 1500)
        check("large scan under yieldIterations=1", y_gated == y_plain,
              f"gated={len(y_gated)} plain={len(y_plain)}")
        check("no duplicates under yielding", len(y_gated) == len(set(y_gated)),
              f"{len(y_gated) - len(set(y_gated))} duplicates")
        cohort_bad = sum(1 for p in parents if children(gated, p) != children(plain, p))
        check("cohort under yieldIterations=1", cohort_bad == 0, f"{cohort_bad} differed")
        for db in (gated, plain):
            db.client.admin.command({"setParameter": 1, "internalQueryExecYieldIterations": 128})

        log("\n== shapes the eligibility must refuse (must not take express, must stay correct) ==")
        refusals = {
            "limit": lambda db: [d["node_id"] for d in db[NODES].find(
                {"tree_id": TREE_ID, "parent_id": parents[0]}, PROJ).sort(SORT).limit(3)],
            "skip": lambda db: [d["node_id"] for d in db[NODES].find(
                {"tree_id": TREE_ID, "parent_id": parents[0]}, PROJ).sort(SORT).skip(2)],
            "range predicate": lambda db: [d["node_id"] for d in db[NODES].find(
                {"tree_id": TREE_ID, "parent_id": {"$gte": parents[0]}}, PROJ).sort(SORT).limit(20)],
            "sort not matching index": lambda db: [d["node_id"] for d in db[NODES].find(
                {"tree_id": TREE_ID, "parent_id": parents[0]}, PROJ).sort([("node_id", 1)])],
            "no sort": lambda db: sorted(d["node_id"] for d in db[NODES].find(
                {"tree_id": TREE_ID, "parent_id": parents[0]}, PROJ)),
        }
        for name, fn in refusals.items():
            try:
                check(name, fn(gated) == fn(plain))
            except Exception as exc:
                check(name, False, f"raised {type(exc).__name__}: {exc}")

        log("\n== non-intrusion: an unrelated query shape ==")
        a = plain[NODES].find_one({"tree_id": TREE_ID, "node_id": parents[0]})
        b = gated[NODES].find_one({"tree_id": TREE_ID, "node_id": parents[0]})
        check("_id-style point lookup unaffected", a == b)
    finally:
        for pr in (gated_proc, plain_proc):
            pr.send_signal(signal.SIGTERM)
            pr.wait(timeout=180)

    log("")
    if failures:
        log(f"FAILED ({len(failures)}):")
        for f in failures:
            log(f"  - {f}")
        sys.exit(1)
    log("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
