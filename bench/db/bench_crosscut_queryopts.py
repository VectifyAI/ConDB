#!/usr/bin/env python3
"""Price query-level options that need no schema change and no mongod change.

Everything here is a *query option*, not a connection option, so every arm can
be driven over ONE shared connection.  That matters: on this box a fresh TCP
connection to the same standalone mongod carries a persistent latency penalty
or bonus of up to 23% in P50 (``bench_crosscut_connection_lottery.py``), which
swamps the few-percent effects these options produce.  Sharing the connection
removes that term from the comparison entirely.

Arms:

``hint``      pin the index hint, exactly as ``bench_all_ops_layouts.py`` does
``nohint``    same query with no hint, so the plan cache is consulted
``exhaust``   OP_MSG exhaust cursors (get_children / get_subtree only)
``covered``   get_children served from an index that covers title+summary
              (only present if that index exists; see --allow-covered)

The design is paired and interleaved: within each (block, input) the arm order
is an independent shuffle, so no arm has a fixed predecessor.  A cyclic
rotation, which is what ``bench_all_ops_layouts.py`` uses, does fix the
predecessor of every arm; ``--rotate`` reproduces it so the two can be
compared.  Measured over five byte-identical arms and 30 blocks, they are
indistinguishable (both within +-0.2%), so rotation is not in fact a source of
bias here -- see ``armorder_rotation.json`` and ``armorder_shuffled.json``.
An early six-arm smoke run appeared to show rotation fabricating a 30%
difference; that was the per-connection placement lottery at two blocks, not
the ordering, and the claim is withdrawn.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Sequence

MONGO_NODES = "layout2_view"
MONGO_TEXT = "layout_shared_text"
MONGO_SUBTREE_INDEX = "layout2_rootcause_exact_cover"
TREE_ID = "base"

NODE_PROJECTION = {
    "_id": 0, "node_id": 1, "parent_id": 1, "depth": 1, "title": 1,
    "summary": 1, "start_index": 1, "end_index": 1,
}
LIST_PROJECTION = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def normalize(rows):
    return [tuple("" if v is None else v for v in row) for row in rows]


def fingerprint(rows) -> str:
    digest = hashlib.sha256()
    for row in rows:
        for value in row:
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def build_arms(database, arm_names: list[str]) -> dict[str, dict[str, Callable]]:
    from pymongo import CursorType

    nodes = database[MONGO_NODES]
    text = database[MONGO_TEXT]
    arms: dict[str, dict[str, Callable]] = {}

    for arm in arm_names:
        hinted = arm != "nohint"
        exhaust = arm == "exhaust"
        cursor_type = CursorType.EXHAUST if exhaust else CursorType.NON_TAILABLE
        # "narrow": drive get_subtree off the 280 MB (path,node_id) index and
        # pay a FETCH per row, instead of the 4.66 GB index that also encodes
        # title and summary into the key.  This prices the covering index.
        subtree_index = ("path_1_node_id_1" if arm == "narrow"
                         else MONGO_SUBTREE_INDEX)

        def get_node(tree_id, node_id, hinted=hinted):
            kwargs = {"hint": "allops_tree_node"} if hinted else {}
            row = nodes.find_one(
                {"tree_id": tree_id, "node_id": node_id}, NODE_PROJECTION,
                **kwargs)
            if row is None:
                return []
            return normalize([(
                row.get("node_id"), row.get("parent_id"), row.get("depth"),
                row.get("title"), row.get("summary"),
                row.get("start_index"), row.get("end_index"))])

        def get_children(tree_id, node_id, hinted=hinted, ct=cursor_type):
            cursor = nodes.find(
                {"tree_id": tree_id, "parent_id": node_id}, LIST_PROJECTION,
                cursor_type=ct).sort([("path", 1), ("node_id", 1)])
            if hinted:
                cursor = cursor.hint("allops_tree_parent_path")
            return normalize(
                (r.get("node_id"), r.get("title"), r.get("summary"))
                for r in cursor)

        def get_subtree(tree_id, node_id, hinted=hinted, ct=cursor_type,
                        subtree_index=subtree_index):
            kwargs = {"hint": "allops_tree_node"} if hinted else {}
            root = nodes.find_one(
                {"tree_id": tree_id, "node_id": node_id}, {"_id": 0, "path": 1},
                **kwargs)
            if root is None:
                return []
            lower, upper = root["path"] + "/", root["path"] + "0"
            cursor = nodes.find(
                {"path": {"$gte": lower, "$lt": upper}}, LIST_PROJECTION,
                cursor_type=ct).sort([("path", 1), ("node_id", 1)])
            if hinted:
                cursor = cursor.hint(subtree_index)
            return normalize(
                (r.get("node_id"), r.get("title"), r.get("summary"))
                for r in cursor)

        def get_entity(_tree_id, node_id):
            row = text.find_one({"_id": node_id}, {"_id": 1, "text": 1})
            return normalize([(node_id, row.get("text"))]) if row else []

        arms[arm] = {
            "get_node": get_node, "get_children": get_children,
            "get_subtree": get_subtree, "get_entity": get_entity,
        }
    return arms


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="mongodb://localhost:57017")
    parser.add_argument("--arms", default="hint,nohint")
    parser.add_argument("--ops", default="get_node,get_children,get_subtree")
    parser.add_argument("--blocks", type=int, default=30)
    parser.add_argument("--inputs", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument(
        "--expected",
        default="bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--rotate", action="store_true",
                        help="order arms by cyclic rotation, reproducing the "
                             "design of bench_all_ops_layouts.py, instead of "
                             "an independent shuffle")
    args = parser.parse_args()

    order = [a.strip() for a in args.arms.split(",") if a.strip()]
    operations = [o.strip() for o in args.ops.split(",") if o.strip()]
    baseline = order[0]

    from pymongo import MongoClient

    client = MongoClient(args.uri)
    database = client["bench"]
    client.admin.command("ping")
    arms = build_arms(database, order)

    document = json.loads(Path(args.expected).read_text())
    tree_ids = [s["path"].rsplit("/", 1)[-1] for s in document["samples"]]
    point_ids = [
        d["node_id"] for d in database[MONGO_NODES]
        .find({"tree_id": TREE_ID}, {"node_id": 1, "_id": 0})
        .limit(max(args.inputs, args.warmup))
    ]
    inputs_for = {
        "get_node": point_ids[:args.inputs],
        "get_entity": point_ids[:args.inputs],
        "get_children": tree_ids[:args.inputs],
        "get_subtree": tree_ids[:args.inputs],
    }

    server = client.admin.command("serverStatus")
    output: dict[str, Any] = {
        "run": {
            "label": args.label, "uri": args.uri, "arms": order,
            "baseline_arm": baseline, "operations": operations,
            "blocks": args.blocks, "inputs": args.inputs,
            "shared_connection": True,
            "arm_order": "cyclic_rotation" if args.rotate else "shuffled",
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mongodb_version": server["version"],
            "profiling_slowms": database.command("profile", -1)["slowms"],
            "loadavg": os.getloadavg(),
        },
        "results": {},
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rng = random.Random(0xC0FFEE)
    for operation in operations:
        op_inputs = inputs_for[operation]
        log(f"{operation}: warm + verify {len(op_inputs)} inputs x {len(order)} arms")
        for arm in order:
            for node_id in op_inputs[:min(args.warmup, len(op_inputs))]:
                arms[arm][operation](TREE_ID, node_id)
        truth = {}
        for node_id in op_inputs:
            rows = arms[baseline][operation](TREE_ID, node_id)
            truth[node_id] = {"rows": len(rows), "fp": fingerprint(rows)}
        for arm in order[1:]:
            for node_id in op_inputs:
                rows = arms[arm][operation](TREE_ID, node_id)
                if (len(rows) != truth[node_id]["rows"]
                        or fingerprint(rows) != truth[node_id]["fp"]):
                    raise RuntimeError(f"output mismatch {operation} {arm} {node_id}")

        log(f"{operation}: timing {len(op_inputs)} x {len(order)} arms x {args.blocks} blocks")
        observations = {a: [[] for _ in range(args.blocks)] for a in order}
        for block in range(args.blocks):
            for index, node_id in enumerate(op_inputs):
                if args.rotate:
                    pivot = (block + index) % len(order)
                    sequence = order[pivot:] + order[:pivot]
                else:
                    sequence = list(order)
                    rng.shuffle(sequence)
                for arm in sequence:
                    gc.disable()
                    try:
                        started = time.perf_counter()
                        rows = arms[arm][operation](TREE_ID, node_id)
                        elapsed = (time.perf_counter() - started) * 1000.0
                    finally:
                        gc.enable()
                    if len(rows) != truth[node_id]["rows"]:
                        raise RuntimeError(f"row drift {operation} {arm} {node_id}")
                    observations[arm][block].append(elapsed)
                    del rows
            if (block + 1) % 5 == 0:
                log(f"  {operation}: block {block + 1}/{args.blocks}")

        per_block = {
            a: [percentile(observations[a][b], 50) for b in range(args.blocks)]
            for a in order
        }
        per_block_mean = {
            a: [statistics.mean(observations[a][b]) for b in range(args.blocks)]
            for a in order
        }
        entry: dict[str, Any] = {"arms": {}, "avg_rows": round(
            statistics.mean(truth[n]["rows"] for n in op_inputs), 3)}
        for arm in order:
            flat = [v for block in observations[arm] for v in block]
            deltas = [
                (per_block[arm][b] - per_block[baseline][b])
                / per_block[baseline][b] * 100.0
                for b in range(args.blocks)
            ]
            deltas_mean = [
                (per_block_mean[arm][b] - per_block_mean[baseline][b])
                / per_block_mean[baseline][b] * 100.0
                for b in range(args.blocks)
            ]
            sd = statistics.stdev(deltas) if args.blocks > 1 else 0.0
            entry["arms"][arm] = {
                "n": len(flat),
                "p50_ms": round(percentile(flat, 50), 6),
                "p95_ms": round(percentile(flat, 95), 6),
                "mean_ms": round(statistics.mean(flat), 6),
                "per_block_p50_ms": [round(v, 6) for v in per_block[arm]],
                "paired_delta_p50_pct_mean": round(statistics.mean(deltas), 4),
                "paired_delta_p50_pct_sd": round(sd, 4),
                "paired_delta_p50_pct_sem": round(
                    sd / (args.blocks ** 0.5), 4),
                "paired_delta_mean_pct_mean": round(
                    statistics.mean(deltas_mean), 4),
                "blocks_faster": sum(1 for d in deltas if d < 0),
                "blocks": args.blocks,
            }
        entry["raw_ms"] = {
            a: [[round(v, 6) for v in b] for b in observations[a]] for a in order
        }
        entry["verified_outputs_identical"] = True
        output["results"][operation] = entry
        out_path.write_text(json.dumps(output, indent=1))
        log(f"{operation}: done")

    output["run"]["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    output["run"]["loadavg_end"] = os.getloadavg()
    output["run"]["status"] = "complete"
    out_path.write_text(json.dumps(output, indent=1))

    for operation in operations:
        entry = output["results"][operation]
        print(f"\n=== {operation} (avg_rows={entry['avg_rows']}) ===")
        for arm in order:
            a = entry["arms"][arm]
            print(f"  {arm:10s} p50={a['p50_ms']:9.4f} p95={a['p95_ms']:9.4f} "
                  f"dP50={a['paired_delta_p50_pct_mean']:+7.3f}% "
                  f"+-{a['paired_delta_p50_pct_sem']:.3f} (sem) "
                  f"[{a['blocks_faster']}/{a['blocks']} blocks faster]")
    client.close()


if __name__ == "__main__":
    main()
