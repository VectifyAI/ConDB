#!/usr/bin/env python3
"""L3 ceiling probe: what would non-key payload columns (PostgreSQL's INCLUDE) be worth?

MongoDB has no way to store a covering field in an index without order-encoding it into the key.
For this workload that means `title` (~53 B) and `summary` (~340 B) are escaped into the KeyString
and must be decoded back out on every row. PostgreSQL's INCLUDE stores such columns uninterpreted
and returns them with a length-prefixed copy.

Rather than implement a storage-format feature to find out what it is worth, bound it. Two covered,
fused scans over the same path range and the same rows:

  wide    (path, node_id, title, summary), projecting {node_id, title, summary}
  narrow  (path, node_id),                 projecting {node_id}

Both are covered and both fuse, so the plan shape and the stage path are identical; the only
difference is that the wide scan carries ~393 bytes of payload through the key and the narrow one
does not. The per-row server-CPU difference is therefore an **upper bound** on what INCLUDE could
save, because:

  * it contains the whole cost of the payload -- order-encoded decode AND the copy into the output
    object -- while INCLUDE would still have to do the copy;
  * the narrow index is 0.28 GB against the wide index's 4.66 GB, so the narrow scan also walks a
    denser B-tree with fewer pages, which flatters it further.

Both directions of that bias inflate the bound, which is the right way round for a ceiling.

Usage:
    bench_subtree_l3_ceiling.py --path /000006/000075/000773 --blocks 10 --seconds 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient

import bench_subtree_fused_ab as ab

DB = "bench"
NODES = "layout2_view"
WIDE_INDEX = "layout2_rootcause_exact_cover"      # (path, node_id, title, summary)
NARROW_INDEX = "path_1_node_id_1"                 # (path, node_id)


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def scan_wide(coll: Any, lower: str, upper: str) -> int:
    cur = (coll.find({"path": {"$gte": lower, "$lt": upper}},
                     {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
           .sort([("path", 1), ("node_id", 1)]).hint(WIDE_INDEX))
    return sum(1 for _ in cur)


def scan_narrow(coll: Any, lower: str, upper: str) -> int:
    cur = (coll.find({"path": {"$gte": lower, "$lt": upper}}, {"_id": 0, "node_id": 1})
           .sort([("path", 1), ("node_id", 1)]).hint(NARROW_INDEX))
    return sum(1 for _ in cur)


def window(coll: Any, fn: Any, lower: str, upper: str, pid: int, seconds: float,
           tag: str) -> dict[str, Any]:
    handle = ab.start_perf(pid, seconds, tag)
    time.sleep(0.20)
    ops = 0
    rows = 0
    cpu0 = ab.proc_cpu_us(pid)
    t0 = time.perf_counter()
    while True:
        elapsed = time.perf_counter() - t0
        if ops and elapsed + (elapsed / ops) > seconds:
            break
        if elapsed >= seconds:
            break
        rows = fn(coll, lower, upper)
        ops += 1
    wall = (time.perf_counter() - t0) * 1e6
    cpu = ab.proc_cpu_us(pid) - cpu0
    instr = ab.read_perf(handle)
    return {"ops": ops, "rows": rows,
            "wall_us": wall / max(ops, 1), "cpu_us": cpu / max(ops, 1),
            "instructions": (instr / ops) if (instr is not None and ops) else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--path", default="/000006/000075/000773")
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    uri = f"mongodb://localhost:{args.port}/?directConnection=true"
    client = MongoClient(uri)
    coll = client[DB][NODES]
    pid = ab.mongod_pid(args.port)
    lower, upper = args.path + "/", args.path + "0"

    # Both arms fused; this probe is about payload width, not about the fold.
    ab.set_arm(client, True)
    nw, nn = scan_wide(coll, lower, upper), scan_narrow(coll, lower, upper)
    log(f"wide {nw} rows, narrow {nn} rows")
    if nw != nn:
        raise SystemExit("the two indexes do not cover the same rows; probe is invalid")

    blocks = []
    for b in range(args.blocks):
        order = [("wide", scan_wide), ("narrow", scan_narrow)]
        if b % 2:
            order.reverse()
        block = {}
        for name, fn in order:
            block[name] = window(coll, fn, lower, upper, pid, args.seconds, f"{b}{name}")
        for key, label in (("cpu_us", "cpu"), ("instructions", "instr"), ("wall_us", "wall")):
            if block["wide"][key] and block["narrow"][key]:
                block[f"{label}_payload_share_pct"] = (
                    1 - block["narrow"][key] / block["wide"][key]) * 100
        blocks.append(block)
        log(f"  block {b}: payload share of server CPU "
            f"{block.get('cpu_payload_share_pct', float('nan')):.2f}%  "
            f"of instructions {block.get('instr_payload_share_pct', float('nan')):.2f}%")

    out: dict[str, Any] = {"path": args.path, "rows": nw, "blocks": blocks}
    for lbl in ("cpu", "instr", "wall"):
        vals = [b[f"{lbl}_payload_share_pct"] for b in blocks if f"{lbl}_payload_share_pct" in b]
        if vals:
            out[f"{lbl}_payload_share_pct"] = {
                "median": statistics.median(vals), "min": min(vals), "max": max(vals)}
            log(f"payload share of {lbl}: median {statistics.median(vals):.2f}% "
                f"[{min(vals):.2f}, {max(vals):.2f}]")

    ab.set_arm(client, False)
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2))
        log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
