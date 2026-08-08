#!/usr/bin/env python3
"""What is a fast path worth?  Measure express against full planning, like for like.

``get_children`` cannot use the express executor -- express cannot iterate, and this
operation returns a bounded run of rows.  Extending it is the remaining lead, and
before writing that, this prices what the *existing* express path is worth on this
box, on a shape that can use it.

The lever is exact and needs no rebuild and no knob.  ``isIdHackEligibleQueryWithoutCollator``
requires ``findCommand.getHint().isEmpty()``, so:

    express   find({_id: X})                     -> express fast path
    planned   find({_id: X}, hint={_id: 1})      -> CanonicalQuery, planning, executor

Both read the same ``_id`` index and return the same document.  The difference is
plan selection and executor construction -- exactly what a bounded-prefix-scan fast
path would skip for ``get_children``.

**This is a different shape from get_children and the number does not transfer as a
get_children result.**  What it bounds is how much of the *fixed per-command cost* a
fast path can remove at all; a bounded-scan fast path must additionally iterate, so
it would recover less than this, never more.

Both arms run on **one** server, each on its own pinned connection, so there is no
process-to-process bias to correct for.  ``control`` is a second express connection:
it prices connection-to-connection variance, which is the floor here.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient

DB_NAME = "bench"
NODES = "layout2_view"
ARM_NAMES = ("express", "planned", "control")


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def thread_nanos(pid: int, comm: str) -> int:
    total = 0
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return 0
    for tid in tids:
        try:
            with open(f"/proc/{pid}/task/{tid}/comm") as handle:
                if handle.read().strip() != comm:
                    continue
            with open(f"/proc/{pid}/task/{tid}/schedstat") as handle:
                total += int(handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            continue
    return total


def start_mongod(binary: Path, dbpath: Path, logpath: Path, port: int,
                 cache_gb: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("MONGO_PROBE_HINTED_PLAN_MEMO", None)
    proc = subprocess.Popen(
        [str(binary), "--port", str(port), "--dbpath", str(dbpath),
         "--bind_ip", "127.0.0.1", "--wiredTigerCacheSizeGB", str(cache_gb),
         "--logpath", str(logpath),
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


def build_arm(port: int, hinted: bool) -> dict[str, Any]:
    uri = f"mongodb://localhost:{port}/?directConnection=true"
    client = MongoClient(uri, maxPoolSize=1, minPoolSize=1)
    db = client[DB_NAME]
    connection_id = db.command("hello")["connectionId"]
    nodes = db[NODES]

    if hinted:
        def reader(oid):
            return list(nodes.find({"_id": oid}).hint({"_id": 1}))
    else:
        def reader(oid):
            return list(nodes.find({"_id": oid}))

    return {"client": client, "db": db, "reader": reader,
            "comm": f"conn{connection_id}", "connection_id": connection_id}


def measure(pid: int, comm: str, reader, ids: list, sweeps: int) -> dict[str, float]:
    before = thread_nanos(pid, comm)
    t0 = time.perf_counter()
    for _ in range(sweeps):
        for oid in ids:
            reader(oid)
    wall = time.perf_counter() - t0
    cpu_ns = thread_nanos(pid, comm) - before
    ops = sweeps * len(ids)
    return {"server_cpu_us": cpu_ns / ops / 1000.0,
            "client_wall_us": wall / ops * 1e6, "ops": ops}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--dbpath", required=True)
    parser.add_argument("--port", type=int, default=57041)
    parser.add_argument("--cache-gb", type=int, default=8)
    parser.add_argument("--ids", type=int, default=64)
    parser.add_argument("--blocks", type=int, default=20)
    parser.add_argument("--sweeps", type=int, default=40)
    parser.add_argument("--warmup-sweeps", type=int, default=8)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    out: dict[str, Any] = {
        "run": {"generated_unix_s": time.time(), "binary": args.binary,
                "blocks": args.blocks, "sweeps_per_block": args.sweeps},
        "what": "express vs full planning on an _id point query, same index, same document. "
                "A different shape from get_children; bounds how much fixed per-command cost a "
                "fast path can remove, and does not transfer as a get_children result.",
        "units": {"server_cpu_us": "mongod CPU on the arm's own connection thread",
                  "client_wall_us": "client wall per op; never combined with CPU"},
    }
    proc = start_mongod(Path(args.binary).resolve(), Path(args.dbpath),
                        Path(args.out).parent / "express_mongod.log", args.port, args.cache_gb)
    try:
        probe = MongoClient(f"mongodb://localhost:{args.port}/?directConnection=true",
                            maxPoolSize=1)[DB_NAME]
        ids = [d["_id"] for d in probe[NODES].find({}, {"_id": 1}).limit(args.ids)]
        if len(ids) < args.ids:
            raise SystemExit(f"only {len(ids)} ids available")
        log(f"  {len(ids)} _id values")

        arms = {"express": build_arm(args.port, hinted=False),
                "planned": build_arm(args.port, hinted=True),
                "control": build_arm(args.port, hinted=False)}
        for name, arm in arms.items():
            log(f"  arm {name:<8} connectionId {arm['connection_id']}")

        log("verifying element-wise output equality")
        elements = 0
        for oid in ids:
            expected = arms["express"]["reader"](oid)
            if not expected:
                raise SystemExit(f"express returned nothing for _id {oid!r}")
            for name in ("planned", "control"):
                got = arms[name]["reader"](oid)
                if got != expected:
                    raise SystemExit(f"OUTPUT MISMATCH arm {name} _id {oid!r}")
                elements += len(got)
        log(f"  {elements} documents compared, all equal")

        for name, arm in arms.items():
            measure(proc.pid, arm["comm"], arm["reader"], ids, args.warmup_sweeps)

        blocks = []
        log(f"measuring {args.blocks} blocks x {args.sweeps} sweeps x {len(ids)} ids per arm")
        for block in range(args.blocks):
            rotated = (list(ARM_NAMES)[block % 3:] + list(ARM_NAMES)[:block % 3])
            row: dict[str, Any] = {"block": block, "order": rotated}
            for name in rotated:
                row[name] = measure(proc.pid, arms[name]["comm"], arms[name]["reader"],
                                    ids, args.sweeps)
            base = row["express"]["server_cpu_us"]
            row["planned_vs_express_pct"] = 100.0 * (row["planned"]["server_cpu_us"] - base) / base
            row["control_vs_express_pct"] = 100.0 * (row["control"]["server_cpu_us"] - base) / base
            blocks.append(row)
            log("  block %2d  express %7.2f  planned %7.2f (%+6.2f%%)  control %7.2f (%+6.2f%%)"
                % (block, base, row["planned"]["server_cpu_us"], row["planned_vs_express_pct"],
                   row["control"]["server_cpu_us"], row["control_vs_express_pct"]))

        out["blocks"] = blocks
        pv = [b["planned_vs_express_pct"] for b in blocks]
        cv = [b["control_vs_express_pct"] for b in blocks]
        out["summary"] = {
            "planned_vs_express": {"median_pct": statistics.median(pv),
                                   "min_pct": min(pv), "max_pct": max(pv)},
            "control_vs_express": {"median_pct": statistics.median(cv),
                                   "min_pct": min(cv), "max_pct": max(cv)},
            "express_saving_vs_planned_pct":
                100.0 * (statistics.median([b["express"]["server_cpu_us"] for b in blocks])
                         - statistics.median([b["planned"]["server_cpu_us"] for b in blocks]))
                / statistics.median([b["planned"]["server_cpu_us"] for b in blocks]),
        }
        log("")
        log("planned vs express  median %+.2f%%  spread [%+.2f, %+.2f]"
            % (out["summary"]["planned_vs_express"]["median_pct"],
               out["summary"]["planned_vs_express"]["min_pct"],
               out["summary"]["planned_vs_express"]["max_pct"]))
        log("control vs express  median %+.2f%%  spread [%+.2f, %+.2f]   (floor)"
            % (out["summary"]["control_vs_express"]["median_pct"],
               out["summary"]["control_vs_express"]["min_pct"],
               out["summary"]["control_vs_express"]["max_pct"]))
        log("express saves %.2f%% of the planned arm's server CPU"
            % -out["summary"]["express_saving_vs_planned_pct"])
        out["run"]["status"] = "complete"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=180)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))
        log(f"written to {args.out}")


if __name__ == "__main__":
    main()
