#!/usr/bin/env python3
"""Candidate wins for get_subtree on MongoDB 7.0.34.

Two modes:

  explore  run each arm once on one input, check output equality against the
           baseline element-wise, print explain plans and rough timings.
  paired   interleaved paired blocks over the chosen arms, per-block deltas.

Every arm returns the same logical result: the ordered list of
(node_id, title, summary) for the strict descendants of a node, so the
equality check is element-wise on that list.  Arms that only drain (never
materialize rows) are marked drain-only and compared on row count plus a
fingerprint taken from a separate materializing pass.
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
from typing import Any, Callable

import bson
import pymongo
from bson.codec_options import CodecOptions
from bson.raw_bson import RawBSONDocument
from pymongo import CursorType

URI = "mongodb://localhost:57017"
DB = "bench"
NODES = "layout2_view"
COVER_INDEX = "layout2_rootcause_exact_cover"
NODE_INDEX = "allops_tree_node"
TREE_ID = "base"
PROJECTION = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}
COHORT = "bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json"

CLK_TCK = os.sysconf("SC_CLK_TCK")


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


# --------------------------------------------------------------------------
# server cpu accounting: sum of utime+stime deltas over every mongod thread
# --------------------------------------------------------------------------

def mongod_pid() -> int:
    """Host pid of the condb_mongo container's mongod (serverStatus reports 1)."""
    out = os.popen("docker inspect -f '{{.State.Pid}}' condb_mongo").read().strip()
    return int(out)


def proc_cpu_ns(pid: int) -> int:
    """Sum of schedstat run-time over every mongod thread, in nanoseconds."""
    total = 0
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return 0
    for t in tids:
        try:
            total += int(Path(f"/proc/{pid}/task/{t}/schedstat").read_text().split()[0])
        except (OSError, IndexError, ValueError):
            continue
    return total


# --------------------------------------------------------------------------
# arms
# --------------------------------------------------------------------------

def make_arms(client: pymongo.MongoClient, lower: str, upper: str,
              root_node_id: str):
    db = client[DB]
    nodes = db[NODES]
    raw_opts = CodecOptions(document_class=RawBSONDocument)
    raw_db = db.with_options(codec_options=raw_opts)

    flt = {"path": {"$gte": lower, "$lt": upper}}
    sort_spec = [("path", 1), ("node_id", 1)]

    def cursor():
        return (nodes.find(flt, PROJECTION)
                .sort(sort_spec).hint(COVER_INDEX))

    # --- A. report baseline: drain the pymongo cursor -----------------------
    def a_drain():
        return sum(1 for _ in cursor())

    # --- B. report contract: materialize row tuples -------------------------
    def b_rows():
        return [(r["node_id"], r["title"], r["summary"]) for r in cursor()]

    # --- C. manual find/getMore: no per-document python ---------------------
    def _manual(batch_kwargs=None, database=db):
        cmd = {"find": NODES, "filter": flt, "projection": PROJECTION,
               "sort": {"path": 1, "node_id": 1}, "hint": COVER_INDEX}
        if batch_kwargs:
            cmd.update(batch_kwargs)
        reply = database.command(cmd)
        cur = reply["cursor"]
        out = [cur["firstBatch"]]
        cid = cur["id"]
        while cid:
            reply = database.command({"getMore": cid, "collection": NODES})
            cur = reply["cursor"]
            out.append(cur["nextBatch"])
            cid = cur["id"]
        return out

    def c_manual_batches():
        return sum(len(b) for b in _manual())

    def c_manual_rows():
        out = []
        for b in _manual():
            out.extend((d["node_id"], d["title"], d["summary"]) for d in b)
        return out

    # --- D. network floor: reply never decoded past the top level ----------
    def d_raw_reply():
        n = 0
        for b in _manual(database=raw_db):
            n += len(b)
        return n

    # --- D0. true client floor: reply received, batch array never touched --
    # RawBSONDocument keeps the reply lazy; reading only cursor.id avoids
    # building the per-document objects, so this is server + wire + nothing.
    def d0_raw_untouched():
        cmd = {"find": NODES, "filter": flt, "projection": PROJECTION,
               "sort": {"path": 1, "node_id": 1}, "hint": COVER_INDEX}
        reply = raw_db.command(cmd)
        cid = reply["cursor"]["id"]
        n = 0
        while cid:
            n += 1
            reply = raw_db.command({"getMore": cid, "collection": NODES})
            cid = reply["cursor"]["id"]
        return n

    # --- E. exhaust cursor --------------------------------------------------
    def e_exhaust():
        cur = (nodes.find(flt, PROJECTION, cursor_type=CursorType.EXHAUST)
               .sort(sort_spec).hint(COVER_INDEX))
        return sum(1 for _ in cur)

    def e_exhaust_rows():
        cur = (nodes.find(flt, PROJECTION, cursor_type=CursorType.EXHAUST)
               .sort(sort_spec).hint(COVER_INDEX))
        return [(r["node_id"], r["title"], r["summary"]) for r in cur]

    # --- F. short output field names via aggregation ------------------------
    AGG_SHORT = [{"$match": flt},
                 {"$project": {"_id": 0, "a": "$node_id", "b": "$title",
                               "c": "$summary"}}]

    def f_short_rows():
        cur = nodes.aggregate(AGG_SHORT, hint=COVER_INDEX)
        return [(r["a"], r["b"], r["c"]) for r in cur]

    # --- G. columnar: one document, three parallel arrays -------------------
    AGG_COL = [{"$match": flt},
               {"$group": {"_id": None,
                           "a": {"$push": "$node_id"},
                           "b": {"$push": "$title"},
                           "c": {"$push": "$summary"}}}]

    def g_columnar_rows():
        docs = list(nodes.aggregate(AGG_COL, hint=COVER_INDEX))
        if not docs:
            return []
        d = docs[0]
        return list(zip(d["a"], d["b"], d["c"]))

    # --- H. columnar chunked by a fixed-width path prefix -------------------
    # every path component is exactly 7 bytes ("/" + 6 digits), so a byte
    # prefix of length L is a whole number of components and its lexicographic
    # order is the same as full path order.
    def make_chunked(prefix_len: int):
        pipeline = [{"$match": flt},
                    {"$group": {"_id": {"$substrBytes": ["$path", 0, prefix_len]},
                                "a": {"$push": "$node_id"},
                                "b": {"$push": "$title"},
                                "c": {"$push": "$summary"}}},
                    {"$sort": {"_id": 1}}]

        def run():
            out = []
            for d in nodes.aggregate(pipeline, hint=COVER_INDEX, allowDiskUse=False):
                out.extend(zip(d["a"], d["b"], d["c"]))
            return out
        return run, pipeline

    # --- I. count only: server walk, no key->BSON, no payload --------------
    def i_count():
        return nodes.count_documents(flt, hint=COVER_INDEX)

    # --- J. pipelining: exhaust + explicit batch size -----------------------
    # A non-exhaust cursor is strictly serialized: the server materializes a
    # whole batch (up to 16 MB) before writing any of it, and only starts the
    # next batch when the client's getMore arrives.  An exhaust cursor keeps
    # producing without waiting, so batch k+1 is built while the client is
    # still decoding batch k.  Small batches make that overlap possible.
    def make_paced(batch: int | None, exhaust: bool, materialize: bool):
        kw = {}
        if exhaust:
            kw["cursor_type"] = CursorType.EXHAUST

        def run():
            cur = nodes.find(flt, PROJECTION, **kw).sort(sort_spec).hint(COVER_INDEX)
            if batch:
                cur = cur.batch_size(batch)
            if materialize:
                return [(r["node_id"], r["title"], r["summary"]) for r in cur]
            return sum(1 for _ in cur)
        return run

    # --- K. range-sharded parallel cursors ---------------------------------
    def make_parallel(shards: int, materialize: bool, batch: int | None = None,
                      exhaust: bool = False):
        import threading

        def split_points() -> list[tuple[str, str]]:
            # direct children of the root give natural, order-preserving cuts;
            # group them into `shards` contiguous runs.  One indexed lookup on
            # (tree_id,parent_id,path,node_id), the same index get_children uses.
            kids = [d["path"] for d in nodes.find(
                {"tree_id": TREE_ID, "parent_id": root_node_id},
                {"_id": 0, "path": 1}).sort([("path", 1), ("node_id", 1)])
                .hint("allops_tree_parent_path")]
            if len(kids) < shards:
                return [(lower, upper)]
            cuts = [lower] + [kids[round(i * len(kids) / shards)]
                              for i in range(1, shards)] + [upper]
            return list(zip(cuts[:-1], cuts[1:]))

        ranges = split_points()

        def run():
            out: list[Any] = [None] * len(ranges)

            def worker(i, lo, hi):
                kw = {"cursor_type": CursorType.EXHAUST} if exhaust else {}
                cur = (nodes.find({"path": {"$gte": lo, "$lt": hi}}, PROJECTION, **kw)
                       .sort(sort_spec).hint(COVER_INDEX))
                if batch:
                    cur = cur.batch_size(batch)
                if materialize:
                    out[i] = [(r["node_id"], r["title"], r["summary"]) for r in cur]
                else:
                    out[i] = sum(1 for _ in cur)

            ts = [threading.Thread(target=worker, args=(i, lo, hi))
                  for i, (lo, hi) in enumerate(ranges)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            if materialize:
                merged: list[Any] = []
                for part in out:
                    merged.extend(part)
                return merged
            return sum(out)
        run.ranges = ranges  # type: ignore[attr-defined]
        return run

    built = {
        "a_drain": a_drain,
        "b_rows": b_rows,
        "c_manual_batches": c_manual_batches,
        "c_manual_rows": c_manual_rows,
        "d_raw_reply": d_raw_reply,
        "d0_raw_untouched": d0_raw_untouched,
        "e_exhaust": e_exhaust,
        "e_exhaust_rows": e_exhaust_rows,
        "f_short_rows": f_short_rows,
        "g_columnar_rows": g_columnar_rows,
        "i_count": i_count,
    }
    for bs in (250, 500, 1000, 2000, 4000, 8000, 16000):
        built[f"j_exh{bs}"] = make_paced(bs, True, False)
        built[f"j_exh{bs}_rows"] = make_paced(bs, True, True)
        built[f"j_plain{bs}"] = make_paced(bs, False, False)
        built[f"j_plain{bs}_rows"] = make_paced(bs, False, True)
    built["j_exhdefault"] = make_paced(None, True, False)
    for n in (2, 4, 8, 16, 32):
        built[f"k_par{n}"] = make_parallel(n, False)
        built[f"k_par{n}_rows"] = make_parallel(n, True)
        built[f"k_par{n}_exh1000"] = make_parallel(n, False, 1000, True)
        built[f"k_par{n}_exh1000_rows"] = make_parallel(n, True, 1000, True)
    return built, make_chunked


def load_input(target_rows: int) -> dict[str, Any]:
    samples = json.loads(Path(COHORT).read_text())["samples"][:200]
    s = min(samples, key=lambda x: abs(x["rows"] - target_rows))
    return {"path": s["path"], "rows": s["rows"],
            "node_id": s["path"].rsplit("/", 1)[-1],
            "lower": s["path"] + "/", "upper": s["path"] + "0"}


def timed(fn: Callable[[], Any]) -> tuple[float, Any]:
    gc.collect()
    gc.disable()
    t0 = time.perf_counter()
    r = fn()
    dt = (time.perf_counter() - t0) * 1e6
    gc.enable()
    return dt, r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["explore", "paired"])
    ap.add_argument("--rows", type=int, default=11686)
    ap.add_argument("--arms", default="")
    ap.add_argument("--blocks", type=int, default=8)
    ap.add_argument("--iters", type=int, default=2)
    ap.add_argument("--prefix-extra", type=int, default=14)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    inp = load_input(args.rows)
    log(f"input path={inp['path']} rows={inp['rows']}")
    client = pymongo.MongoClient(URI)
    arms, make_chunked = make_arms(client, inp["lower"], inp["upper"],
                                   inp["node_id"])
    plen = len(inp["path"]) + args.prefix_extra
    chunk_fn, chunk_pipeline = make_chunked(plen)
    arms["h_chunked_rows"] = chunk_fn

    if args.mode == "explore":
        db = client[DB]
        nodes = db[NODES]
        # reference
        ref = arms["b_rows"]()
        ref_fp = fingerprint(ref)
        log(f"reference rows={len(ref)} fp={ref_fp[:16]}")
        results = {}
        for name, fn in arms.items():
            try:
                fn()  # warm
                dt, r = timed(fn)
                if isinstance(r, list) and r and isinstance(r[0], tuple):
                    ok = (len(r) == len(ref)) and (fingerprint(r) == ref_fp)
                    detail = f"rows={len(r)} equal={ok}"
                elif isinstance(r, int):
                    detail = f"count={r} equal={r == len(ref)}"
                else:
                    detail = f"type={type(r)}"
                results[name] = {"us": round(dt, 1), "detail": detail}
                log(f"  {name:20s} {dt/1000:9.2f} ms   {detail}")
            except Exception as exc:  # noqa: BLE001
                results[name] = {"error": repr(exc)}
                log(f"  {name:20s} ERROR {exc!r}")
        log("\nchunked pipeline prefix_len=%d" % plen)
        exp = nodes.database.command(
            {"explain": {"aggregate": NODES, "pipeline": chunk_pipeline,
                         "cursor": {}, "hint": COVER_INDEX},
             "verbosity": "queryPlanner"})
        log(json.dumps(exp.get("stages", exp)[:1] if isinstance(exp.get("stages"), list) else exp,
                       default=str)[:2000])
        if args.out:
            Path(args.out).write_text(json.dumps(
                {"input": inp, "results": results}, indent=1))
        return

    # ---- paired ----------------------------------------------------------
    names = [n for n in args.arms.split(",") if n]
    for n in names:
        if n not in arms:
            raise SystemExit(f"unknown arm {n}")
    pid = mongod_pid()
    log(f"mongod pid {pid}; arms {names}; blocks {args.blocks} iters {args.iters}")

    ref = arms["b_rows"]()
    ref_fp = fingerprint(ref)
    for n in names:
        r = arms[n]()
        if isinstance(r, list):
            assert fingerprint(r) == ref_fp, f"{n} output differs"
        elif isinstance(r, int) and n != "d0_raw_untouched":
            assert r == len(ref), f"{n} count differs: {r} vs {len(ref)}"
    del ref
    gc.collect()

    samples: dict[str, list[float]] = {n: [] for n in names}
    cpu: dict[str, list[float]] = {n: [] for n in names}
    ccpu: dict[str, list[float]] = {n: [] for n in names}
    idle: list[float] = []
    for b in range(args.blocks):
        # idle CPU of the whole mongod process: other tenants on this box also
        # drive this server, so record the background rate per block.
        c0 = proc_cpu_ns(pid)
        t0 = time.perf_counter()
        time.sleep(0.15)
        idle.append((proc_cpu_ns(pid) - c0) / (time.perf_counter() - t0) / 1e9)
        order = names if b % 2 == 0 else list(reversed(names))
        for n in order:
            fn = arms[n]
            fn()
            gc.collect()
            gc.disable()
            c0 = proc_cpu_ns(pid)
            p0 = time.process_time()
            t0 = time.perf_counter()
            for _ in range(args.iters):
                r = fn()
                del r
            dt = (time.perf_counter() - t0) * 1e6 / args.iters
            p1 = time.process_time()
            c1 = proc_cpu_ns(pid)
            gc.enable()
            samples[n].append(dt)
            cpu[n].append((c1 - c0) / 1000 / args.iters)
            ccpu[n].append((p1 - p0) * 1e6 / args.iters)
        log(f"  block {b}: " + "  ".join(
            f"{n}={samples[n][-1]/1000:.2f}ms/{cpu[n][-1]/1000:.2f}cpu" for n in order))

    base = names[0]
    log(f"\nmongod background CPU while idle: median {statistics.median(idle):.3f} "
        f"cores, max {max(idle):.3f} (this box has other tenants)")
    out = {"input": inp, "blocks": args.blocks, "iters": args.iters,
           "mongod_idle_cores": [round(x, 4) for x in idle],
           "arms": {}, "paired_pct_vs_" + base: {}}
    for n in names:
        out["arms"][n] = {
            "wall_us": [round(x, 1) for x in samples[n]],
            "server_cpu_us": [round(x, 1) for x in cpu[n]],
            "client_cpu_us": [round(x, 1) for x in ccpu[n]],
            "wall_p50_us": round(statistics.median(samples[n]), 1),
            "cpu_p50_us": round(statistics.median(cpu[n]), 1),
            "client_cpu_p50_us": round(statistics.median(ccpu[n]), 1),
        }
        out["paired_pct_vs_" + base][n] = [
            round(100 * (samples[n][i] - samples[base][i]) / samples[base][i], 2)
            for i in range(args.blocks)]
    log("\nsummary (wall p50 / server cpu p50):")
    for n in names:
        a = out["arms"][n]
        pp = out["paired_pct_vs_" + base][n]
        log(f"  {n:20s} {a['wall_p50_us']/1000:8.2f} ms  srv {a['cpu_p50_us']/1000:6.2f}  cli {a['client_cpu_p50_us']/1000:6.2f}   "
            f"paired median {statistics.median(pp):+6.2f}%  range [{min(pp):+.1f},{max(pp):+.1f}]")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=1))
        log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
