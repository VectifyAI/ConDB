#!/usr/bin/env python3
"""Depth expansion of get_children: BFS vs DFS vs $graphLookup vs path+depth.

Stock MongoDB has no first-class tree walk on find(). The one recursive operator
is $graphLookup, and its executor is explicitly a breadth-first search
(GraphLookUpStage::doBreadthFirstSearch). This script prices that operator
against the two walks the application can already issue with get_children, and
against the materialized-path range the subtree arm uses.

The public ConDB API's get_subtree / expand / format_tree_view default to depth
1 or 2. Beam retrieval is a BFS that calls get_children_many on the frontier
each turn. The storage-level get_subtree benchmark is the other extreme: an
unbounded path range with no depth filter.

All arms return descendant node_ids only (the start node is excluded), so the
sets can be compared. Path order is DFS/preorder; BFS order is level order;
$graphLookup order is unspecified.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

from pymongo import MongoClient
from pymongo.collection import Collection


TREE_ID = "depth0"
PROJ = {"_id": 0, "node_id": 1, "parent_id": 1, "path": 1, "depth": 1, "title": 1, "summary": 1}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return ordered[index]


def build_tree(n_nodes: int, fanout: tuple[int, int], max_depth: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    docs: list[dict] = []
    # BFS grow. Root is depth 0.
    root_id = "n000000"
    docs.append({
        "tree_id": TREE_ID,
        "node_id": root_id,
        "parent_id": None,
        "path": f"/{root_id}",
        "depth": 0,
        "title": "root",
        "summary": "root-summary",
        "slot": 0,
    })
    frontier = [0]  # indices into docs
    next_id = 1
    while next_id < n_nodes and frontier:
        parent_idx = frontier.pop(0)
        parent = docs[parent_idx]
        if parent["depth"] >= max_depth:
            continue
        k = rng.randint(fanout[0], fanout[1])
        remaining = n_nodes - next_id
        k = min(k, remaining)
        for slot in range(k):
            nid = f"n{next_id:06d}"
            child = {
                "tree_id": TREE_ID,
                "node_id": nid,
                "parent_id": parent["node_id"],
                "path": f"{parent['path']}/{nid}",
                "depth": parent["depth"] + 1,
                "title": f"t{next_id}",
                "summary": f"s{next_id}" * 4,
                "slot": slot,
            }
            docs.append(child)
            frontier.append(len(docs) - 1)
            next_id += 1
            if next_id >= n_nodes:
                break
    return docs


def ingest(coll: Collection, docs: list[dict]) -> None:
    coll.drop()
    batch = 2000
    for i in range(0, len(docs), batch):
        coll.insert_many(docs[i:i + batch], ordered=False)
    coll.create_index([("tree_id", 1), ("node_id", 1)], unique=True, name="allops_tree_node")
    coll.create_index(
        [("tree_id", 1), ("parent_id", 1), ("path", 1), ("node_id", 1)],
        name="allops_tree_parent_path",
    )
    coll.create_index([("tree_id", 1), ("path", 1)], name="allops_tree_path")
    coll.create_index(
        [("path", 1), ("node_id", 1), ("title", 1), ("summary", 1)],
        name="layout2_rootcause_exact_cover",
    )


def get_children(coll: Collection, parent_id: str) -> list[dict]:
    return list(
        coll.find(
            {"tree_id": TREE_ID, "parent_id": parent_id},
            PROJ,
        )
        .sort([("path", 1), ("node_id", 1)])
        .hint("allops_tree_parent_path")
    )


def get_children_many(coll: Collection, parent_ids: list[str]) -> dict[str, list[dict]]:
    unique = list(dict.fromkeys(parent_ids))
    out: dict[str, list[dict]] = {p: [] for p in unique}
    if not unique:
        return out
    cursor = (
        coll.find(
            {"tree_id": TREE_ID, "parent_id": {"$in": unique}},
            PROJ,
        )
        .sort([("parent_id", 1), ("path", 1), ("node_id", 1)])
        .hint("allops_tree_parent_path")
    )
    for row in cursor:
        out[row["parent_id"]].append(row)
    return out


def bfs_scalar(coll: Collection, start: dict, max_depth: int | None) -> list[str]:
    """One find per parent, level by level. Depth 1 == get_children."""
    ids: list[str] = []
    frontier = [start["node_id"]]
    depth = 0
    while frontier:
        if max_depth is not None and depth >= max_depth:
            break
        nxt: list[str] = []
        for parent in frontier:
            for child in get_children(coll, parent):
                ids.append(child["node_id"])
                nxt.append(child["node_id"])
        frontier = nxt
        depth += 1
    return ids


def bfs_level(coll: Collection, start: dict, max_depth: int | None) -> list[str]:
    """One $in find per BFS level. Depth 1 == get_children."""
    ids: list[str] = []
    frontier = [start["node_id"]]
    depth = 0
    while frontier:
        if max_depth is not None and depth >= max_depth:
            break
        by_parent = get_children_many(coll, frontier)
        nxt: list[str] = []
        for parent in frontier:
            for child in by_parent[parent]:
                ids.append(child["node_id"])
                nxt.append(child["node_id"])
        frontier = nxt
        depth += 1
    return ids


def dfs_scalar(coll: Collection, start: dict, max_depth: int | None) -> list[str]:
    """Recursive children walk, preorder. Same primitive, different schedule."""
    ids: list[str] = []

    def walk(parent_id: str, remaining: int | None) -> None:
        if remaining is not None and remaining <= 0:
            return
        children = get_children(coll, parent_id)
        next_remaining = None if remaining is None else remaining - 1
        for child in children:
            ids.append(child["node_id"])
            walk(child["node_id"], next_remaining)

    walk(start["node_id"], max_depth)
    return ids


def path_depth(coll: Collection, start: dict, max_depth: int | None) -> list[str]:
    lower = start["path"] + "/"
    upper = start["path"] + "0"
    filt: dict = {"path": {"$gte": lower, "$lt": upper}}
    if max_depth is not None:
        filt["depth"] = {"$lte": start["depth"] + max_depth}
    cursor = (
        coll.find(filt, {"_id": 0, "node_id": 1})
        .sort([("path", 1), ("node_id", 1)])
        .hint("layout2_rootcause_exact_cover")
    )
    return [row["node_id"] for row in cursor]


def graphlookup(coll: Collection, start: dict, max_depth: int | None) -> list[str]:
    spec: dict = {
        "from": coll.name,
        "startWith": start["node_id"],
        "connectFromField": "node_id",
        "connectToField": "parent_id",
        "as": "desc",
        "restrictSearchWithMatch": {"tree_id": TREE_ID},
    }
    if max_depth is not None:
        spec["maxDepth"] = max_depth - 1  # 0 = direct children only
    pipeline = [
        {"$match": {"tree_id": TREE_ID, "node_id": start["node_id"]}},
        {"$graphLookup": spec},
        {"$project": {"_id": 0, "ids": "$desc.node_id"}},
    ]
    docs = list(coll.aggregate(pipeline, allowDiskUse=True))
    if not docs:
        return []
    return list(docs[0].get("ids") or [])


ARMS = {
    "bfs_scalar": bfs_scalar,
    "bfs_level": bfs_level,
    "dfs_scalar": dfs_scalar,
    "path_depth": path_depth,
    "graphlookup": graphlookup,
}


def opcounters(client: MongoClient) -> dict:
    s = client.admin.command("serverStatus")
    oc = s["opcounters"]
    metrics = s.get("metrics", {}).get("commands", {})
    return {
        "query": oc.get("query", 0),
        "command": oc.get("command", 0),
        "getmore": oc.get("getmore", 0),
        "aggregate": metrics.get("aggregate", {}).get("total", 0),
        "find": metrics.get("find", {}).get("total", 0),
    }


def delta_counters(before: dict, after: dict) -> dict:
    return {k: after[k] - before[k] for k in before}


def explain_arm(coll: Collection, start: dict, depth: int) -> dict:
    """One-shot explains, not timed."""
    out: dict = {}
    children_ex = coll.find(
        {"tree_id": TREE_ID, "parent_id": start["node_id"]},
        PROJ,
    ).sort([("path", 1), ("node_id", 1)]).hint("allops_tree_parent_path").explain()
    out["get_children"] = _winning(children_ex)

    lower, upper = start["path"] + "/", start["path"] + "0"
    path_ex = coll.find(
        {"path": {"$gte": lower, "$lt": upper}, "depth": {"$lte": start["depth"] + depth}},
        {"_id": 0, "node_id": 1},
    ).sort([("path", 1), ("node_id", 1)]).hint("layout2_rootcause_exact_cover").explain()
    out["path_depth"] = _winning(path_ex)
    examined = out["path_depth"].get("totalKeysExamined") or 0
    returned = out["path_depth"].get("nReturned") or 0
    out["path_overscan"] = (examined / returned) if returned else None

    spec = {
        "from": coll.name,
        "startWith": start["node_id"],
        "connectFromField": "node_id",
        "connectToField": "parent_id",
        "as": "desc",
        "maxDepth": depth - 1,
        "restrictSearchWithMatch": {"tree_id": TREE_ID},
    }
    gl_ex = coll.database.command(
        "aggregate",
        coll.name,
        pipeline=[
            {"$match": {"tree_id": TREE_ID, "node_id": start["node_id"]}},
            {"$graphLookup": spec},
            {"$project": {"_id": 0, "n": {"$size": "$desc"}}},
        ],
        explain=True,
        cursor={},
    )
    blob = json.dumps(gl_ex, default=str)
    out["graphlookup_uses_ixscan"] = "IXSCAN" in blob
    out["graphlookup_uses_collscan"] = "COLLSCAN" in blob
    out["graphlookup_uses_parent_index"] = "allops_tree_parent_path" in blob
    out["graphlookup_plan_fingerprint"] = sorted({
        token for token in ("IXSCAN", "COLLSCAN", "FETCH", "PROJECTION_SIMPLE",
                            "EXPRESS_IXSCAN", "allops_tree_parent_path",
                            "allops_tree_node", "$graphLookup")
        if token in blob
    })
    return out


def _winning(explain: dict) -> dict:
    qw = explain.get("queryPlanner", {}).get("winningPlan", {})
    exec_stats = explain.get("executionStats", {})
    stages: list[str] = []
    index_name = None

    def walk(node: dict) -> None:
        nonlocal index_name
        if not isinstance(node, dict):
            return
        if "stage" in node:
            stages.append(node["stage"])
        if node.get("indexName") and index_name is None:
            index_name = node["indexName"]
        if "inputStage" in node:
            walk(node["inputStage"])
        for s in node.get("inputStages", []):
            walk(s)

    walk(qw)
    walk(exec_stats.get("executionStages", {}))
    return {
        "stages": stages,
        "index": index_name,
        "nReturned": exec_stats.get("nReturned"),
        "totalDocsExamined": exec_stats.get("totalDocsExamined"),
        "totalKeysExamined": exec_stats.get("totalKeysExamined"),
    }


def pick_cohort(docs: list[dict], k: int, seed: int) -> list[dict]:
    """Internal nodes at depth 0-2, preferring larger fan-out subtrees."""
    by_parent: dict[str | None, int] = defaultdict(int)
    for d in docs:
        by_parent[d["parent_id"]] += 1
    candidates = [d for d in docs if d["depth"] <= 2 and by_parent[d["node_id"]] > 0]
    rng = random.Random(seed)
    rng.shuffle(candidates)
    # Prefer nodes that actually have children.
    candidates.sort(key=lambda d: (-by_parent[d["node_id"]], d["depth"], d["node_id"]))
    return candidates[:k]


def time_arm(fn, coll, start, depth, repeats: int) -> dict:
    ids = fn(coll, start, depth)  # warmup + row count
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        ids = fn(coll, start, depth)
        samples.append((time.perf_counter() - t0) * 1e3)
    return {
        "n": len(ids),
        "ids": ids,
        "wall_ms": samples,
        "p50_ms": percentile(samples, 50),
        "p95_ms": percentile(samples, 95),
        "mean_ms": statistics.fmean(samples),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="mongodb://localhost:57017/?directConnection=true")
    parser.add_argument("--nodes", type=int, default=80_000)
    parser.add_argument("--fanout-min", type=int, default=6)
    parser.add_argument("--fanout-max", type=int, default=14)
    parser.add_argument("--tree-depth", type=int, default=8)
    parser.add_argument("--cohort", type=int, default=32)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--depths", default="1,2,3")
    parser.add_argument("--unbounded", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    depths: list[int | None] = [int(x) for x in args.depths.split(",") if x]
    if args.unbounded:
        depths.append(None)

    client = MongoClient(args.uri, maxPoolSize=1, minPoolSize=1)
    coll = client["bench"]["layout2_view"]
    log(f"hello {client.admin.command('buildInfo')['version']}")

    log(f"build tree n={args.nodes} fanout={args.fanout_min}-{args.fanout_max} depth={args.tree_depth}")
    docs = build_tree(args.nodes, (args.fanout_min, args.fanout_max), args.tree_depth, seed=7)
    log(f"generated {len(docs)} docs, ingesting")
    ingest(coll, docs)
    assert coll.count_documents({}) == len(docs)

    by_id = {d["node_id"]: d for d in docs}
    children_of: dict[str, list[str]] = defaultdict(list)
    for d in docs:
        if d["parent_id"] is not None:
            children_of[d["parent_id"]].append(d["node_id"])

    def expected(start: dict, max_depth: int | None) -> set[str]:
        out: set[str] = set()
        q = deque([(start["node_id"], 0)])
        while q:
            nid, dep = q.popleft()
            if max_depth is not None and dep >= max_depth:
                continue
            for cid in children_of[nid]:
                out.add(cid)
                q.append((cid, dep + 1))
        return out

    cohort = pick_cohort(docs, args.cohort, seed=11)
    log(f"cohort {len(cohort)} depths {[d['depth'] for d in cohort[:8]]}...")

    explains = explain_arm(coll, cohort[0], 2)

    results: dict = {
        "mongo_version": client.admin.command("buildInfo")["version"],
        "n_docs": len(docs),
        "fanout": [args.fanout_min, args.fanout_max],
        "tree_depth": args.tree_depth,
        "cohort": len(cohort),
        "repeats": args.repeats,
        "explains": explains,
        "depths": {},
        "correctness": {},
        "counters": {},
    }

    for depth in depths:
        label = "unbounded" if depth is None else f"d{depth}"
        log(f"=== depth {label} ===")
        per_arm: dict[str, dict] = {name: {"per_root": []} for name in ARMS}
        mismatches = {name: 0 for name in ARMS}

        for i, start in enumerate(cohort):
            want = expected(start, depth)
            got_sets: dict[str, set[str]] = {}
            for name, fn in ARMS.items():
                if depth is None and name == "graphlookup":
                    # Array result can exceed 16 MB on an unbounded subtree.
                    continue
                try:
                    timed = time_arm(fn, coll, start, depth, args.repeats)
                except Exception as exc:
                    log(f"  {name} failed on {start['node_id']}: {type(exc).__name__}: {exc}")
                    per_arm[name]["per_root"].append({"node_id": start["node_id"], "error": str(exc)})
                    mismatches[name] += 1
                    continue
                got = set(timed.pop("ids"))
                got_sets[name] = got
                if got != want:
                    mismatches[name] += 1
                    log(f"  MISMATCH {name} root={start['node_id']} got={len(got)} want={len(want)}")
                timed["node_id"] = start["node_id"]
                timed["root_depth"] = start["depth"]
                timed["want_n"] = len(want)
                per_arm[name]["per_root"].append(timed)
            if i == 0:
                log(f"  root {start['node_id']} depth={start['depth']} want={len(want)} "
                    + " ".join(f"{n}={len(got_sets.get(n, []))}" for n in ARMS))

        for name, payload in per_arm.items():
            walls = [r["p50_ms"] for r in payload["per_root"] if "p50_ms" in r]
            ns = [r["n"] for r in payload["per_root"] if "n" in r]
            payload["summary"] = {
                "roots": len(walls),
                "p50_ms": percentile(walls, 50) if walls else None,
                "p95_ms": percentile(walls, 95) if walls else None,
                "mean_ms": statistics.fmean(walls) if walls else None,
                "median_n": percentile([float(x) for x in ns], 50) if ns else None,
            }
            log(f"  {name:12} p50={payload['summary']['p50_ms']} ms  "
                f"median_n={payload['summary']['median_n']}  mismatches={mismatches[name]}")
        results["depths"][label] = {name: {"summary": p["summary"], "per_root": p["per_root"]}
                                    for name, p in per_arm.items()}
        results["correctness"][label] = mismatches

        # Command-count probe on the first root, one execution each.
        start = cohort[0]
        for name, fn in ARMS.items():
            if depth is None and name == "graphlookup":
                continue
            before = opcounters(client)
            fn(coll, start, depth)
            results.setdefault("counters", {}).setdefault(label, {})[name] = delta_counters(
                before, opcounters(client)
            )

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2, default=str) + "\n")
    log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
