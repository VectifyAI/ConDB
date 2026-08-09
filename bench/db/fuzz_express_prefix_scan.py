#!/usr/bin/env python3
"""Differential fuzz of the express prefix-scan gate against an ungated server.

The A/B benchmark proves the change is fast on one shape.  This asks the question that
actually decides whether it can be proposed: does turning the gate on change the answer
to ANY other query?

Two mongods from the same binary on byte-identical data, differing only in
MONGO_EXPRESS_PREFIX_SCAN.  Every generated query runs on both and the results are
compared element-wise **and order-sensitively** -- order matters because the whole point
of the new path is that it relies on index order to satisfy the sort, so a silently
wrong order is exactly the bug to look for.

The data is chosen to be hostile to the eligibility rules rather than representative:
arrays (multikey), nulls, missing fields, dotted paths, mixed BSON types, duplicate
values, empty strings, and numbers that compare equal across types.  The indexes include
the ones the eligibility is supposed to refuse -- multikey, sparse, partial, descending,
collated -- so that a refusal that does not actually happen shows up as a wrong answer.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from pymongo import MongoClient

GATE = "internalQueryEnableExpressPrefixScan"
DB_NAME = "fuzzdb"
COLL = "fz"

failures: list[str] = []
checked = 0


def log(m: str) -> None:
    print(m, flush=True)


def start(binary: Path, dbpath: Path, logpath: Path, port: int, gate: str | None):
    dbpath.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    proc = subprocess.Popen(
        [str(binary), "--port", str(port), "--dbpath", str(dbpath), "--bind_ip", "127.0.0.1",
         "--wiredTigerCacheSizeGB", "2", "--logpath", str(logpath),
         "--setParameter", "diagnosticDataCollectionEnabled=false",
         "--setParameter", f"{GATE}={'true' if gate == '1' else 'false'}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env)
    uri = f"mongodb://localhost:{port}/?directConnection=true"
    for _ in range(240):
        try:
            MongoClient(uri, serverSelectionTimeoutMS=500).admin.command("ping")
            log(f"  mongod up on {port} ({GATE}={gate})")
            return proc, MongoClient(uri, maxPoolSize=2)[DB_NAME]
        except Exception:
            if proc.poll() is not None:
                raise SystemExit(f"mongod on {port} exited early; see {logpath}")
            time.sleep(0.5)
    raise SystemExit(f"mongod on {port} did not start")


def build_data(db) -> None:
    coll = db[COLL]
    coll.drop()
    docs = []
    n = 0
    for a in ["x", "y", None, ""]:
        for b in [1, 2, 2.0, "1", None]:
            for c in range(3):
                n += 1
                d = {"_id": n, "a": a, "b": b, "c": c, "d": f"d{c}",
                     "sub": {"k": c, "deep": {"z": a}}}
                # every third document carries an array, making arr multikey
                if n % 3 == 0:
                    d["arr"] = [c, c + 1]
                # every fifth document omits 'opt' entirely, so missing vs null differs
                if n % 5 != 0:
                    d["opt"] = c
                docs.append(d)
    # duplicates on the (a,b) prefix, so a prefix run has many rows
    for i in range(60):
        docs.append({"_id": 10_000 + i, "a": "x", "b": 1, "c": i % 7, "d": f"dup{i}",
                     "sub": {"k": i, "deep": {"z": "x"}}, "opt": i})
    coll.insert_many(docs)

    coll.create_index([("a", 1), ("b", 1), ("c", 1), ("d", 1)], name="abcd")
    coll.create_index([("a", 1), ("b", -1)], name="a_b_desc")
    coll.create_index([("arr", 1), ("c", 1)], name="multikey_arr_c")
    coll.create_index([("opt", 1), ("c", 1)], name="sparse_opt", sparse=True)
    coll.create_index([("a", 1), ("c", 1)], name="partial_a_c",
                      partialFilterExpression={"c": {"$gt": 0}})
    coll.create_index([("a", 1), ("d", 1)], name="collated_a_d",
                      collation={"locale": "en", "strength": 2})
    coll.create_index([("sub.k", 1), ("c", 1)], name="dotted_subk_c")
    coll.create_index([("b", "hashed")], name="hashed_b")


def compare(gated, plain, label, run, ordered=True):
    """When the query has no sort, MongoDB guarantees no order, so only the set is compared.
    With a sort, order is part of the answer and is compared."""
    global checked
    checked += 1
    try:
        a = run(gated)
    except Exception as exc:
        a = f"EXC:{type(exc).__name__}:{exc}"
    try:
        b = run(plain)
    except Exception as exc:
        b = f"EXC:{type(exc).__name__}:{exc}"
    if not ordered and isinstance(a, list) and isinstance(b, list):
        a = sorted(a, key=lambda x: json.dumps(x, sort_keys=True, default=str))
        b = sorted(b, key=lambda x: json.dumps(x, sort_keys=True, default=str))
    if a != b:
        failures.append(label)
        log(f"  [DIFF] {label}")
        log(f"         gated: {str(a)[:220]}")
        log(f"         plain: {str(b)[:220]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--scratch", default="/tmp/mongo-getchildren-fuzz")
    args = ap.parse_args()

    scratch = Path(args.scratch)
    for sub in ("g", "p"):
        d = scratch / sub
        if d.exists():
            subprocess.run(["rm", "-rf", str(d)], check=True)

    gp, gated = start(Path(args.binary).resolve(), scratch / "g", scratch / "g.log", 57081, "1")
    pp, plain = start(Path(args.binary).resolve(), scratch / "p", scratch / "p.log", 57082, None)
    try:
        for db in (gated, plain):
            build_data(db)
        log(f"  data built: {gated[COLL].count_documents({})} docs, "
            f"{len(list(gated[COLL].list_indexes()))} indexes")

        eq_values = {"a": ["x", "y", None, "", "zzz"],
                     "b": [1, 2, 2.0, "1", None],
                     "c": [0, 1, 2],
                     "opt": [0, 1, None],
                     "arr": [0, 1, 2],
                     "sub.k": [0, 1, 2]}
        sorts = [None, [("c", 1)], [("c", 1), ("d", 1)], [("d", 1)], [("c", -1)],
                 [("b", 1), ("c", 1)], [("_id", 1)]]
        projections = [None, {"_id": 0, "c": 1}, {"_id": 0, "a": 1, "b": 1, "c": 1, "d": 1},
                       {"c": {"$literal": 1}}]
        hints = [None, "abcd", "a_b_desc", "multikey_arr_c", "sparse_opt", "partial_a_c",
                 "dotted_subk_c"]

        log("\n== equality prefixes x sorts x projections ==")
        for a in eq_values["a"]:
            for b in eq_values["b"]:
                for srt in sorts:
                    for proj in projections:
                        flt = {"a": a, "b": b}
                        lbl = f"find({flt}) sort={srt} proj={proj}"

                        def run(db, flt=flt, srt=srt, proj=proj):
                            cur = db[COLL].find(flt, proj)
                            if srt:
                                cur = cur.sort(srt)
                            return [dict(d) for d in cur]
                        compare(gated, plain, lbl, run, ordered=bool(srt))

        log("\n== single-field equalities, including multikey and dotted ==")
        for field in ("a", "c", "opt", "arr", "sub.k"):
            for v in eq_values.get(field, [0, 1]):
                for srt in (None, [("c", 1)], [("c", 1), ("d", 1)]):
                    flt = {field: v}
                    lbl = f"find({flt}) sort={srt}"

                    def run(db, flt=flt, srt=srt):
                        cur = db[COLL].find(flt, {"_id": 1})
                        if srt:
                            cur = cur.sort(srt)
                        return [dict(d) for d in cur]
                    compare(gated, plain, lbl, run, ordered=bool(srt))

        log("\n== hints: the gated path must not silently pick a different index ==")
        for h in hints:
            for srt in (None, [("c", 1)], [("c", 1), ("d", 1)], [("b", 1)]):
                flt = {"a": "x", "b": 1}
                lbl = f"find({flt}) hint={h} sort={srt}"

                def run(db, flt=flt, h=h, srt=srt):
                    cur = db[COLL].find(flt, {"_id": 1})
                    if srt:
                        cur = cur.sort(srt)
                    if h:
                        cur = cur.hint(h)
                    return [dict(d) for d in cur]
                compare(gated, plain, lbl, run, ordered=bool(srt))

        log("\n== plans: shapes that must NOT take express ==")
        # Comparing results alone is not enough, and this is not a hypothetical. {$eq: null} came
        # back identical on both servers here while the gated one was wrongly taking express --
        # only MongoDB's own plan assertion caught it. Results prove "did not go wrong this time";
        # asserting the plan proves "did not take the path that can go wrong".
        def gated_stages(flt, srt=None):
            cmd = {"find": COLL, "filter": flt}
            if srt:
                cmd["sort"] = dict(srt)
            wp = gated.command("explain", cmd, verbosity="queryPlanner")["queryPlanner"]["winningPlan"]
            out, node = [], wp
            while node:
                out.append(node.get("stage"))
                node = node.get("inputStage")
            return out

        must_not_express = [
            ({"a": {"$eq": None}}, [("c", 1)], "equality to null (bounds are inexact)"),
            ({"a": None, "b": None}, [("c", 1)], "two null equalities"),
            ({"a": {"$eq": [0, 1]}}, None, "equality to an array"),
            ({"a": {"$eq": {"k": 1}}}, [("c", 1)], "equality to a subdocument"),
            ({"arr": 1}, [("c", 1)], "equality on a multikey field"),
            ({"a": "x", "c": {"$gt": 0}}, [("d", 1)], "a range alongside the equality"),
        ]
        for flt, srt, why in must_not_express:
            stages = gated_stages(flt, srt)
            if any("EXPRESS" in (st or "") for st in stages):
                failures.append(f"plan:{why}")
                log(f"  [PLAN] {why}: gated took express -- {stages}")

        log("\n== shapes the eligibility must refuse ==")
        refusals = {
            "limit": lambda db: [dict(d) for d in db[COLL].find({"a": "x", "b": 1}, {"_id": 1})
                                 .sort([("c", 1), ("d", 1)]).limit(5)],
            "skip": lambda db: [dict(d) for d in db[COLL].find({"a": "x", "b": 1}, {"_id": 1})
                                .sort([("c", 1), ("d", 1)]).skip(3)],
            "batchSize": lambda db: [dict(d) for d in db[COLL].find({"a": "x", "b": 1}, {"_id": 1})
                                     .sort([("c", 1), ("d", 1)]).batch_size(2)],
            "range": lambda db: [dict(d) for d in db[COLL].find({"a": "x", "b": {"$gte": 1}},
                                                                {"_id": 1}).sort([("c", 1)])],
            "$in": lambda db: [dict(d) for d in db[COLL].find({"a": {"$in": ["x", "y"]}},
                                                              {"_id": 1}).sort([("c", 1)])],
            "regex eq": lambda db: [dict(d) for d in db[COLL].find({"a": {"$eq": "x"}, "d": "d1"},
                                                                   {"_id": 1})],
            "array equality": lambda db: [dict(d) for d in db[COLL].find({"arr": [0, 1]},
                                                                         {"_id": 1})],
            "collation query": lambda db: [dict(d) for d in db[COLL].find({"a": "X"}, {"_id": 1})
                                           .collation({"locale": "en", "strength": 2})],
            "count": lambda db: db[COLL].count_documents({"a": "x", "b": 1}),
            "distinct": lambda db: sorted(str(v) for v in db[COLL].distinct("c", {"a": "x"})),
            "aggregate $match": lambda db: [dict(d) for d in db[COLL].aggregate(
                [{"$match": {"a": "x", "b": 1}}, {"$sort": {"c": 1, "d": 1}},
                 {"$project": {"_id": 1}}])],
            "update": lambda db: db[COLL].update_many({"a": "zzz"}, {"$set": {"touched": 1}}).raw_result,
            "delete none": lambda db: db[COLL].delete_many({"a": "zzz-none"}).raw_result,
            "explain shape": lambda db: db.command(
                "explain", {"find": COLL, "filter": {"a": "x", "b": 1},
                            "sort": {"c": 1, "d": 1}},
                verbosity="queryPlanner")["queryPlanner"]["namespace"],
        }
        for name, fn in refusals.items():
            compare(gated, plain, f"refusal:{name}", fn)

        log("\n== duplicate index keys spanning batches ==")
        # The case that caught silent data loss. An earlier version stored the resume point as the
        # key WITHOUT its RecordId, and for a standard index the RecordId is part of the key, so
        # re-seeking exclusive-after it stepped past EVERY entry sharing that key value. 500 docs
        # with one identical key returned 101 -- the first batch, then nothing, no error.
        #
        # No tree workload can catch this: every child has a distinct sort key, so a run of
        # duplicates never exists in the data. It has to be built on purpose.
        for db in (gated, plain):
            db.drop_collection("dupkeys")
            db["dupkeys"].insert_many(
                [{"_id": i, "a": 1, "b": 2, "c": 3, "d": 4} for i in range(500)])
            db["dupkeys"].create_index([("a", 1), ("b", 1), ("c", 1), ("d", 1)], name="abcd")

        def dup_run(db):
            return [x["_id"] for x in
                    db["dupkeys"].find({"a": 1, "b": 2}, {"_id": 1}).sort([("c", 1), ("d", 1)])]
        compare(gated, plain, "500 identical index keys across ~5 batches", dup_run)

        # and a mixture, so some runs of duplicates straddle a batch boundary and some do not
        for db in (gated, plain):
            db.drop_collection("dupmix")
            db["dupmix"].insert_many(
                [{"_id": i, "a": 1, "b": 2, "c": i // 37, "d": "same"} for i in range(700)])
            db["dupmix"].create_index([("a", 1), ("b", 1), ("c", 1), ("d", 1)], name="abcd")

        def dupmix_run(db):
            return [x["_id"] for x in
                    db["dupmix"].find({"a": 1, "b": 2}, {"_id": 1}).sort([("c", 1), ("d", 1)])]
        compare(gated, plain, "700 docs in runs of 37 identical keys", dupmix_run)

        log("\n== getMore across many batches, and under forced yielding ==")
        for yield_iters in (128, 1):
            for db in (gated, plain):
                db.client.admin.command({"setParameter": 1,
                                         "internalQueryExecYieldIterations": yield_iters})

            def run(db):
                return [d["_id"] for d in db[COLL].find({"a": "x", "b": 1}, {"_id": 1})
                        .sort([("c", 1), ("d", 1)])]
            compare(gated, plain, f"prefix run, yieldIterations={yield_iters}", run)
    finally:
        for pr in (gp, pp):
            pr.send_signal(signal.SIGTERM)
            pr.wait(timeout=180)

    log("")
    log(f"{checked} query shapes compared")
    if failures:
        log(f"FAILED — {len(failures)} differed:")
        for f in failures[:40]:
            log(f"  - {f}")
        sys.exit(1)
    log("NO DIFFERENCES — the gate does not change the answer to any of these")


if __name__ == "__main__":
    main()
