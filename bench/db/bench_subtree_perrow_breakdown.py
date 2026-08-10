#!/usr/bin/env python3
"""Which parts of the per-row cost could batching actually amortise?

The per-row probe established that a row of this scan costs ~4,487 retired instructions regardless
of how many bytes it carries -- about 89% of the instructions and ~44% of the server CPU of the
whole operation. That is the ceiling on a batched or vectorised index scan, but only the ceiling:
some of that per-row cost is dispatch and allocation, which batching removes, and some is the
inherent cost of producing one more BSON document, which it does not.

This splits it, using the same trick per symbol. With total payload bytes held equal, a symbol's
cost is `rows * f + bytes * v`, so differencing the two arms isolates `f` for each symbol
individually:

    f(symbol) = (cost_many(symbol) - cost_few(symbol)) / (rows_many - rows_few)

Profiles are taken with `-e instructions:u`, so a symbol's share is directly its share of retired
user instructions, and absolute per-operation instruction counts come from `perf stat` over the
same load. Symbols are then classified by what a batched scan could do about them.

Usage:
    bench_subtree_perrow_breakdown.py --seconds 20
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from pymongo import ASCENDING, MongoClient

import bench_subtree_fused_ab as ab

DB = "perrow_probe"
PREFIX = "/p"
TOTAL_PAYLOAD = 8_000_000

# What a batched / vectorised scan could do about each symbol, judged from what the symbol is.
# "amortisable" = paid once per batch instead of once per row.
# "inherent"    = one more output document costs this no matter how rows are fetched.
CLASSIFY = [
    (r"PlanStage::work|ProjectionStage::doWork|IndexScan::doWork|getNextBatch|"
     r"__invoke_impl.*BSONObjCursorAppender", "amortisable: stage dispatch"),
    (r"__curfile_next|__wt_btcur_next|__wt_cursor_get_raw_key_value|advanceNext|"
     r"wiredTigerPrepareConflictRetry|__wt_btcur_bounds_early_exit|__wt_txn_read",
     "amortisable: WT cursor call"),
    (r"DocumentStorage|Document::toBson|transitionMemberToOwnedObj|WorkingSetMember|"
     r"DocumentMetadataFields|MutableDocument", "amortisable: WSM / Document round-trip"),
    (r"operator new|operator delete|malloc|free|_int_malloc|_int_free",
     "amortisable: per-row allocation"),
    (r"BSONArrayBuilder|BSONObjBuilder.*_done|BSONObjBuilderBase.*append",
     "inherent: BSON document structure"),
    (r"toBsonProjectedSafe|toBsonValue|readCString|readStringLike|ConstDataRange|"
     r"withoutRecordIdLongAtEnd|decodeRecordId", "inherent: KeyString component decode"),
    (r"memmove|memcpy|memchr", "inherent: byte movement"),
]


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def classify(sym: str) -> str:
    for pattern, label in CLASSIFY:
        if re.search(pattern, sym):
            return label
    return "unclassified"


def build(client: MongoClient, name: str, rows: int) -> int:
    db = client[DB]
    if name in db.list_collection_names() and db[name].estimated_document_count() == rows:
        return rows
    db[name].drop()
    per = TOTAL_PAYLOAD // rows
    batch = []
    for i in range(rows):
        summary = ("s%08d" % i) * (per // 9) + "x" * (per % 9)
        batch.append({"path": f"{PREFIX}/{i:09d}", "node_id": f"n{i}",
                      "title": f"t{i}", "summary": summary})
        if len(batch) >= 1000:
            db[name].insert_many(batch, ordered=False)
            batch = []
    if batch:
        db[name].insert_many(batch, ordered=False)
    db[name].create_index([("path", ASCENDING), ("node_id", ASCENDING),
                           ("title", ASCENDING), ("summary", ASCENDING)], name="cover")
    return rows


class Driver(threading.Thread):
    def __init__(self, uri: str, name: str) -> None:
        super().__init__(daemon=True)
        self.uri, self.name, self.stop = uri, name, threading.Event()
        self.ops = 0

    def run(self) -> None:
        c = MongoClient(self.uri)
        coll = c[DB][self.name]
        lower, upper = PREFIX + "/", PREFIX + "0"
        while not self.stop.is_set():
            for _ in (coll.find({"path": {"$gte": lower, "$lt": upper}},
                                {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
                      .sort([("path", 1), ("node_id", 1)]).hint("cover")):
                pass
            self.ops += 1
        c.close()


def profile_arm(uri: str, name: str, pid: int, seconds: int,
                outdir: Path) -> tuple[dict[str, float], float]:
    """Returns (symbol -> share of user instructions, instructions per operation)."""
    d = Driver(uri, name)
    d.start()
    time.sleep(6)

    data = outdir / f"perrow_{name}.data"
    stat = outdir / f"perrow_{name}.stat"
    ops0 = d.ops
    rec = subprocess.Popen(
        ["perf", "record", "-e", "instructions:u", "-F", "1999", "-p", str(pid),
         "-o", str(data), "--", "sleep", str(seconds)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    st = subprocess.Popen(
        ["perf", "stat", "-x,", "-e", "instructions:u", "-p", str(pid),
         "-o", str(stat), "--", "sleep", str(seconds)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rec.wait()
    st.wait()
    ops = d.ops - ops0
    d.stop.set()
    d.join(timeout=60)

    total = 0.0
    for line in stat.read_text().splitlines():
        if "instructions:u" in line and not line.startswith("#"):
            try:
                total = float(line.split(",")[0])
            except ValueError:
                pass
    per_op = total / max(ops, 1)

    out = subprocess.run(
        ["perf", "report", "-i", str(data), "--no-children", "--stdio",
         "--percent-limit", "0.15", "-s", "symbol", "-F", "overhead,symbol"],
        capture_output=True, text=True).stdout
    shares: dict[str, float] = {}
    for line in out.splitlines():
        m = re.match(r"^\s+(\d+\.\d+)%\s+\[[.k]\]\s+(.*?)\s*$", line)
        if m:
            shares[m.group(2)] = float(m.group(1)) / 100.0
    log(f"  {name}: {ops} ops in {seconds}s, {per_op/1e6:.1f} Minstr/op, "
        f"{len(shares)} symbols above 0.15%")
    return shares, per_op


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--many-rows", type=int, default=20000)
    ap.add_argument("--few-rows", type=int, default=2000)
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--real-rows", type=int, default=11686)
    ap.add_argument("--outdir", default="report/evidence/subtree_stream_20260809")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    uri = f"mongodb://localhost:{args.port}/?directConnection=true"
    client = MongoClient(uri)
    client.admin.command({"setParameter": 1,
                          "internalQueryEnableFusedCoveredProjection": True})
    pid = ab.mongod_pid(args.port)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    for name, rows in (("many", args.many_rows), ("few", args.few_rows)):
        build(client, name, rows)
    log(f"collections ready: many={args.many_rows} rows, few={args.few_rows} rows, "
        f"{TOTAL_PAYLOAD:,} payload bytes each")

    shares_m, perop_m = profile_arm(uri, "many", pid, args.seconds, outdir)
    shares_f, perop_f = profile_arm(uri, "few", pid, args.seconds, outdir)

    dr = args.many_rows - args.few_rows
    rows_fixed: dict[str, float] = {}
    for sym in set(shares_m) | set(shares_f):
        cm = shares_m.get(sym, 0.0) * perop_m
        cf = shares_f.get(sym, 0.0) * perop_f
        rows_fixed[sym] = (cm - cf) / dr

    buckets: dict[str, float] = {}
    for sym, f in rows_fixed.items():
        if f <= 0:
            continue
        buckets[classify(sym)] = buckets.get(classify(sym), 0.0) + f
    total_fixed = sum(buckets.values())

    print()
    print(f"{'category':<44} {'instr/row':>10} {'share':>8}")
    for label, v in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print(f"{label:<44} {v:>10,.0f} {v/total_fixed*100:>7.1f}%")
    print(f"{'TOTAL per-row fixed':<44} {total_fixed:>10,.0f}")

    amort = sum(v for k, v in buckets.items() if k.startswith("amortisable"))
    print()
    print(f"amortisable by batching: {amort:,.0f} instr/row "
          f"({amort/total_fixed*100:.0f}% of per-row fixed cost)")
    print(f"  -> on the real {args.real_rows}-row subtree: {amort*args.real_rows/1e6:.1f} "
          f"Minstr of {total_fixed*args.real_rows/1e6:.1f} Minstr per-row fixed")

    print()
    print("top per-row symbols:")
    for sym, f in sorted(rows_fixed.items(), key=lambda kv: -kv[1])[:16]:
        if f <= 0:
            continue
        print(f"  {f:>8,.0f}  [{classify(sym).split(':')[0]:<12}] {sym[:74]}")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"per_row_fixed_by_symbol": rows_fixed, "buckets": buckets,
             "total_fixed": total_fixed, "amortisable": amort,
             "instr_per_op": {"many": perop_m, "few": perop_f}}, indent=2, default=str))
        log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
