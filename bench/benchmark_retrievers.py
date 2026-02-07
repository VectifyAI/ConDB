#!/usr/bin/env python3
"""
Unified Benchmark for Retriever Comparison
==========================================

Automatically discovers and benchmarks all retrievers in contextdb/retriever/algorithm/
(excluding base_retriever.py).

Usage:
    python bench/benchmark_retrievers.py --doc <document.json> --config <queries.json>

Examples:
    python bench/benchmark_retrievers.py --doc examples/large_doc.json --config bench/queries.json
    python bench/benchmark_retrievers.py -d examples/large_doc.json -c bench/queries.json --output json
"""

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
from contextdb.config import Config
from contextdb.metrics import LLMWithStats, StatisticsRecorder
from contextdb.retriever.algorithm.base_retriever import BaseRetriever

ALGORITHM_DIR = Path(__file__).parent.parent / "contextdb/retriever/algorithm"
EXCLUDED_FILES = {"base_retriever.py", "block_cutter.py", "block_types.py", "__init__.py"}


def discover_retrievers() -> dict[str, type]:
    """Discover all retriever classes in the algorithm directory."""
    retrievers = {}

    for py_file in ALGORITHM_DIR.glob("*.py"):
        if py_file.name in EXCLUDED_FILES or py_file.name.startswith("_"):
            continue

        module_name = py_file.stem
        try:
            module = importlib.import_module(
                f"contextdb.retriever.algorithm.{module_name}"
            )
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, BaseRetriever)
                    and obj is not BaseRetriever
                    and obj.__module__ == module.__name__
                ):
                    short_name = name.replace("Retriever", "")
                    retrievers[short_name] = obj
        except Exception as e:
            print(f"Warning: Failed to load {module_name}: {e}")

    return retrievers


@dataclass
class RetrieverMetrics:
    """Metrics for a single retriever run."""
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
    """Result for a single query across all retrievers."""
    query: str
    results: dict[str, RetrieverMetrics] = field(default_factory=dict)


@dataclass
class BenchmarkResult:
    """Full benchmark result."""
    document_path: str
    document_sections: int
    tree_id: str
    retriever_names: list[str] = field(default_factory=list)
    queries: list[QueryResult] = field(default_factory=list)

    def summary(self) -> dict:
        """Generate summary statistics."""
        def avg(items, attr):
            return sum(getattr(i, attr) for i in items) / len(items) if items else 0

        def total(items, attr):
            return sum(getattr(i, attr) for i in items) if items else 0

        summary = {"queries_run": len(self.queries)}

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

            # Claude pricing: input $3/M, output $15/M, cache write $3.75/M, cache read $0.30/M
            cost = (total_input * 3 + total_output * 15 + total_cache_write * 3.75 + total_cache_read * 0.30) / 1_000_000

            n = len(valid) if valid else 1
            summary[name] = {
                "avg_time": total(valid, "time_seconds") / n,
                "avg_llm_calls": total(valid, "llm_calls") / n,
                "avg_input_tokens": total_input / n,
                "avg_output_tokens": total_output / n,
                "avg_cache_read": total_cache_read / n,
                "avg_cache_write": total_cache_write / n,
                "total_cost": cost,
                "successful_queries": len(valid),
            }

        return summary


def convert_flat_to_tree(flat_list: list) -> dict:
    """Convert flat list with levels to nested tree structure."""
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
    """Build entities dict from flat list."""
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


def run_retriever(
    retriever,
    name: str,
    tree_id: str,
    query: str,
    llm: "LLMWithStats",
    beam_size: int = 3,
    max_turns: int = 10,
) -> RetrieverMetrics:
    """Run a single retriever and collect metrics."""
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
            nodes_pruned=getattr(result, "nodes_pruned", 0),
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
    doc_path: Path,
    queries: list[str],
    beam_size: int = 3,
    max_turns: int = 10,
    clear_cache: bool = False,
) -> BenchmarkResult:
    """Run full benchmark on a document."""
    # Discover retrievers
    retriever_classes = discover_retrievers()
    print(f"\nDiscovered retrievers: {', '.join(retriever_classes.keys())}")

    # Load document
    with open(doc_path) as f:
        flat_data = json.load(f)

    tree_structure = convert_flat_to_tree(flat_data)
    entities = build_entities(flat_data)

    # Create database
    db = TreeDB(":memory:")
    tree_id = db.ingest_tree(tree_structure, entities=entities)

    # Create LLM client with stats
    llm = Config.get_llm_client()
    llm_with_stats = LLMWithStats(llm, StatisticsRecorder())

    # Instantiate retrievers
    retrievers = {}
    for name, cls in retriever_classes.items():
        try:
            retrievers[name] = cls(db, llm_with_stats)
        except Exception as e:
            print(f"Warning: Failed to instantiate {name}: {e}")

    result = BenchmarkResult(
        document_path=str(doc_path),
        document_sections=len(entities),
        tree_id=tree_id,
        retriever_names=list(retrievers.keys()),
    )

    # Initialize query results
    query_results = {q: QueryResult(query=q) for q in queries}

    # Run each retriever on all queries (to test cross-query cache reuse)
    for name, retriever in retrievers.items():
        print(f"\n{'='*70}")
        print(f"[{name}Retriever] Running {len(queries)} queries...")
        print("=" * 70)

        # Clear cache at start of each retriever (isolate between retrievers)
        if hasattr(retriever, "clear_cache"):
            retriever.clear_cache()

        for i, query in enumerate(queries):
            print(f"\n  Query {i+1}: {query[:50]}...")

            # Optionally clear cache between queries (default: reuse)
            if clear_cache and i > 0 and hasattr(retriever, "clear_cache"):
                retriever.clear_cache()

            metrics = run_retriever(
                retriever, name, tree_id, query, llm_with_stats, beam_size, max_turns
            )
            query_results[query].results[name] = metrics

            if metrics.error:
                print(f"    ERROR: {metrics.error}")
            else:
                print(f"    Time: {metrics.time_seconds:.2f}s, LLM: {metrics.llm_calls}, Input: {metrics.input_tokens:,}", end="")
                if metrics.cache_read_tokens > 0:
                    print(f", Cache read: {metrics.cache_read_tokens:,}", end="")
                print()

    # Collect results in query order
    for query in queries:
        result.queries.append(query_results[query])

    db.close()
    return result


def print_summary(result: BenchmarkResult):
    """Print human-readable summary."""
    summary = result.summary()

    print("\n" + "=" * 70)
    print("BENCHMARK SUMMARY")
    print("=" * 70)
    print(f"\nModel: {Config.LLM_PROVIDER}/{Config.LLM_MODEL}")
    print(f"Document: {result.document_path}")
    print(f"Sections: {result.document_sections}")
    print(f"Queries: {summary['queries_run']}")
    print(f"Retrievers: {', '.join(result.retriever_names)}")

    # Build table
    headers = ["Metric"] + result.retriever_names
    rows = []

    # Time row (avg per query)
    time_row = ["Time (s)"]
    for name in result.retriever_names:
        time_row.append(f"{summary[name]['avg_time']:.2f}")
    rows.append(time_row)

    # LLM calls row (avg per query)
    calls_row = ["LLM Calls"]
    for name in result.retriever_names:
        calls_row.append(f"{summary[name]['avg_llm_calls']:.1f}")
    rows.append(calls_row)

    # Input tokens row (avg per query)
    tokens_row = ["Input Tokens"]
    for name in result.retriever_names:
        tokens_row.append(f"{summary[name]['avg_input_tokens']:,.0f}")
    rows.append(tokens_row)

    # Cache write row (avg per query)
    cache_write_row = ["Cache Write"]
    for name in result.retriever_names:
        cache_write_row.append(f"{summary[name]['avg_cache_write']:,.0f}")
    rows.append(cache_write_row)

    # Cache read row (avg per query)
    cache_read_row = ["Cache Read"]
    for name in result.retriever_names:
        cache_read_row.append(f"{summary[name]['avg_cache_read']:,.0f}")
    rows.append(cache_read_row)

    # Cost row
    cost_row = ["Cost ($)"]
    for name in result.retriever_names:
        cost_row.append(f"{summary[name]['total_cost']:.4f}")
    rows.append(cost_row)

    # Print table
    col_widths = [max(len(str(row[i])) for row in [headers] + rows) for i in range(len(headers))]
    header_line = " | ".join(h.ljust(w) for h, w in zip(headers, col_widths))
    print(f"\n{header_line}")
    print("-" * len(header_line))
    for row in rows:
        print(" | ".join(str(cell).ljust(w) for cell, w in zip(row, col_widths)))

    print("\n" + "=" * 70)


def load_config(config_path: Path) -> dict:
    """Load benchmark configuration from JSON file."""
    with open(config_path) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark retriever performance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--doc", "-d",
        type=Path,
        required=True,
        help="Path to document JSON file",
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        required=True,
        help="Path to queries config JSON file",
    )
    parser.add_argument(
        "--output", "-o",
        choices=["text", "json"],
        default="text",
        help="Output format",
    )
    parser.add_argument(
        "--beam-size", "-b",
        type=int,
        default=3,
        help="Beam size for search",
    )
    parser.add_argument(
        "--max-turns", "-t",
        type=int,
        default=10,
        help="Maximum LLM turns",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear cache before each query (default: reuse cache across queries)",
    )

    args = parser.parse_args()

    # Validate LLM config
    try:
        Config.validate()
    except ValueError as e:
        print(f"ERROR: {e}")
        print("Please set ANTHROPIC_API_KEY environment variable")
        sys.exit(1)

    # Check document exists
    if not args.doc.exists():
        print(f"ERROR: Document not found: {args.doc}")
        sys.exit(1)

    # Load config file
    if not args.config.exists():
        print(f"ERROR: Config file not found: {args.config}")
        print("Create a queries config file. See bench/queries.json for example.")
        sys.exit(1)

    config = load_config(args.config)
    queries = config.get("queries", [])
    if not queries:
        print("ERROR: No queries found in config file")
        print("Add queries to the 'queries' array in your config file.")
        sys.exit(1)

    print("=" * 70)
    print("Retriever Benchmark")
    print("=" * 70)
    print(f"\nDocument: {args.doc}")
    print(f"Queries: {len(queries)}")
    print(f"Beam size: {args.beam_size}")
    print(f"Max turns: {args.max_turns}")
    print(f"Cache reuse: {'disabled' if args.clear_cache else 'enabled'}")

    result = run_benchmark(
        args.doc,
        queries,
        beam_size=args.beam_size,
        max_turns=args.max_turns,
        clear_cache=args.clear_cache,
    )

    if args.output == "json":
        output = {
            "document_path": result.document_path,
            "document_sections": result.document_sections,
            "tree_id": result.tree_id,
            "retrievers": result.retriever_names,
            "queries": [
                {
                    "query": q.query,
                    "results": {
                        name: asdict(metrics)
                        for name, metrics in q.results.items()
                    },
                }
                for q in result.queries
            ],
            "summary": result.summary(),
        }
        print(json.dumps(output, indent=2))
    else:
        print_summary(result)


if __name__ == "__main__":
    main()
