#!/usr/bin/env python3
"""Compare MongoDB two-collection and three-collection subtree layouts.

The comparison changes only where title/summary live:

  two collections
    view = structure + metadata (all node fields except text)
    text = leaf text keyed by node_id

  three collections
    struct = topology/path fields
    meta = title/summary keyed by node_id
    text = the same text collection used by the two-collection layout

The measured query returns the same ordered list of
``(node_id, title, summary)`` for both layouts.  The two-collection path uses
one range query over small view documents.  The three-collection path uses a
covered structure scan, chunked metadata lookups, and an order-preserving
client merge.  Text is ingested once because it is identical and is not read
by either subtree query.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

from bench_databases import flatten


DB = "bench"
VIEW = "layout2_view"
STRUCT = "layout3_struct"
META = "layout3_meta"
TEXT = "layout_shared_text"

STRUCT_FIELDS = ("tree_id", "node_id", "parent_id", "depth", "path")
LEAN = [("path", 1), ("node_id", 1)]
PROJ_VIEW = {"node_id": 1, "title": 1, "summary": 1, "_id": 0}
PROJ_ID = {"node_id": 1, "_id": 0}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def free_gb() -> float:
    return shutil.disk_usage("/").free / 1e9


def ingest(collection, documents, batch_size: int = 10_000) -> tuple[float, int]:
    started = time.perf_counter()
    batch = []
    count = 0
    for document in documents:
        batch.append(document)
        if len(batch) >= batch_size:
            collection.insert_many(batch, ordered=False)
            count += len(batch)
            batch = []
            if count % 2_000_000 == 0:
                log(f"      ... {count:,}")
    if batch:
        collection.insert_many(batch, ordered=False)
        count += len(batch)
    return time.perf_counter() - started, count


def percentile(values: list[float], p: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return round(ordered[index], 3)


def summary(samples: list[dict], latency_key: str) -> dict:
    latencies = [sample[latency_key] for sample in samples]
    rows = [sample["rows"] for sample in samples]
    return {
        "n": len(samples),
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "mean_ms": round(statistics.mean(latencies), 3) if latencies else 0.0,
        "avg_rows": round(statistics.mean(rows), 1) if rows else 0.0,
    }


def fingerprint(rows: list[tuple[str, str, str]]) -> str:
    digest = hashlib.sha256()
    for node_id, title, summary_text in rows:
        for value in (node_id, title, summary_text):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def plan_stages(cursor) -> dict:
    explanation = cursor.explain()
    plan = explanation.get("queryPlanner", {}).get("winningPlan", {})
    stages = []

    def walk(value):
        if isinstance(value, dict):
            if "stage" in value:
                stages.append(value["stage"])
            for key in ("inputStage", "inputStages", "queryPlan"):
                if key in value:
                    walk(value[key])
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(plan)
    stats = explanation.get("executionStats", {})
    return {
        "stages": stages,
        "docs_examined": stats.get("totalDocsExamined"),
        "keys_examined": stats.get("totalKeysExamined"),
        "n_returned": stats.get("nReturned"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", default="bench/db/data/large.json")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument("--out", default="bench/db/runs/layout_2v3_mongo_10m.json")
    parser.add_argument("--chunk", type=int, default=1_000)
    parser.add_argument("--max-paths", type=int, default=200)
    parser.add_argument("--min-free-gb", type=float, default=30.0)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if free_gb() < args.min_free_gb:
        raise SystemExit(
            f"ABORT: free disk {free_gb():.1f}GB < {args.min_free_gb:.1f}GB"
        )

    from pymongo import ASCENDING, MongoClient

    log(f"loading {args.doc} ...")
    started = time.perf_counter()
    with open(args.doc) as source:
        document = json.load(source)
    records = flatten(document, tree_id="base", seed=7)
    del document
    rows = records.rows
    paths = records.subtree_paths[: args.max_paths]
    log(
        f"flattened {len(rows):,} nodes and selected {len(paths)} paths "
        f"in {time.perf_counter() - started:.1f}s"
    )

    unique_ids = len({row["node_id"] for row in rows})
    if unique_ids != len(rows):
        raise SystemExit(
            f"ABORT: node_id is not globally unique ({unique_ids:,}/{len(rows):,})"
        )

    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    database = client[DB]
    names = (VIEW, STRUCT, META, TEXT)
    output = {
        "doc": args.doc,
        "nodes": len(rows),
        "paths": len(paths),
        "chunk": args.chunk,
        "status": "running",
        "collections": {},
        "plans": {},
        "samples": [],
    }
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        output_path.write_text(json.dumps(output, indent=2))

    def drop() -> None:
        for name in names:
            database.drop_collection(name)

    try:
        drop()

        log("ingesting two-collection view (structure + metadata, no text) ...")
        elapsed, count = ingest(
            database[VIEW],
            ({key: value for key, value in row.items() if key != "text"} for row in rows),
        )
        database[VIEW].create_index(LEAN)
        output["collections"]["two_view"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        log("ingesting three-collection structure ...")
        elapsed, count = ingest(
            database[STRUCT],
            ({key: row[key] for key in STRUCT_FIELDS} for row in rows),
        )
        database[STRUCT].create_index(LEAN)
        output["collections"]["three_struct"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        log("ingesting three-collection metadata ...")
        elapsed, count = ingest(
            database[META],
            (
                {
                    "_id": row["node_id"],
                    "title": row["title"],
                    "summary": row["summary"],
                    "start_index": row["start_index"],
                    "end_index": row["end_index"],
                }
                for row in rows
            ),
        )
        output["collections"]["three_meta"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        log("ingesting shared leaf-text collection ...")
        elapsed, count = ingest(
            database[TEXT],
            (
                {"_id": row["node_id"], "text": row["text"]}
                for row in rows
                if row["text"]
            ),
        )
        output["collections"]["shared_text"] = {
            "count": count,
            "ingest_s": round(elapsed, 1),
        }
        save()

        # The Python source rows are no longer needed during measurement.
        rows = None
        records.rows.clear()

        view = database[VIEW]
        struct = database[STRUCT]
        meta = database[META]

        def bounds(path: str) -> dict:
            return {"path": {"$gte": path + "/", "$lt": path + "0"}}

        def two_collections(path: str) -> list[tuple[str, str, str]]:
            cursor = (
                view.find(bounds(path), PROJ_VIEW)
                .sort(LEAN)
                .hint(LEAN)
            )
            return [
                (doc["node_id"], doc.get("title", ""), doc.get("summary", ""))
                for doc in cursor
            ]

        def three_collections(path: str) -> list[tuple[str, str, str]]:
            ids = [
                doc["node_id"]
                for doc in struct.find(bounds(path), PROJ_ID).sort(LEAN).hint(LEAN)
            ]
            metadata = {}
            for start in range(0, len(ids), args.chunk):
                chunk = ids[start : start + args.chunk]
                for doc in meta.find(
                    {"_id": {"$in": chunk}},
                    {"title": 1, "summary": 1},
                ):
                    metadata[doc["_id"]] = (
                        doc.get("title", ""),
                        doc.get("summary", ""),
                    )
            if len(metadata) != len(ids):
                raise RuntimeError(
                    f"metadata mismatch for {path}: {len(metadata):,}/{len(ids):,}"
                )
            return [(node_id, *metadata[node_id]) for node_id in ids]

        first = paths[0]
        output["plans"]["two_collections"] = plan_stages(
            view.find(bounds(first), PROJ_VIEW).sort(LEAN).hint(LEAN)
        )
        output["plans"]["three_structure"] = plan_stages(
            struct.find(bounds(first), PROJ_ID).sort(LEAN).hint(LEAN)
        )

        # Warm both layouts on the same three paths.  Alternate timed order per
        # path so neither layout is systematically measured first.
        log("warming both layouts on the first three paths ...")
        for path in paths[:3]:
            left = two_collections(path)
            right = three_collections(path)
            if left != right:
                raise RuntimeError(f"warm-up output mismatch for {path}")
        del left, right

        log("timing paired two-collection vs three-collection queries ...")
        for index, path in enumerate(paths):
            if index % 2 == 0:
                started = time.perf_counter()
                two_rows = two_collections(path)
                two_ms = (time.perf_counter() - started) * 1_000

                started = time.perf_counter()
                three_rows = three_collections(path)
                three_ms = (time.perf_counter() - started) * 1_000
            else:
                started = time.perf_counter()
                three_rows = three_collections(path)
                three_ms = (time.perf_counter() - started) * 1_000

                started = time.perf_counter()
                two_rows = two_collections(path)
                two_ms = (time.perf_counter() - started) * 1_000

            if two_rows != three_rows:
                raise RuntimeError(f"timed output mismatch for {path}")

            sample = {
                "path": path,
                "rows": len(two_rows),
                "two_ms": round(two_ms, 6),
                "three_ms": round(three_ms, 6),
                "three_over_two": round(three_ms / two_ms, 6) if two_ms else None,
                "fingerprint": fingerprint(two_rows),
            }
            output["samples"].append(sample)
            # Release the materialized results outside either timed region.
            # Otherwise assigning the next path's result decrefs the previous
            # lists inside that next path's latency measurement.
            del two_rows, three_rows
            if (index + 1) % 20 == 0:
                log(f"      ... {index + 1}/{len(paths)} paths")
                save()

        output["two_collections"] = summary(output["samples"], "two_ms")
        output["three_collections"] = summary(output["samples"], "three_ms")
        ratios = [sample["three_over_two"] for sample in output["samples"]]
        output["paired"] = {
            "three_faster_paths": sum(
                sample["three_ms"] < sample["two_ms"] for sample in output["samples"]
            ),
            "two_faster_paths": sum(
                sample["two_ms"] < sample["three_ms"] for sample in output["samples"]
            ),
            "ratio_p50": percentile(ratios, 50),
            "ratio_p95": percentile(ratios, 95),
        }
        output["status"] = "complete"
        save()

        print("\nMongoDB two-collection vs three-collection subtree view")
        print(f"  nodes={output['nodes']:,} paths={output['paths']} chunk={args.chunk}")
        for label in ("two_collections", "three_collections"):
            stats = output[label]
            print(
                f"  {label:20s} p50={stats['p50_ms']:9.3f} ms "
                f"p95={stats['p95_ms']:9.3f} ms rows~{stats['avg_rows']:,.1f}"
            )
        print(
            "  paired: "
            f"two faster on {output['paired']['two_faster_paths']}/{len(paths)} paths; "
            f"median three/two={output['paired']['ratio_p50']:.3f}x"
        )
        log(f"wrote {args.out}")
    finally:
        if not args.keep:
            drop()
            log("cleaned up layout comparison collections")
        client.close()
        save()


if __name__ == "__main__":
    main()
