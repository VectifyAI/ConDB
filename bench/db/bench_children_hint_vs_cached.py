#!/usr/bin/env python3
"""Is a hinted baseline the right reference for `get_children`?  Measured in instructions.

The get_node agent's warning: a hinted control arm re-plans on every call, because a
hint disqualifies plan caching.  Measuring a fast path against it therefore measures
"fast path vs UNCACHED planning", which overstates production gain for a workload whose
queries would otherwise hit the cache.

For `get_children` the answer is that the hinted arm *is* production -- the operation is
specified with `.hint("allops_tree_parent_path")` and the plan-cache counters confirm
0 hits / 53,056 skipped.  But that only holds while the workload keeps its hint, so this
prices the alternative directly:

    hinted     find(...).hint(CHILD_INDEX)   -> shouldCacheQuery false, replans every call
    unhinted   find(...)                     -> cacheable, hits the plan cache once warm
    control    a second hinted connection    -> the floor

Unit is **retired instructions per operation**, per the get_node agent's finding that wall
and CPU cannot resolve changes under ~5% on this path while instruction counts hold to
about 1%.  Instructions are counted on each arm's own connection thread only.

Instructions are not time.  A instruction-count delta does not convert to a latency delta
and is not reported as one.
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
from typing import Any

from pymongo import MongoClient

DB_NAME = "bench"
NODES = "layout2_view"
TREE_ID = "base"
CHILD_INDEX = "allops_tree_parent_path"
CHILD_PROJECTION = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}
CHILD_SORT = [("path", 1), ("node_id", 1)]
ARM_NAMES = ("hinted", "unhinted", "control")


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def start_mongod(binary: Path, dbpath: Path, logpath: Path, port: int, cache_gb: int):
    env = dict(os.environ)
    env.pop("MONGO_PROBE_HINTED_PLAN_MEMO", None)
    proc = subprocess.Popen(
        [str(binary), "--port", str(port), "--dbpath", str(dbpath), "--bind_ip", "127.0.0.1",
         "--wiredTigerCacheSizeGB", str(cache_gb), "--logpath", str(logpath),
         "--setParameter", "diagnosticDataCollectionEnabled=false"],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env)
    uri = f"mongodb://localhost:{port}/?directConnection=true"
    for _ in range(240):
        try:
            MongoClient(uri, serverSelectionTimeoutMS=500).admin.command("ping")
            log(f"  mongod up on {port} (pid {proc.pid})")
            return proc
        except Exception:
            if proc.poll() is not None:
                raise SystemExit(f"mongod exited early; see {logpath}")
            time.sleep(0.5)
    raise SystemExit("mongod did not become ready")


def find_tid(pid: int, comm: str) -> int:
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            with open(f"/proc/{pid}/task/{tid}/comm") as h:
                if h.read().strip() == comm:
                    return int(tid)
        except OSError:
            continue
    raise SystemExit(f"no thread {comm} in {pid}")


def build_arm(port: int, hinted: bool) -> dict[str, Any]:
    client = MongoClient(f"mongodb://localhost:{port}/?directConnection=true",
                         maxPoolSize=1, minPoolSize=1)
    db = client[DB_NAME]
    conn_id = db.command("hello")["connectionId"]
    nodes = db[NODES]

    if hinted:
        def reader(parent):
            return [(d.get("node_id"), d.get("title"), d.get("summary"))
                    for d in nodes.find({"tree_id": TREE_ID, "parent_id": parent},
                                        CHILD_PROJECTION).sort(CHILD_SORT).hint(CHILD_INDEX)]
    else:
        def reader(parent):
            return [(d.get("node_id"), d.get("title"), d.get("summary"))
                    for d in nodes.find({"tree_id": TREE_ID, "parent_id": parent},
                                        CHILD_PROJECTION).sort(CHILD_SORT)]

    return {"client": client, "db": db, "reader": reader, "comm": f"conn{conn_id}",
            "connection_id": conn_id}


def measure_instructions(tid: int, reader, parents, seconds: float) -> dict[str, float]:
    """Retired instructions per operation on one connection thread."""
    with tempfile.NamedTemporaryFile("r", suffix=".perf", delete=False) as tf:
        out_path = tf.name
    perf = subprocess.Popen(
        ["perf", "stat", "-e", "instructions", "--tid", str(tid), "-x", ",", "-o", out_path,
         "--", "sleep", str(seconds)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.perf_counter()
    ops = 0
    idx = 0
    while time.perf_counter() - t0 < seconds:
        reader(parents[idx % len(parents)])
        idx += 1
        ops += 1
    perf.wait(timeout=60)

    insn = None
    for line in Path(out_path).read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split(",")
        if len(parts) >= 3 and "instructions" in parts[2]:
            try:
                insn = float(parts[0])
            except ValueError:
                pass
    os.unlink(out_path)
    if insn is None:
        raise SystemExit("perf produced no instruction count")
    return {"instructions_per_op": insn / ops, "ops": ops}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--dbpath", required=True)
    ap.add_argument("--cohort-json", required=True)
    ap.add_argument("--port", type=int, default=57051)
    ap.add_argument("--cache-gb", type=int, default=8)
    ap.add_argument("--blocks", type=int, default=9)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    parents = json.loads(Path(args.cohort_json).read_text())["parents"]
    out: dict[str, Any] = {
        "run": {"generated_unix_s": time.time(), "blocks": args.blocks,
                "seconds_per_arm_per_block": args.seconds},
        "unit": "retired instructions per operation, on the arm's own connection thread. "
                "Instructions are not time and do not convert to a latency delta.",
    }
    proc = start_mongod(Path(args.binary).resolve(), Path(args.dbpath),
                        Path(args.out).parent / "hintcmp_mongod.log", args.port, args.cache_gb)
    try:
        arms = {"hinted": build_arm(args.port, True),
                "unhinted": build_arm(args.port, False),
                "control": build_arm(args.port, True)}
        for n, a in arms.items():
            a["tid"] = find_tid(proc.pid, a["comm"])
            log(f"  arm {n:<9} connectionId {a['connection_id']} tid {a['tid']}")

        log("verifying element-wise output equality (unhinted must pick an equivalent plan)")
        elements = 0
        for p in parents:
            exp = arms["hinted"]["reader"](p)
            if not exp:
                raise SystemExit(f"hinted arm returned nothing for {p!r}")
            for n in ("unhinted", "control"):
                got = arms[n]["reader"](p)
                if got != exp:
                    raise SystemExit(f"OUTPUT MISMATCH arm {n} parent {p!r}")
                elements += len(got)
        log(f"  {elements} elements compared, all equal")

        db = arms["hinted"]["db"]
        before = db.command("serverStatus")["metrics"]["query"]["planCache"]["classic"]
        for n, a in arms.items():
            measure_instructions(a["tid"], a["reader"], parents, 1.0)
        after = db.command("serverStatus")["metrics"]["query"]["planCache"]["classic"]
        out["plan_cache_during_warmup"] = {k: after.get(k, 0) - before.get(k, 0)
                                           for k in ("hits", "misses", "skipped")}
        log(f"  plan cache during warm-up: {out['plan_cache_during_warmup']}")

        blocks = []
        log(f"measuring {args.blocks} blocks x {args.seconds}s per arm")
        for b in range(args.blocks):
            order = list(ARM_NAMES)[b % 3:] + list(ARM_NAMES)[:b % 3]
            row: dict[str, Any] = {"block": b, "order": order}
            for n in order:
                row[n] = measure_instructions(arms[n]["tid"], arms[n]["reader"],
                                              parents, args.seconds)
            h = row["hinted"]["instructions_per_op"]
            row["unhinted_vs_hinted_pct"] = 100.0 * (row["unhinted"]["instructions_per_op"] - h) / h
            row["control_vs_hinted_pct"] = 100.0 * (row["control"]["instructions_per_op"] - h) / h
            blocks.append(row)
            log("  block %d  hinted %,.0f  unhinted %,.0f (%+6.2f%%)  control %,.0f (%+6.2f%%)"
                .replace("%,.0f", "%9.0f")
                % (b, h, row["unhinted"]["instructions_per_op"], row["unhinted_vs_hinted_pct"],
                   row["control"]["instructions_per_op"], row["control_vs_hinted_pct"]))

        out["blocks"] = blocks
        uv = [x["unhinted_vs_hinted_pct"] for x in blocks]
        cv = [x["control_vs_hinted_pct"] for x in blocks]
        out["summary"] = {
            "unhinted_vs_hinted": {"median_pct": statistics.median(uv),
                                   "min_pct": min(uv), "max_pct": max(uv)},
            "control_vs_hinted": {"median_pct": statistics.median(cv),
                                  "min_pct": min(cv), "max_pct": max(cv)},
            "median_insn_per_op": {n: statistics.median(
                [x[n]["instructions_per_op"] for x in blocks]) for n in ARM_NAMES},
        }
        out["plan_cache_total"] = db.command("serverStatus")["metrics"]["query"]["planCache"]["classic"]
        log("")
        log("unhinted vs hinted  median %+.2f%%  spread [%+.2f, %+.2f]"
            % (out["summary"]["unhinted_vs_hinted"]["median_pct"],
               out["summary"]["unhinted_vs_hinted"]["min_pct"],
               out["summary"]["unhinted_vs_hinted"]["max_pct"]))
        log("control  vs hinted  median %+.2f%%  spread [%+.2f, %+.2f]   (floor)"
            % (out["summary"]["control_vs_hinted"]["median_pct"],
               out["summary"]["control_vs_hinted"]["min_pct"],
               out["summary"]["control_vs_hinted"]["max_pct"]))
        log(f"median insn/op: {out['summary']['median_insn_per_op']}")
        out["run"]["status"] = "complete"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=180)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))
        log(f"written to {args.out}")


if __name__ == "__main__":
    main()
