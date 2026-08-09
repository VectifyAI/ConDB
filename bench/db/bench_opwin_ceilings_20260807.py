#!/usr/bin/env python3
"""Server-side ceilings for get_children, and the IDHACK eligibility check.

Part 1 -- ceilings.  These arms do NOT return the same rows as get_children.
They are deliberately weaker contracts run against the same index and the same
key range, so each one is an upper bound on what a change of that kind could
recover.  They are labelled ceilings and must never be quoted as candidates.

    children_base        node_id,title,summary   IXSCAN -> FETCH -> project
    ceil_covered         node_id only            IXSCAN covered, no FETCH
    ceil_count           one integer             COUNT_SCAN, no key decode

Part 2 -- IDHACK eligibility of a natural-key _id.  7.0.34's
CanonicalQuery::isSimpleIdQuery accepts a literal sub-document, and
isIdHackEligibleQuery additionally requires an empty hint.  Confirmed by
explain against a small probe collection, not by reading the source alone.

Part 3 -- embedded children.  One document per parent, _id = the natural key.
Built from real rows over a path-prefix slice of the real collection.  The
absolute number is not transferable to the 10M collection because the probe
collection's _id index is shallower; what IS transferable is the *payload
increment* between a 1-child and an n-child embedded read, which composes with
the IDHACK point-read cost already measured on the real 10M collection.
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
    NODES, CHILD_PROJ, RawMongo, thread_table, mongod_pid,
)

PROBE = "opwin_children_embedded"


def log(m: str) -> None:
    print(m, flush=True)


def timed_blocks(arms: dict[str, Any], names: list[str], pid: int,
                 blocks: int, iters: int, n_in: int) -> dict[str, list]:
    results = {n: [] for n in names}
    for b in range(blocks):
        order = names[b % len(names):] + names[:b % len(names)]
        for n in order:
            fn, comm = arms[n]["fn"], arms[n]["conn"]
            before = thread_table(pid)
            gc.disable()
            t0 = time.perf_counter(); c0 = time.process_time()
            for k in range(iters):
                fn(k % n_in)
            c1 = time.process_time(); t1 = time.perf_counter()
            gc.enable()
            after = thread_table(pid)
            results[n].append({
                "wall_us": (t1 - t0) * 1e6 / iters,
                "ccpu_us": (c1 - c0) * 1e6 / iters,
                "scpu_us": (after.get(comm, 0) - before.get(comm, 0)) / 1e3 / iters,
            })
        log(f"  block {b+1}/{blocks}")
    return results


def med(results, n, k):
    return statistics.median(r[k] for r in results[n])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="mongodb://172.17.0.3:27017")
    ap.add_argument("--db", default="bench")
    ap.add_argument("--blocks", type=int, default=8)
    ap.add_argument("--iters", type=int, default=300)
    ap.add_argument("--inputs", type=int, default=64)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--slice-prefix", default="/000000")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    host, port = args.uri.split("//")[1].split(":")
    cli = MongoClient(args.uri, maxPoolSize=1, minPoolSize=1,
                      directConnection=True)
    db = cli[args.db]
    conn = db.command("hello")["connectionId"]
    raw = RawMongo(host, int(port))
    pid = mongod_pid("condb_mongo")
    out: dict[str, Any] = {"run": {"generated_unix_s": time.time(),
                                   "uri": args.uri, "blocks": args.blocks,
                                   "iters": args.iters,
                                   "profile": db.command("profile", -1)}}

    samples = json.loads(
        Path("bench/db/runs/report_3eng_20260716/"
             "layout_2v3_postgres_10m_final.json").read_text())["samples"]
    parent_ids = [s["path"].rsplit("/", 1)[-1] for s in samples[:args.inputs]]

    # ---------------- Part 2: IDHACK eligibility ------------------------
    log("part 2: IDHACK eligibility of natural-key _id shapes")
    probe = db["opwin_idhack_probe"]
    probe.drop()
    probe.insert_many([
        {"_id": {"tree_id": "base", "node_id": f"{i:06d}"}, "v": i}
        for i in range(1000)
    ] + [
        {"_id": f"base|{i:06d}", "v": i} for i in range(1000)
    ])
    elig = {}
    for label, flt, hint in (
        ("subdoc_id_nohint", {"_id": {"tree_id": "base", "node_id": "000500"}}, None),
        ("string_id_nohint", {"_id": "base|000500"}, None),
        ("string_id_withhint", {"_id": "base|000500"}, "_id_"),
    ):
        cmd = {"find": "opwin_idhack_probe", "filter": flt}
        if hint:
            cmd["hint"] = hint
        ex = db.command({"explain": cmd, "verbosity": "queryPlanner"})
        elig[label] = ex["queryPlanner"]["winningPlan"].get("stage")
        log(f"  {label:22} winning stage = {elig[label]}")
    ex = db.command({"explain": {"find": NODES,
                                 "filter": {"tree_id": "base",
                                            "node_id": parent_ids[0]}},
                     "verbosity": "queryPlanner"})
    wp = ex["queryPlanner"]["winningPlan"]
    elig["get_node_unhinted_real"] = json.dumps(wp)[:400]
    log(f"  get_node unhinted on the real collection: {json.dumps(wp)[:200]}")
    probe.drop()
    out["idhack_eligibility"] = elig

    # ---------------- Part 3: build embedded probe ----------------------
    if args.build:
        log(f"part 3: building {PROBE} from path prefix {args.slice_prefix}")
        t0 = time.time()
        db[PROBE].drop()
        db[NODES].aggregate([
            {"$match": {"path": {"$gte": args.slice_prefix + "/",
                                 "$lt": args.slice_prefix + "0"}}},
            {"$group": {"_id": {"$concat": ["$tree_id", "|", "$parent_id"]},
                        "children": {"$push": {"node_id": "$node_id",
                                               "title": "$title",
                                               "summary": "$summary"}}}},
            {"$out": PROBE},
        ], allowDiskUse=True)
        # matched single-child twin: same _ids, children truncated to one.
        # Its only job is to price the payload, so the increment between the
        # two is a clean per-child cost on identical keys.
        db[PROBE + "_1"].drop()
        db[PROBE].aggregate([
            {"$project": {"children": {"$slice": ["$children", 1]}}},
            {"$out": PROBE + "_1"},
        ], allowDiskUse=True)
        log(f"  built in {time.time()-t0:.1f}s, "
            f"{db[PROBE].estimated_document_count()} documents")
    st = db.command("collStats", PROBE)
    st1 = db.command("collStats", PROBE + "_1")
    out["embedded_probe"] = {"count": st["count"], "avgObjSize": st.get("avgObjSize"),
                             "size": st["size"], "storageSize": st["storageSize"],
                             "slice_prefix": args.slice_prefix,
                             "twin_count": st1["count"],
                             "twin_avgObjSize": st1.get("avgObjSize")}
    log(f"  {PROBE}: {st['count']} docs, avgObjSize {st.get('avgObjSize')}")

    # inputs for the embedded arm: parents whose fan-out matches the cohort
    target = statistics.median(
        len(list(db[NODES].find({"tree_id": "base", "parent_id": p}, CHILD_PROJ)
                 .hint("allops_tree_parent_path")))
        for p in parent_ids[:16])
    cand = list(db[PROBE].aggregate([
        {"$project": {"n": {"$size": "$children"}}},
        {"$match": {"n": {"$gte": int(target) - 2, "$lte": int(target) + 2}}},
        {"$limit": args.inputs},
    ]))
    emb_ids = [c["_id"] for c in cand]
    one_child = emb_ids  # same keys, read from the single-child twin
    log(f"  cohort median fan-out {target}; {len(emb_ids)} matched parents, "
        f"{len(one_child)} single-child parents")
    if len(emb_ids) < 8 or len(one_child) < 8:
        raise SystemExit("not enough probe inputs")
    out["embedded_probe"]["target_fanout"] = target
    out["embedded_probe"]["n_inputs"] = len(emb_ids)

    # ---------------- arms ----------------------------------------------
    arms: dict[str, Any] = {}

    def add(name, fn, note):
        arms[name] = {"fn": fn, "conn": f"conn{raw.connection_id}", "note": note}

    def rawfind(cmd):
        r = raw.command(dict(cmd, **{"$db": args.db}))
        assert r["cursor"]["id"] == 0
        return r["cursor"]["firstBatch"]

    add("children_base", lambda i: rawfind({
        "find": NODES, "filter": {"tree_id": "base", "parent_id": parent_ids[i]},
        "sort": {"path": 1, "node_id": 1}, "hint": "allops_tree_parent_path",
        "projection": CHILD_PROJ}), "REFERENCE contract: node_id,title,summary")
    add("ceil_covered", lambda i: rawfind({
        "find": NODES, "filter": {"tree_id": "base", "parent_id": parent_ids[i]},
        "sort": {"path": 1, "node_id": 1}, "hint": "allops_tree_parent_path",
        "projection": {"_id": 0, "node_id": 1}}),
        "CEILING covered scan, node_id only, no FETCH")
    add("ceil_count", lambda i: raw.command({
        "count": NODES, "query": {"tree_id": "base", "parent_id": parent_ids[i]},
        "hint": "allops_tree_parent_path", "$db": args.db}),
        "CEILING count only, COUNT_SCAN")
    add("emb_n", lambda i: rawfind({
        "find": PROBE, "filter": {"_id": emb_ids[i % len(emb_ids)]}}),
        f"embedded array, ~{int(target)} children, one IDHACK read")
    add("emb_1", lambda i: rawfind({
        "find": PROBE + "_1", "filter": {"_id": one_child[i % len(one_child)]}}),
        "embedded array, 1 child, same keys, one IDHACK read")

    names = list(arms)
    for n in names:
        for i in range(200):
            arms[n]["fn"](i % len(parent_ids))
    log("timing")
    results = timed_blocks(arms, names, pid, args.blocks, args.iters,
                           min(len(parent_ids), len(emb_ids), len(one_child)))

    out["summary"] = {
        n: {"note": arms[n]["note"],
            "wall_us": round(med(results, n, "wall_us"), 2),
            "ccpu_us": round(med(results, n, "ccpu_us"), 2),
            "scpu_us": round(med(results, n, "scpu_us"), 2)}
        for n in names
    }
    out["paired_scpu_vs_children_base"] = {}
    for n in names:
        if n == "children_base":
            continue
        d = [results[n][b]["scpu_us"] - results["children_base"][b]["scpu_us"]
             for b in range(args.blocks)]
        p = [100 * dd / results["children_base"][b]["scpu_us"]
             for b, dd in enumerate(d)]
        out["paired_scpu_vs_children_base"][n] = {
            "median_delta_us": round(statistics.median(d), 2),
            "median_pct": round(statistics.median(p), 2),
            "min_pct": round(min(p), 2), "max_pct": round(max(p), 2)}
    out["raw"] = results

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2))
    log("")
    log(f"{'arm':<18}{'wall':>9}{'ccpu':>9}{'scpu':>9}   note")
    for n in names:
        s = out["summary"][n]
        log(f"{n:<18}{s['wall_us']:>9.1f}{s['ccpu_us']:>9.1f}{s['scpu_us']:>9.1f}"
            f"   {s['note'][:50]}")
    log("")
    for n, p in out["paired_scpu_vs_children_base"].items():
        log(f"{n:<18} scpu {p['median_pct']:>7.2f}% "
            f"({p['median_delta_us']:+.1f} us) [{p['min_pct']:.1f},{p['max_pct']:.1f}]")
    raw.close(); cli.close()


if __name__ == "__main__":
    main()
