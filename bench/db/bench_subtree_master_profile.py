#!/usr/bin/env python3
"""Profile get_subtree inside a locally-built master mongod.

The 36.6% figure this project quotes for key materialisation plus covered
projection comes from a 7.0.34 profile.  Master has since grown a zero-copy key
API (`SortedDataKeyValueView`, used by SBE and express but not by classic
`IndexScan`), so the denominator has to be re-measured on master before anything
is built against it.

The local mongod runs as this account's own uid, so perf resolves user-space
frames; the container's 7.0.34 runs as uid 999 and every user frame there comes
back `[unknown]`.

Load generator and profiler run concurrently: driver threads replay the exact
`get_subtree` shape from bench_all_ops_layouts.py while `perf record` samples the
mongod process.  Reported percentages are **self (exclusive) time as a share of
mongod's sampled cycles**, so they may be summed.  Inclusive figures, where shown,
are labelled and must not be added to each other.

Usage:
    bench_subtree_master_profile.py --path /000006/000075/000773 --seconds 30
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient

DB = "bench"
NODES = "layout2_view"
COVER_INDEX = "layout2_rootcause_exact_cover"
NODE_INDEX = "allops_tree_node"
TREE_ID = "base"


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def mongod_pid(port: int) -> int:
    """The mongod serving `port`, owned by this uid (perf can only read our own maps)."""
    import os
    uid = os.getuid()
    cand = subprocess.run(["pgrep", "-u", str(uid), "-f", f"mongod.*port[= ]{port}"],
                          capture_output=True, text=True).stdout.split()
    # A shell that launched mongod also matches on its command line; keep only real mongods.
    out = []
    for p in cand:
        try:
            with open(f"/proc/{p}/comm") as fh:
                if fh.read().strip() == "mongod":
                    out.append(p)
        except OSError:
            continue
    if not out:
        raise SystemExit(f"no mongod owned by uid {uid} found for port {port}")
    if len(out) > 1:
        raise SystemExit(f"ambiguous: {len(out)} mongod processes match port {port}: {out}")
    return int(out[0])


class Driver(threading.Thread):
    """Replays get_subtree against the target until told to stop."""

    def __init__(self, uri: str, path: str, use_root_lookup: bool) -> None:
        super().__init__(daemon=True)
        self.uri, self.path, self.use_root_lookup = uri, path, use_root_lookup
        self.stop = threading.Event()
        self.ops = 0
        self.rows = 0
        self.wall_us: list[float] = []

    def run(self) -> None:
        client = MongoClient(self.uri)
        nodes = client[DB][NODES]
        lower, upper = self.path + "/", self.path + "0"
        while not self.stop.is_set():
            t0 = time.perf_counter()
            if self.use_root_lookup:
                node_id = self.path.rsplit("/", 1)[-1]
                nodes.find_one({"tree_id": TREE_ID, "node_id": node_id},
                               {"_id": 0, "path": 1}, hint=NODE_INDEX)
            cur = (nodes.find({"path": {"$gte": lower, "$lt": upper}},
                              {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
                   .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX))
            n = 0
            for _ in cur:
                n += 1
            self.wall_us.append((time.perf_counter() - t0) * 1e6)
            self.ops += 1
            self.rows = n
        client.close()


def proc_cpu_us(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as fh:
        parts = fh.read().rsplit(") ", 1)[1].split()
    clk = 100.0  # CLK_TCK
    return (int(parts[11]) + int(parts[12])) / clk * 1e6


def parse_perf_report(perf_data: Path, mode: str) -> list[tuple[float, str]]:
    """Return [(percent, symbol)] from perf report, self time unless mode=inclusive."""
    flag = "--children" if mode == "inclusive" else "--no-children"
    out = subprocess.run(
        ["perf", "report", "-i", str(perf_data), flag, "--stdio", "--percent-limit", "0.3",
         "-s", "symbol", "--comms", "mongod"],
        capture_output=True, text=True).stdout
    rows: list[tuple[float, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\d+\.\d+)%\s+(?:(\d+\.\d+)%\s+)?(.*)$", line)
        if not m:
            continue
        pct = float(m.group(2) if (mode != "inclusive" and m.group(2)) else m.group(1))
        sym = m.group(3).strip()
        sym = re.sub(r"^\[[^\]]*\]\s*", "", sym)
        rows.append((pct, sym))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--path", default="/000006/000075/000773", help="subtree root path")
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=8)
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--freq", type=int, default=999)
    ap.add_argument("--callgraph", action="store_true",
                    help="record dwarf call graphs too; adds overhead, only needed for inclusive")
    ap.add_argument("--no-root-lookup", action="store_true",
                    help="profile the range scan alone, without the root find_one")
    ap.add_argument("--outdir", default="report/evidence/subtree_stream_20260809")
    ap.add_argument("--tag", default="base")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    uri = f"mongodb://localhost:{args.port}/?directConnection=true"
    pid = mongod_pid(args.port)
    log(f"mongod pid {pid} on port {args.port}")

    drivers = [Driver(uri, args.path, not args.no_root_lookup) for _ in range(args.threads)]
    for d in drivers:
        d.start()
    log(f"warming up {args.warmup}s")
    t_end = time.time() + args.warmup
    while time.time() < t_end:
        time.sleep(0.5)

    perf_data = outdir / f"perf_{args.tag}.data"
    cpu0 = proc_cpu_us(pid)
    ops0 = sum(d.ops for d in drivers)
    log(f"recording {args.seconds}s at {args.freq} Hz")
    # Self-time attribution needs no call graph, and dwarf unwinding on an opt build is
    # expensive enough to distort what it measures.  --callgraph is opt-in.
    cmd = ["perf", "record", "-F", str(args.freq)]
    if args.callgraph:
        cmd += ["-g", "--call-graph", "dwarf,4096"]
    cmd += ["-p", str(pid), "-o", str(perf_data), "--", "sleep", str(args.seconds)]
    perf = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    perf.wait()
    cpu1 = proc_cpu_us(pid)
    ops1 = sum(d.ops for d in drivers)

    for d in drivers:
        d.stop.set()
    for d in drivers:
        d.join(timeout=60)

    ops = ops1 - ops0
    server_cpu_us = cpu1 - cpu0
    rows = drivers[0].rows
    log(f"{ops} operations in {args.seconds}s, {rows} rows each")
    log(f"server CPU {server_cpu_us:,.0f} us total = {server_cpu_us / max(ops,1):,.0f} us/op")

    self_rows = parse_perf_report(perf_data, "self")
    incl_rows = parse_perf_report(perf_data, "inclusive")

    result: dict[str, Any] = {
        "tag": args.tag,
        "path": args.path,
        "rows": rows,
        "seconds": args.seconds,
        "threads": args.threads,
        "operations": ops,
        "server_cpu_us_total": server_cpu_us,
        "server_cpu_us_per_op": server_cpu_us / max(ops, 1),
        "client_wall_us_per_op": (sum(sum(d.wall_us) for d in drivers)
                                  / max(sum(len(d.wall_us) for d in drivers), 1)),
        "self_percent": [{"pct": p, "symbol": s} for p, s in self_rows[:60]],
        "inclusive_percent": [{"pct": p, "symbol": s} for p, s in incl_rows[:60]],
    }
    out = outdir / f"profile_{args.tag}.json"
    out.write_text(json.dumps(result, indent=2))

    print("\n--- self (exclusive) time, share of sampled mongod cycles; summable ---")
    for p, s in self_rows[:30]:
        print(f"  {p:6.2f}%  {s}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
