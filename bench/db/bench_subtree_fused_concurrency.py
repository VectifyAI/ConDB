#!/usr/bin/env python3
"""The tail subtree under concurrent load, fused against base.

Every other measurement here is single-client latency. The prior work on this operation notes that
no input above 30,000 rows was ever run concurrently, so the largest reads -- which carry most of
the cohort's time -- have never been measured under load. This does that: the 1,404,566-row subtree,
replayed by N clients at once.

What changes under load is which resource runs out first. A change that removes server CPU should
show up as throughput when CPU is the constraint and as nothing when it is not, so throughput is the
headline here and per-operation latency is secondary.

Clients stream their cursor and fold each row into a checksum rather than materialising 1.4M tuples
per client; the server-side work and the driver's BSON decode are unchanged by that, only the
client's retention is. Row count and checksum are compared against a reference captured outside the
timed window, so a silent divergence under concurrency still fails.

Same pairing discipline as the other harnesses: arms alternate within blocks, leading arm rotates,
deltas are per block.

Usage:
    bench_subtree_fused_concurrency.py --path /000000/000007 --clients 4 --blocks 6 --seconds 12
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import zlib
from pathlib import Path
from typing import Any

from pymongo import MongoClient

import bench_subtree_fused_ab as ab

DB = "bench"
NODES = "layout2_view"
COVER_INDEX = "layout2_rootcause_exact_cover"


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def scan(coll: Any, lower: str, upper: str) -> tuple[int, int]:
    """Consume the subtree, returning (row count, checksum over the projected fields)."""
    cur = (coll.find({"path": {"$gte": lower, "$lt": upper}},
                     {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
           .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX))
    n = 0
    crc = 0
    for d in cur:
        crc = zlib.crc32(
            f"{d.get('node_id')}\x1f{d.get('title')}\x1f{d.get('summary')}".encode(), crc)
        n += 1
    return n, crc


class Client(threading.Thread):
    def __init__(self, uri: str, lower: str, upper: str, stop: threading.Event,
                 start: threading.Event) -> None:
        super().__init__(daemon=True)
        self.uri, self.lower, self.upper = uri, lower, upper
        self.stop, self.start_gate = stop, start
        self.ops = 0
        self.results: set[tuple[int, int]] = set()
        self.error: str | None = None

    def run(self) -> None:
        try:
            client = MongoClient(self.uri)
            coll = client[DB][NODES]
            scan(coll, self.lower, self.upper)  # warm this connection
            self.start_gate.wait()
            while not self.stop.is_set():
                self.results.add(scan(coll, self.lower, self.upper))
                self.ops += 1
            client.close()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            self.error = repr(exc)


def measure(uri: str, lower: str, upper: str, pid: int, clients: int,
            seconds: float) -> dict[str, Any]:
    stop, start = threading.Event(), threading.Event()
    threads = [Client(uri, lower, upper, stop, start) for _ in range(clients)]
    for t in threads:
        t.start()
    time.sleep(1.5)  # let every client warm before the window opens

    cpu0 = ab.proc_cpu_us(pid)
    t0 = time.perf_counter()
    start.set()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join(timeout=300)
    elapsed = time.perf_counter() - t0
    cpu = ab.proc_cpu_us(pid) - cpu0

    ops = sum(t.ops for t in threads)
    errors = [t.error for t in threads if t.error]
    seen: set[tuple[int, int]] = set()
    for t in threads:
        seen |= t.results
    return {
        "ops": ops,
        "elapsed_s": elapsed,
        "throughput_ops_s": ops / elapsed,
        "server_cpu_us_per_op": cpu / max(ops, 1),
        "server_cpu_cores": cpu / 1e6 / elapsed,
        "distinct_results": sorted(seen),
        "errors": errors,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--path", default="/000000/000007")
    ap.add_argument("--clients", type=int, nargs="+", default=[4])
    ap.add_argument("--blocks", type=int, default=6)
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    uri = f"mongodb://localhost:{args.port}/?directConnection=true&maxPoolSize=64"
    admin = MongoClient(uri)
    coll = admin[DB][NODES]
    pid = ab.mongod_pid(args.port)
    lower, upper = args.path + "/", args.path + "0"

    ab.set_arm(admin, False)
    reference = scan(coll, lower, upper)
    log(f"{args.path}: reference {reference[0]} rows, crc {reference[1]:#x}")

    results: dict[str, Any] = {"path": args.path, "reference_rows": reference[0],
                               "by_clients": {}}

    for nclients in args.clients:
        log(f"--- {nclients} concurrent clients, {args.blocks} blocks x {args.seconds}s ---")
        blocks: list[dict[str, Any]] = []
        bad = 0
        for b in range(args.blocks):
            order = [False, True] if b % 2 == 0 else [True, False]
            block: dict[str, Any] = {}
            for enabled in order:
                arm = "fused" if enabled else "base"
                ab.set_arm(admin, enabled)
                m = measure(uri, lower, upper, pid, nclients, args.seconds)
                if m["errors"] or m["distinct_results"] != [list(reference)] and \
                        m["distinct_results"] != [reference]:
                    bad += 1
                    log(f"    MISMATCH/ERROR in {arm}: results={m['distinct_results']} "
                        f"errors={m['errors']}")
                block[arm] = m
            block["throughput_delta_pct"] = (
                block["fused"]["throughput_ops_s"] / block["base"]["throughput_ops_s"] - 1) * 100
            block["cpu_per_op_delta_pct"] = (
                block["fused"]["server_cpu_us_per_op"]
                / block["base"]["server_cpu_us_per_op"] - 1) * 100
            blocks.append(block)
            log(f"  block {b}: throughput {block['throughput_delta_pct']:+.2f}%  "
                f"server CPU/op {block['cpu_per_op_delta_pct']:+.2f}%  "
                f"(ops {block['base']['ops']}/{block['fused']['ops']}, "
                f"cores {block['base']['server_cpu_cores']:.1f}/"
                f"{block['fused']['server_cpu_cores']:.1f})")

        tp = [b["throughput_delta_pct"] for b in blocks]
        cp = [b["cpu_per_op_delta_pct"] for b in blocks]
        entry = {
            "blocks": blocks,
            "bad_blocks": bad,
            "paired_throughput_delta_pct": {
                "median": statistics.median(tp), "min": min(tp), "max": max(tp),
                "blocks_improved": sum(1 for d in tp if d > 0), "blocks": len(tp)},
            "paired_cpu_per_op_delta_pct": {
                "median": statistics.median(cp), "min": min(cp), "max": max(cp),
                "blocks_improved": sum(1 for d in cp if d < 0), "blocks": len(cp)},
        }
        results["by_clients"][str(nclients)] = entry
        t, c = entry["paired_throughput_delta_pct"], entry["paired_cpu_per_op_delta_pct"]
        log(f"{nclients} clients: PAIRED throughput {t['median']:+.2f}% "
            f"[{t['min']:+.2f}, {t['max']:+.2f}] {t['blocks_improved']}/{t['blocks']} up; "
            f"server CPU/op {c['median']:+.2f}% {c['blocks_improved']}/{c['blocks']} down; "
            f"bad blocks {bad}")

    ab.set_arm(admin, False)
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
