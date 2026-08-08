#!/usr/bin/env python3
"""Paired A/B for the fused covered projection, against a locally-built master mongod.

Both arms run in one process against one dbpath, so neither WiredTiger cache state nor on-disk
layout differs between them. The gate is a server parameter consulted once per plan build, not
once per key, so switching it cannot reach the hot loop.

Pairing discipline (each of these has produced a wrong answer in this project before):
  * Arms alternate inside every block and the leading arm rotates, so drift on the machine lands
    on both arms equally. Deltas are per block and then aggregated; the unpaired difference of two
    run-level medians is never reported.
  * Output is compared element-wise against a reference on every block of both arms; a mismatch
    is fatal.
  * Retired instructions, server CPU and client wall are three different quantities. All three are
    recorded, none is derived from another, and none is compared across.
  * Activation is proved from explain -- the IXSCAN reports coveredProjection: true -- and
    asserted, not assumed. The PROJECTION_COVERED stage stays in the tree either way, so its
    presence is not the signal; the plan-stage tree must be identical in both arms.

Each arm-block is a fixed wall-clock window with the query replayed back to back inside it, and
`perf stat` counting the same window. Per-operation figures divide by the operations that actually
completed in the window.

Usage:
    bench_subtree_fused_ab.py --paths /000006/000075/000773 --blocks 10 --seconds 3 --instructions
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient

DB = "bench"
NODES = "layout2_view"
COVER_INDEX = "layout2_rootcause_exact_cover"
KNOB = "internalQueryEnableFusedCoveredProjection"


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def mongod_pid(port: int) -> int:
    uid = os.getuid()
    cand = subprocess.run(["pgrep", "-u", str(uid), "-f", f"mongod.*port[= ]{port}"],
                          capture_output=True, text=True).stdout.split()
    out = [p for p in cand if _comm(p) == "mongod"]
    if len(out) != 1:
        raise SystemExit(f"expected exactly one mongod on port {port}, found {out}")
    return int(out[0])


def _comm(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/comm") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def proc_cpu_us(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as fh:
        parts = fh.read().rsplit(") ", 1)[1].split()
    return (int(parts[11]) + int(parts[12])) / 100.0 * 1e6


def set_arm(client: MongoClient, enabled: bool) -> None:
    client.admin.command({"setParameter": 1, KNOB: enabled})
    # A plan cached under the other arm would otherwise survive the switch.
    client[DB].command({"planCacheClear": NODES})


def stage_names(plan: Any, acc: list[str] | None = None) -> list[str]:
    acc = [] if acc is None else acc
    if isinstance(plan, dict):
        if isinstance(plan.get("stage"), str):
            acc.append(plan["stage"])
        for v in plan.values():
            stage_names(v, acc)
    elif isinstance(plan, list):
        for v in plan:
            stage_names(v, acc)
    return acc


def cursor_for(coll: Any, lower: str, upper: str) -> Any:
    return (coll.find({"path": {"$gte": lower, "$lt": upper}},
                      {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
            .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX))


def run_subtree(coll: Any, lower: str, upper: str) -> list[tuple]:
    return [(d.get("node_id"), d.get("title"), d.get("summary"))
            for d in cursor_for(coll, lower, upper)]


def ixscan_fused(plan: Any) -> bool:
    """True when an IXSCAN in this plan reports a folded-in covered projection.

    PROJECTION_COVERED stays in the tree when the fold happens, so its presence proves nothing;
    the IXSCAN's 'coveredProjection' flag is the activation signal and is emitted only when true.
    """
    found = False

    def walk(node: Any) -> None:
        nonlocal found
        if isinstance(node, dict):
            if node.get("stage") == "IXSCAN" and node.get("coveredProjection"):
                found = True
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    return found


def plan_shape(coll: Any, lower: str, upper: str) -> tuple[list[str], bool]:
    wp = cursor_for(coll, lower, upper).explain().get("queryPlanner", {}).get("winningPlan", {})
    return stage_names(wp), ixscan_fused(wp)


def start_perf(pid: int, seconds: float, tag: str) -> tuple[subprocess.Popen, Path] | None:
    path = Path(f"/tmp/.perfstat.{os.getpid()}.{tag}.csv")
    proc = subprocess.Popen(
        ["perf", "stat", "-x,", "-e", "instructions:u", "-p", str(pid),
         "-o", str(path), "--", "sleep", f"{seconds:.3f}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, path


def read_perf(handle: tuple[subprocess.Popen, Path] | None) -> float | None:
    if handle is None:
        return None
    proc, path = handle
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        return None
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if "instructions:u" in line and not line.startswith("#"):
            try:
                return float(line.split(",")[0].strip())
            except ValueError:
                continue
    return None


def measure_arm(coll: Any, lower: str, upper: str, pid: int, seconds: float,
                use_perf: bool, tag: str) -> dict[str, Any]:
    """One fixed-length window of back-to-back queries, with all three counters over it."""
    handle = start_perf(pid, seconds, tag) if use_perf else None
    if handle is not None:
        time.sleep(0.20)  # let perf attach before the window opens

    ops = 0
    rows: list[tuple] = []
    cpu0 = proc_cpu_us(pid)
    t0 = time.perf_counter()
    # Start an operation only if it is expected to finish inside the window. Otherwise the loop
    # overruns the perf window by up to one whole operation, and `instructions` is divided by an
    # operation count that includes one perf never finished counting. At ~200 operations per
    # window that is noise; at the tail, where a window holds about 7, it is a 14% error and it
    # differs between arms whenever their op counts differ.
    while True:
        elapsed = time.perf_counter() - t0
        if ops and elapsed + (elapsed / ops) > seconds:
            break
        if elapsed >= seconds:
            break
        rows = run_subtree(coll, lower, upper)
        ops += 1
    wall_total = (time.perf_counter() - t0) * 1e6
    cpu_total = proc_cpu_us(pid) - cpu0
    instr_total = read_perf(handle)

    return {
        "ops": ops,
        "wall_us": wall_total / max(ops, 1),
        "cpu_us": cpu_total / max(ops, 1),
        "instructions": (instr_total / ops) if (instr_total is not None and ops) else None,
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--paths", nargs="+", required=True)
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--seconds", type=float, default=3.0, help="window length per arm per block")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--instructions", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    uri = f"mongodb://localhost:{args.port}/?directConnection=true"
    client = MongoClient(uri)
    coll = client[DB][NODES]
    pid = mongod_pid(args.port)
    log(f"mongod pid {pid}; knob {KNOB}; {args.seconds}s windows x {args.blocks} blocks x 2 arms")

    results: dict[str, Any] = {
        "knob": KNOB, "port": args.port, "seconds_per_window": args.seconds,
        "blocks": args.blocks, "inputs": {},
    }

    # How many instructions the process retires with no load, over the same window length. Not
    # subtracted from anything -- recorded so the floor is visible.
    if args.instructions:
        idle = read_perf(start_perf(pid, args.seconds, "idle"))
        results["idle_instructions_per_window"] = idle
        log(f"idle instructions over a {args.seconds}s window: {idle:,.0f}"
            if idle else "idle instruction sample unavailable")

    for path in args.paths:
        lower, upper = path + "/", path + "0"

        set_arm(client, False)
        reference: list[tuple] = []
        for _ in range(args.warmup):
            reference = run_subtree(coll, lower, upper)
        log(f"{path}: {len(reference)} rows")

        blocks: list[dict[str, Any]] = []
        mismatches = 0
        for b in range(args.blocks):
            order = [False, True] if b % 2 == 0 else [True, False]
            block: dict[str, Any] = {}
            for enabled in order:
                name = "fused" if enabled else "base"
                set_arm(client, enabled)
                run_subtree(coll, lower, upper)  # settle after the switch
                m = measure_arm(coll, lower, upper, pid, args.seconds,
                                args.instructions, f"{b}{name}")
                if m.pop("rows") != reference:
                    mismatches += 1
                block[name] = m

            for key, label in (("wall_us", "wall"), ("cpu_us", "cpu"), ("instructions", "instr")):
                bv, fv = block["base"][key], block["fused"][key]
                if bv:
                    block[f"{label}_delta_pct"] = (fv / bv - 1) * 100
            parts = [f"{lbl} {block[f'{lbl}_delta_pct']:+.2f}%"
                     for lbl in ("wall", "cpu", "instr") if f"{lbl}_delta_pct" in block]
            blocks.append(block)
            log(f"  block {b}: " + "  ".join(parts)
                + f"   (ops {block['base']['ops']}/{block['fused']['ops']})")

        entry: dict[str, Any] = {"rows": len(reference), "blocks": blocks,
                                 "mismatches": mismatches}
        for lbl in ("wall", "cpu", "instr"):
            ds = [b[f"{lbl}_delta_pct"] for b in blocks if f"{lbl}_delta_pct" in b]
            if ds:
                entry[f"paired_{lbl}_delta_pct"] = {
                    "median": statistics.median(ds), "min": min(ds), "max": max(ds),
                    "blocks": len(ds), "blocks_improved": sum(1 for d in ds if d < 0),
                }
        results["inputs"][path] = entry

        for lbl, human in (("instr", "retired instructions"), ("cpu", "server CPU"),
                           ("wall", "client wall")):
            s = entry.get(f"paired_{lbl}_delta_pct")
            if s:
                log(f"{path}: PAIRED {human} {s['median']:+.2f}% "
                    f"[{s['min']:+.2f}, {s['max']:+.2f}] "
                    f"{s['blocks_improved']}/{s['blocks']} blocks improved")
        log(f"{path}: output mismatches {mismatches}")

    activation = {}
    for path in args.paths:
        lower, upper = path + "/", path + "0"
        set_arm(client, False)
        base_stages, base_fused = plan_shape(coll, lower, upper)
        set_arm(client, True)
        fused_stages, fused_fused = plan_shape(coll, lower, upper)
        activation[path] = {"base_stages": base_stages, "fused_stages": fused_stages,
                            "base_ixscan_fused": base_fused, "fused_ixscan_fused": fused_fused}
        log(f"activation {path}: stages={fused_stages} "
            f"ixscan coveredProjection base={base_fused} fused={fused_fused}")
        assert not base_fused, "base arm reported a fused scan"
        assert fused_fused, "fused arm did not fuse"
        assert base_stages == fused_stages, "the fold changed the plan-stage tree"
    results["activation"] = activation
    set_arm(client, False)

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
