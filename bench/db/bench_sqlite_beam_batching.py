#!/usr/bin/env python3
"""Measure production Beam short-read coalescing on the SQLite storage path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("LOG_LEVEL", "WARNING")

from contextdb.core.storage import TreeDB
from contextdb.retriever.algorithm.beam_retriever import BeamRetriever

DEFAULT_BEAM_SIZE = 3


class FixedRanker:
    """Return a fixed two-turn ranking without network latency."""

    def __init__(self, beam_size: int):
        self.beam_size = beam_size
        self.calls = 0

    def chat(self, messages, system="", tools=None, cache_key=None):
        self.calls += 1
        candidate_ids = re.findall(r"- id: ([0-9a-f-]+)", messages[0]["content"])
        ranked_ids = candidate_ids[:self.beam_size] if self.calls == 1 else candidate_ids[:1]
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank-call",
                    "name": "rank",
                    "input": {"ranked_ids": ranked_ids, "done": self.calls >= 2},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }


class ScalarTreeDB(TreeDB):
    """TreeDB variant that hides optional batch methods from BeamRetriever."""

    _BATCH_METHODS = {"get_children_many", "get_entities"}

    def __getattribute__(self, name):
        if name in super().__getattribute__("_BATCH_METHODS"):
            raise AttributeError(name)
        return super().__getattribute__(name)


def build_tree(branches: int, leaves_per_branch: int, payload_bytes: int) -> tuple[dict, dict]:
    tree: dict[str, Any] = {"type": "object", "children": {}}
    entities = {}
    for branch in range(branches):
        branch_entity_id = f"branch-{branch}"
        entities[branch_entity_id] = {
            "type": "section",
            "title": branch_entity_id,
            "text": "x" * payload_bytes,
        }
        children = {}
        for leaf in range(leaves_per_branch):
            entity_id = f"entity-{branch}-{leaf}"
            entities[entity_id] = {
                "type": "text",
                "title": entity_id,
                "text": "x" * payload_bytes,
            }
            children[f"leaf-{leaf:03d}"] = {"type": "leaf", "entity_id": entity_id}
        tree["children"][f"branch-{branch:03d}"] = {
            "type": "object",
            "entity_id": branch_entity_id,
            "children": children,
        }
    return tree, entities


def run_query(storage, tree_id: str, beam_size: int):
    return BeamRetriever(storage, FixedRanker(beam_size)).retrieve(
        tree_id,
        "find one leaf",
        beam_size=beam_size,
    )


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def summarize(values_us: list[float]) -> dict[str, float]:
    return {
        "p50_us": statistics.median(values_us),
        "p95_us": percentile(values_us, 0.95),
        "mean_us": statistics.fmean(values_us),
    }


def fingerprint(result) -> str:
    payload = {
        "nodes": result.nodes,
        "contents": result.contents,
        "trace": result.trace,
        "turns": result.turns,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def trace_selects(storage: TreeDB, query) -> list[str]:
    statements = []
    storage.conn.set_trace_callback(
        lambda statement: statements.append(statement)
        if statement.lstrip().upper().startswith("SELECT")
        else None
    )
    try:
        query()
    finally:
        storage.conn.set_trace_callback(None)
    return statements


def timed_round(
    scalar_storage: ScalarTreeDB,
    batched_storage: TreeDB,
    tree_id: str,
    beam_size: int,
    repeats: int,
) -> dict[str, Any]:
    samples = {"scalar": [], "batched": []}
    for repetition in range(repeats):
        order = (
            (("scalar", scalar_storage), ("batched", batched_storage))
            if repetition % 2 == 0
            else (("batched", batched_storage), ("scalar", scalar_storage))
        )
        for name, storage in order:
            started = time.perf_counter_ns()
            run_query(storage, tree_id, beam_size)
            samples[name].append((time.perf_counter_ns() - started) / 1_000)

    scalar = summarize(samples["scalar"])
    batched = summarize(samples["batched"])
    return {
        "scalar": scalar,
        "batched": batched,
        "speedup_at_marginal_percentile": {
            "p50": scalar["p50_us"] / batched["p50_us"],
            "p95": scalar["p95_us"] / batched["p95_us"],
        },
    }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--branches", type=int, default=8)
    parser.add_argument("--leaves-per-branch", type=int, default=8)
    parser.add_argument("--payload-bytes", type=int, default=1024)
    parser.add_argument("--beam-size", type=int, default=DEFAULT_BEAM_SIZE)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--repeats-per-round", type=int, default=250)
    parser.add_argument("--warmups", type=int, default=30)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.branches < 2 or args.leaves_per_branch < 1:
        raise SystemExit("branches must be >= 2 and leaves-per-branch must be >= 1")
    if args.beam_size < 2 or args.beam_size > args.branches:
        raise SystemExit("beam-size must be between 2 and branches")
    if args.rounds < 1 or args.repeats_per_round < 20 or args.warmups < 0:
        raise SystemExit("rounds must be >= 1, repeats-per-round >= 20, and warmups >= 0")

    script_path = Path(__file__).resolve()
    repo_root = script_path.parents[2]
    logical_cpus = os.cpu_count()
    load_average_before = list(os.getloadavg())
    tree, entities = build_tree(args.branches, args.leaves_per_branch, args.payload_bytes)
    with tempfile.TemporaryDirectory(prefix="condb-beam-batch-") as temp_dir:
        batched_path = Path(temp_dir) / "batched.sqlite"
        scalar_path = Path(temp_dir) / "scalar.sqlite"
        with TreeDB(str(batched_path)) as batched_storage, ScalarTreeDB(str(scalar_path)) as scalar_storage:
            tree_id = batched_storage.ingest_tree(tree, entities=entities)
            batched_storage.conn.backup(scalar_storage.conn)
            scalar_result = run_query(scalar_storage, tree_id, args.beam_size)
            batched_result = run_query(batched_storage, tree_id, args.beam_size)
            scalar_fingerprint = fingerprint(scalar_result)
            batched_fingerprint = fingerprint(batched_result)
            if scalar_fingerprint != batched_fingerprint:
                raise RuntimeError("scalar and batched retrieval outputs differ")
            if len(batched_result.nodes) != 1 or len(batched_result.contents) != 1:
                raise RuntimeError("default select_k must return one node with content")
            selected_node = batched_storage.get_node(tree_id, batched_result.nodes[0])
            if not selected_node or selected_node.node_type != TreeDB.LEAF:
                raise RuntimeError("default query did not return a leaf node")

            select_statements = {
                "scalar": trace_selects(
                    scalar_storage,
                    lambda: run_query(scalar_storage, tree_id, args.beam_size),
                ),
                "batched": trace_selects(
                    batched_storage,
                    lambda: run_query(batched_storage, tree_id, args.beam_size),
                ),
            }

            scalar_short_reads = (
                2
                + args.branches
                + args.beam_size * (args.leaves_per_branch + 1)
            )
            batched_short_reads = 5
            expected_selects = {
                "scalar": scalar_short_reads + 2,
                "batched": batched_short_reads + 2,
            }
            actual_selects = {name: len(statements) for name, statements in select_statements.items()}
            if actual_selects != expected_selects:
                raise RuntimeError(
                    f"unexpected SELECT counts: expected {expected_selects}, got {actual_selects}"
                )

            for _ in range(args.warmups):
                run_query(scalar_storage, tree_id, args.beam_size)
                run_query(batched_storage, tree_id, args.beam_size)

            rounds = [
                timed_round(
                    scalar_storage,
                    batched_storage,
                    tree_id,
                    args.beam_size,
                    args.repeats_per_round,
                )
                for _ in range(args.rounds)
            ]

    result = {
        "benchmark": "sqlite_production_beam_batching",
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
            "storage_sha256": file_sha256(repo_root / "contextdb/core/storage.py"),
            "beam_retriever_sha256": file_sha256(
                repo_root / "contextdb/retriever/algorithm/beam_retriever.py"
            ),
        },
        "concurrency": {
            "client_threads": 1,
            "background_threads": 0,
            "max_concurrent_clients": 1,
        },
        "workload": {
            "branches": args.branches,
            "leaves_per_branch": args.leaves_per_branch,
            "beam_size": args.beam_size,
            "candidate_leaves_on_second_turn": args.beam_size * args.leaves_per_branch,
            "payload_bytes": args.payload_bytes,
            "turns": 2,
            "select_k": 1,
            "max_turns": "tree-depth default",
            "storage": "file-backed SQLite with warm caches",
            "rounds": args.rounds,
            "repeats_per_round": args.repeats_per_round,
            "warmups_per_arm": args.warmups,
            "order": "alternating scalar-first and batched-first",
        },
        "equivalence": {
            "matched": True,
            "sha256": scalar_fingerprint,
            "nodes": 1,
            "contents": 1,
            "selected_node_type": "leaf",
        },
        "sqlite_select_statements_per_query": {
            name: {
                "count": len(statements),
                "sha256": hashlib.sha256(
                    "\n-- statement --\n".join(statements).encode()
                ).hexdigest(),
            }
            for name, statements in select_statements.items()
        },
        "batchable_short_reads_per_query": {
            "scalar": scalar_short_reads,
            "batched": batched_short_reads,
            "derivation": (
                "scalar: root children + first-turn entities + frontier children "
                "+ second-turn entities + final entity; batched: the same five logical stages"
            ),
        },
        "rounds": rounds,
        "speedup_range": {
            "p50_min": min(round_["speedup_at_marginal_percentile"]["p50"] for round_ in rounds),
            "p50_max": max(round_["speedup_at_marginal_percentile"]["p50"] for round_ in rounds),
            "p95_min": min(round_["speedup_at_marginal_percentile"]["p95"] for round_ in rounds),
            "p95_max": max(round_["speedup_at_marginal_percentile"]["p95"] for round_ in rounds),
        },
    }
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
