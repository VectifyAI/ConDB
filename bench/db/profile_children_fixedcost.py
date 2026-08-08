#!/usr/bin/env python3
"""Profile ``get_children`` inside a locally built mongod, to find the attackable
terms in the fixed per-command cost.

The source analysis puts 71% of this operation in fixed per-command work, and the
plan-cache probe has now measured planning at about 8.6 points of that.  This
script exists to find where the remaining ~60 points go.

perf can resolve user-space symbols here because the mongod is this account's own
build running as this account's uid -- unlike the containerised 7.0.34 server,
whose /proc/<pid>/maps is unreadable from this account.

Sampling is restricted to the **one connection thread** serving the workload, so
background threads (WiredTiger eviction, checkpointer, TTL) cannot dilute the
profile.  Both an inclusive (``--children``) and an exclusive (``--no-children``)
report are written, and they are labelled: inclusive percentages of sibling
frames must never be added together.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from pymongo import MongoClient

DB_NAME = "bench"
NODES = "layout2_view"
TREE_ID = "base"
CHILD_INDEX = "allops_tree_parent_path"
CHILD_PROJECTION = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}
CHILD_SORT = [("path", 1), ("node_id", 1)]


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def start_mongod(binary: Path, dbpath: Path, logpath: Path, port: int,
                 cache_gb: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("MONGO_PROBE_HINTED_PLAN_MEMO", None)
    proc = subprocess.Popen(
        [str(binary), "--port", str(port), "--dbpath", str(dbpath),
         "--bind_ip", "127.0.0.1", "--wiredTigerCacheSizeGB", str(cache_gb),
         "--logpath", str(logpath),
         "--setParameter", "diagnosticDataCollectionEnabled=false"],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env,
    )
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
            with open(f"/proc/{pid}/task/{tid}/comm") as handle:
                if handle.read().strip() == comm:
                    return int(tid)
        except OSError:
            continue
    raise SystemExit(f"no thread named {comm} in pid {pid}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--dbpath", required=True)
    parser.add_argument("--cohort-json", required=True)
    parser.add_argument("--port", type=int, default=57031)
    parser.add_argument("--cache-gb", type=int, default=8)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--freq", type=int, default=999)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    parents = json.loads(Path(args.cohort_json).read_text())["parents"]

    proc = start_mongod(Path(args.binary).resolve(), Path(args.dbpath),
                        outdir / "mongod.log", args.port, args.cache_gb)
    stop = threading.Event()
    counter = {"ops": 0}

    try:
        uri = f"mongodb://localhost:{args.port}/?directConnection=true"
        client = MongoClient(uri, maxPoolSize=1, minPoolSize=1)
        db = client[DB_NAME]
        connection_id = db.command("hello")["connectionId"]
        nodes = db[NODES]
        comm = f"conn{connection_id}"

        def drive() -> None:
            while not stop.is_set():
                for parent in parents:
                    if stop.is_set():
                        break
                    list(nodes.find({"tree_id": TREE_ID, "parent_id": parent},
                                    CHILD_PROJECTION).sort(CHILD_SORT).hint(CHILD_INDEX))
                    counter["ops"] += 1

        driver = threading.Thread(target=drive, daemon=True)
        driver.start()
        time.sleep(2)  # let the connection thread settle before locating it

        tid = find_tid(proc.pid, comm)
        log(f"  profiling tid {tid} (comm {comm}) for {args.seconds}s at {args.freq}Hz")

        perf_data = outdir / "perf.data"
        subprocess.run(
            ["perf", "record", "-F", str(args.freq), "-g", "--tid", str(tid),
             "-o", str(perf_data), "--", "sleep", str(args.seconds)],
            check=True, capture_output=True)

        stop.set()
        driver.join(timeout=30)
        log(f"  {counter['ops']} operations driven during the capture")

        for label, flag in (("inclusive", "--children"), ("exclusive", "--no-children")):
            report = outdir / f"children.{label}.txt"
            with open(report, "w") as handle:
                handle.write(
                    f"# {label} symbol profile of one mongod connection thread serving\n"
                    f"# get_children. {'Inclusive percentages of sibling frames must NOT be added.' if label == 'inclusive' else 'Exclusive: self time only.'}\n\n")
                handle.flush()
                subprocess.run(["perf", "report", "-i", str(perf_data), "--stdio",
                                flag, "--percent-limit", "0.4", "-g", "none"],
                               check=True, stdout=handle)
            log(f"  wrote {report}")
    finally:
        stop.set()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=180)
        log("  mongod stopped")


if __name__ == "__main__":
    main()
