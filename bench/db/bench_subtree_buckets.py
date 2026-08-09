#!/usr/bin/env python3
"""Build and measure a general bucketed get_subtree layout for MongoDB.

This is a structural experiment, not a cursor tuning experiment.  The current
covered path scan reads one wide index entry per returned node.  The bucketed
layout stores consecutive DFS/path-order nodes in bounded BSON documents, so a
large subtree reads a small range of bucket documents and filters only the two
boundary buckets.

Correctness and generality constraints:

* every source node appears exactly once (no per-query/precomputed subtrees);
* arbitrary materialized-path ranges are supported;
* get_subtree returns the same ordered (node_id, title, summary) rows;
* every bucket is BSON-size checked well below MongoDB's 16 MiB limit;
* a full source-vs-bucket digest is checked after construction;
* baseline and bucket arms are paired, interleaved, fully materialized, and
  fingerprint-checked on every timed observation.

The target workload is a static/write-once tree.  Updating one element rewrites
its bucket, and arbitrary inserts require a bucket split/repacking policy.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from bson import BSON


DEFAULT_TREE_ID = "base"
SOURCE_COLLECTION = "layout2_view"
SOURCE_COVER_INDEX = "layout2_rootcause_exact_cover"
NODE_INDEX = "allops_tree_node"
SOURCE_INDEX_SPECS = {
    SOURCE_COVER_INDEX: {
        "key": [
            ("path", 1),
            ("node_id", 1),
            ("title", 1),
            ("summary", 1),
        ],
        "unique": False,
    },
    NODE_INDEX: {
        "key": [("tree_id", 1), ("node_id", 1)],
        "unique": True,
    },
}
DEFAULT_BUCKET_COLLECTION = "subtree_buckets_v2_8192"
FIRST_PATH_INDEX = "bucket_first_path"
LAST_PATH_INDEX = "bucket_last_path"
SEQUENCE_INDEX = "bucket_sequence"
MAX_SAFE_BSON_BYTES = 15 * 1024 * 1024
FIELDS = ("path", "node_id", "title", "summary")
VARIANTS = ("baseline", "bucket")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def provenance() -> dict[str, Any]:
    script_path = Path(__file__).resolve()
    repo_path = script_path.parents[2]
    script_bytes = script_path.read_bytes()

    def git_output(*args: str) -> bytes:
        return subprocess.check_output(
            ("git", *args),
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
        )

    try:
        revision = git_output("rev-parse", "HEAD").decode().strip()
        status = git_output("status", "--porcelain=v1", "-z")
    except (OSError, subprocess.CalledProcessError):
        revision = None
        status = b""
    return {
        "argv": sys.argv,
        "git_revision": revision,
        "git_dirty": bool(status),
        "git_status_sha256": hashlib.sha256(status).hexdigest(),
        "script": str(script_path.relative_to(repo_path)),
        "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
        "script_source": script_bytes.decode(),
    }


def log(message: str) -> None:
    print(message, flush=True)


def normalize(values: Iterable[Any]) -> tuple[Any, ...]:
    """Apply the benchmark contract: missing/null text is the empty string."""
    return tuple("" if value is None else value for value in values)


def encoded_row(values: Sequence[Any]) -> bytes:
    """Encode one canonicalized row without collapsing remaining BSON types."""
    return BSON.encode({"values": list(normalize(values))})


def fingerprint(rows: Sequence[Sequence[Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        encoded = encoded_row(row)
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def update_row_digest(digest: Any, values: Sequence[Any]) -> None:
    encoded = encoded_row(values)
    digest.update(len(encoded).to_bytes(4, "big"))
    digest.update(encoded)


def percentile(values: Sequence[float], pct: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return round(float(ordered[index]), 6)


def stats(values: Iterable[float]) -> dict[str, float | int]:
    samples = list(values)
    if not samples:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0}
    return {
        "n": len(samples),
        "mean": round(statistics.mean(samples), 6),
        "p50": percentile(samples, 50),
        "p95": percentile(samples, 95),
        "p99": percentile(samples, 99),
        "min": round(min(samples), 6),
        "max": round(max(samples), 6),
    }


def coll_stats(database: Any, name: str) -> dict[str, Any]:
    result = database.command("collStats", name)
    return {
        "count": result.get("count", 0),
        "logical_bytes": result.get("size", 0),
        "storage_bytes": result.get("storageSize", 0),
        "total_index_bytes": result.get("totalIndexSize", 0),
        "index_bytes": result.get("indexSizes", {}),
    }


def disk_free_gb() -> float:
    return shutil.disk_usage("/").free / 1e9


def disk_guard(floor_gb: float) -> None:
    free = disk_free_gb()
    if free < floor_gb:
        raise RuntimeError(
            f"free disk {free:.1f} GB is below the {floor_gb:.1f} GB floor"
        )


def validate_source_contract(database: Any, tree_id: str) -> dict[str, Any]:
    """Bind the one-tree query contract and the indexes used by timed reads."""
    source = database[SOURCE_COLLECTION]
    options = source.options()
    collation = options.get("collation")
    if collation is not None and collation.get("locale") != "simple":
        raise RuntimeError(
            f"{SOURCE_COLLECTION} must use simple collation, got {collation}"
        )
    source_count = source.count_documents({})
    if source_count != 10_000_000:
        raise RuntimeError(
            f"{SOURCE_COLLECTION} has {source_count} rows, expected 10,000,000"
        )
    foreign = source.find_one(
        {"tree_id": {"$ne": tree_id}},
        {"_id": 1, "tree_id": 1},
    )
    if foreign is not None:
        raise RuntimeError(
            "this experiment requires exactly one tree in the source; "
            f"found tree_id={foreign.get('tree_id')!r}"
        )
    indexes = source.index_information()
    bound_indexes: dict[str, Any] = {}
    for name, expected in SOURCE_INDEX_SPECS.items():
        actual = indexes.get(name)
        if actual is None:
            raise RuntimeError(f"missing source index {name}")
        if actual.get("key") != expected["key"]:
            raise RuntimeError(
                f"source index {name} key mismatch: {actual.get('key')}"
            )
        if bool(actual.get("unique", False)) != expected["unique"]:
            raise RuntimeError(f"source index {name} uniqueness mismatch")
        if actual.get("partialFilterExpression") is not None:
            raise RuntimeError(f"source index {name} must not be partial")
        if actual.get("collation") is not None:
            raise RuntimeError(f"source index {name} must use simple collation")
        bound_indexes[name] = {
            "key": actual["key"],
            "unique": bool(actual.get("unique", False)),
            "partialFilterExpression": actual.get("partialFilterExpression"),
            "collation": actual.get("collation"),
        }
    return {
        "collection": SOURCE_COLLECTION,
        "collection_options": options,
        "tree_id": tree_id,
        "rows": source_count,
        "single_tree": True,
        "indexes": bound_indexes,
    }


def bucket_document(
    tree_id: str,
    bucket_id: int,
    rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "_id": f"{tree_id}:{bucket_id:012d}",
        "kind": "bucket",
        "tree_id": tree_id,
        "seq": bucket_id,
        "count": len(rows),
        "first_path": rows[0]["path"],
        "last_path": rows[-1]["path"],
        "paths": [row["path"] for row in rows],
        "node_ids": [row["node_id"] for row in rows],
        "titles": [row.get("title", "") for row in rows],
        "summaries": [row.get("summary", "") for row in rows],
    }


def reservoir_add(
    reservoirs: defaultdict[int, list[dict[str, str]]],
    seen: defaultdict[int, int],
    row: dict[str, Any],
    capacity: int,
    rng: random.Random,
) -> None:
    depth = str(row["path"]).count("/")
    seen[depth] += 1
    item = {"path": str(row["path"]), "node_id": str(row["node_id"])}
    target = reservoirs[depth]
    if len(target) < capacity:
        target.append(item)
        return
    replacement = rng.randrange(seen[depth])
    if replacement < capacity:
        target[replacement] = item


def build_buckets(
    database: Any,
    collection_name: str,
    tree_id: str,
    rows_per_bucket: int,
    min_free_gb: float,
    reservoir_per_depth: int,
    seed: int,
) -> dict[str, Any]:
    source = database[SOURCE_COLLECTION]
    buckets = database[collection_name]
    if collection_name in database.list_collection_names():
        raise RuntimeError(
            f"{collection_name} already exists; use --reuse or --rebuild"
        )
    disk_guard(min_free_gb)
    source_count = source.count_documents({"tree_id": tree_id})
    if source_count != 10_000_000:
        raise RuntimeError(
            f"source count for tree {tree_id!r} is {source_count}, "
            "expected 10,000,000"
        )
    other_tree = source.find_one(
        {"tree_id": {"$ne": tree_id}},
        {"_id": 1, "tree_id": 1},
    )
    if other_tree is not None:
        raise RuntimeError(
            "this experiment requires one tree per source collection; "
            f"found tree_id={other_tree.get('tree_id')!r}"
        )
    if SOURCE_COVER_INDEX not in source.index_information():
        raise RuntimeError(f"missing source index {SOURCE_COVER_INDEX}")

    started = time.perf_counter()
    manifest_id = f"{tree_id}:manifest"
    manifest = {
        "_id": manifest_id,
        "kind": "manifest",
        "tree_id": tree_id,
        "status": "building",
        "started_at": utc_now(),
        "source_collection": SOURCE_COLLECTION,
        "source_index": SOURCE_COVER_INDEX,
        "source_count": source_count,
        "rows_per_bucket": rows_per_bucket,
        "max_safe_bson_bytes": MAX_SAFE_BSON_BYTES,
        "seed": seed,
    }
    buckets.insert_one(manifest)

    source_digest = hashlib.sha256()
    reservoirs: defaultdict[int, list[dict[str, str]]] = defaultdict(list)
    depth_seen: defaultdict[int, int] = defaultdict(int)
    reservoir_rng = random.Random(seed)
    row_buffer: list[dict[str, Any]] = []
    insert_buffer: list[dict[str, Any]] = []
    bucket_id = 0
    rows_written = 0
    min_bson_bytes: int | None = None
    max_bson_bytes = 0
    previous_key: tuple[str, str] | None = None
    first_key: tuple[str, str] | None = None
    last_key: tuple[str, str] | None = None

    def flush_inserts() -> None:
        nonlocal insert_buffer
        if insert_buffer:
            buckets.insert_many(insert_buffer, ordered=True)
            insert_buffer = []

    def emit(rows: Sequence[dict[str, Any]]) -> None:
        nonlocal bucket_id, rows_written, min_bson_bytes, max_bson_bytes
        document = bucket_document(tree_id, bucket_id, rows)
        encoded_bytes = len(BSON.encode(document))
        if encoded_bytes > MAX_SAFE_BSON_BYTES:
            if len(rows) == 1:
                raise RuntimeError(
                    f"single row exceeds BSON guard: {encoded_bytes} bytes"
                )
            midpoint = len(rows) // 2
            emit(rows[:midpoint])
            emit(rows[midpoint:])
            return
        min_bson_bytes = (
            encoded_bytes
            if min_bson_bytes is None
            else min(min_bson_bytes, encoded_bytes)
        )
        max_bson_bytes = max(max_bson_bytes, encoded_bytes)
        insert_buffer.append(document)
        bucket_id += 1
        rows_written += len(rows)
        # Six 4-MiB target documents stay below MongoDB's 48-MiB message limit.
        if len(insert_buffer) >= 6:
            flush_inserts()

    cursor = (
        source.find(
            {},
            {"_id": 0, "path": 1, "node_id": 1, "title": 1, "summary": 1},
            no_cursor_timeout=True,
        )
        .sort([("path", 1), ("node_id", 1)])
        .hint(SOURCE_COVER_INDEX)
        .batch_size(10_000)
    )
    try:
        for row_index, raw in enumerate(cursor, start=1):
            row = {
                "path": str(raw["path"]),
                "node_id": str(raw["node_id"]),
                "title": "" if raw.get("title") is None else raw.get("title", ""),
                "summary": (
                    "" if raw.get("summary") is None else raw.get("summary", "")
                ),
            }
            key = (row["path"], row["node_id"])
            if previous_key is not None and key <= previous_key:
                raise RuntimeError(f"source order is not strict at {key}")
            if first_key is None:
                first_key = key
            previous_key = key
            last_key = key
            update_row_digest(source_digest, tuple(row[field] for field in FIELDS))
            reservoir_add(
                reservoirs,
                depth_seen,
                row,
                reservoir_per_depth,
                reservoir_rng,
            )
            row_buffer.append(row)
            if len(row_buffer) >= rows_per_bucket:
                emit(row_buffer)
                row_buffer = []
            if row_index % 1_000_000 == 0:
                flush_inserts()
                buckets.update_one(
                    {"_id": manifest_id},
                    {"$set": {
                        "progress_rows": row_index,
                        "progress_buckets": bucket_id,
                        "updated_at": utc_now(),
                    }},
                )
                log(
                    f"  build {row_index:,}/{source_count:,} rows, "
                    f"{bucket_id:,} buckets, free {disk_free_gb():.1f} GB"
                )
                disk_guard(min_free_gb)
    finally:
        cursor.close()
    if row_buffer:
        emit(row_buffer)
    flush_inserts()
    if rows_written != source_count:
        raise RuntimeError(f"bucket row count {rows_written} != {source_count}")

    index_started = time.perf_counter()
    buckets.create_index(
        [("tree_id", 1), ("first_path", 1), ("seq", 1)],
        name=FIRST_PATH_INDEX,
        partialFilterExpression={"kind": "bucket"},
    )
    buckets.create_index(
        [("tree_id", 1), ("last_path", 1), ("seq", 1)],
        name=LAST_PATH_INDEX,
        partialFilterExpression={"kind": "bucket"},
    )
    buckets.create_index(
        [("tree_id", 1), ("seq", 1)],
        name=SEQUENCE_INDEX,
        partialFilterExpression={"kind": "bucket"},
        unique=True,
    )
    index_seconds = time.perf_counter() - index_started
    result = {
        "status": "built",
        "tree_id": tree_id,
        "source_count": source_count,
        "rows": rows_written,
        "buckets": bucket_id,
        "rows_per_bucket": rows_per_bucket,
        "min_bson_bytes": min_bson_bytes,
        "max_bson_bytes": max_bson_bytes,
        "max_bson_fraction_of_limit": round(
            max_bson_bytes / (16 * 1024 * 1024), 6
        ),
        "source_digest": source_digest.hexdigest(),
        "first_key": list(first_key) if first_key else None,
        "last_key": list(last_key) if last_key else None,
        "reservoirs": {
            str(depth): values for depth, values in sorted(reservoirs.items())
        },
        "depth_counts": {
            str(depth): count for depth, count in sorted(depth_seen.items())
        },
        "build_seconds": round(time.perf_counter() - started, 3),
        "index_seconds": round(index_seconds, 3),
    }
    buckets.update_one(
        {"_id": manifest_id},
        {"$set": {
            **result,
            "status": "complete",
            "completed_at": utc_now(),
        }},
    )
    return result


def digest_source(
    database: Any,
    tree_id: str,
    min_free_gb: float,
) -> dict[str, Any]:
    """Re-read the live source so --reuse cannot trust a stale manifest digest."""
    source = database[SOURCE_COLLECTION]
    source_contract = validate_source_contract(database, tree_id)
    started = time.perf_counter()
    digest = hashlib.sha256()
    rows = 0
    previous_key: tuple[str, str] | None = None
    first_key: tuple[str, str] | None = None
    last_key: tuple[str, str] | None = None
    next_log_rows = 1_000_000
    cursor = (
        source.find(
            {"tree_id": tree_id},
            {"_id": 0, "path": 1, "node_id": 1, "title": 1, "summary": 1},
            no_cursor_timeout=True,
        )
        .sort([("path", 1), ("node_id", 1)])
        .hint(SOURCE_COVER_INDEX)
        .batch_size(10_000)
    )
    try:
        for raw in cursor:
            raw_path = raw.get("path")
            raw_node_id = raw.get("node_id")
            if not isinstance(raw_path, str) or not isinstance(
                raw_node_id,
                str,
            ):
                raise RuntimeError("source path and node_id must be strings")
            segments = raw_path.split("/")
            if (
                not raw_path.startswith("/")
                or len(segments) < 2
                or any(not segment.isdecimal() for segment in segments[1:])
                or segments[-1] != raw_node_id
            ):
                raise RuntimeError(
                    f"source path violates numeric slash grammar: {raw_path!r}"
                )
            values = normalize(
                (
                    raw_path,
                    raw_node_id,
                    raw.get("title"),
                    raw.get("summary"),
                )
            )
            key = (str(values[0]), str(values[1]))
            if previous_key is not None and key <= previous_key:
                raise RuntimeError(f"live source order is not strict at {key}")
            if first_key is None:
                first_key = key
            previous_key = key
            last_key = key
            update_row_digest(digest, values)
            rows += 1
            if rows >= next_log_rows:
                log(f"  source rescan {rows:,} rows")
                disk_guard(min_free_gb)
                next_log_rows += 1_000_000
    finally:
        cursor.close()
    return {
        "status": "complete",
        "tree_id": tree_id,
        "rows": rows,
        "digest": digest.hexdigest(),
        "first_key": list(first_key) if first_key else None,
        "last_key": list(last_key) if last_key else None,
        "path_grammar": (
            "simple-collation slash-delimited decimal segments; final "
            "segment equals node_id"
        ),
        "source_contract": source_contract,
        "seconds": round(time.perf_counter() - started, 3),
    }


def validate_buckets(
    database: Any,
    collection_name: str,
    tree_id: str,
    expected_digest: str,
    min_free_gb: float,
    rows_per_bucket: int | None = None,
) -> dict[str, Any]:
    buckets = database[collection_name]
    started = time.perf_counter()
    digest = hashlib.sha256()
    rows = 0
    documents = 0
    min_bson_bytes: int | None = None
    max_bson_bytes = 0
    previous_key: tuple[str, str] | None = None
    first_key: tuple[str, str] | None = None
    last_key: tuple[str, str] | None = None
    expected_seq = 0
    short_bucket_seqs: list[int] = []
    next_log_rows = 1_000_000
    cursor = (
        buckets.find({"kind": "bucket", "tree_id": tree_id})
        .sort("seq", 1)
        .hint(SEQUENCE_INDEX)
    )
    for document in cursor:
        documents += 1
        if document.get("tree_id") != tree_id:
            raise RuntimeError(
                f"bucket {document['_id']} has wrong tree_id"
            )
        if document.get("seq") != expected_seq:
            raise RuntimeError(
                f"bucket sequence gap: got {document.get('seq')}, "
                f"expected {expected_seq}"
            )
        expected_seq += 1
        if document.get("_id") != f"{tree_id}:{document['seq']:012d}":
            raise RuntimeError(f"bucket {document.get('_id')} has wrong _id")
        if not isinstance(document.get("count"), int) or document["count"] <= 0:
            raise RuntimeError(f"bucket {document['_id']} has invalid count")
        if rows_per_bucket is not None:
            if document["count"] > rows_per_bucket:
                raise RuntimeError(
                    f"bucket {document['_id']} exceeds {rows_per_bucket} rows"
                )
            if document["count"] != rows_per_bucket:
                short_bucket_seqs.append(int(document["seq"]))
        arrays = [document[field] for field in (
            "paths", "node_ids", "titles", "summaries"
        )]
        lengths = {len(values) for values in arrays}
        if lengths != {document["count"]}:
            raise RuntimeError(f"bucket {document['_id']} has unequal arrays")
        if document["first_path"] != document["paths"][0]:
            raise RuntimeError(f"bucket {document['_id']} first_path mismatch")
        if document["last_path"] != document["paths"][-1]:
            raise RuntimeError(f"bucket {document['_id']} last_path mismatch")
        encoded_bytes = len(BSON.encode(document))
        if encoded_bytes > MAX_SAFE_BSON_BYTES:
            raise RuntimeError(f"bucket {document['_id']} exceeds BSON guard")
        min_bson_bytes = (
            encoded_bytes
            if min_bson_bytes is None
            else min(min_bson_bytes, encoded_bytes)
        )
        max_bson_bytes = max(max_bson_bytes, encoded_bytes)
        for values in zip(*arrays):
            key = (str(values[0]), str(values[1]))
            if previous_key is not None and key <= previous_key:
                raise RuntimeError(f"bucket order is not strict at {key}")
            if first_key is None:
                first_key = key
            previous_key = key
            last_key = key
            update_row_digest(digest, values)
            rows += 1
        if rows >= next_log_rows:
            log(f"  validate {documents:,} buckets / {rows:,} rows")
            disk_guard(min_free_gb)
            next_log_rows += 1_000_000
    actual_digest = digest.hexdigest()
    if actual_digest != expected_digest:
        raise RuntimeError(
            f"full digest mismatch: {actual_digest} != {expected_digest}"
        )
    if rows_per_bucket is not None:
        expected_documents = (rows + rows_per_bucket - 1) // rows_per_bucket
        if documents != expected_documents:
            raise RuntimeError(
                f"bucket count {documents} != ceil({rows}/{rows_per_bucket})"
            )
        allowed_short = [] if rows % rows_per_bucket == 0 else [documents - 1]
        if short_bucket_seqs != allowed_short:
            raise RuntimeError(
                "only the final bucket may be short: "
                f"got {short_bucket_seqs}, expected {allowed_short}"
            )
    return {
        "status": "complete",
        "rows": rows,
        "buckets": documents,
        "digest": actual_digest,
        "digest_matches_source": True,
        "first_key": list(first_key) if first_key else None,
        "last_key": list(last_key) if last_key else None,
        "min_bson_bytes": min_bson_bytes,
        "max_bson_bytes": max_bson_bytes,
        "rows_per_bucket": rows_per_bucket,
        "short_bucket_seqs": short_bucket_seqs,
        "seconds": round(time.perf_counter() - started, 3),
    }


def root_path(nodes: Any, tree_id: str, node_id: str) -> str | None:
    row = nodes.find_one(
        {"tree_id": tree_id, "node_id": node_id},
        {"_id": 0, "path": 1},
        hint=NODE_INDEX,
    )
    return None if row is None else str(row["path"])


def baseline_query(
    nodes: Any,
    tree_id: str,
    node_id: str,
) -> tuple[list[tuple[Any, ...]], dict[str, float | int]]:
    total_started = time.perf_counter()
    started = time.perf_counter()
    path = root_path(nodes, tree_id, node_id)
    root_ms = (time.perf_counter() - started) * 1_000
    if path is None:
        return [], {
            "total_ms": (time.perf_counter() - total_started) * 1_000,
            "root_ms": root_ms,
            "directory_ms": 0.0,
            "fetch_ms": 0.0,
            "normalize_filter_ms": 0.0,
            "bucket_docs": 0,
            "rows_read": 0,
            "overfetch_rows": 0,
        }
    lower, upper = path + "/", path + "0"

    started = time.perf_counter()
    raw_rows = list(
        nodes.find(
            {"path": {"$gte": lower, "$lt": upper}},
            {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
        )
        .sort([("path", 1), ("node_id", 1)])
        .hint(SOURCE_COVER_INDEX)
    )
    fetch_ms = (time.perf_counter() - started) * 1_000
    started = time.perf_counter()
    rows = [
        normalize((row.get("node_id"), row.get("title"), row.get("summary")))
        for row in raw_rows
    ]
    normalize_ms = (time.perf_counter() - started) * 1_000
    total_ms = (time.perf_counter() - total_started) * 1_000
    return rows, {
        "total_ms": total_ms,
        "root_ms": root_ms,
        "directory_ms": 0.0,
        "fetch_ms": fetch_ms,
        "normalize_filter_ms": normalize_ms,
        "bucket_docs": 0,
        "rows_read": len(raw_rows),
        "overfetch_rows": 0,
    }


def bucket_query(
    nodes: Any,
    buckets: Any,
    tree_id: str,
    node_id: str,
) -> tuple[list[tuple[Any, ...]], dict[str, float | int]]:
    total_started = time.perf_counter()
    started = time.perf_counter()
    path = root_path(nodes, tree_id, node_id)
    root_ms = (time.perf_counter() - started) * 1_000
    if path is None:
        return [], {
            "total_ms": (time.perf_counter() - total_started) * 1_000,
            "root_ms": root_ms,
            "directory_ms": 0.0,
            "fetch_ms": 0.0,
            "normalize_filter_ms": 0.0,
            "bucket_docs": 0,
            "rows_read": 0,
            "overfetch_rows": 0,
        }
    lower, upper = path + "/", path + "0"

    started = time.perf_counter()
    first = buckets.find_one(
        {
            "kind": "bucket",
            "tree_id": tree_id,
            "last_path": {"$gte": lower},
        },
        {"_id": 0, "seq": 1},
        sort=[("last_path", 1), ("seq", 1)],
        hint=LAST_PATH_INDEX,
    )
    last = buckets.find_one(
        {
            "kind": "bucket",
            "tree_id": tree_id,
            "first_path": {"$lt": upper},
        },
        {"_id": 0, "seq": 1},
        sort=[("first_path", -1), ("seq", -1)],
        hint=FIRST_PATH_INDEX,
    )
    directory_ms = (time.perf_counter() - started) * 1_000
    if first is None or last is None or first["seq"] > last["seq"]:
        return [], {
            "total_ms": (time.perf_counter() - total_started) * 1_000,
            "root_ms": root_ms,
            "directory_ms": directory_ms,
            "fetch_ms": 0.0,
            "normalize_filter_ms": 0.0,
            "bucket_docs": 0,
            "rows_read": 0,
            "overfetch_rows": 0,
        }

    started = time.perf_counter()
    raw_buckets = list(
        buckets.find(
            {
                "kind": "bucket",
                "tree_id": tree_id,
                "seq": {"$gte": first["seq"], "$lte": last["seq"]},
            },
            {
                "_id": 0,
                "seq": 1,
                "count": 1,
                "paths": 1,
                "node_ids": 1,
                "titles": 1,
                "summaries": 1,
            },
        )
        .sort("seq", 1)
        .hint(SEQUENCE_INDEX)
    )
    fetch_ms = (time.perf_counter() - started) * 1_000
    started = time.perf_counter()
    rows: list[tuple[Any, ...]] = []
    rows_read = 0
    for document in raw_buckets:
        rows_read += int(document["count"])
        for item_path, node_id_value, title, summary in zip(
            document["paths"],
            document["node_ids"],
            document["titles"],
            document["summaries"],
        ):
            if lower <= item_path < upper:
                rows.append(normalize((node_id_value, title, summary)))
    filter_ms = (time.perf_counter() - started) * 1_000
    total_ms = (time.perf_counter() - total_started) * 1_000
    return rows, {
        "total_ms": total_ms,
        "root_ms": root_ms,
        "directory_ms": directory_ms,
        "fetch_ms": fetch_ms,
        "normalize_filter_ms": filter_ms,
        "bucket_docs": len(raw_buckets),
        "rows_read": rows_read,
        "overfetch_rows": rows_read - len(rows),
    }


def summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    per_variant: dict[str, list[dict[str, float]]] = {}
    metric_names = (
        "total_ms",
        "root_ms",
        "directory_ms",
        "fetch_ms",
        "normalize_filter_ms",
        "bucket_docs",
        "rows_read",
        "overfetch_rows",
    )
    for variant in VARIANTS:
        path_means = []
        for sample in samples:
            observations = sample["times"][variant]
            path_means.append({
                metric: statistics.mean(item[metric] for item in observations)
                for metric in metric_names
            })
        per_variant[variant] = path_means
        output[variant] = {
            metric: stats(item[metric] for item in path_means)
            for metric in metric_names
        }
    baseline = per_variant["baseline"]
    bucket = per_variant["bucket"]
    paired = [
        left["total_ms"] / right["total_ms"]
        for left, right in zip(baseline, bucket)
        if right["total_ms"] > 0
    ]
    output["paired_speedup"] = stats(paired)
    output["speedup_p50"] = round(
        float(output["baseline"]["total_ms"]["p50"])
        / float(output["bucket"]["total_ms"]["p50"]),
        6,
    )
    output["speedup_p95"] = round(
        float(output["baseline"]["total_ms"]["p95"])
        / float(output["bucket"]["total_ms"]["p95"]),
        6,
    )
    output["inputs"] = len(samples)
    output["avg_rows"] = round(statistics.mean(s["rows"] for s in samples), 3)
    return output


def benchmark_inputs(
    nodes: Any,
    buckets: Any,
    tree_id: str,
    inputs: list[dict[str, Any]],
    repeats: int,
    seed: int,
    label: str,
    out_path: Path,
    output: dict[str, Any],
) -> dict[str, Any]:
    samples = [
        {
            "node_id": item["node_id"],
            "path": item["path"],
            "rows": item.get("rows"),
            "fingerprint": item.get("fingerprint"),
            "times": {variant: [] for variant in VARIANTS},
        }
        for item in inputs
    ]
    log(f"warming {label}: {len(samples)} inputs x 2 variants")
    for input_index, sample in enumerate(samples):
        observed: dict[str, tuple[int, str]] = {}
        order = (
            VARIANTS
            if input_index % 2 == 0
            else tuple(reversed(VARIANTS))
        )
        for variant in order:
            rows, _ = (
                baseline_query(nodes, tree_id, sample["node_id"])
                if variant == "baseline"
                else bucket_query(nodes, buckets, tree_id, sample["node_id"])
            )
            actual_fingerprint = fingerprint(rows)
            observed[variant] = (len(rows), actual_fingerprint)
            if sample["rows"] is not None:
                if len(rows) != sample["rows"]:
                    raise RuntimeError(
                        f"warm row mismatch {label} {sample['node_id']} "
                        f"{variant}: {len(rows)} != {sample['rows']}"
                    )
                if actual_fingerprint != sample["fingerprint"]:
                    raise RuntimeError(
                        f"warm fingerprint mismatch {label} "
                        f"{sample['node_id']} {variant}"
                    )
            del rows
        if sample["rows"] is None:
            sample["rows"], sample["fingerprint"] = observed["baseline"]
        if observed["bucket"] != observed["baseline"]:
            raise RuntimeError(
                f"warm cross-arm mismatch {label} {sample['node_id']}: "
                f"{observed['baseline']} != {observed['bucket']}"
            )
        if (input_index + 1) % 50 == 0:
            log(f"  warm {input_index + 1}/{len(samples)}")

    log(
        f"timing {label}: {len(samples)} inputs x {repeats} repeats x 2 variants"
    )
    for repeat in range(repeats):
        order_indices = list(range(len(samples)))
        random.Random(seed + repeat).shuffle(order_indices)
        for position, input_index in enumerate(order_indices):
            sample = samples[input_index]
            rotation = (repeat + position) % 2
            variant_order = VARIANTS[rotation:] + VARIANTS[:rotation]
            for variant in variant_order:
                gc.disable()
                try:
                    rows, metrics = (
                        baseline_query(nodes, tree_id, sample["node_id"])
                        if variant == "baseline"
                        else bucket_query(
                            nodes,
                            buckets,
                            tree_id,
                            sample["node_id"],
                        )
                    )
                finally:
                    gc.enable()
                if len(rows) != sample["rows"]:
                    raise RuntimeError(
                        f"timed row mismatch {label} {sample['node_id']} "
                        f"{variant}: {len(rows)} != {sample['rows']}"
                    )
                if fingerprint(rows) != sample["fingerprint"]:
                    raise RuntimeError(
                        f"timed fingerprint mismatch {label} "
                        f"{sample['node_id']} {variant}"
                    )
                sample["times"][variant].append({
                    key: round(float(value), 6)
                    for key, value in metrics.items()
                })
                del rows
            if (position + 1) % 50 == 0:
                log(
                    f"  {label} repeat {repeat + 1}/{repeats}: "
                    f"{position + 1}/{len(samples)}"
                )
        output["benchmarks"][label] = {
            "status": "running",
            "samples": samples,
            "summary": summarize_samples(samples),
        }
        out_path.write_text(json.dumps(output, indent=2))
    return {
        "status": "complete",
        "samples": samples,
        "summary": summarize_samples(samples),
    }


def load_main_inputs(path: Path, limit: int) -> list[dict[str, Any]]:
    source = json.loads(path.read_text())
    result = []
    for sample in source["samples"][:limit]:
        result.append({
            "node_id": str(sample["path"]).rsplit("/", 1)[-1],
            "path": str(sample["path"]),
            "rows": int(sample["rows"]),
            "fingerprint": str(sample["fingerprint"]),
        })
    return result


def spectrum_inputs(
    reservoirs: dict[str, list[dict[str, str]]],
    per_depth: int,
) -> list[dict[str, Any]]:
    result = []
    for depth_text, items in sorted(reservoirs.items(), key=lambda item: int(item[0])):
        depth = int(depth_text)
        if depth < 4:
            continue
        for item in items[:per_depth]:
            result.append({
                "node_id": item["node_id"],
                "path": item["path"],
                "depth": depth,
            })
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument("--mongo-db", default="bench")
    parser.add_argument("--tree-id", default=DEFAULT_TREE_ID)
    parser.add_argument(
        "--collection",
        default=DEFAULT_BUCKET_COLLECTION,
    )
    parser.add_argument(
        "--expected",
        default=(
            "bench/db/runs/report_3eng_20260716/"
            "layout_2v3_postgres_10m_final.json"
        ),
    )
    parser.add_argument(
        "--out",
        default=(
            "bench/db/runs/subtree_buckets_20260724/"
            "bucket_8192_10m.json"
        ),
    )
    parser.add_argument("--rows-per-bucket", type=int, default=8192)
    parser.add_argument("--main-inputs", type=int, default=200)
    parser.add_argument("--main-repeats", type=int, default=5)
    parser.add_argument("--spectrum-per-depth", type=int, default=20)
    parser.add_argument("--spectrum-repeats", type=int, default=3)
    parser.add_argument("--reservoir-per-depth", type=int, default=32)
    parser.add_argument("--seed", type=int, default=20260724)
    parser.add_argument("--min-free-gb", type=float, default=300.0)
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="build and fully validate the layout without latency benchmarks",
    )
    args = parser.parse_args()
    if args.rows_per_bucket < 128:
        parser.error("rows-per-bucket must be at least 128")
    if args.main_repeats < 2 or args.spectrum_repeats < 2:
        parser.error("benchmark repeats must be at least 2")
    if args.reuse and args.rebuild:
        parser.error("--reuse and --rebuild are mutually exclusive")

    from pymongo import MongoClient

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    database = client[args.mongo_db]
    nodes = database[SOURCE_COLLECTION]
    existing = args.collection in database.list_collection_names()
    if existing and args.rebuild:
        log(f"dropping exact experiment collection {args.collection}")
        database.drop_collection(args.collection)
        existing = False
    if existing and not args.reuse:
        raise RuntimeError(
            f"{args.collection} exists; pass --reuse or --rebuild explicitly"
        )

    output: dict[str, Any] = {
        "provenance": provenance(),
        "run": {
            "status": "running",
            "started_at": utc_now(),
            "seed": args.seed,
            "main_repeats": args.main_repeats,
            "spectrum_repeats": args.spectrum_repeats,
        },
        "contract": {
            "input": ["tree_id", "node_id"],
            "output": ["node_id", "title", "summary"],
            "root_excluded": True,
            "depth_limit": None,
            "baseline": (
                "root lookup plus covered path range over one index entry per node"
            ),
            "bucket": (
                "same root lookup, two boundary directory probes, contiguous "
                "bucket-id range, exact boundary filtering"
            ),
            "generality": (
                "one explicitly identified tree per source/bucket collection; "
                "every source row stored once; arbitrary path range; no "
                "sample-specific or per-subtree materialization"
            ),
            "timing": (
                "paired/interleaved client wall time through complete result "
                "materialization; fingerprints checked outside timing"
            ),
        },
        "environment": {
            "mongodb": client.server_info()["version"],
            "pymongo": __import__("pymongo").version,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "loadavg_before": list(os.getloadavg()),
            "free_disk_gb_before": round(disk_free_gb(), 3),
        },
        "sources": {
            "tree_id": args.tree_id,
            "collection": SOURCE_COLLECTION,
            "cover_index": SOURCE_COVER_INDEX,
            "expected": args.expected,
            "bucket_collection": args.collection,
        },
        "build": {},
        "validation": {},
        "storage": {},
        "benchmarks": {},
    }
    out_path.write_text(json.dumps(output, indent=2))
    disk_guard(args.min_free_gb)

    if not existing:
        log(
            f"building {args.collection}: {args.rows_per_bucket} rows/bucket, "
            f"free disk {disk_free_gb():.1f} GB"
        )
        output["build"] = build_buckets(
            database,
            args.collection,
            args.tree_id,
            args.rows_per_bucket,
            args.min_free_gb,
            args.reservoir_per_depth,
            args.seed,
        )
        out_path.write_text(json.dumps(output, indent=2))
    else:
        manifest = database[args.collection].find_one(
            {"_id": f"{args.tree_id}:manifest"}
        )
        if manifest is None or manifest.get("status") != "complete":
            raise RuntimeError("existing bucket collection has no complete manifest")
        if manifest.get("tree_id") != args.tree_id:
            raise RuntimeError("existing bucket tree_id differs from requested tree")
        if manifest.get("rows_per_bucket") != args.rows_per_bucket:
            raise RuntimeError("existing bucket size differs from requested size")
        output["build"] = {
            key: value
            for key, value in manifest.items()
            if key != "_id"
        }
        output["build"]["reused"] = True

    log("re-reading the live source before bucket validation")
    output["source_rescan"] = digest_source(
        database,
        args.tree_id,
        args.min_free_gb,
    )
    if output["source_rescan"]["digest"] != output["build"]["source_digest"]:
        raise RuntimeError(
            "live source digest differs from the digest recorded during build"
        )
    if output["source_rescan"]["rows"] != output["build"]["source_count"]:
        raise RuntimeError(
            "live source row count differs from the count recorded during build"
        )
    log("validating every bucket row against the live source digest")
    output["validation"] = validate_buckets(
        database,
        args.collection,
        args.tree_id,
        str(output["source_rescan"]["digest"]),
        args.min_free_gb,
        args.rows_per_bucket,
    )
    output["storage"] = {
        "source": coll_stats(database, SOURCE_COLLECTION),
        "bucket": coll_stats(database, args.collection),
        "free_disk_gb_after_build": round(disk_free_gb(), 3),
    }
    out_path.write_text(json.dumps(output, indent=2))
    if args.build_only:
        output["run"]["status"] = "complete"
        output["run"]["completed_at"] = utc_now()
        output["run"]["mode"] = "build-only"
        output["environment"]["loadavg_after"] = list(os.getloadavg())
        output["environment"]["free_disk_gb_after"] = round(disk_free_gb(), 3)
        out_path.write_text(json.dumps(output, indent=2))
        client.close()
        log(f"wrote {out_path}")
        return

    buckets = database[args.collection]
    main_inputs = load_main_inputs(Path(args.expected), args.main_inputs)
    output["benchmarks"]["main"] = benchmark_inputs(
        nodes,
        buckets,
        args.tree_id,
        main_inputs,
        args.main_repeats,
        args.seed,
        "main",
        out_path,
        output,
    )
    out_path.write_text(json.dumps(output, indent=2))

    extra_inputs = spectrum_inputs(
        output["build"]["reservoirs"],
        args.spectrum_per_depth,
    )
    output["benchmarks"]["spectrum"] = benchmark_inputs(
        nodes,
        buckets,
        args.tree_id,
        extra_inputs,
        args.spectrum_repeats,
        args.seed + 1_000_000,
        "spectrum",
        out_path,
        output,
    )
    output["run"]["status"] = "complete"
    output["run"]["completed_at"] = utc_now()
    output["environment"]["loadavg_after"] = list(os.getloadavg())
    output["environment"]["free_disk_gb_after"] = round(disk_free_gb(), 3)
    out_path.write_text(json.dumps(output, indent=2))
    client.close()

    summary = {
        name: benchmark["summary"]
        for name, benchmark in output["benchmarks"].items()
    }
    print(json.dumps(summary, indent=2))
    log(f"wrote {out_path}")


if __name__ == "__main__":
    main()
