#!/usr/bin/env python3
"""
Unified Benchmark for Retriever Comparison
==========================================

Supports document mode and filesystem mode.

Usage:
    Document mode:
        python bench/benchmark_retrievers.py --mode doc --doc <document.json> --config <queries.json>

    Filesystem mode:
        python bench/benchmark_retrievers.py --mode fs --repo-dir <path> --queries-config <queries.json>

Examples:
    python bench/benchmark_retrievers.py --mode doc --doc examples/large_doc.json --config bench/queries.json
    python bench/benchmark_retrievers.py --mode fs --repo-dir bench/filesystem/repo --queries-config bench/filesystem/repo/queries.json
    python bench/benchmark_retrievers.py --mode fs --repo-dir bench/filesystem/arxiv --queries-config bench/filesystem/arxiv/queries.json
    python bench/benchmark_retrievers.py --mode fs --repo-dir bench/filesystem/context7 --queries-config bench/filesystem/context7/queries.json
"""

import argparse
import importlib
import inspect
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

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
    seen_classes = set()

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
                    and obj not in seen_classes
                ):
                    short_name = name.replace("Retriever", "")
                    retrievers[short_name] = obj
                    seen_classes.add(obj)
        except Exception as e:
            print(f"Warning: Failed to load {module_name}: {e}")

    return retrievers


# ── Data Classes ────────────────────────────────────────────────────


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
    # FS-specific
    retrieved_files: Optional[list[str]] = None
    hit_at: Optional[dict[int, bool]] = None
    mrr: Optional[float] = None


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
    mode: str = "doc"
    retriever_names: list[str] = field(default_factory=list)
    queries: list[QueryResult] = field(default_factory=list)

    def summary(self) -> dict:
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

            cost = (total_input * 3 + total_output * 15 + total_cache_write * 3.75 + total_cache_read * 0.30) / 1_000_000

            n = len(valid) if valid else 1
            s = {
                "avg_time": total(valid, "time_seconds") / n,
                "avg_llm_calls": total(valid, "llm_calls") / n,
                "avg_input_tokens": total_input / n,
                "avg_output_tokens": total_output / n,
                "avg_cache_read": total_cache_read / n,
                "avg_cache_write": total_cache_write / n,
                "total_cost": cost,
                "successful_queries": len(valid),
            }

            # FS hit metrics
            if self.mode == "fs":
                for k in [1, 3, 5, 10]:
                    hits = [r for r in valid if r.hit_at and k in r.hit_at]
                    s[f"hit@{k}"] = sum(1 for r in hits if r.hit_at[k]) / len(hits) if hits else 0
                mrr_vals = [r.mrr for r in valid if r.mrr is not None]
                s["mrr"] = sum(mrr_vals) / len(mrr_vals) if mrr_vals else 0

            summary[name] = s

        return summary


# ── Document Mode Helpers ───────────────────────────────────────────


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


# ── FS Mode Helpers ─────────────────────────────────────────────────


def compute_fs_metrics(retrieved_files: list[str], ground_truth: list[str]) -> tuple[dict[int, bool], float]:
    """Returns (hit_at_k_dict, mrr)."""
    gt_set = set(ground_truth)
    hit_at = {}
    first_hit_rank = None

    for i, f in enumerate(retrieved_files):
        if f in gt_set and first_hit_rank is None:
            first_hit_rank = i + 1
        for k in [1, 3, 5, 10]:
            if i < k and f in gt_set:
                hit_at.setdefault(k, False)
                hit_at[k] = True

    for k in [1, 3, 5, 10]:
        hit_at.setdefault(k, False)

    mrr = 1.0 / first_hit_rank if first_hit_rank else 0.0
    return hit_at, mrr


def extract_file_paths(db: TreeDB, tree_id: str, node_ids: list[str]) -> list[str]:
    """Extract rel_path from retrieved nodes."""
    paths = []
    for nid in node_ids:
        node = db.get_node(tree_id, nid)
        if not node or not node.attrs_json:
            continue
        try:
            attrs = json.loads(node.attrs_json)
        except json.JSONDecodeError:
            continue
        rel_path = attrs.get("rel_path")
        if rel_path and rel_path != ".":
            paths.append(rel_path)
    return paths


def _fs_prefix_key(ground_truth: list[str]) -> str:
    """Build a coarse prefix key from first ground-truth path."""
    if not ground_truth:
        return ""
    first = str(ground_truth[0]).strip().lstrip("/")
    if not first:
        return ""
    return first.split("/", 1)[0]


def reorder_fs_queries_by_prefix(queries_with_gt: list[dict]) -> list[dict]:
    """Group filesystem queries by shared ground-truth top-level prefix."""
    indexed = list(enumerate(queries_with_gt))
    indexed.sort(key=lambda x: (_fs_prefix_key(x[1].get("ground_truth", [])), x[0]))
    return [item for _, item in indexed]


# ── Runner ──────────────────────────────────────────────────────────


def run_retriever(
    retriever,
    name: str,
    tree_id: str,
    query: str,
    llm: "LLMWithStats",
    beam_size: int = 3,
    max_turns: int = 10,
    *,
    mode: str = "doc",
    db: TreeDB = None,
    ground_truth: list[str] = None,
) -> RetrieverMetrics:
    fresh_recorder = StatisticsRecorder()
    llm.recorder = fresh_recorder

    start_time = time.time()
    try:
        result = retriever.retrieve(tree_id, query, beam_size=beam_size, max_turns=max_turns)
        elapsed = time.time() - start_time

        metrics = RetrieverMetrics(
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

        if mode == "fs" and db and ground_truth:
            retrieved_files = extract_file_paths(db, tree_id, result.nodes)
            hit_at, mrr = compute_fs_metrics(retrieved_files, ground_truth)
            metrics.retrieved_files = retrieved_files
            metrics.hit_at = hit_at
            metrics.mrr = mrr

        return metrics
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


# ── Benchmark Orchestration ────────────────────────────────────────


def run_benchmark(
    *,
    mode: str = "doc",
    doc_path: Path = None,
    repo_dir: Path = None,
    query_list: list = None,
    queries_with_gt: list[dict] = None,
    queries_config_path: Path = None,
    beam_size: int = 3,
    max_turns: int = 10,
    clear_cache: bool = False,
    fs_query_order: str = "original",
    retrievers: list[str] | None = None,
) -> BenchmarkResult:
    retriever_classes = discover_retrievers()
    discovered = list(retriever_classes.keys())
    print(f"\nDiscovered retrievers: {', '.join(discovered)}")
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

    if mode == "doc":
        with open(doc_path) as f:
            flat_data = json.load(f)
        tree_structure = convert_flat_to_tree(flat_data)
        entities = build_entities(flat_data)
        queries = query_list
        ground_truths = [None] * len(queries)
        doc_info = str(doc_path)
        sections_count = len(entities)

        def make_tree(db):
            return db.ingest_tree(tree_structure, entities=entities)

    elif mode == "fs":
        from contextdb.adapter.filesystem import DEFAULT_IGNORE_PATTERNS, FileSystemAdapter

        ignore_patterns = list(DEFAULT_IGNORE_PATTERNS)
        effective_queries_config = queries_config_path
        if effective_queries_config is None:
            default_queries = repo_dir / "queries.json"
            if default_queries.exists():
                effective_queries_config = default_queries

        if effective_queries_config is not None:
            try:
                rel_cfg = str(effective_queries_config.resolve().relative_to(repo_dir.resolve())).replace("\\", "/")
                ignore_patterns.append(rel_cfg)
            except ValueError:
                pass

        if fs_query_order == "prefix":
            queries_with_gt = reorder_fs_queries_by_prefix(queries_with_gt)

        adapter = FileSystemAdapter(str(repo_dir), ignore_patterns=ignore_patterns)
        tree_structure, entities = adapter.convert()
        queries = [q["query"] for q in queries_with_gt]
        ground_truths = [q["ground_truth"] for q in queries_with_gt]
        doc_info = str(repo_dir)
        sections_count = len(entities)

        def make_tree(db):
            return db.ingest_tree(tree_structure, entities=entities)
    else:
        raise ValueError(f"Unknown mode: {mode}")

    result = BenchmarkResult(
        document_path=doc_info,
        document_sections=sections_count,
        tree_id="(per-retriever)",
        mode=mode,
        retriever_names=list(retriever_classes.keys()),
    )

    query_results = {q: QueryResult(query=q) for q in queries}

    for name, cls in retriever_classes.items():
        print(f"\n{'='*70}")
        print(f"[{name}Retriever] Running {len(queries)} queries...")
        print("=" * 70)

        # Fresh DB + tree per retriever for fair comparison
        db = TreeDB(":memory:")
        tree_id = make_tree(db)

        # Instantiate retriever (pass mode if supported)
        try:
            sig = inspect.signature(cls.__init__)
            kwargs = {}
            if 'mode' in sig.parameters:
                kwargs['mode'] = "filesystem" if mode == "fs" else "document"
            retriever = cls(db, llm_with_stats, **kwargs)
        except Exception as e:
            print(f"Warning: Failed to instantiate {name}: {e}")
            db.close()
            continue

        for i, query in enumerate(queries):
            print(f"\n  Query {i+1}: {query[:60]}...")

            if clear_cache and i > 0 and hasattr(retriever, "clear_cache"):
                retriever.clear_cache()

            gt = ground_truths[i]
            metrics = run_retriever(
                retriever, name, tree_id, query, llm_with_stats,
                beam_size, max_turns,
                mode=mode, db=db, ground_truth=gt,
            )
            query_results[query].results[name] = metrics

            if metrics.error:
                print(f"    ERROR: {metrics.error}")
            else:
                parts = [f"Time: {metrics.time_seconds:.2f}s, LLM: {metrics.llm_calls}"]
                parts.append(f"Input: {metrics.input_tokens:,}")
                if metrics.cache_read_tokens > 0:
                    parts.append(f"Cache read: {metrics.cache_read_tokens:,}")
                if mode == "fs" and metrics.hit_at:
                    parts.append(f"Hit@1: {metrics.hit_at.get(1, False)}")
                    parts.append(f"MRR: {metrics.mrr:.3f}")
                    if metrics.retrieved_files:
                        parts.append(f"Files: {metrics.retrieved_files[:5]}")
                print(f"    {', '.join(parts)}")

        db.close()

    for query in queries:
        result.queries.append(query_results[query])

    return result


# ── Summary Printing ────────────────────────────────────────────────


def print_summary(result: BenchmarkResult):
    summary = result.summary()

    print("\n" + "=" * 70)
    title = "FILESYSTEM BENCHMARK SUMMARY" if result.mode == "fs" else "BENCHMARK SUMMARY"
    print(title)
    print("=" * 70)
    print(f"\nModel: {Config.LLM_PROVIDER}/{Config.LLM_MODEL}")
    print(f"{'Repository' if result.mode == 'fs' else 'Document'}: {result.document_path}")
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

    if result.mode == "fs":
        for k in [1, 3, 5, 10]:
            row = [f"Hit@{k}"]
            for name in result.retriever_names:
                val = summary[name].get(f"hit@{k}", 0)
                row.append(f"{val:.1%}")
            rows.append(row)
        mrr_row = ["MRR"]
        for name in result.retriever_names:
            mrr_row.append(f"{summary[name].get('mrr', 0):.3f}")
        rows.append(mrr_row)

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
        description="Benchmark retriever performance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mode", choices=["doc", "fs"], default="doc",
                        help="Benchmark mode: doc (document) or fs (filesystem)")
    parser.add_argument("--doc", "-d", type=Path,
                        help="Path to document JSON file (doc mode)")
    parser.add_argument("--config", "-c", type=Path,
                        help="Path to queries config JSON (doc mode)")
    parser.add_argument("--repo-dir", type=Path,
                        help="Path to repository directory (fs mode)")
    parser.add_argument("--queries-config", type=Path,
                        help="Path to queries+ground_truth JSON (fs mode)")
    parser.add_argument("--output", "-o", choices=["text", "json"], default="text")
    parser.add_argument("--beam-size", "-b", type=int, default=3)
    parser.add_argument("--max-turns", "-t", type=int, default=10)
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument(
        "--retrievers",
        type=str,
        default="",
        help="Comma-separated retriever names to run, e.g. Block,Beam,Vertical",
    )
    parser.add_argument(
        "--fs-query-order",
        choices=["original", "prefix"],
        default="original",
        help="FS mode query order: original or grouped by ground-truth prefix",
    )

    args = parser.parse_args()
    selected_retrievers = [x.strip() for x in args.retrievers.split(",") if x.strip()]

    try:
        Config.validate()
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if args.mode == "doc":
        if not args.doc or not args.doc.exists():
            print("ERROR: --doc required and must exist for doc mode")
            sys.exit(1)
        if not args.config or not args.config.exists():
            print("ERROR: --config required and must exist for doc mode")
            sys.exit(1)
        config = load_config(args.config)
        queries = config.get("queries", [])
        if not queries:
            print("ERROR: No queries in config"); sys.exit(1)

        print("=" * 70)
        print("Retriever Benchmark (DOC mode)")
        print("=" * 70)
        result = run_benchmark(
            mode="doc", doc_path=args.doc, query_list=queries,
            beam_size=args.beam_size, max_turns=args.max_turns,
            clear_cache=args.clear_cache,
            fs_query_order=args.fs_query_order,
            retrievers=selected_retrievers or None,
        )

    elif args.mode == "fs":
        if not args.repo_dir or not args.repo_dir.exists():
            print("ERROR: --repo-dir required and must exist for fs mode")
            sys.exit(1)
        if not args.queries_config or not args.queries_config.exists():
            print("ERROR: --queries-config required and must exist for fs mode")
            sys.exit(1)
        config = load_config(args.queries_config)
        queries_with_gt = config.get("queries", [])
        if not queries_with_gt:
            print("ERROR: No queries in config"); sys.exit(1)

        print("=" * 70)
        print("Retriever Benchmark (FS mode)")
        print("=" * 70)
        result = run_benchmark(
            mode="fs", repo_dir=args.repo_dir, queries_with_gt=queries_with_gt,
            queries_config_path=args.queries_config,
            beam_size=args.beam_size, max_turns=args.max_turns,
            clear_cache=args.clear_cache,
            fs_query_order=args.fs_query_order,
            retrievers=selected_retrievers or None,
        )

    if args.output == "json":
        print(json.dumps({
            "mode": args.mode,
            "document_path": result.document_path,
            "document_sections": result.document_sections,
            "retrievers": result.retriever_names,
            "queries": [
                {"query": q.query, "results": {n: asdict(m) for n, m in q.results.items()}}
                for q in result.queries
            ],
            "summary": result.summary(),
        }, indent=2))
    else:
        print_summary(result)


if __name__ == "__main__":
    main()
