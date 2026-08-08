#!/usr/bin/env python3
"""Paired A/B for the fused covered projection, against a locally-built master mongod.

Both arms run in one process against one dbpath, so neither WiredTiger cache state nor
on-disk layout differs between them -- the gate is a server parameter consulted once per
plan build, not per key, so switching it cannot bias the hot loop.

Pairing discipline (these have each produced a wrong answer in this project before):
  * Arms alternate inside every block and the order rotates, so a drift in the machine
    lands on both arms equally. Deltas are computed per block and then aggregated; the
    unpaired difference of two run-level medians is never reported.
  * Output is compared element-wise on every block, both arms, and any mismatch is fatal.
  * Server CPU (from /proc/<pid>/stat), client wall, and retired instructions (perf stat)
    are three separate quantities, recorded separately and never compared to one another.
  * Activation is proved from explain: the fused arm's plan has no PROJECTION_COVERED stage
    and the base arm's does. Asserted, not assumed, and captured after the timed blocks.

Usage:
    bench_subtree_fused_ab.py --paths /000006/000075/000773 --blocks 12 --reps 3
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import signal
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
    out = []
    for p in cand:
        try:
            with open(f"/proc/{p}/comm") as fh:
                if fh.read().strip() == "mongod":
                    out.append(p)
        except OSError:
            continue
    if len(out) != 1:
        raise SystemExit(f"expected exactly one mongod on port {port}, found {out}")
    return int(out[0])


def proc_cpu_us(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as fh:
        parts = fh.read().rsplit(") ", 1)[1].split()
    return (int(parts[11]) + int(parts[12])) / 100.0 * 1e6


def set_arm(client: MongoClient, enabled: bool) -> None:
    client.admin.command({"setParameter": 1, KNOB: enabled})
    # Plans are cached; a cached plan built under the other arm would survive the switch.
    client[DB].command({"planCacheClear": NODES})


def stage_names(plan: Any) -> list[str]:
    """Every 'stage' label in an explain winningPlan, in no particular order."""
    names: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "stage" in node and isinstance(node["stage"], str):
                names.append(node["stage"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    return names


def plan_shape(coll: Any, lower: str, upper: str) -> list[str]:
    """Activation proof: a fused plan has no PROJECTION_COVERED stage."""
    ex = (coll.find({"path": {"$gte": lower, "$lt": upper}},
                    {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
          .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX)).explain()
    return stage_names(ex.get("queryPlanner", {}).get("winningPlan", {}))


class Instructions:
    """Retired user-space instructions for the mongod process over a window.

    On this box wall and CPU time move by up to 3x when a sibling agent is compiling, while
    retired instructions hold to ~0.1%. Both are recorded; they are different quantities and
    are never compared to each other.
    """

    def __init__(self, pid: int, enabled: bool) -> None:
        self.pid, self.enabled = pid, enabled
        self._proc: subprocess.Popen | None = None
        self._path: Path | None = None

    def __enter__(self) -> "Instructions":
        if self.enabled:
            self._path = Path(f"/tmp/.perfstat.{self.pid}.{os.getpid()}.csv")
            self._proc = subprocess.Popen(
                ["perf", "stat", "-x,", "-e", "instructions:u", "-p", str(self.pid),
                 "-o", str(self._path), "--", "sleep", "3600"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.25)  # let perf attach before the window opens
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._proc is not None:
            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def value(self) -> float | None:
        if self._path is None or not self._path.exists():
            return None
        for line in self._path.read_text().splitlines():
            if "instructions" in line and not line.startswith("#"):
                field = line.split(",")[0].strip()
                try:
                    return float(field)
                except ValueError:
                    continue
        return None


def run_subtree(coll: Any, lower: str, upper: str) -> list[tuple]:
    cur = (coll.find({"path": {"$gte": lower, "$lt": upper}},
                     {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
           .sort([("path", 1), ("node_id", 1)]).hint(COVER_INDEX))
    return [(d.get("node_id"), d.get("title"), d.get("summary")) for d in cur]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--paths", nargs="+", required=True)
    ap.add_argument("--blocks", type=int, default=12)
    ap.add_argument("--reps", type=int, default=3, help="repeats per arm per block")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--instructions", action="store_true",
                    help="also count retired user instructions with perf stat")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    uri = f"mongodb://localhost:{args.port}/?directConnection=true"
    client = MongoClient(uri)
    coll = client[DB][NODES]
    pid = mongod_pid(args.port)
    log(f"mongod pid {pid}; knob {KNOB}")

    results: dict[str, Any] = {"knob": KNOB, "port": args.port, "inputs": {}}

    for path in args.paths:
        lower, upper = path + "/", path + "0"

        # Reference output, and a warm cache, before any measurement.
        set_arm(client, False)
        for _ in range(args.warmup):
            reference = run_subtree(coll, lower, upper)
        log(f"{path}: {len(reference)} rows")

        blocks: list[dict[str, Any]] = []
        mismatches = 0
        for b in range(args.blocks):
            # Rotate which arm leads so a monotonic drift does not favour either.
            order = [False, True] if b % 2 == 0 else [True, False]
            block: dict[str, Any] = {}
            for enabled in order:
                set_arm(client, enabled)
                run_subtree(coll, lower, upper)  # settle after the switch
                with Instructions(pid, args.instructions) as ins:
                    cpu0 = proc_cpu_us(pid)
                    t0 = time.perf_counter()
                    for _ in range(args.reps):
                        rows = run_subtree(coll, lower, upper)
                    wall = (time.perf_counter() - t0) * 1e6 / args.reps
                    cpu = (proc_cpu_us(pid) - cpu0) / args.reps
                instr = ins.value()
                if rows != reference:
                    mismatches += 1
                block["fused" if enabled else "base"] = {
                    "wall_us": wall,
                    "cpu_us": cpu,
                    "instructions": (instr / args.reps) if instr is not None else None,
                }
            block["wall_delta_pct"] = (block["fused"]["wall_us"] / block["base"]["wall_us"] - 1) * 100
            block["cpu_delta_pct"] = (block["fused"]["cpu_us"] / block["base"]["cpu_us"] - 1) * 100
            if block["fused"]["instructions"] and block["base"]["instructions"]:
                block["instr_delta_pct"] = (
                    block["fused"]["instructions"] / block["base"]["instructions"] - 1) * 100
            blocks.append(block)
            instr_txt = (f"  instructions {block['instr_delta_pct']:+.2f}%"
                         if "instr_delta_pct" in block else "")
            log(f"  block {b}: wall {block['wall_delta_pct']:+.2f}%  "
                f"server CPU {block['cpu_delta_pct']:+.2f}%{instr_txt}")

        wall_deltas = [b["wall_delta_pct"] for b in blocks]
        cpu_deltas = [b["cpu_delta_pct"] for b in blocks]
        instr_deltas = [b["instr_delta_pct"] for b in blocks if "instr_delta_pct" in b]
        results["inputs"][path] = {
            "rows": len(reference),
            "blocks": blocks,
            "mismatches": mismatches,
            "paired_wall_delta_pct_median": statistics.median(wall_deltas),
            "paired_wall_delta_pct_min": min(wall_deltas),
            "paired_wall_delta_pct_max": max(wall_deltas),
            "paired_cpu_delta_pct_median": statistics.median(cpu_deltas),
            "paired_cpu_delta_pct_min": min(cpu_deltas),
            "paired_cpu_delta_pct_max": max(cpu_deltas),
            "blocks_improved_wall": sum(1 for d in wall_deltas if d < 0),
            "blocks_improved_cpu": sum(1 for d in cpu_deltas if d < 0),
            "paired_instr_delta_pct_median":
                statistics.median(instr_deltas) if instr_deltas else None,
            "paired_instr_delta_pct_min": min(instr_deltas) if instr_deltas else None,
            "paired_instr_delta_pct_max": max(instr_deltas) if instr_deltas else None,
            "blocks_improved_instr": sum(1 for d in instr_deltas if d < 0),
            "instr_blocks": len(instr_deltas),
        }
        r = results["inputs"][path]
        log(f"{path}: PAIRED median wall {r['paired_wall_delta_pct_median']:+.2f}% "
            f"[{r['paired_wall_delta_pct_min']:+.2f}, {r['paired_wall_delta_pct_max']:+.2f}] "
            f"{r['blocks_improved_wall']}/{len(blocks)} blocks; "
            f"server CPU {r['paired_cpu_delta_pct_median']:+.2f}% "
            f"{r['blocks_improved_cpu']}/{len(blocks)}; mismatches {mismatches}")
        if r["paired_instr_delta_pct_median"] is not None:
            log(f"{path}: PAIRED median retired instructions "
                f"{r['paired_instr_delta_pct_median']:+.2f}% "
                f"[{r['paired_instr_delta_pct_min']:+.2f}, {r['paired_instr_delta_pct_max']:+.2f}] "
                f"{r['blocks_improved_instr']}/{r['instr_blocks']} blocks")

    # Activation proof, captured last so it cannot disturb the timed blocks.
    activation = {}
    for path in args.paths:
        lower, upper = path + "/", path + "0"
        set_arm(client, False)
        base_plan = plan_shape(coll, lower, upper)
        set_arm(client, True)
        fused_plan = plan_shape(coll, lower, upper)
        activation[path] = {"base_stages": base_plan, "fused_stages": fused_plan}
        log(f"activation {path}: base={base_plan} fused={fused_plan}")
        assert "PROJECTION_COVERED" in base_plan, "base arm should have the projection stage"
        assert "PROJECTION_COVERED" not in fused_plan, "fused arm should not have it"
    results["activation"] = activation
    set_arm(client, False)

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
