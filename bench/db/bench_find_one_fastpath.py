"""Paired A/B for the find_one single-batch fast path.

Both arms run in one process against one pooled connection, so the comparison
is not confounded by which socket the client happened to get -- fresh
connections differ 14-26% in P50 on this box.

  stock   the body of pymongo's find_one as it stands on master, inlined here:
          build a Cursor, iterate it once, let it tear itself down
  fast    Collection.find_one, which on the patched driver takes the
          single-batch path and never builds a Cursor

Inlining the stock body rather than calling an unpatched driver keeps the two
arms in the same interpreter, which is what makes the pairing meaningful. That
the inlined body is faithful is checked against the driver's own source at
startup, so this file cannot silently drift from what it claims to measure.

Three instruments, never mixed: wall (perf_counter), client CPU
(process_time), and retired user-space instructions (perf_event_open).  The
instruction count is the one to read when the box is busy -- three sibling
agents build mongod here and contention inflates cycles without changing the
work.  The CPU column is only meaningful on a quiet box.

Read-only.  No server settings are touched.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pymongo
from perfcount import open_counter

PROJ = {"_id": 1, "text": 1}
COLL = "layout_shared_text"
DB = "bench"


def _cpu_us() -> float:
    return time.process_time() * 1e6


def stock_find_one(coll, filt, projection):
    """pymongo master's find_one body, for a filter that is already a Mapping."""
    cursor = coll.find(filt, projection)
    for result in cursor.limit(-1):
        return result
    return None


def check_stock_body_is_faithful() -> str:
    """Fail loudly if the driver's find_one no longer matches the inlined arm.

    The stock arm is only a valid control while it is the same code the driver
    would have run.  Compare against find_one's source and report what was
    found either way, rather than trusting a comment.
    """
    src = inspect.getsource(pymongo.synchronous.collection.Collection.find_one)
    body = src.split('"""')[-1]
    needed = ["cursor = self.find(filter, *args, **kwargs)",
              "for result in cursor.limit(-1):"]
    missing = [n for n in needed if n not in body]
    if missing:
        raise SystemExit(
            "find_one no longer contains the cursor path this harness inlines "
            f"as its control; missing: {missing}"
        )
    fast = "_find_one_single_batch" in body
    return ("patched: find_one has a single-batch fast path and keeps the "
            "cursor path as its fallback"
            if fast else
            "stock: find_one has only the cursor path, so both arms are the same code")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--entity-id", default=None)
    ap.add_argument("--blocks", type=int, default=14)
    ap.add_argument("--iters", type=int, default=500)
    ap.add_argument("--warm", type=int, default=500)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    print(check_stock_body_is_faithful())

    client = pymongo.MongoClient(args.mongo_uri)
    coll = client[DB][COLL]
    entity_id = args.entity_id or coll.find_one({}, {"_id": 1})["_id"]
    q = {"_id": entity_id}

    arms = {
        "stock": lambda: stock_find_one(coll, q, PROJ),
        "fast": lambda: coll.find_one(q, PROJ),
    }
    names = list(arms)
    counter = open_counter()
    print("instrument: retired user instructions"
          if counter else "instrument: CPU time only (no perf counter)")

    expected = arms["stock"]()
    if expected is None:
        raise SystemExit(f"no document with _id={entity_id!r}")
    for name, fn in arms.items():
        if fn() != expected:
            raise SystemExit(f"arm {name} returned different data")
    print(f"correctness ok: both arms return the same document "
          f"({len(expected['text'])} B of text)")

    def timed(fn, count):
        i0 = counter.read() if counter else 0
        c0, t0 = _cpu_us(), time.perf_counter()
        for _ in range(count):
            r = fn()
        wall = (time.perf_counter() - t0) / count * 1e6
        cpu = (_cpu_us() - c0) / count
        ins = ((counter.read() - i0) / count) if counter else 0.0
        return wall, cpu, ins, r

    for fn in arms.values():
        timed(fn, args.warm)

    wall = {n: [] for n in names}
    cpu = {n: [] for n in names}
    instr = {n: [] for n in names}
    for b in range(args.blocks):
        for name in (names if b % 2 == 0 else names[::-1]):
            w, c, i, r = timed(arms[name], args.iters)
            if r != expected:
                raise SystemExit(f"arm {name} drifted from expected output")
            wall[name].append(w)
            cpu[name].append(c)
            instr[name].append(i)
        print(f"  block {b + 1:2d}: stock {instr['stock'][-1]:7.0f} instr  "
              f"fast {instr['fast'][-1]:7.0f}  "
              f"saved {instr['stock'][-1] - instr['fast'][-1]:7.0f}  "
              f"({wall['stock'][-1]:6.1f} vs {wall['fast'][-1]:6.1f} us wall)")
    if counter:
        counter.close()

    def summarize(metric, samples):
        s, f = samples["stock"], samples["fast"]
        deltas = [s[i] - f[i] for i in range(len(s))]
        return {
            "stock_p50": st.median(s), "fast_p50": st.median(f),
            "paired_saved_median": st.median(deltas),
            "paired_saved_min": min(deltas), "paired_saved_max": max(deltas),
            "blocks_improved": sum(1 for d in deltas if d > 0),
            "blocks": len(deltas),
            "pct_of_stock": st.median(deltas) / st.median(s) * 100,
            "stock_samples": s, "fast_samples": f,
        }

    result = {
        "blocks": args.blocks, "iters_per_block": args.iters,
        "pymongo_version": pymongo.version, "entity_id": entity_id,
        "driver_state": check_stock_body_is_faithful(),
        "shape": {"collection": COLL, "filter": q, "projection": PROJ},
        "instr": summarize("instr", instr),
        "client_cpu_us": summarize("cpu", cpu),
        "wall_us": summarize("wall", wall),
    }

    print()
    for label, key in (("retired instructions", "instr"),
                       ("client CPU us", "client_cpu_us"),
                       ("wall us", "wall_us")):
        m = result[key]
        print(f"{label:<22} stock {m['stock_p50']:9.1f}   fast {m['fast_p50']:9.1f}   "
              f"saved {m['paired_saved_median']:9.1f} "
              f"({m['pct_of_stock']:5.1f}%)  "
              f"[{m['paired_saved_min']:.1f}, {m['paired_saved_max']:.1f}]  "
              f"{m['blocks_improved']}/{m['blocks']} blocks")

    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
