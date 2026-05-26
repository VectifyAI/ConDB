#!/usr/bin/env python3
"""
Document-mode retriever benchmark.

Compares retrievers (Block / Beam / Vertical ...) on a single hierarchical
document. Reports time, LLM calls, token usage (incl. prompt cache),
and total USD cost.

Usage:
    python bench/run_document_bench.py --doc <document.json> --config <queries.json>

Example:
    python bench/run_document_bench.py \
        --doc examples/large_doc.json \
        --config bench/queries.json
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from contextdb import TreeDB
from contextdb.config import Config, get_llm_config
from contextdb.metrics import LLMWithStats, StatisticsRecorder
from contextdb.retriever.algorithm.base_retriever import BaseRetriever

ALGORITHM_DIR = Path(__file__).parent.parent / "contextdb/retriever/algorithm"
EXCLUDED_FILES = {"base_retriever.py", "block_cutter.py", "block_types.py", "__init__.py"}


def discover_retrievers() -> dict[str, type]:
    retrievers: dict[str, type] = {}
    seen_classes: set[type] = set()

    for py_file in ALGORITHM_DIR.glob("*.py"):
        if py_file.name in EXCLUDED_FILES or py_file.name.startswith("_"):
            continue

        module_name = f"contextdb.retriever.algorithm.{py_file.stem}"
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            print(f"Warning: Failed to import {module_name}: {e}")
            continue

        for _attr_name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, BaseRetriever)
                and obj is not BaseRetriever
                and obj.__module__ == module_name
                and obj not in seen_classes
            ):
                name = obj.__name__.replace("Retriever", "")
                retrievers[name] = obj
                seen_classes.add(obj)

    return retrievers


# ── Data classes ────────────────────────────────────────────────────


@dataclass
class RetrieverMetrics:
    name: str
    time_seconds: float
    turns: int
    nodes_selected: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    nodes_pruned: int = 0
    blocks_processed: int = 0
    error: Optional[str] = None


@dataclass
class QueryResult:
    query: str
    results: dict[str, RetrieverMetrics] = field(default_factory=dict)


@dataclass
class BenchmarkResult:
    document_path: str
    document_sections: int
    tree_id: str
    retriever_names: list[str] = field(default_factory=list)
    queries: list[QueryResult] = field(default_factory=list)

    def summary(self, pricing: dict = None) -> dict:
        def total(items, attr):
            return sum(getattr(i, attr) for i in items) if items else 0

        p = pricing or {}
        pi = p.get("price_input", 3)
        po = p.get("price_output", 15)
        pcw = p.get("price_cache_write", 3.75)
        pcr = p.get("price_cache_read", 0.30)

        out = {"queries_run": len(self.queries)}
        for name in self.retriever_names:
            valid = [
                q.results[name]
                for q in self.queries
                if name in q.results and not q.results[name].error
            ]
            total_input = total(valid, "input_tokens")
            total_output = total(valid, "output_tokens")
            total_cache_read = total(valid, "cache_read_tokens")
            total_cache_write = total(valid, "cache_creation_tokens")

            uncached_input = total_input - total_cache_read - total_cache_write
            cost = (
                uncached_input * pi
                + total_output * po
                + total_cache_write * pcw
                + total_cache_read * pcr
            ) / 1_000_000

            n = len(valid) if valid else 1
            out[name] = {
                "avg_time": total(valid, "time_seconds") / n,
                "avg_llm_calls": total(valid, "llm_calls") / n,
                "avg_input_tokens": total_input / n,
                "avg_output_tokens": total_output / n,
                "avg_cache_read": total_cache_read / n,
                "avg_cache_write": total_cache_write / n,
                "total_cost": cost,
                "successful_queries": len(valid),
            }
        return out


# ── Tree construction ──────────────────────────────────────────────


def convert_flat_to_tree(flat_list: list) -> dict:
    if not flat_list:
        return {"type": "object", "children": {}}

    root = {"type": "object", "attrs": {"title": "Document Root"}, "children": {}}
    stack = [(root, 0)]

    for i, item in enumerate(flat_list):
        level = item.get("level", 1)
        title = item.get("title", f"Section {i}")
        text = item.get("text", "")
        node_id = f"node_{i}"

        new_node = {
            "type": "object",
            "attrs": {"title": title, "summary": text[:300] if text else ""},
            "entity_id": node_id,
            "children": {},
        }

        while len(stack) > 1 and stack[-1][1] >= level:
            stack.pop()

        parent = stack[-1][0]
        parent["children"][node_id] = new_node
        stack.append((new_node, level))

    return root


def build_entities(flat_list: list) -> dict:
    entities = {}
    for i, item in enumerate(flat_list):
        node_id = f"node_{i}"
        entities[node_id] = {
            "type": "section",
            "title": item.get("title", ""),
            "text": item.get("text", ""),
            "level": item.get("level", 1),
        }
    return entities


# ── Runner ──────────────────────────────────────────────────────────


def run_retriever(
    retriever,
    name: str,
    tree_id: str,
    query: str,
    llm: "LLMWithStats",
    beam_size: int = 3,
    max_turns: int = 10,
) -> RetrieverMetrics:
    fresh_recorder = StatisticsRecorder()
    llm.recorder = fresh_recorder

    start_time = time.time()
    try:
        result = retriever.retrieve(tree_id, query, beam_size=beam_size, max_turns=max_turns)
        elapsed = time.time() - start_time
        return RetrieverMetrics(
            name=name,
            time_seconds=elapsed,
            turns=result.turns,
            nodes_selected=len(result.nodes),
            llm_calls=getattr(result, "total_llm_calls", result.turns),
            input_tokens=fresh_recorder.input_tokens,
            output_tokens=fresh_recorder.output_tokens,
            cache_read_tokens=fresh_recorder.cache_read_tokens,
            cache_creation_tokens=fresh_recorder.cache_creation_tokens,
            blocks_processed=getattr(result, "blocks_processed", 0),
        )
    except Exception as e:
        elapsed = time.time() - start_time
        return RetrieverMetrics(
            name=name,
            time_seconds=elapsed,
            turns=0,
            nodes_selected=0,
            llm_calls=0,
            input_tokens=fresh_recorder.input_tokens,
            output_tokens=fresh_recorder.output_tokens,
            cache_read_tokens=fresh_recorder.cache_read_tokens,
            cache_creation_tokens=fresh_recorder.cache_creation_tokens,
            error=str(e),
        )


def run_benchmark(
    *,
    doc_path: Path,
    query_list: list,
    beam_size: int = 3,
    max_turns: int = 10,
    clear_cache: bool = False,
    retrievers: list[str] | None = None,
) -> BenchmarkResult:
    retriever_classes = discover_retrievers()
    print(f"\nDiscovered retrievers: {', '.join(retriever_classes.keys())}")
    if retrievers:
        selected: dict[str, type] = {}
        unknown: list[str] = []
        for name in retrievers:
            key = name.strip()
            if not key:
                continue
            cls = retriever_classes.get(key)
            if cls is None:
                unknown.append(key)
                continue
            selected[key] = cls
        if unknown:
            print(f"Warning: Unknown retrievers ignored: {', '.join(unknown)}")
        if not selected:
            raise ValueError("No valid retrievers selected")
        retriever_classes = selected
        print(f"Selected retrievers: {', '.join(retriever_classes.keys())}")

    llm = Config.get_llm_client()
    llm_with_stats = LLMWithStats(llm, StatisticsRecorder())

    with open(doc_path) as f:
        flat_data = json.load(f)
    tree_structure = convert_flat_to_tree(flat_data)
    entities = build_entities(flat_data)

    result = BenchmarkResult(
        document_path=str(doc_path),
        document_sections=len(entities),
        tree_id="(per-retriever)",
        retriever_names=list(retriever_classes.keys()),
    )

    query_results = {q: QueryResult(query=q) for q in query_list}

    for name, cls in retriever_classes.items():
        print(f"\n{'='*70}")
        print(f"[{name}Retriever] Running {len(query_list)} queries...")
        print("=" * 70)

        db = TreeDB(":memory:")
        tree_id = db.ingest_tree(tree_structure, entities=entities)

        try:
            sig = inspect.signature(cls.__init__)
            kwargs: dict = {}
            if "mode" in sig.parameters:
                kwargs["mode"] = "document"
            retriever = cls(db, llm_with_stats, **kwargs)
        except Exception as e:
            print(f"Warning: Failed to instantiate {name}: {e}")
            db.close()
            continue

        for i, query in enumerate(query_list):
            print(f"\n  Query {i+1}: {query[:60]}...")

            if clear_cache and i > 0 and hasattr(retriever, "clear_cache"):
                retriever.clear_cache()

            metrics = run_retriever(
                retriever, name, tree_id, query, llm_with_stats,
                beam_size, max_turns,
            )
            query_results[query].results[name] = metrics

            if metrics.error:
                print(f"    ERROR: {metrics.error}")
            else:
                parts = [f"Time: {metrics.time_seconds:.2f}s, LLM: {metrics.llm_calls}"]
                parts.append(f"Input: {metrics.input_tokens:,}")
                if metrics.cache_read_tokens > 0:
                    parts.append(f"Cache read: {metrics.cache_read_tokens:,}")
                print(f"    {', '.join(parts)}")

        db.close()

    for query in query_list:
        result.queries.append(query_results[query])

    return result


# ── Summary ─────────────────────────────────────────────────────────


def print_summary(result: BenchmarkResult):
    pricing = get_llm_config(Config.LLM_PROVIDER, Config.LLM_MODEL)
    summary = result.summary(pricing)

    print("\n" + "=" * 70)
    print("BENCHMARK SUMMARY")
    print("=" * 70)
    print(f"\nModel: {Config.LLM_PROVIDER}/{Config.LLM_MODEL}")
    print(f"Document: {result.document_path}")
    print(f"Entities: {result.document_sections}")
    print(f"Queries: {summary['queries_run']}")
    print(f"Retrievers: {', '.join(result.retriever_names)}")

    headers = ["Metric"] + result.retriever_names
    rows = []

    for label, key, fmt in [
        ("Time (s)", "avg_time", "{:.2f}"),
        ("LLM Calls", "avg_llm_calls", "{:.1f}"),
        ("Input Tokens", "avg_input_tokens", "{:,.0f}"),
        ("Cache Write", "avg_cache_write", "{:,.0f}"),
        ("Cache Read", "avg_cache_read", "{:,.0f}"),
        ("Cost ($)", "total_cost", "{:.4f}"),
    ]:
        row = [label]
        for name in result.retriever_names:
            row.append(fmt.format(summary[name][key]))
        rows.append(row)

    col_widths = [max(len(str(row[i])) for row in [headers] + rows) for i in range(len(headers))]
    header_line = " | ".join(h.ljust(w) for h, w in zip(headers, col_widths))
    print(f"\n{header_line}")
    print("-" * len(header_line))
    for row in rows:
        print(" | ".join(str(cell).ljust(w) for cell, w in zip(row, col_widths)))
    print("\n" + "=" * 70)


def load_config(config_path: Path) -> dict:
    with open(config_path) as f:
        return json.load(f)


# ── Main ────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Document-mode retriever benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--doc", "-d", type=Path, required=True,
                        help="Path to document JSON file")
    parser.add_argument("--config", "-c", type=Path, required=True,
                        help="Path to queries config JSON")
    parser.add_argument("--output", "-o", choices=["text", "json"], default="text")
    parser.add_argument("--beam-size", "-b", type=int, default=3)
    parser.add_argument("--max-turns", "-t", type=int, default=10)
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument(
        "--retrievers",
        type=str,
        default="",
        help="Comma-separated retriever names, e.g. Block,Beam,Vertical",
    )

    args = parser.parse_args()
    selected = [x.strip() for x in args.retrievers.split(",") if x.strip()]

    try:
        Config.validate()
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if not args.doc.exists():
        sys.exit(f"ERROR: doc not found: {args.doc}")
    if not args.config.exists():
        sys.exit(f"ERROR: config not found: {args.config}")

    config = load_config(args.config)
    queries = config.get("queries", [])
    if not queries:
        sys.exit("ERROR: No queries in config")

    print("=" * 70)
    print("Document Retriever Benchmark")
    print("=" * 70)
    result = run_benchmark(
        doc_path=args.doc,
        query_list=queries,
        beam_size=args.beam_size,
        max_turns=args.max_turns,
        clear_cache=args.clear_cache,
        retrievers=selected or None,
    )

    if args.output == "json":
        print(json.dumps({
            "document_path": result.document_path,
            "document_sections": result.document_sections,
            "retrievers": result.retriever_names,
            "queries": [
                {"query": q.query, "results": {n: asdict(m) for n, m in q.results.items()}}
                for q in result.queries
            ],
            "summary": result.summary(get_llm_config(Config.LLM_PROVIDER, Config.LLM_MODEL)),
        }, indent=2))
    else:
        print_summary(result)


if __name__ == "__main__":
    main()
