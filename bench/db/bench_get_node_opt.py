#!/usr/bin/env python3
"""Measure the three stacking get_node optimizations against the shipped path.

Arms, each one layer on top of the last:

  baseline   bench_all_ops_layouts.py:146-168 verbatim -- hinted find_one
  command    Database.command; same command and same 303-byte encoded length,
             field order aside, and no Cursor
  pinned     + server selection and pool checkout amortized, session kept
  idhack     + string _id natural key, no hint, on layout2_view_idhack

Three quantities per arm, never mixed:

  wall_us   client wall clock            time.perf_counter
  ccpu_us   client process CPU           time.process_time (user+sys).  This is
            WHOLE-PROCESS, not per arm: the process holds one MongoClient per
            arm plus a probe, and their monitor threads are in the total.  It
            is usable as a paired delta -- the background is the same for every
            arm in a block -- but the absolute is not one arm's client CPU.
  scpu_us   mongod CPU on this arm's own connection thread, from the first
            field of /proc/<pid>/task/<tid>/schedstat, sum_exec_runtime,
            summed by thread name.  (Called "field 1" in the earlier campaigns;
            read 0-based that names run_delay, which is a different counter and
            is identically zero on this host.)
            Each arm gets its own MongoClient with maxPoolSize=minPoolSize=1,
            so one arm is one connection thread and a sibling process on
            another connection cannot leak in.

Design, following bench_opwin_20260807.py: output equality is checked
element-wise against the baseline for every input before any timing runs; arms
rotate within each block; the reported effect is the median of the per-block
paired delta.  Block min/max is a range over blocks, not a confidence interval,
and is labelled as such in the output.

Two things this deliberately does not do.

maxPoolSize=1 is the cheapest possible pool for the baseline to check out of --
one connection, never contended, never grown.  The pinned arm's win is
therefore a lower bound on what the fast path is worth against a real pool.

Per-operation logging is turned off for the run window (slowms=100) and
restored afterwards, because slowms=0 costs 47.1 us of server CPU on this
operation -- 39.8% -- and the report's headline table is a logging-off basis.
The restore runs in a finally block and the final value is read back and
recorded.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import random
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from pymongo import MongoClient

from opt_get_node import (
    BASELINE_COLLECTION,
    BASELINE_INDEX,
    CONTROL_COLLECTION,
    IDHACK_COLLECTION,
    PROJECTION,
    READERS,
    idhack_key,
)

ARM_ORDER = ("baseline", "command", "unpinned", "pinned", "idhack")
CONTROL_ARM = "control"
NOHINT_ARM = "nohint"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ------------------------------------------------------------ server CPU tap

def thread_table(pid: int) -> dict[str, int]:
    """comm -> schedstat nanoseconds, for every thread of ``pid``."""
    table: dict[str, int] = {}
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return table
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/comm") as handle:
                comm = handle.read().strip()
            with open(f"{task_dir}/{tid}/schedstat") as handle:
                nanos = int(handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            continue
        table[comm] = table.get(comm, 0) + nanos
    return table


def mongod_pid(container: str) -> int:
    completed = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Pid}}", container],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(completed.stdout.strip())


# ----------------------------------------------------------------- profiling

def read_slowms(database: Any) -> tuple[int, int]:
    """Current (profiling level, slowms)."""
    current = database.command("profile", -1)
    return int(current.get("was", 0)), int(current.get("slowms", 0))


def set_slowms(database: Any, value: int) -> int:
    """Set profiler slowms without changing the profiling level.  Returns the old value."""
    level, before = read_slowms(database)
    database.command("profile", level, slowms=value)
    _, after = read_slowms(database)
    if after != value:
        raise SystemExit(f"slowms did not take: asked {value}, server reports {after}")
    return before


# --------------------------------------------------------------------- arms

def build_arms(
    uri: str, db_name: str, arm_order: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    arms: dict[str, dict[str, Any]] = {}
    for name in arm_order:
        client = MongoClient(uri, maxPoolSize=1, minPoolSize=1)
        database = client[db_name]
        connection_id = database.command("hello")["connectionId"]
        reader = READERS[name](database)
        arms[name] = {
            "client": client,
            "database": database,
            "reader": reader,
            "comm": f"conn{connection_id}",
            "connection_id": connection_id,
        }
        log(f"  arm {name:<9} connectionId {connection_id}")
    return arms


def explain_plans(arms: dict[str, dict[str, Any]], tree_id: str, node_id: str) -> dict:
    """Record the winning plan each arm's filter actually takes."""
    def stage_chain(plan: dict[str, Any]) -> list[str]:
        chain = []
        node = plan
        while isinstance(node, dict) and "stage" in node:
            chain.append(node["stage"])
            node = node.get("inputStage")
        return chain

    baseline_db = arms["baseline"]["database"]
    nodes = baseline_db[BASELINE_COLLECTION]
    idhack = baseline_db[IDHACK_COLLECTION]

    baseline_plan = nodes.find(
        {"tree_id": tree_id, "node_id": node_id}, PROJECTION
    ).hint(BASELINE_INDEX).explain()["queryPlanner"]["winningPlan"]
    idhack_plan = idhack.find(
        {"_id": idhack_key(tree_id, node_id)}, PROJECTION
    ).explain()["queryPlanner"]["winningPlan"]
    return {
        "baseline_and_command_and_pinned": stage_chain(baseline_plan),
        "idhack": stage_chain(idhack_plan),
    }


def verify_equality(
    arms: dict[str, dict[str, Any]],
    tree_id: str,
    node_ids: list[str],
    arm_order: tuple[str, ...],
) -> dict[str, Any]:
    """Every arm must return the identical tuple for every input.

    The baseline is the reference and is excluded from the comparison count: an
    earlier version iterated all of arm_order and so compared the baseline
    against itself, inflating `elements_compared` by one arm's worth.
    """
    elements = 0
    others = [name for name in arm_order if name != "baseline"]
    for node_id in node_ids:
        expected = arms["baseline"]["reader"](tree_id, node_id)
        if not expected:
            raise SystemExit(f"baseline returned nothing for node_id {node_id!r}")
        for name in others:
            got = arms[name]["reader"](tree_id, node_id)
            if got != expected:
                raise SystemExit(
                    f"OUTPUT MISMATCH arm {name} node_id {node_id!r}:\n"
                    f"  got      {got!r}\n  expected {expected!r}"
                )
            elements += len(got[0]) if got else 0
    log(
        f"equality verified: {len(others)} arms vs baseline x {len(node_ids)} "
        f"inputs, {elements} elements compared, all equal"
    )
    return {
        "reference": "baseline",
        "arms_compared": others,
        "inputs": len(node_ids),
        "elements_compared": elements,
        "note": "the baseline is the reference and is not compared with itself",
        "all_equal": True,
    }


# ---------------------------------------------------------------- statistics

def outlier_blocks(
    results: dict[str, list[dict]], factor: float = 1.5
) -> list[int]:
    """Blocks where some arm ran more than `factor` x its own median wall.

    arms.json from the first campaign had its first eight blocks inflated by a
    6 GB $out that finished ten seconds before the run started -- baseline wall
    261-333 us against 173-187 us for the rest -- and `paired` medianed straight
    over them.  Eight of twenty is enough to move a median, so screening cannot
    be left to the median alone.  Which blocks were dropped is recorded in the
    output; nothing is silently truncated.
    """
    flagged: set[int] = set()
    for rows in results.values():
        walls = sorted(row["wall_us"] for row in rows)
        middle = walls[len(walls) // 2]
        for index, row in enumerate(rows):
            if row["wall_us"] > factor * middle:
                flagged.add(index)
    return sorted(flagged)


def paired(
    results: dict[str, list[dict]],
    name: str,
    key: str,
    drop: Sequence[int] = (),
) -> dict[str, Any]:
    keep = [i for i in range(len(results["baseline"])) if i not in set(drop)]
    base = [results["baseline"][i] for i in keep]
    arm = [results[name][i] for i in keep]
    deltas = [arm[i][key] - base[i][key] for i in range(len(base))]
    pcts = [
        100.0 * deltas[i] / base[i][key] if base[i][key] else 0.0
        for i in range(len(base))
    ]
    return {
        "median_delta_us": round(statistics.median(deltas), 3),
        "median_pct": round(statistics.median(pcts), 2),
        "block_min_pct": round(min(pcts), 2),
        "block_max_pct": round(max(pcts), 2),
        "blocks_favouring_arm": sum(1 for d in deltas if d < 0),
        "blocks": len(deltas),
        "blocks_dropped": list(drop),
        "note": "block_min/max is a range over blocks, not a confidence interval",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument("--mongo-db", default="bench")
    parser.add_argument("--container", default="condb_mongo")
    parser.add_argument("--tree-id", default="base")
    parser.add_argument("--blocks", type=int, default=20)
    parser.add_argument("--iters", type=int, default=500)
    parser.add_argument("--inputs", type=int, default=64)
    parser.add_argument("--warm", type=int, default=400)
    parser.add_argument("--keep-logging", action="store_true",
                        help="do not touch profiler slowms")
    parser.add_argument(
        "--input-mode", choices=("head", "sample"), default="head",
        help="head: the first --inputs node_ids in sort order, which is what "
             "the earlier campaigns used.  sample: a seeded subset of a $sample "
             "draw four times the size, which spreads the working set.  $sample "
             "is not seedable server-side, so the draw differs run to run and "
             "the exact cohort is recorded under run.node_ids; --seed fixes the "
             "block order and the subset taken from a given draw.",
    )
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument(
        "--with-control", action="store_true",
        help="add the freshness control arm: the baseline query on "
             "layout2_view_control, a same-age copy built by "
             "prepare_get_node_control.py",
    )
    parser.add_argument(
        "--settle", type=float, default=30.0,
        help="seconds to wait before the first block, so a write or a build "
             "that just finished cannot bleed into block 0",
    )
    parser.add_argument(
        "--with-nohint", action="store_true",
        help="add the unhinted baseline arm, which prices what dropping the "
             "hint is worth on its own; the idhack arm cannot carry a hint",
    )
    parser.add_argument("--out", default="runs/get_node_opt/arms.json")
    args = parser.parse_args()

    pid = mongod_pid(args.container)
    log(f"mongod pid {pid} in {args.container}")

    probe = MongoClient(args.mongo_uri)
    probe_db = probe[args.mongo_db]
    present = set(probe_db.list_collection_names())
    if IDHACK_COLLECTION not in present:
        raise SystemExit(
            f"{IDHACK_COLLECTION} does not exist; run prepare_get_node_idhack.py first"
        )
    arm_order = ARM_ORDER
    if args.with_control:
        if CONTROL_COLLECTION not in present:
            raise SystemExit(
                f"{CONTROL_COLLECTION} does not exist; "
                "run prepare_get_node_control.py first"
            )
        arm_order = ARM_ORDER + (CONTROL_ARM,)
    if args.with_nohint:
        arm_order = arm_order + (NOHINT_ARM,)

    if args.input_mode == "head":
        node_ids = [
            document["node_id"]
            for document in probe_db[BASELINE_COLLECTION]
            .find({"tree_id": args.tree_id}, {"_id": 0, "node_id": 1})
            .sort([("node_id", 1)])
            .limit(args.inputs)
        ]
    else:
        # $sample is server-side and unseeded, so draw a larger pool once and
        # take a seeded subset of it: the inputs are then reproducible from the
        # recorded seed given the same pool, and the pool spans the collection
        # rather than its first page.
        pool = [
            document["node_id"]
            for document in probe_db[BASELINE_COLLECTION].aggregate(
                [
                    {"$sample": {"size": args.inputs * 4}},
                    {"$project": {"_id": 0, "node_id": 1}},
                ]
            )
        ]
        # random.sample over the whole draw, not sorted()[:n].  An earlier
        # version sorted and truncated, which silently took the
        # lexicographically smallest quarter of the draw -- in one retained run
        # nothing above node_id 6,846,721 appeared at all, so "sampled across
        # all 10M" was false.  The shuffle before it was dead code, erased by
        # the sort, which also made --seed have no effect on the subset.
        unique = sorted(set(pool))
        rng = random.Random(args.seed)
        node_ids = rng.sample(unique, min(args.inputs, len(unique)))
    if len(node_ids) < args.inputs:
        raise SystemExit(f"only {len(node_ids)} inputs available")
    log(
        f"{len(node_ids)} inputs, tree_id {args.tree_id!r}, "
        f"mode {args.input_mode}, arms {list(arm_order)}"
    )

    output: dict[str, Any] = {
        "run": {
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mongodb_version": probe.server_info()["version"],
            "uri": args.mongo_uri,
            "blocks": args.blocks,
            "iters_per_block": args.iters,
            "inputs": len(node_ids),
            "input_mode": args.input_mode,
            "seed": args.seed,
            "arms": list(arm_order),
            "node_ids": node_ids,
            "pool": "maxPoolSize=minPoolSize=1 per arm",
            "settle_seconds": args.settle,
        },
        "instruments": {
            "wall_us": "time.perf_counter, client",
            "ccpu_us": "time.process_time, WHOLE client process incl. all "
                       "MongoClient monitor threads; valid as a paired delta, "
                       "not as one arm's absolute",
            "scpu_us": "mongod /proc/<pid>/task/<tid>/schedstat first field "
                       "(sum_exec_runtime), this arm's connection thread only",
        },
    }

    slowms_before = None
    arms: dict[str, dict[str, Any]] = {}
    try:
        if not args.keep_logging:
            slowms_before = set_slowms(probe_db, 100)
            log(f"profiler slowms {slowms_before} -> 100 for the run window")
            output["run"]["slowms_during_run"] = 100
            output["run"]["slowms_before_run"] = slowms_before
        else:
            output["run"]["slowms_during_run"] = "untouched"

        log("opening one client per arm")
        arms = build_arms(args.mongo_uri, args.mongo_db, arm_order)

        output["plans"] = explain_plans(arms, args.tree_id, node_ids[0])
        log(f"plans: {json.dumps(output['plans'])}")

        output["equality"] = verify_equality(
            arms, args.tree_id, node_ids, arm_order
        )

        if args.settle > 0:
            log(f"settling {args.settle:.0f}s before timing")
            time.sleep(args.settle)
        log(f"warming {args.warm} iterations per arm")
        for name in arm_order:
            reader = arms[name]["reader"]
            for i in range(args.warm):
                reader(args.tree_id, node_ids[i % len(node_ids)])

        results: dict[str, list[dict]] = {name: [] for name in arm_order}
        block_orders: list[list[str]] = []
        names = list(arm_order)
        count = len(node_ids)
        # A cyclic shift keeps arm *adjacency* fixed -- `command` would follow
        # `baseline` in most blocks -- which matters because gc.enable() runs
        # outside the timed region, so each arm's deferred garbage is collected
        # during a systematically determined neighbour's block.  A seeded
        # permutation decorrelates it.
        order_rng = random.Random(args.seed)
        for block in range(args.blocks):
            order = order_rng.sample(names, len(names))
            block_orders.append(list(order))
            for name in order:
                reader = arms[name]["reader"]
                comm = arms[name]["comm"]
                before = thread_table(pid)
                gc.disable()
                try:
                    wall0 = time.perf_counter()
                    cpu0 = time.process_time()
                    for i in range(args.iters):
                        rows = reader(args.tree_id, node_ids[i % count])
                    cpu1 = time.process_time()
                    wall1 = time.perf_counter()
                finally:
                    gc.enable()
                after = thread_table(pid)
                # A liveness check on the final iteration only -- checking every
                # iteration would put the comparison inside the timed region.
                # Drift across the whole run is caught by the post-run equality
                # pass below, not here.
                if not rows:
                    raise SystemExit(
                        f"arm {name} returned nothing on the last iteration of "
                        f"block {block}"
                    )
                other = sum(
                    after.get(c, 0) - before.get(c, 0)
                    for c in after
                    if c.startswith("conn") and c != comm
                )
                results[name].append({
                    "block": block,
                    "wall_us": (wall1 - wall0) * 1e6 / args.iters,
                    "ccpu_us": (cpu1 - cpu0) * 1e6 / args.iters,
                    "scpu_us": (after.get(comm, 0) - before.get(comm, 0))
                    / 1e3 / args.iters,
                    "other_conn_us": other / 1e3 / args.iters,
                })
            log(f"block {block + 1}/{args.blocks} done")

        output["equality_after"] = verify_equality(
            arms, args.tree_id, node_ids, arm_order
        )
        output["block_orders"] = block_orders

        # The fast path must actually have engaged; if it silently fell back to
        # a full checkout every call the arm measures nothing.
        output["checkout"] = {
            name: arms[name]["reader"].checkout_stats
            for name in ("pinned", "idhack")
            if name in arms
        }
        log(f"checkout: {json.dumps(output['checkout'])}")

        output["blocks"] = results
        dropped = outlier_blocks(results)
        output["outlier_screen"] = {
            "rule": "drop a block if any arm's wall exceeds 1.5x that arm's "
                    "own median wall over the run",
            "blocks_dropped": dropped,
            "blocks_kept": args.blocks - len(dropped),
        }
        if dropped:
            log(f"outlier screen drops blocks {dropped}")
        else:
            log("outlier screen drops nothing")
        output["summary"] = {
            name: {
                "wall_us": round(statistics.median(
                    r["wall_us"] for r in results[name]), 2),
                "ccpu_us": round(statistics.median(
                    r["ccpu_us"] for r in results[name]), 2),
                "scpu_us": round(statistics.median(
                    r["scpu_us"] for r in results[name]), 2),
                "other_conn_us": round(statistics.median(
                    r["other_conn_us"] for r in results[name]), 3),
            }
            for name in arm_order
        }
        output["paired_vs_baseline"] = {
            name: {
                key: paired(results, name, key, dropped)
                for key in ("wall_us", "ccpu_us", "scpu_us")
            }
            for name in arm_order
            if name != "baseline"
        }
        output["paired_vs_baseline_all_blocks"] = {
            name: {
                key: paired(results, name, key)
                for key in ("wall_us", "ccpu_us", "scpu_us")
            }
            for name in arm_order
            if name != "baseline"
        }
        output["run"]["status"] = "complete"
    finally:
        for arm in arms.values():
            try:
                arm["reader"].close()
            except Exception as error:  # noqa: BLE001
                log(f"reader close failed: {error}")
            arm["client"].close()
        if slowms_before is not None:
            set_slowms(probe_db, slowms_before)
            output.setdefault("run", {})["slowms_after_run"] = read_slowms(probe_db)[1]
            log(f"profiler slowms restored to {output['run']['slowms_after_run']}")
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2))
        log(f"wrote {out_path}")
        probe.close()

    print()
    print(f"{'arm':<10}{'wall us':>10}{'ccpu us':>10}{'scpu us':>10}"
          f"{'d wall':>10}{'d ccpu':>10}{'d scpu':>10}")
    base = output["summary"]["baseline"]
    for name in arm_order:
        row = output["summary"][name]
        if name == "baseline":
            print(f"{name:<10}{row['wall_us']:>10.1f}{row['ccpu_us']:>10.1f}"
                  f"{row['scpu_us']:>10.1f}{'—':>10}{'—':>10}{'—':>10}")
            continue
        pair = output["paired_vs_baseline"][name]
        print(f"{name:<10}{row['wall_us']:>10.1f}{row['ccpu_us']:>10.1f}"
              f"{row['scpu_us']:>10.1f}"
              f"{pair['wall_us']['median_pct']:>9.1f}%"
              f"{pair['ccpu_us']['median_pct']:>9.1f}%"
              f"{pair['scpu_us']['median_pct']:>9.1f}%")


if __name__ == "__main__":
    main()
