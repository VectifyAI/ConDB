#!/usr/bin/env python3
"""Cross-check the express prefix-scan result in retired instructions.

Server CPU already puts this at about -35% with a control floor near 1%, so this is
confirmation on a second instrument rather than the primary measurement.  It is here
because instruction counts are reproducible across machine load in a way schedstat is
not, so the number survives being read on a different day on a busier box.

The gate is read at process start, so the two arms are two mongods from the same binary
over byte-identical dbpath copies.  A third ungated server is the floor.

Instructions are not time.  This delta is not converted to a latency delta.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from pymongo import MongoClient

DB_NAME, NODES, TREE_ID = "bench", "layout2_view", "base"
CHILD_INDEX = "allops_tree_parent_path"
PROJ = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}
SORT = [("path", 1), ("node_id", 1)]
GATE = "MONGO_EXPRESS_PREFIX_SCAN"


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def start(binary: Path, dbpath: Path, logpath: Path, port: int, gate: str | None):
    env = dict(os.environ)
    env.pop(GATE, None)
    if gate is not None:
        env[GATE] = gate
    proc = subprocess.Popen(
        [str(binary), "--port", str(port), "--dbpath", str(dbpath), "--bind_ip", "127.0.0.1",
         "--wiredTigerCacheSizeGB", "8", "--logpath", str(logpath),
         "--setParameter", "diagnosticDataCollectionEnabled=false"],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env)
    uri = f"mongodb://localhost:{port}/?directConnection=true"
    for _ in range(240):
        try:
            MongoClient(uri, serverSelectionTimeoutMS=500).admin.command("ping")
            log(f"  mongod up on {port} ({GATE}={gate})")
            return proc, MongoClient(uri, maxPoolSize=1, minPoolSize=1)[DB_NAME]
        except Exception:
            if proc.poll() is not None:
                raise SystemExit(f"mongod on {port} exited early; see {logpath}")
            time.sleep(0.5)
    raise SystemExit(f"mongod on {port} did not start")


def find_tid(pid: int, comm: str) -> int:
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            with open(f"/proc/{pid}/task/{tid}/comm") as h:
                if h.read().strip() == comm:
                    return int(tid)
        except OSError:
            continue
    raise SystemExit(f"no thread {comm} in {pid}")


def reader_for(db):
    nodes = db[NODES]

    def read(parent):
        return [(d.get("node_id"), d.get("title"), d.get("summary"))
                for d in nodes.find({"tree_id": TREE_ID, "parent_id": parent},
                                    PROJ).sort(SORT).hint(CHILD_INDEX)]
    return read


def instructions_per_op(tid: int, read, parents, seconds: float) -> float:
    with tempfile.NamedTemporaryFile("r", suffix=".perf", delete=False) as tf:
        out = tf.name
    perf = subprocess.Popen(
        ["perf", "stat", "-e", "instructions", "--tid", str(tid), "-x", ",", "-o", out,
         "--", "sleep", str(seconds)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0, ops, i = time.perf_counter(), 0, 0
    while time.perf_counter() - t0 < seconds:
        read(parents[i % len(parents)])
        i += 1
        ops += 1
    perf.wait(timeout=60)
    insn = None
    for line in Path(out).read_text().splitlines():
        parts = line.split(",")
        if len(parts) >= 3 and "instructions" in parts[2]:
            try:
                insn = float(parts[0])
            except ValueError:
                pass
    os.unlink(out)
    if insn is None:
        raise SystemExit("perf produced no instruction count")
    return insn / ops


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--scratch", default="/tmp/mongo-getchildren-full")
    ap.add_argument("--cohort-json", required=True)
    ap.add_argument("--blocks", type=int, default=9)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    parents = json.loads(Path(args.cohort_json).read_text())["parents"]
    scratch = Path(args.scratch)
    arms_spec = {"express": (57071, "1"), "baseline": (57072, None), "control": (57073, None)}

    procs, arms = {}, {}
    out: dict = {"unit": "retired instructions per operation on the arm's own connection thread; "
                         "instructions are not time",
                 "run": {"blocks": args.blocks, "seconds_per_arm_per_block": args.seconds}}
    try:
        for name, (port, gate) in arms_spec.items():
            dbp = scratch / f"dbpath_i{port}"
            if not dbp.exists():
                subprocess.run(["cp", "-a", str(scratch / "dbpath_base"), str(dbp)], check=True)
            proc, db = start(Path(args.binary).resolve(), dbp,
                             Path(args.out).parent / f"insn_{name}.log", port, gate)
            procs[name] = proc
            comm = f"conn{db.command('hello')['connectionId']}"
            arms[name] = {"db": db, "read": reader_for(db), "tid": find_tid(proc.pid, comm)}

        log("verifying element-wise equality before timing")
        elems = 0
        for p in parents:
            exp = arms["baseline"]["read"](p)
            for n in ("express", "control"):
                if arms[n]["read"](p) != exp:
                    raise SystemExit(f"OUTPUT MISMATCH arm {n} parent {p!r}")
            elems += len(exp)
        log(f"  {elems} rows compared, all equal")

        for n in arms:
            instructions_per_op(arms[n]["tid"], arms[n]["read"], parents, 1.0)

        blocks = []
        order = list(arms_spec)
        for b in range(args.blocks):
            rot = order[b % 3:] + order[:b % 3]
            row = {n: instructions_per_op(arms[n]["tid"], arms[n]["read"], parents, args.seconds)
                   for n in rot}
            row["express_vs_baseline_pct"] = 100.0 * (row["express"] - row["baseline"]) / row["baseline"]
            row["control_vs_baseline_pct"] = 100.0 * (row["control"] - row["baseline"]) / row["baseline"]
            blocks.append(row)
            log("  block %d  baseline %9.0f  express %9.0f (%+6.2f%%)  control %9.0f (%+6.2f%%)"
                % (b, row["baseline"], row["express"], row["express_vs_baseline_pct"],
                   row["control"], row["control_vs_baseline_pct"]))

        out["blocks"] = blocks
        ev = [x["express_vs_baseline_pct"] for x in blocks]
        cv = [x["control_vs_baseline_pct"] for x in blocks]
        out["summary"] = {
            "express_vs_baseline": {"median_pct": statistics.median(ev),
                                    "min_pct": min(ev), "max_pct": max(ev)},
            "control_vs_baseline": {"median_pct": statistics.median(cv),
                                    "min_pct": min(cv), "max_pct": max(cv)},
            "median_insn_per_op": {n: statistics.median([x[n] for x in blocks]) for n in order},
        }
        log("")
        log("express vs baseline  median %+.2f%%  spread [%+.2f, %+.2f]"
            % (out["summary"]["express_vs_baseline"]["median_pct"],
               out["summary"]["express_vs_baseline"]["min_pct"],
               out["summary"]["express_vs_baseline"]["max_pct"]))
        log("control vs baseline  median %+.2f%%  spread [%+.2f, %+.2f]   (floor)"
            % (out["summary"]["control_vs_baseline"]["median_pct"],
               out["summary"]["control_vs_baseline"]["min_pct"],
               out["summary"]["control_vs_baseline"]["max_pct"]))
        log(f"median insn/op: {out['summary']['median_insn_per_op']}")
        out["run"]["status"] = "complete"
    finally:
        for p in procs.values():
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=180)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))
        log(f"written to {args.out}")


if __name__ == "__main__":
    main()
