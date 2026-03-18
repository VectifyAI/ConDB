#!/usr/bin/env python3
"""Grid-search max_tokens_per_block for filesystem BlockRetriever cache savings."""

import argparse
import json
import os
import time
import uuid
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from contextdb import TreeDB
from contextdb.adapter.filesystem import DEFAULT_IGNORE_PATTERNS, FileSystemAdapter
from contextdb.config import Config
from contextdb.metrics import LLMWithStats, StatisticsRecorder
from contextdb.retriever.algorithm.block_retriever import BlockRetriever

PRICE_INPUT = 3.0
PRICE_OUTPUT = 15.0
PRICE_CACHE_WRITE = 3.75
PRICE_CACHE_READ = 0.30

SCENARIO_ROOT = Path("bench/filesystem")


def _parse_list(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _fs_prefix_key(ground_truth: list[str]) -> str:
    if not ground_truth:
        return ""
    first = str(ground_truth[0]).strip().lstrip("/")
    return first.split("/", 1)[0] if first else ""


def _reorder_queries_by_prefix(queries_with_gt: list[dict]) -> list[dict]:
    indexed = list(enumerate(queries_with_gt))
    indexed.sort(key=lambda x: (_fs_prefix_key(x[1].get("ground_truth", [])), x[0]))
    return [q for _, q in indexed]


def _costs(total_input: int, total_output: int, total_cache_write: int, total_cache_read: int) -> tuple[float, float, float]:
    actual = (
        total_input * PRICE_INPUT
        + total_output * PRICE_OUTPUT
        + total_cache_write * PRICE_CACHE_WRITE
        + total_cache_read * PRICE_CACHE_READ
    ) / 1_000_000
    nocache = (
        (total_input + total_cache_write + total_cache_read) * PRICE_INPUT
        + total_output * PRICE_OUTPUT
    ) / 1_000_000
    saved = nocache - actual
    return actual, nocache, saved


def run_scenario(
    name: str,
    max_tokens: int,
    cache_subtree_block: bool,
    beam_size: int,
    max_turns: int,
    salt: str,
) -> dict:
    root = SCENARIO_ROOT / name
    cfg_path = root / "queries.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    queries_with_gt = _reorder_queries_by_prefix(cfg.get("queries", []))

    ignore_patterns = list(DEFAULT_IGNORE_PATTERNS)
    try:
        rel_cfg = str(cfg_path.resolve().relative_to(root.resolve())).replace("\\", "/")
        ignore_patterns.append(rel_cfg)
    except ValueError:
        pass

    adapter = FileSystemAdapter(str(root), ignore_patterns=ignore_patterns)
    tree, entities = adapter.convert()

    db = TreeDB(":memory:")
    tree_id = db.ingest_tree(tree, entities=entities)

    llm = Config.get_llm_client()
    llm_with_stats = LLMWithStats(llm, StatisticsRecorder())
    retriever = BlockRetriever(
        db,
        llm_with_stats,
        max_tokens_per_block=max_tokens,
        cache_current_block=True,
        cache_subtree_block=cache_subtree_block,
        mode="filesystem",
    )

    total_time = 0.0
    total_calls = 0
    total_input = 0
    total_output = 0
    total_cache_write = 0
    total_cache_read = 0
    ok = 0
    failures = []

    old_salt = os.getenv("CONDB_CACHE_KEY_SALT")
    os.environ["CONDB_CACHE_KEY_SALT"] = salt
    try:
        for i, item in enumerate(queries_with_gt, start=1):
            query = item.get("query", "")
            fresh = StatisticsRecorder()
            llm_with_stats.recorder = fresh
            started = time.time()
            try:
                result = retriever.retrieve(tree_id, query, beam_size=beam_size, max_turns=max_turns)
                elapsed = time.time() - started
                total_time += elapsed
                total_calls += int(getattr(result, "total_llm_calls", result.turns))
                total_input += fresh.input_tokens
                total_output += fresh.output_tokens
                total_cache_write += fresh.cache_creation_tokens
                total_cache_read += fresh.cache_read_tokens
                ok += 1
                print(
                    f"[{name}] q{i}/{len(queries_with_gt)} ok "
                    f"time={elapsed:.2f}s calls={getattr(result, 'total_llm_calls', result.turns)} "
                    f"in={fresh.input_tokens} cr={fresh.cache_read_tokens}",
                    flush=True,
                )
            except Exception as exc:
                elapsed = time.time() - started
                failures.append(str(exc))
                print(f"[{name}] q{i}/{len(queries_with_gt)} err time={elapsed:.2f}s err={exc}", flush=True)
    finally:
        if old_salt is None:
            os.environ.pop("CONDB_CACHE_KEY_SALT", None)
        else:
            os.environ["CONDB_CACHE_KEY_SALT"] = old_salt
        db.close()

    actual, nocache, saved = _costs(total_input, total_output, total_cache_write, total_cache_read)

    return {
        "scenario": name,
        "max_tokens_per_block": max_tokens,
        "cache_subtree_block": cache_subtree_block,
        "queries_total": len(queries_with_gt),
        "queries_ok": ok,
        "queries_failed": len(queries_with_gt) - ok,
        "avg_time_s": (total_time / ok) if ok else 0.0,
        "avg_llm_calls": (total_calls / ok) if ok else 0.0,
        "avg_input": (total_input / ok) if ok else 0.0,
        "avg_output": (total_output / ok) if ok else 0.0,
        "avg_cache_write": (total_cache_write / ok) if ok else 0.0,
        "avg_cache_read": (total_cache_read / ok) if ok else 0.0,
        "total_input": total_input,
        "total_output": total_output,
        "total_cache_write": total_cache_write,
        "total_cache_read": total_cache_read,
        "actual_cost_usd": actual,
        "nocache_counterfactual_usd": nocache,
        "saved_usd": saved,
        "saved_pct_vs_nocache": (saved / nocache * 100) if nocache > 0 else 0.0,
        "error_samples": failures[:3],
    }


def aggregate_param(max_tokens: int, cache_subtree_block: bool, rows: list[dict]) -> dict:
    agg_actual = sum(r["actual_cost_usd"] for r in rows)
    agg_nocache = sum(r["nocache_counterfactual_usd"] for r in rows)
    agg_saved = agg_nocache - agg_actual
    total_ok = sum(r["queries_ok"] for r in rows)
    total_queries = sum(r["queries_total"] for r in rows)
    total_calls = sum(r["avg_llm_calls"] * r["queries_ok"] for r in rows)
    avg_calls = (total_calls / total_ok) if total_ok else 0.0
    avg_time = (
        sum(r["avg_time_s"] * r["queries_ok"] for r in rows) / total_ok
        if total_ok
        else 0.0
    )

    return {
        "max_tokens_per_block": max_tokens,
        "cache_subtree_block": cache_subtree_block,
        "queries_ok": total_ok,
        "queries_total": total_queries,
        "avg_time_s": avg_time,
        "avg_llm_calls": avg_calls,
        "actual_cost_usd": agg_actual,
        "nocache_counterfactual_usd": agg_nocache,
        "saved_usd": agg_saved,
        "saved_pct_vs_nocache": (agg_saved / agg_nocache * 100) if agg_nocache > 0 else 0.0,
        "scenario_rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description="Tune filesystem block size for cache savings")
    parser.add_argument(
        "--grid",
        default="16000,8000,4000,2500,2000,1500",
        help="Comma-separated max_tokens_per_block candidates",
    )
    parser.add_argument(
        "--scenarios",
        default="context7,arxiv,repo",
        help="Comma-separated scenario names under bench/filesystem",
    )
    parser.add_argument(
        "--cache-subtree-grid",
        default="1",
        help="Comma-separated bools for caching fs subtree blocks (1/0, true/false, yes/no)",
    )
    parser.add_argument("--beam-size", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--output-json", type=Path, default=None)
    args = parser.parse_args()

    Config.validate()

    grid = [int(x) for x in _parse_list(args.grid)]
    cache_subtree_grid = []
    for item in _parse_list(args.cache_subtree_grid):
        value = item.lower()
        if value in {"1", "true", "yes", "on"}:
            cache_subtree_grid.append(True)
        elif value in {"0", "false", "no", "off"}:
            cache_subtree_grid.append(False)
        else:
            raise ValueError(f"Invalid cache-subtree-grid value: {item}")
    if not cache_subtree_grid:
        raise ValueError("Empty cache-subtree-grid")
    # deduplicate while preserving order
    cache_subtree_grid = list(dict.fromkeys(cache_subtree_grid))
    scenarios = _parse_list(args.scenarios)
    if not grid:
        raise ValueError("Empty grid")
    if not scenarios:
        raise ValueError("Empty scenarios")

    run_id = uuid.uuid4().hex[:8]
    all_results = []
    started = time.time()

    total_jobs = len(grid) * len(cache_subtree_grid)
    job_idx = 0
    for max_tokens in grid:
        for cache_subtree_block in cache_subtree_grid:
            job_idx += 1
            print(
                f"\n=== GRID {job_idx}/{total_jobs} "
                f"max_tokens_per_block={max_tokens} cache_subtree_block={cache_subtree_block} ===",
                flush=True,
            )
            scenario_rows = []
            salt = f"grid-{run_id}-t{max_tokens}-sub{int(cache_subtree_block)}"
            for scenario in scenarios:
                print(f"-- scenario: {scenario}", flush=True)
                row = run_scenario(
                    scenario,
                    max_tokens=max_tokens,
                    cache_subtree_block=cache_subtree_block,
                    beam_size=args.beam_size,
                    max_turns=args.max_turns,
                    salt=salt,
                )
                scenario_rows.append(row)
            agg = aggregate_param(max_tokens, cache_subtree_block, scenario_rows)
            all_results.append(agg)
            print(
                f"[param={max_tokens},subtree_cache={cache_subtree_block}] saved=${agg['saved_usd']:.4f} "
                f"({agg['saved_pct_vs_nocache']:.2f}%), avg_calls={agg['avg_llm_calls']:.2f}, "
                f"ok={agg['queries_ok']}/{agg['queries_total']}",
                flush=True,
            )

    elapsed = time.time() - started
    ranked = sorted(
        all_results,
        key=lambda x: (
            x["queries_ok"] == x["queries_total"],
            x["saved_pct_vs_nocache"],
            x["saved_usd"],
        ),
        reverse=True,
    )
    summary = {
        "run_id": run_id,
        "elapsed_s": elapsed,
        "grid": grid,
        "cache_subtree_grid": cache_subtree_grid,
        "scenarios": scenarios,
        "ranked": ranked,
        "best": ranked[0] if ranked else None,
    }

    print("\n=== GRID SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nSaved summary to: {args.output_json}")


if __name__ == "__main__":
    main()
