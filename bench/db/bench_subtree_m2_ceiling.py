#!/usr/bin/env python3
"""M2, measured rather than estimated: what would non-key payload columns actually save?

PostgreSQL's INCLUDE stores covering columns uninterpreted and returns them with a length-prefixed
copy. MongoDB order-encodes every covering field into the KeyString, so `title` and `summary` are
escaped on write and scanned back out on read. The question is what the *encoding* costs, separately
from storing and returning the bytes, which INCLUDE also has to do.

The earlier ceiling probe compared a wide index against a narrow one and got 37% of server CPU, but
that is a loose upper bound: it also removes the payload's copy into the output and swaps a 4.66 GB
B-tree for a 0.28 GB one, neither of which INCLUDE recovers.

This measures the encoding directly, in situ, on the real server and the real fused path, by
exploiting a property of KeyString: **BinData is already length-prefixed.**

    key_string.cpp:1629-1644 -- kBinData reads a 1- or 4-byte length, skips that many bytes, and
    hands over a pointer. No terminator scan, no 0x00/0xFF escape handling, no TypeBits.

    key_string.cpp:1576-1600 -- kStringLike calls readCStringWithNuls, which memchrs for the
    terminator and re-splices around embedded NULs, and reads TypeBits to recover String vs Symbol.

So two collections holding the *same payload bytes*, one with the payload as strings and one as
BinData, indexed and scanned identically, differ in exactly the term INCLUDE would remove. The
delta in server CPU and retired instructions per row is the answer.

What this still does not model: INCLUDE would also let the payload skip comparison entirely during
index maintenance and seeks, which this cannot show, and BinData carries 2-5 bytes of length and
subtype per value against a string's single terminator, so the BinData index is marginally larger.
Index sizes are reported so the reader can see the difference.

Outputs differ by type (BinData vs String) and so are not element-wise comparable; row counts and
payload byte totals are checked instead, and only server-side metrics are compared.

Usage:
    bench_subtree_m2_ceiling.py --path /000006/000075/000773 --blocks 10 --seconds 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from bson.binary import Binary
from pymongo import ASCENDING, MongoClient

import bench_subtree_fused_ab as ab

SRC_DB = "bench"
SRC_COLL = "layout2_view"
PROBE_DB = "m2_probe"
COVER = "cover"


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def build_collections(client: MongoClient, path: str) -> tuple[int, int]:
    """Two collections with identical payload bytes, differing only in payload BSON type."""
    lower, upper = path + "/", path + "0"
    src = client[SRC_DB][SRC_COLL]
    db = client[PROBE_DB]
    db["as_string"].drop()
    db["as_bindata"].drop()

    str_batch, bin_batch = [], []
    rows = payload_bytes = 0
    for d in src.find({"path": {"$gte": lower, "$lt": upper}},
                      {"_id": 0, "path": 1, "node_id": 1, "title": 1, "summary": 1}):
        title, summary = d.get("title", ""), d.get("summary", "")
        payload_bytes += len(title.encode()) + len(summary.encode())
        str_batch.append({"path": d["path"], "node_id": d["node_id"],
                          "title": title, "summary": summary})
        bin_batch.append({"path": d["path"], "node_id": d["node_id"],
                          "title": Binary(title.encode()),
                          "summary": Binary(summary.encode())})
        rows += 1
        if len(str_batch) >= 2000:
            db["as_string"].insert_many(str_batch, ordered=False)
            db["as_bindata"].insert_many(bin_batch, ordered=False)
            str_batch, bin_batch = [], []
    if str_batch:
        db["as_string"].insert_many(str_batch, ordered=False)
        db["as_bindata"].insert_many(bin_batch, ordered=False)

    for name in ("as_string", "as_bindata"):
        db[name].create_index(
            [("path", ASCENDING), ("node_id", ASCENDING),
             ("title", ASCENDING), ("summary", ASCENDING)], name=COVER)
    log(f"built {rows} rows in each collection, {payload_bytes:,} payload bytes")
    for name in ("as_string", "as_bindata"):
        st = client[PROBE_DB].command("collStats", name)
        log(f"  {name}: index {st['indexSizes'][COVER] / 1e6:.1f} MB, "
            f"data {st['size'] / 1e6:.1f} MB")
    return rows, payload_bytes


def scan(coll: Any, lower: str, upper: str) -> tuple[int, int]:
    cur = (coll.find({"path": {"$gte": lower, "$lt": upper}},
                     {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
           .sort([("path", 1), ("node_id", 1)]).hint(COVER))
    n = nbytes = 0
    for d in cur:
        t, s = d.get("title"), d.get("summary")
        nbytes += len(t) + len(s)
        n += 1
    return n, nbytes


def window(coll: Any, lower: str, upper: str, pid: int, seconds: float,
           tag: str) -> dict[str, Any]:
    scan(coll, lower, upper)  # settle
    handle = ab.start_perf(pid, seconds, tag)
    time.sleep(0.20)
    ops = 0
    res = (0, 0)
    cpu0 = ab.proc_cpu_us(pid)
    t0 = time.perf_counter()
    while True:
        elapsed = time.perf_counter() - t0
        if ops and elapsed + (elapsed / ops) > seconds:
            break
        if elapsed >= seconds:
            break
        res = scan(coll, lower, upper)
        ops += 1
    wall = (time.perf_counter() - t0) * 1e6
    cpu = ab.proc_cpu_us(pid) - cpu0
    instr = ab.read_perf(handle)
    return {"ops": ops, "rows": res[0], "payload_bytes": res[1],
            "wall_us": wall / max(ops, 1), "cpu_us": cpu / max(ops, 1),
            "instructions": (instr / ops) if (instr is not None and ops) else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--path", default="/000006/000075/000773")
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--warmup-blocks", type=int, default=2)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    uri = f"mongodb://localhost:{args.port}/?directConnection=true"
    client = MongoClient(uri)
    pid = ab.mongod_pid(args.port)
    client.admin.command({"setParameter": 1,
                          "internalQueryEnableFusedCoveredProjection": True})

    rows, payload_bytes = build_collections(client, args.path)
    lower, upper = args.path + "/", args.path + "0"
    db = client[PROBE_DB]

    # Both arms must take the fused path, or this measures something else.
    for name in ("as_string", "as_bindata"):
        wp = (db[name].find({"path": {"$gte": lower, "$lt": upper}},
                            {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
              .sort([("path", 1), ("node_id", 1)]).hint(COVER)).explain()
        wp = wp.get("queryPlanner", {}).get("winningPlan", {})
        fused = ab.ixscan_fused(wp)
        log(f"  {name}: stages={ab.stage_names(wp)} fused={fused}")
        if not fused:
            raise SystemExit(f"{name} did not take the fused path; probe invalid")

    arms = ("as_string", "as_bindata")
    per: dict[str, list[dict[str, Any]]] = {a: [] for a in arms}
    for b in range(args.blocks + args.warmup_blocks):
        order = list(arms) if b % 2 == 0 else list(reversed(arms))
        warm = b < args.warmup_blocks
        for name in order:
            m = window(db[name], lower, upper, pid, args.seconds, f"{b}{name}")
            if m["rows"] != rows or m["payload_bytes"] != payload_bytes:
                raise SystemExit(
                    f"{name} returned {m['rows']} rows / {m['payload_bytes']} payload bytes, "
                    f"expected {rows} / {payload_bytes}")
            if not warm:
                per[name].append(m)
        if warm:
            log(f"  block {b}: warmup, discarded")
            continue
        s_, b_ = per["as_string"][-1], per["as_bindata"][-1]
        log(f"  block {b}: server CPU {(b_['cpu_us']/s_['cpu_us']-1)*100:+.2f}%  "
            f"instructions {(b_['instructions']/s_['instructions']-1)*100:+.2f}%")

    out: dict[str, Any] = {"path": args.path, "rows": rows,
                           "payload_bytes": payload_bytes, "arms": {}}
    for metric in ("instructions", "cpu_us", "wall_us"):
        deltas = []
        for i in range(len(per["as_string"])):
            a, bb = per["as_string"][i][metric], per["as_bindata"][i][metric]
            if a and bb:
                deltas.append((bb / a - 1) * 100)
        if deltas:
            out["arms"][metric] = {"median": statistics.median(deltas),
                                   "min": min(deltas), "max": max(deltas),
                                   "improved": sum(1 for d in deltas if d < 0),
                                   "blocks": len(deltas)}
            log(f"length-prefixed payload vs order-encoded, {metric}: "
                f"{statistics.median(deltas):+.2f}% "
                f"[{min(deltas):+.2f}, {max(deltas):+.2f}] "
                f"{sum(1 for d in deltas if d < 0)}/{len(deltas)} blocks lower")

    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2))
        log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
