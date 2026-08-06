#!/usr/bin/env python3
"""Run the SQLite Beam batching control with independent client connections."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("LOG_LEVEL", "WARNING")

from bench_sqlite_beam_batching import (
    DEFAULT_BEAM_SIZE,
    ScalarTreeDB,
    build_tree,
    file_sha256,
    fingerprint,
    git_head,
    percentile,
    run_query,
)

from contextdb.core.storage import TreeDB

MAX_CLIENTS = 16


def parse_levels(raw: str) -> list[int]:
    levels = list(dict.fromkeys(int(value) for value in raw.split(",")))
    if not levels or min(levels) < 1 or max(levels) > MAX_CLIENTS:
        raise ValueError(f"client levels must stay between 1 and {MAX_CLIENTS}")
    return levels


def run_client(storage, tree_id: str, beam_size: int, queries: int) -> tuple[list[float], str]:
    samples = []
    first_fingerprint = ""
    for index in range(queries):
        started = time.perf_counter_ns()
        result = run_query(storage, tree_id, beam_size)
        samples.append((time.perf_counter_ns() - started) / 1_000)
        if index == 0:
            first_fingerprint = fingerprint(result)
    return samples, first_fingerprint


def run_arm(storage_cls, db_path: Path, tree_id: str, clients: int, queries: int, beam_size: int):
    storages = [storage_cls(str(db_path)) for _ in range(clients)]
    try:
        for storage in storages:
            run_query(storage, tree_id, beam_size)
        started = time.perf_counter_ns()
        with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as executor:
            futures = [
                executor.submit(run_client, storage, tree_id, beam_size, queries)
                for storage in storages
            ]
            results = [future.result() for future in futures]
        wall_seconds = (time.perf_counter_ns() - started) / 1_000_000_000
    finally:
        for storage in storages:
            storage.close()

    samples = [sample for client_samples, _ in results for sample in client_samples]
    fingerprints = {client_fingerprint for _, client_fingerprint in results}
    if len(fingerprints) != 1:
        raise RuntimeError("clients returned different results")
    return {
        "queries": len(samples),
        "wall_seconds": wall_seconds,
        "throughput_qps": len(samples) / wall_seconds,
        "latency_p50_us": percentile(samples, 0.50),
        "latency_p95_us": percentile(samples, 0.95),
        "fingerprint": fingerprints.pop(),
    }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-levels", default="1,4,16")
    parser.add_argument("--queries-per-client", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--branches", type=int, default=8)
    parser.add_argument("--leaves-per-branch", type=int, default=8)
    parser.add_argument("--payload-bytes", type=int, default=1024)
    parser.add_argument("--beam-size", type=int, default=DEFAULT_BEAM_SIZE)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        client_levels = parse_levels(args.client_levels)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if args.queries_per_client < 10 or args.rounds < 1:
        raise SystemExit("queries-per-client must be >= 10 and rounds must be >= 1")
    if args.beam_size < 2 or args.beam_size > args.branches:
        raise SystemExit("beam-size must be between 2 and branches")

    script_path = Path(__file__).resolve()
    repo_root = script_path.parents[2]
    logical_cpus = os.cpu_count()
    load_average_before = list(os.getloadavg())
    tree, entities = build_tree(args.branches, args.leaves_per_branch, args.payload_bytes)
    rounds = []
    with tempfile.TemporaryDirectory(prefix="condb-beam-load-") as temp_dir:
        db_path = Path(temp_dir) / "tree.sqlite"
        with TreeDB(str(db_path)) as setup_storage:
            tree_id = setup_storage.ingest_tree(tree, entities=entities)
            sample = run_query(setup_storage, tree_id, args.beam_size)
            if len(sample.nodes) != 1 or len(sample.contents) != 1:
                raise RuntimeError("default select_k must return one node with content")
            selected_node = setup_storage.get_node(tree_id, sample.nodes[0])
            if not selected_node or selected_node.node_type != TreeDB.LEAF:
                raise RuntimeError("default query did not return a leaf node")

        for round_index in range(args.rounds):
            for clients in client_levels:
                arm_order = ("scalar", "batched") if round_index % 2 == 0 else ("batched", "scalar")
                row = {"round": round_index + 1, "clients": clients}
                for arm in arm_order:
                    storage_cls = ScalarTreeDB if arm == "scalar" else TreeDB
                    row[arm] = run_arm(
                        storage_cls,
                        db_path,
                        tree_id,
                        clients,
                        args.queries_per_client,
                        args.beam_size,
                    )
                if row["scalar"]["fingerprint"] != row["batched"]["fingerprint"]:
                    raise RuntimeError("scalar and batched arms returned different results")
                row["ratio"] = {
                    "throughput": row["batched"]["throughput_qps"] / row["scalar"]["throughput_qps"],
                    "latency_p50": row["scalar"]["latency_p50_us"] / row["batched"]["latency_p50_us"],
                    "latency_p95": row["scalar"]["latency_p95_us"] / row["batched"]["latency_p95_us"],
                }
                rounds.append(row)

    result = {
        "benchmark": "sqlite_production_beam_batching_load",
        "generated_at_unix_ms": int(time.time() * 1000),
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "logical_cpus": logical_cpus,
            "load_average_before": load_average_before,
            "load_average_after": list(os.getloadavg()),
        },
        "provenance": {
            "argv": sys.argv,
            "git_head": git_head(repo_root),
            "script_sha256": file_sha256(script_path),
            "single_client_script_sha256": file_sha256(
                repo_root / "bench/db/bench_sqlite_beam_batching.py"
            ),
            "storage_sha256": file_sha256(repo_root / "contextdb/core/storage.py"),
            "beam_retriever_sha256": file_sha256(
                repo_root / "contextdb/retriever/algorithm/beam_retriever.py"
            ),
        },
        "concurrency": {
            "client_levels": client_levels,
            "max_concurrent_clients": max(client_levels),
            "hard_cap": MAX_CLIENTS,
            "connection_per_client": True,
        },
        "workload": {
            "queries_per_client": args.queries_per_client,
            "rounds": args.rounds,
            "branches": args.branches,
            "leaves_per_branch": args.leaves_per_branch,
            "beam_size": args.beam_size,
            "select_k": 1,
            "max_turns": "tree-depth default",
            "payload_bytes": args.payload_bytes,
            "storage": "file-backed SQLite with warm caches",
        },
        "rounds": rounds,
    }
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
