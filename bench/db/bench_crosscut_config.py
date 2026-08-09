#!/usr/bin/env python3
"""Price cross-cutting configuration levers for the four ConDB reads.

Scope: things that need no change to mongod and no application redesign --
connection string / transport, driver options (wire compression, cursor type,
retryReads, read concern), and query options that are not part of the result
contract (the pinned index hint).

Design constraints this file exists to satisfy:

* every arm is measured in the same process, against the same inputs, in a
  paired interleaved rotation, so an arm never systematically gets the warm or
  the cold position;
* the reported statistic is the mean of per-block paired deltas, together with
  the observed block-to-block spread, so no effect smaller than the noise can
  be claimed;
* every arm's output is verified element-wise against the baseline arm before
  any timing happens;
* raw per-observation timings are written out, not just summaries.

The baseline arm reproduces exactly what ``bench_all_ops_layouts.py`` does for
MongoDB: ``mongodb://localhost:57017`` and the pinned hints.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import random
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

MONGO_NODES = "layout2_view"
MONGO_TEXT = "layout_shared_text"
MONGO_SUBTREE_INDEX = "layout2_rootcause_exact_cover"
PG_PATH_NODES = "layout2_pg_view"
PG_TEXT = "layout_shared_pg_text"

MONGO_PUBLISHED = "mongodb://localhost:57017"
MONGO_BRIDGE = "mongodb://172.17.0.3:27017"
PG_PUBLISHED = "host=localhost port=55432 dbname=bench user=postgres password=bench"
PG_BRIDGE = "host=172.17.0.2 port=5432 dbname=bench user=postgres password=bench"

TREE_ID = "base"
OPERATIONS = ("get_node", "get_children", "get_subtree", "get_entity")

Rows = list[tuple[Any, ...]]
Query = Callable[[str, str], Rows]


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def normalize(rows: Iterable[Sequence[Any]]) -> Rows:
    return [tuple("" if v is None else v for v in row) for row in rows]


def fingerprint(rows: Sequence[Sequence[Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        for value in row:
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return ordered[index]


# --------------------------------------------------------------------------
# arm definitions
# --------------------------------------------------------------------------

# name -> (kind, connection kwargs, behaviour flags)
ARM_SPECS: dict[str, dict[str, Any]] = {
    # --- the published baseline: exactly bench_all_ops_layouts.py ---
    "mongo_base": {"kind": "mongo", "uri": MONGO_PUBLISHED, "hint": True},
    # --- transport ---
    "mongo_bridge": {"kind": "mongo", "uri": MONGO_BRIDGE, "hint": True},
    # --- query option: drop the pinned hint ---
    "mongo_nohint": {"kind": "mongo", "uri": MONGO_PUBLISHED, "hint": False},
    "mongo_bridge_nohint": {"kind": "mongo", "uri": MONGO_BRIDGE, "hint": False},
    # --- wire compression (transport held at the published baseline) ---
    "mongo_snappy": {
        "kind": "mongo", "uri": MONGO_PUBLISHED, "hint": True,
        "opts": {"compressors": "snappy"},
    },
    "mongo_zstd": {
        "kind": "mongo", "uri": MONGO_PUBLISHED, "hint": True,
        "opts": {"compressors": "zstd"},
    },
    "mongo_zlib1": {
        "kind": "mongo", "uri": MONGO_PUBLISHED, "hint": True,
        "opts": {"compressors": "zlib", "zlibCompressionLevel": 1},
    },
    # --- driver / topology options ---
    "mongo_directconn": {
        "kind": "mongo", "uri": MONGO_PUBLISHED, "hint": True,
        "opts": {"directConnection": True},
    },
    "mongo_noretry": {
        "kind": "mongo", "uri": MONGO_PUBLISHED, "hint": True,
        "opts": {"retryReads": False},
    },
    "mongo_rc_available": {
        "kind": "mongo", "uri": MONGO_PUBLISHED, "hint": True,
        "read_concern": "available",
    },
    "mongo_nomonitor": {
        # push topology monitoring out of the way: 1 h heartbeat, no RTT probes
        "kind": "mongo", "uri": MONGO_PUBLISHED, "hint": True,
        "opts": {"directConnection": True, "heartbeatFrequencyMS": 3600000},
    },
    "mongo_exhaust": {
        "kind": "mongo", "uri": MONGO_PUBLISHED, "hint": True, "exhaust": True,
    },
    # --- every connection-level lever at once, hint left pinned so that this
    #     arm is purely connection-level and composes with the query-level
    #     hint result measured in bench_crosscut_queryopts.py ---
    "mongo_stacked": {
        "kind": "mongo", "uri": MONGO_BRIDGE, "hint": True,
        "opts": {"directConnection": True, "retryReads": False,
                 "heartbeatFrequencyMS": 3600000},
    },
    "mongo_bridge_zstd": {
        "kind": "mongo", "uri": MONGO_BRIDGE, "hint": True,
        "opts": {"compressors": "zstd"},
    },
    "mongo_bridge_exhaust": {
        "kind": "mongo", "uri": MONGO_BRIDGE, "hint": True, "exhaust": True,
    },
    "mongo_bridge_exhaust_zstd": {
        "kind": "mongo", "uri": MONGO_BRIDGE, "hint": True, "exhaust": True,
        "opts": {"compressors": "zstd"},
    },
    # --- PostgreSQL reference, both transports ---
    "pg_base": {"kind": "pg", "dsn": PG_PUBLISHED},
    "pg_bridge": {"kind": "pg", "dsn": PG_BRIDGE},
}


def build_mongo_arm(spec: dict[str, Any]) -> tuple[Any, dict[str, Query]]:
    from pymongo import MongoClient, CursorType
    from pymongo.read_concern import ReadConcern

    opts = dict(spec.get("opts", {}))
    client = MongoClient(spec["uri"], **opts)
    client.admin.command("ping")

    database = client["bench"]
    if spec.get("read_concern"):
        database = database.with_options(
            read_concern=ReadConcern(spec["read_concern"])
        )
    nodes = database[MONGO_NODES]
    text = database[MONGO_TEXT]
    hinted = spec.get("hint", True)
    exhaust = spec.get("exhaust", False)
    cursor_type = CursorType.EXHAUST if exhaust else CursorType.NON_TAILABLE

    node_projection = {
        "_id": 0, "node_id": 1, "parent_id": 1, "depth": 1, "title": 1,
        "summary": 1, "start_index": 1, "end_index": 1,
    }

    def get_node(tree_id: str, node_id: str) -> Rows:
        kwargs = {"hint": "allops_tree_node"} if hinted else {}
        row = nodes.find_one(
            {"tree_id": tree_id, "node_id": node_id}, node_projection, **kwargs
        )
        if row is None:
            return []
        return normalize([(
            row.get("node_id"), row.get("parent_id"), row.get("depth"),
            row.get("title"), row.get("summary"),
            row.get("start_index"), row.get("end_index"),
        )])

    def get_children(tree_id: str, node_id: str) -> Rows:
        cursor = nodes.find(
            {"tree_id": tree_id, "parent_id": node_id},
            {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
            cursor_type=cursor_type,
        ).sort([("path", 1), ("node_id", 1)])
        if hinted:
            cursor = cursor.hint("allops_tree_parent_path")
        return normalize(
            (row.get("node_id"), row.get("title"), row.get("summary"))
            for row in cursor
        )

    def get_subtree(tree_id: str, node_id: str) -> Rows:
        kwargs = {"hint": "allops_tree_node"} if hinted else {}
        root = nodes.find_one(
            {"tree_id": tree_id, "node_id": node_id}, {"_id": 0, "path": 1},
            **kwargs
        )
        if root is None:
            return []
        lower, upper = root["path"] + "/", root["path"] + "0"
        cursor = nodes.find(
            {"path": {"$gte": lower, "$lt": upper}},
            {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
            cursor_type=cursor_type,
        ).sort([("path", 1), ("node_id", 1)])
        if hinted:
            cursor = cursor.hint(MONGO_SUBTREE_INDEX)
        return normalize(
            (row.get("node_id"), row.get("title"), row.get("summary"))
            for row in cursor
        )

    def get_entity(_: str, node_id: str) -> Rows:
        row = text.find_one({"_id": node_id}, {"_id": 1, "text": 1})
        return normalize([(node_id, row.get("text"))]) if row else []

    return client, {
        "get_node": get_node, "get_children": get_children,
        "get_subtree": get_subtree, "get_entity": get_entity,
    }


def build_pg_arm(spec: dict[str, Any]) -> tuple[Any, dict[str, Query]]:
    import psycopg

    connection = psycopg.connect(spec["dsn"], autocommit=True)

    def get_node(tree_id: str, node_id: str) -> Rows:
        return normalize(connection.execute(
            f"SELECT node_id,parent_id,depth,title,summary,start_index,end_index"
            f" FROM {PG_PATH_NODES} WHERE tree_id=%s AND node_id=%s",
            (tree_id, node_id),
        ).fetchall())

    def get_children(tree_id: str, node_id: str) -> Rows:
        return normalize(connection.execute(
            f"SELECT node_id,title,summary FROM {PG_PATH_NODES}"
            f" WHERE tree_id=%s AND parent_id=%s ORDER BY path,node_id",
            (tree_id, node_id),
        ).fetchall())

    def get_subtree(tree_id: str, node_id: str) -> Rows:
        root = connection.execute(
            f"SELECT path FROM {PG_PATH_NODES} WHERE tree_id=%s AND node_id=%s",
            (tree_id, node_id),
        ).fetchone()
        if root is None:
            return []
        lower, upper = root[0] + "/", root[0] + "0"
        return normalize(connection.execute(
            f"SELECT node_id,title,summary FROM {PG_PATH_NODES}"
            f" WHERE tree_id=%s AND path>=%s AND path<%s ORDER BY path,node_id",
            (tree_id, lower, upper),
        ).fetchall())

    def get_entity(_: str, node_id: str) -> Rows:
        return normalize(connection.execute(
            f"SELECT node_id,text FROM {PG_TEXT} WHERE node_id=%s", (node_id,)
        ).fetchall())

    return connection, {
        "get_node": get_node, "get_children": get_children,
        "get_subtree": get_subtree, "get_entity": get_entity,
    }


def build_arm(name: str) -> tuple[Any, dict[str, Query]]:
    # "name@tag" is an alias for the same spec, used to build null-control arms
    # that are byte-identical to another arm but carry their own client.
    spec = ARM_SPECS[name.split("@", 1)[0]]
    if spec["kind"] == "mongo":
        return build_mongo_arm(spec)
    return build_pg_arm(spec)


# --------------------------------------------------------------------------
# campaign
# --------------------------------------------------------------------------

def load_inputs(expected: Path, pg_conn: Any, point_n: int, tree_n: int) -> dict[str, list[str]]:
    document = json.loads(expected.read_text())
    tree_ids = [
        sample["path"].rsplit("/", 1)[-1]
        for sample in document["samples"][:tree_n]
    ]
    entity_ids = [
        row[0] for row in pg_conn.execute(
            f"SELECT node_id FROM {PG_TEXT} ORDER BY node_id LIMIT %s",
            (point_n,),
        ).fetchall()
    ]
    return {
        "get_node": entity_ids, "get_children": tree_ids,
        "get_subtree": tree_ids, "get_entity": entity_ids,
    }


def verify(arms: dict[str, dict[str, Query]], operation: str,
           inputs: list[str], baseline: str) -> dict[str, dict[str, Any]]:
    """Element-wise equality of every arm against the baseline arm."""
    truth: dict[str, dict[str, Any]] = {}
    for node_id in inputs:
        rows = arms[baseline][operation](TREE_ID, node_id)
        truth[node_id] = {"rows": len(rows), "fp": fingerprint(rows)}
    for name, queries in arms.items():
        if name == baseline:
            continue
        for node_id in inputs:
            rows = queries[operation](TREE_ID, node_id)
            want = truth[node_id]
            if len(rows) != want["rows"] or fingerprint(rows) != want["fp"]:
                raise RuntimeError(
                    f"output mismatch: {operation} arm={name} node={node_id} "
                    f"{len(rows)} vs {want['rows']}"
                )
    return truth


def run_operation(arms: dict[str, dict[str, Query]], operation: str,
                  inputs: list[str], truth: dict[str, dict[str, Any]],
                  blocks: int, order: list[str],
                  reconnect: Callable[[], None] | None = None,
                  warmup: int = 0, rotate: bool = False) -> dict[str, Any]:
    # observations[arm][block] = list of per-input ms
    # Order arms by an independent shuffle per (block, input) from a fixed
    # seed, so no arm has a fixed predecessor.  A cyclic rotation -- what
    # bench_all_ops_layouts.py does -- keeps the cyclic order fixed instead.
    # Measured head to head over five byte-identical arms on a shared
    # connection, the two designs are indistinguishable (both within +-0.2%,
    # armorder_rotation.json vs armorder_shuffled.json), so the shuffle is
    # chosen for cleanliness rather than because rotation was shown to bias.
    rng = random.Random(0x5EED)
    observations = {name: [[] for _ in range(blocks)] for name in order}
    for block in range(blocks):
        if reconnect is not None:
            # Each connection carries a persistent latency penalty or bonus of
            # up to 23-26% in P50 (bench_crosscut_connection_lottery.py).  A
            # null control of six byte-identical arms, each holding one
            # connection for a whole run, had one arm drift monotonically to
            # +20%.  Re-drawing every connection each block turns that lottery
            # into within-arm noise instead of a between-arm bias, at the cost
            # of needing many blocks: the per-block paired delta then has a
            # standard deviation of roughly 5-15%.
            reconnect()
            for name in order:
                for node_id in inputs[:warmup]:
                    arms[name][operation](TREE_ID, node_id)
        for index, node_id in enumerate(inputs):
            if rotate:
                # Reproduce the arm ordering used by bench_all_ops_layouts.py,
                # so that the bias it carries can be measured rather than
                # assumed.
                pivot = (block + index) % len(order)
                sequence = order[pivot:] + order[:pivot]
            else:
                sequence = list(order)
                rng.shuffle(sequence)
            for name in sequence:
                gc.disable()
                try:
                    started = time.perf_counter()
                    rows = arms[name][operation](TREE_ID, node_id)
                    elapsed = (time.perf_counter() - started) * 1000.0
                finally:
                    gc.enable()
                if len(rows) != truth[node_id]["rows"]:
                    raise RuntimeError(
                        f"row-count drift: {operation} {name} {node_id}"
                    )
                observations[name][block].append(elapsed)
                del rows
        log(f"  {operation}: block {block + 1}/{blocks} done")
    return observations


def analyze(observations: dict[str, list[list[float]]], baseline: str) -> dict[str, Any]:
    names = list(observations)
    blocks = len(observations[baseline])
    per_block_p50 = {
        name: [percentile(observations[name][b], 50) for b in range(blocks)]
        for name in names
    }
    per_block_mean = {
        name: [statistics.mean(observations[name][b]) for b in range(blocks)]
        for name in names
    }
    result: dict[str, Any] = {"blocks": blocks, "arms": {}}
    base_p50 = per_block_p50[baseline]
    base_mean = per_block_mean[baseline]
    for name in names:
        flat = [v for block in observations[name] for v in block]
        deltas_p50 = [
            (per_block_p50[name][b] - base_p50[b]) / base_p50[b] * 100.0
            for b in range(blocks)
        ]
        deltas_mean = [
            (per_block_mean[name][b] - base_mean[b]) / base_mean[b] * 100.0
            for b in range(blocks)
        ]
        wins = sum(1 for d in deltas_p50 if d < 0)
        result["arms"][name] = {
            "n": len(flat),
            "p50_ms": round(percentile(flat, 50), 6),
            "p95_ms": round(percentile(flat, 95), 6),
            "p99_ms": round(percentile(flat, 99), 6),
            "mean_ms": round(statistics.mean(flat), 6),
            "per_block_p50_ms": [round(v, 6) for v in per_block_p50[name]],
            "per_block_mean_ms": [round(v, 6) for v in per_block_mean[name]],
            "paired_delta_p50_pct_mean": round(statistics.mean(deltas_p50), 4),
            "paired_delta_p50_pct_sd": round(
                statistics.stdev(deltas_p50) if blocks > 1 else 0.0, 4),
            "paired_delta_p50_pct_min": round(min(deltas_p50), 4),
            "paired_delta_p50_pct_max": round(max(deltas_p50), 4),
            "paired_delta_mean_pct_mean": round(statistics.mean(deltas_mean), 4),
            "paired_delta_mean_pct_sd": round(
                statistics.stdev(deltas_mean) if blocks > 1 else 0.0, 4),
            "blocks_faster_than_baseline": wins,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arms", required=True,
                        help="comma-separated arm names; first is the baseline")
    parser.add_argument("--ops", default="get_node,get_children,get_entity")
    parser.add_argument("--blocks", type=int, default=12)
    parser.add_argument("--point-inputs", type=int, default=500)
    parser.add_argument("--tree-inputs", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=200,
                        help="warm-up calls per arm per operation")
    parser.add_argument("--block-warmup", type=int, default=None,
                        help="warm-up calls per arm after each reconnection "
                             "(defaults to --warmup)")
    parser.add_argument(
        "--expected",
        default="bench/db/runs/report_3eng_20260716/layout_2v3_postgres_10m_final.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--rotate", action="store_true",
                        help="order arms by cyclic rotation instead of an "
                             "independent shuffle, reproducing the design of "
                             "bench_all_ops_layouts.py")
    parser.add_argument("--hold-connections", action="store_true",
                        help="keep one connection per arm for the whole run "
                             "(reproduces the connection-placement confound)")
    args = parser.parse_args()

    order = [a.strip() for a in args.arms.split(",") if a.strip()]
    operations = [o.strip() for o in args.ops.split(",") if o.strip()]
    baseline = order[0]
    for name in order:
        if name.split("@", 1)[0] not in ARM_SPECS:
            parser.error(f"unknown arm {name}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    log(f"building arms: {order}")
    handles: dict[str, Any] = {}
    arms: dict[str, dict[str, Query]] = {}

    def rebuild() -> None:
        for existing in handles.values():
            try:
                existing.close()
            except Exception:
                pass
        for name in order:
            handles[name], arms[name] = build_arm(name)

    rebuild()

    import psycopg
    inputs_conn = psycopg.connect(PG_PUBLISHED, autocommit=True)
    inputs = load_inputs(Path(args.expected), inputs_conn,
                         args.point_inputs, args.tree_inputs)
    inputs_conn.close()

    from pymongo import MongoClient
    probe = MongoClient(MONGO_PUBLISHED)
    server = probe.admin.command("serverStatus")
    output: dict[str, Any] = {
        "run": {
            "label": args.label,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "baseline_arm": baseline,
            "arms": order,
            "operations": operations,
            "blocks": args.blocks,
            "arm_order": "cyclic_rotation" if args.rotate else "shuffled",
            "connections": "held" if args.hold_connections else "fresh_per_block",
            "point_inputs": args.point_inputs,
            "tree_inputs": args.tree_inputs,
            "host": platform.node(),
            "loadavg": os.getloadavg(),
            "mongodb_version": server["version"],
            "mongodb_uptime_s": server["uptime"],
            "profiling_slowms": probe["bench"].command("profile", -1)["slowms"],
        },
        "arm_specs": {name: ARM_SPECS[name.split("@", 1)[0]] for name in order},
        "results": {},
    }
    net_before = server["network"]["compression"]

    for operation in operations:
        op_inputs = inputs[operation]
        log(f"{operation}: warming {args.warmup} calls x {len(order)} arms")
        for name in order:
            for node_id in op_inputs[:args.warmup]:
                arms[name][operation](TREE_ID, node_id)
        log(f"{operation}: verifying {len(op_inputs)} inputs x {len(order)} arms")
        truth = verify(arms, operation, op_inputs, baseline)
        log(f"{operation}: timing {len(op_inputs)} inputs x {len(order)} arms "
            f"x {args.blocks} blocks")
        observations = run_operation(
            arms, operation, op_inputs, truth, args.blocks, order,
            reconnect=None if args.hold_connections else rebuild,
            warmup=min(args.block_warmup if args.block_warmup is not None
                       else args.warmup, len(op_inputs)),
            rotate=args.rotate)
        output["results"][operation] = analyze(observations, baseline)
        output["results"][operation]["raw_ms"] = {
            name: [[round(v, 6) for v in block] for block in observations[name]]
            for name in order
        }
        output["results"][operation]["verified_outputs_identical"] = True
        output["results"][operation]["avg_rows"] = round(
            statistics.mean(truth[n]["rows"] for n in op_inputs), 3)
        out_path.write_text(json.dumps(output, indent=1))
        log(f"{operation}: done")

    server_after = probe.admin.command("serverStatus")
    output["run"]["network_compression_before"] = net_before
    output["run"]["network_compression_after"] = server_after["network"]["compression"]
    output["run"]["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    output["run"]["loadavg_end"] = os.getloadavg()
    output["run"]["status"] = "complete"
    out_path.write_text(json.dumps(output, indent=1))

    for operation in operations:
        print(f"\n=== {operation} (avg_rows="
              f"{output['results'][operation]['avg_rows']}) ===")
        for name in order:
            a = output["results"][operation]["arms"][name]
            print(f"  {name:22s} p50={a['p50_ms']:9.4f} p95={a['p95_ms']:9.4f} "
                  f"paired_dP50={a['paired_delta_p50_pct_mean']:+8.3f}% "
                  f"(sd {a['paired_delta_p50_pct_sd']:6.3f}, "
                  f"{a['blocks_faster_than_baseline']}/{args.blocks} blocks faster)")

    for name in order:
        try:
            handles[name].close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
