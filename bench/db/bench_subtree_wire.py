#!/usr/bin/env python3
"""Two wire-level candidates for get_subtree, measured and bounded.

1. Wire compression (snappy / zstd / zlib).  Enabled server-side already;
   the client modules were missing, so this had never been measured.
2. Output field-name width.  MongoDB repeats "node_id"/"title"/"summary" in
   every returned document; PostgreSQL's DataRow carries no column names.
   The first part measures the byte cost exactly; the second measures what
   decoding the same values under one-character names would cost.

Run with the scratch libs on the path so zstandard/snappy are importable:
    PYTHONPATH=<scratch>/libs314 .venv/bin/python3 bench/db/bench_subtree_wire.py
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import time
from pathlib import Path
from typing import Any

import bson
import pymongo
from pymongo import CursorType

URI = "mongodb://localhost:57017"
NODES = "layout2_view"
COVER_INDEX = "layout2_rootcause_exact_cover"
PROJECTION = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}
COHORT = "bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json"


def log(m: str) -> None:
    print(m, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=11686)
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--blocks", type=int, default=8)
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    s = min(json.loads(Path(COHORT).read_text())["samples"][:200],
            key=lambda x: abs(x["rows"] - args.rows))
    lower, upper = s["path"] + "/", s["path"] + "0"
    flt = {"path": {"$gte": lower, "$lt": upper}}
    sort_spec = [("path", 1), ("node_id", 1)]
    log(f"input path={s['path']} rows={s['rows']}")

    out: dict[str, Any] = {"input": {"path": s["path"], "rows": s["rows"]},
                           "batch": args.batch}

    # ---- 1. exact byte accounting and the field-name share ----------------
    plain = pymongo.MongoClient(URI)
    docs = list(plain["bench"][NODES].find(flt, PROJECTION)
                .sort(sort_spec).hint(COVER_INDEX))
    long_bytes = sum(len(bson.encode(d)) for d in docs)
    short = [{"a": d["node_id"], "b": d["title"], "c": d["summary"]} for d in docs]
    short_bytes = sum(len(bson.encode(d)) for d in short)
    # PostgreSQL DataRow: int16 field count + per field int32 length + payload
    pg_bytes = sum(2 + sum(4 + len(str(v).encode()) for v in
                           (d["node_id"], d["title"], d["summary"])) for d in docs)
    log(f"\npayload for {len(docs)} rows:")
    log(f"  mongodb BSON, report field names : {long_bytes/1e6:7.3f} MB "
        f"({long_bytes/len(docs):.1f} B/row)")
    log(f"  mongodb BSON, 1-char field names : {short_bytes/1e6:7.3f} MB "
        f"({short_bytes/len(docs):.1f} B/row)  "
        f"{100*(short_bytes-long_bytes)/long_bytes:+.1f}%")
    log(f"  postgresql DataRow equivalent    : {pg_bytes/1e6:7.3f} MB "
        f"({pg_bytes/len(docs):.1f} B/row)  "
        f"{100*(pg_bytes-long_bytes)/long_bytes:+.1f}%")
    out["bytes"] = {"rows": len(docs), "mongo_long_names": long_bytes,
                    "mongo_short_names": short_bytes, "pg_datarow": pg_bytes}

    # decode cost of the same values under the two name widths
    long_blob = b"".join(bson.encode(d) for d in docs)
    short_blob = b"".join(bson.encode(d) for d in short)
    dec = {}
    for name, blob in (("long_names", long_blob), ("short_names", short_blob)):
        ts = []
        for _ in range(9):
            gc.collect()
            gc.disable()
            t0 = time.perf_counter()
            bson.decode_all(blob)
            ts.append((time.perf_counter() - t0) * 1e6)
            gc.enable()
        dec[name] = statistics.median(ts)
    log(f"\nclient bson.decode_all of the same {len(docs)} rows:")
    log(f"  report field names : {dec['long_names']/1000:7.3f} ms")
    log(f"  1-char field names : {dec['short_names']/1000:7.3f} ms  "
        f"{100*(dec['short_names']-dec['long_names'])/dec['long_names']:+.1f}%")
    out["decode_us"] = {k: round(v, 1) for k, v in dec.items()}
    del docs, short, long_blob, short_blob
    gc.collect()

    # ---- 2. wire compression ---------------------------------------------
    clients = {"none": plain}
    for algo in ("snappy", "zstd", "zlib"):
        try:
            c = pymongo.MongoClient(URI, compressors=algo)
            c.admin.command("ping")
            clients[algo] = c
        except Exception as exc:  # noqa: BLE001
            log(f"  compressor {algo} unavailable: {exc!r}")

    def arm(client, pipelined):
        nodes = client["bench"][NODES]

        def run():
            kw = {"cursor_type": CursorType.EXHAUST} if pipelined else {}
            cur = nodes.find(flt, PROJECTION, **kw).sort(sort_spec).hint(COVER_INDEX)
            if pipelined:
                cur = cur.batch_size(args.batch)
            return sum(1 for _ in cur)
        return run

    arms = {}
    for algo, c in clients.items():
        arms[f"plain_{algo}"] = arm(c, False)
        arms[f"pipe_{algo}"] = arm(c, True)
    names = list(arms)
    for n in names:
        assert arms[n]() == s["rows"], n

    wall = {n: [] for n in names}
    for b in range(args.blocks):
        order = names if b % 2 == 0 else list(reversed(names))
        for n in order:
            arms[n]()
            gc.collect()
            gc.disable()
            t0 = time.perf_counter()
            for _ in range(args.iters):
                arms[n]()
            wall[n].append((time.perf_counter() - t0) * 1e6 / args.iters)
            gc.enable()
    base = "plain_none"
    log("\nwire compression on loopback (wall p50, paired vs uncompressed):")
    out["compression"] = {}
    for n in names:
        pp = [round(100 * (wall[n][i] - wall[base][i]) / wall[base][i], 2)
              for i in range(args.blocks)]
        out["compression"][n] = {"wall_us": [round(x, 1) for x in wall[n]],
                                 "wall_p50_us": round(statistics.median(wall[n]), 1),
                                 "paired_pct_vs_plain_none": pp}
        log(f"  {n:14s} {statistics.median(wall[n])/1000:8.2f} ms   "
            f"paired median {statistics.median(pp):+7.2f}%  "
            f"range [{min(pp):+.1f},{max(pp):+.1f}]")
    ss = plain.admin.command("serverStatus")["network"]["compression"]
    out["server_compression_counters"] = ss
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=1))
        log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
