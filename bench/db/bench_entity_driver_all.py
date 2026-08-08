"""What the whole driver lane is worth on get_entity, measured in one process.

Each of the three changes was measured on its own; this measures them together,
which is the number that answers "what did this lane deliver". Doing it across
two processes would not be paired, and unpaired arms have already turned a -14%
into a +0.5% once in this project, so all three are toggled in place instead:

  C2  serve find_one without a cursor    -> control runs find_one's upstream
                                            body, inlined here
  C1a reuse the last server selection    -> control clears the memo before each
                                            call, forcing the miss
  C1b take the pool mutex once           -> control replaces Pool._checkout_idle
                                            with one that always declines

The toggles are applied in BOTH arms, so the cost of toggling cancels in the
paired delta. The control arm is checked against the driver's own source at
startup, and every block compares the two arms' documents, so an arm that is
faster because it did something else cannot pass.

Requires a driver that carries all three changes. Refuses to run otherwise
rather than silently reporting a smaller number.

Read-only. No server settings are touched.
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


def upstream_find_one(coll, filt, projection):
    """pymongo's find_one body, for a filter that is already a Mapping."""
    cursor = coll.find(filt, projection)
    for result in cursor.limit(-1):
        return result
    return None


def check_driver_carries_all_three() -> dict[str, bool]:
    """Which of the three changes this driver has, checked against its source."""
    from pymongo.synchronous.collection import Collection
    from pymongo.synchronous.pool import Pool
    from pymongo.synchronous.topology import Topology

    body = inspect.getsource(Collection.find_one)
    if "cursor = self.find(filter, *args, **kwargs)" not in body:
        raise SystemExit(
            "find_one no longer contains the cursor path this harness inlines as "
            "its control arm"
        )
    return {
        "C2 find_one single batch": "_find_one_single_batch" in body,
        "C1a server selection memo": "_selected_server"
        in inspect.getsource(Topology._select_server),
        "C1b pool mutex taken once": hasattr(Pool, "_checkout_idle"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--entity-id", default=None)
    ap.add_argument("--blocks", type=int, default=14)
    ap.add_argument("--iters", type=int, default=500)
    ap.add_argument("--warm", type=int, default=500)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    present = check_driver_carries_all_three()
    for name, ok in present.items():
        print(f"  {'present' if ok else 'MISSING'}  {name}")
    if not all(present.values()):
        raise SystemExit(
            "this driver does not carry all three changes; measuring it would "
            "report a smaller number than the lane delivered"
        )

    from pymongo.synchronous.pool import Pool

    client = pymongo.MongoClient(args.mongo_uri)
    coll = client[DB][COLL]
    entity_id = args.entity_id or coll.find_one({}, {"_id": 1})["_id"]
    q = {"_id": entity_id}
    topology = client._topology

    real_checkout_idle = Pool._checkout_idle

    def declined(self):  # noqa: ARG001
        return None

    def arm_off():
        Pool._checkout_idle = declined
        topology._selected_server = None
        return upstream_find_one(coll, q, PROJ)

    def arm_on():
        Pool._checkout_idle = real_checkout_idle
        return coll.find_one(q, PROJ)

    arms = {"off": arm_off, "on": arm_on}
    names = list(arms)
    counter = open_counter()
    print(
        "instrument: retired user instructions"
        if counter
        else "instrument: CPU time only (no perf counter)"
    )

    expected = arm_off()
    if expected is None:
        raise SystemExit(f"no document with _id={entity_id!r}")
    if arm_on() != expected:
        raise SystemExit("the two arms return different documents")
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
        print(f"  block {b + 1:2d}: off {instr['off'][-1]:7.0f} instr  "
              f"on {instr['on'][-1]:7.0f}  "
              f"saved {instr['off'][-1] - instr['on'][-1]:7.0f}  "
              f"({wall['off'][-1]:6.1f} vs {wall['on'][-1]:6.1f} us wall)")
    Pool._checkout_idle = real_checkout_idle
    if counter:
        counter.close()

    def summarize(samples):
        off, on = samples["off"], samples["on"]
        deltas = [off[i] - on[i] for i in range(len(off))]
        return {
            "off_p50": st.median(off), "on_p50": st.median(on),
            "paired_saved_median": st.median(deltas),
            "paired_saved_min": min(deltas), "paired_saved_max": max(deltas),
            "blocks_improved": sum(1 for d in deltas if d > 0),
            "blocks": len(deltas),
            "pct_of_off": st.median(deltas) / st.median(off) * 100,
            "off_samples": off, "on_samples": on,
        }

    result = {
        "blocks": args.blocks, "iters_per_block": args.iters,
        "pymongo_version": pymongo.version, "entity_id": entity_id,
        "changes_present": present,
        "shape": {"collection": COLL, "filter": q, "projection": PROJ},
        "instr": summarize(instr),
        "client_cpu_us": summarize(cpu),
        "wall_us": summarize(wall),
    }

    print()
    for label, key in (("retired instructions", "instr"),
                       ("client CPU us", "client_cpu_us"),
                       ("wall us", "wall_us")):
        m = result[key]
        print(f"{label:<22} off {m['off_p50']:9.1f}   on {m['on_p50']:9.1f}   "
              f"saved {m['paired_saved_median']:9.1f} "
              f"({m['pct_of_off']:5.1f}%)  "
              f"[{m['paired_saved_min']:.1f}, {m['paired_saved_max']:.1f}]  "
              f"{m['blocks_improved']}/{m['blocks']} blocks")

    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
