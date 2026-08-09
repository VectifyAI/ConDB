#!/usr/bin/env python3
"""Source-level CPU attribution for one operation shape, via perf.

Keeps a single query shape hot on the real 10M dataset while perf samples the
whole machine, then reports only the mongod connection thread that served the
loop.  perf cannot attach per-pid here (mongod runs as another uid inside the
condb_mongo container and this account has no CAP_PERFMON), so the capture is
system-wide and the connection thread is isolated at report time by its comm.

The raw perf.data is kept.  Every derived report in this directory can be
regenerated from it with the perf command recorded in <prefix>.perf-cmd.txt.

Symbol resolution: the mongod binary carries a full .symtab (no DWARF) and is
built with -fno-omit-frame-pointer, so frame-pointer unwinding is both possible
and cheap.  The binary is copied out of the container once with
    docker cp condb_mongo:/usr/bin/mongod <runs>/bin/mongod
and registered with `perf buildid-cache --add`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_bottleneck_cpu import (  # noqa: E402
    MONGO_URI, MONGO_DB, build_mongo_arms, mongod_host_pid, pick_ids,
    thread_snapshot, thread_delta,
)


def log(m: str) -> None:
    print(m, flush=True)


def hot_loop(arm: str, subtree_rows: int) -> None:
    """Child mode: drive one arm continuously until killed."""
    from pymongo import MongoClient
    client = MongoClient(MONGO_URI, maxPoolSize=1)
    db = client[MONGO_DB]
    ids = pick_ids(db, subtree_rows)
    arms = build_mongo_arms(db, ids)
    fn = arms[arm]["fn"]
    while True:
        fn()


def busiest_conn_thread(pid: int, seconds: float) -> tuple[str | None, int, int]:
    """Return (comm, tid, ns).  The tid is what matters: perf cannot resolve the
    comm of another uid's threads, so reports have to be filtered by --tid."""
    before = thread_snapshot(pid)
    time.sleep(seconds)
    delta = thread_delta(before, thread_snapshot(pid))
    conns = [t for t in delta["per_thread"] if t["comm"].startswith("conn")]
    if not conns:
        return None, 0, 0
    top = max(conns, key=lambda t: t["sched_ns"] or 0)
    return top["comm"], top["tid"], top["sched_ns"] or 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("arm")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--freq", type=int, default=4999)
    parser.add_argument("--subtree-rows", type=int, default=11686)
    parser.add_argument("--outdir", default="bench/db/runs/bottleneck_20260806/perf")
    parser.add_argument("--slowms", type=int, default=None,
                        help="temporarily set slowms during the capture and restore it")
    args = parser.parse_args()

    if args.child:
        hot_loop(args.arm, args.subtree_rows)
        return

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    prefix = outdir / args.arm
    pid = mongod_host_pid()

    from pymongo import MongoClient
    admin_db = MongoClient(MONGO_URI, maxPoolSize=1)[MONGO_DB]
    original_profile = admin_db.command("profile", -1)
    if args.slowms is not None:
        admin_db.command("profile", original_profile.get("was", 0), slowms=args.slowms)
        log(f"[{args.arm}] slowms set to {args.slowms} "
            f"(was {original_profile.get('slowms')}); will be restored")

    log(f"[{args.arm}] starting hot loop")
    child = subprocess.Popen(
        [sys.executable, __file__, args.arm, "--child",
         "--subtree-rows", str(args.subtree_rows)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        time.sleep(6.0)  # connect, warm, and let the shape settle
        if child.poll() is not None:
            raise SystemExit(f"hot loop died: {child.stderr.read().decode()[:2000]}")

        comm, tid, ns = busiest_conn_thread(pid, 2.0)
        if comm is None:
            raise SystemExit("no mongod connection thread found doing work")
        log(f"[{args.arm}] connection thread {comm} tid {tid} at "
            f"{ns/2e9*100:.1f}% of one core; recording {args.duration}s at {args.freq}Hz")

        data = prefix.with_suffix(".perf.data")
        cmd = ["perf", "record", "-a", "-g", "--call-graph", "fp",
               "-F", str(args.freq), "-o", str(data), "--", "sleep", str(args.duration)]
        rec = subprocess.run(cmd, capture_output=True, text=True)
        (prefix.parent / f"{args.arm}.perf-cmd.txt").write_text(
            " ".join(cmd) + "\n\n--- stderr ---\n" + rec.stderr)
        log(f"[{args.arm}] {rec.stderr.strip().splitlines()[-1] if rec.stderr.strip() else ''}")
    finally:
        child.terminate()
        child.wait(timeout=20)
        if args.slowms is not None:
            admin_db.command("profile", original_profile.get("was", 0),
                             slowms=original_profile.get("slowms", 100))
            log(f"[{args.arm}] slowms restored: {admin_db.command('profile', -1)}")

    meta = {
        "arm": args.arm,
        "comm": comm,
        "tid": tid,
        "duration_s": args.duration,
        "freq_hz": args.freq,
        "mongod_host_pid": pid,
        "slowms_during_capture": args.slowms if args.slowms is not None
        else original_profile.get("slowms"),
        "generated_unix_s": time.time(),
        "raw": str(data),
        "note": "perf record is system-wide because per-pid attach is denied for this "
                "uid (mongod is uid 999, this account 1014). perf also cannot resolve "
                "that process's comm or user-space symbols, so reports must be filtered "
                "by --tid and symbolised with analyze_bottleneck_perf.py, which reads "
                "the load bias out of the live container and the symbol table out of the "
                "binary copied from it.",
    }
    (prefix.parent / f"{args.arm}.perf-meta.json").write_text(json.dumps(meta, indent=2))

    sym = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "analyze_bottleneck_perf.py"),
         str(data), "--tid", str(tid), "--out-prefix", str(prefix) + ".sym"],
        capture_output=True, text=True)
    log(sym.stderr.strip()[-500:] if sym.stderr else "")
    log(f"[{args.arm}] done")


if __name__ == "__main__":
    main()
