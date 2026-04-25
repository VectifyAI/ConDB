#!/usr/bin/env python3
"""
Run ConDB filesystem retrieval on SWEBench-FileTree.

Usage:
  python bench/run_swebench_filetree.py --tier medium
  python bench/run_swebench_filetree.py --tier easy --limit 20
  python bench/run_swebench_filetree.py --tier all --model claude-sonnet-4-6

Outputs to bench/runs/<timestamp>__<tier>/:
  config.json         run metadata
  per_query.jsonl     per-query hit@k + top10 paths
  summary.json        aggregated metrics (overall, per-repo, per-signal)
  report.md           human-readable summary
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from contextdb.adapter.filesystem import FileSystemAdapter
from contextdb.api.condb import ConDB
from contextdb.retriever.algorithm.block_retriever import BlockRetriever
from contextdb.retriever.algorithm.ranker import BM25PathRanker

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_DATA_DIR = Path("data/swebench_pathonly")
K_VALUES = (1, 3, 5, 10)


def make_block_retriever(db: ConDB, args) -> BlockRetriever:
    ranker = BM25PathRanker(db.storage) if args.ranker == "bm25" else None
    return BlockRetriever(
        db.storage,
        db._llm,
        mode="filesystem",
        ranker=ranker,
        fs_ranker=args.ranker,
        max_parallel_blocks=args.max_parallel_blocks,
    )


# ── Data loading ──────────────────────────────────────────────────────

def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_tier(data_dir: Path, tier: str) -> tuple[list[dict], list[dict]]:
    # "all" is the unfiltered 500-query set, stored as queries.jsonl / qrels.jsonl
    if tier == "all":
        q_path, qr_path = data_dir / "queries.jsonl", data_dir / "qrels.jsonl"
    else:
        q_path = data_dir / f"queries_{tier}.jsonl"
        qr_path = data_dir / f"qrels_{tier}.jsonl"
    if not q_path.exists():
        sys.exit(f"Missing: {q_path}. Run scripts/convert_swebench_pathonly.py or point --data to HF cache.")
    return load_jsonl(q_path), load_jsonl(qr_path)


def load_signal_map(data_dir: Path) -> dict[str, int]:
    path = data_dir / "queries_annotated.jsonl"
    if not path.exists():
        return {}
    return {r["id"]: r.get("path_signal_level", 0) for r in load_jsonl(path)}


# ── Tree building ────────────────────────────────────────────────────

def build_tree_for_snapshot(db: ConDB, fs_json_path: Path) -> str:
    """Ingest one filesystem JSON into ConDB, return tree_id."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        shutil.copy(fs_json_path, tmp_path / fs_json_path.name)
        adapter = FileSystemAdapter(root_dir=str(tmp_path))
        return adapter.ingest(db.storage)


def extract_retrieved_paths(db: ConDB, tree_id: str, node_ids: list[str]) -> list[str]:
    """Map node_ids to file rel_paths by reading node attrs."""
    paths: list[str] = []
    for nid in node_ids:
        node = db.storage.get_node(tree_id, nid)
        if node is None or not node.attrs_json:
            continue
        attrs = json.loads(node.attrs_json)
        if attrs.get("is_dir"):
            continue
        rel = attrs.get("rel_path")
        if not rel:
            continue
        # fs_json wraps everything under <filename>.json/ — strip that prefix
        parts = rel.split("/", 1)
        if len(parts) == 2 and parts[0].endswith(".json"):
            rel = parts[1]
        paths.append(rel)
    # dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ── Metrics ──────────────────────────────────────────────────────────

def hit_at_k(preds: list[str], golds: set[str], k: int) -> int:
    return int(any(p in golds for p in preds[:k]))


def reciprocal_rank(preds: list[str], golds: set[str]) -> float:
    for i, p in enumerate(preds, 1):
        if p in golds:
            return 1.0 / i
    return 0.0


def ndcg_at_k(preds: list[str], golds: set[str], k: int = 10) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, p in enumerate(preds[:k], 1) if p in golds)
    ideal_hits = min(len(golds), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def aggregate(records: list[dict]) -> dict:
    n = len(records)
    if n == 0:
        return {"n": 0}
    out = {"n": n}
    for k in K_VALUES:
        out[f"hit@{k}"] = sum(r[f"hit@{k}"] for r in records) / n
    out["mrr"] = sum(r["rr"] for r in records) / n
    out["ndcg@10"] = sum(r["ndcg@10"] for r in records) / n
    out["failed"] = sum(1 for r in records if r.get("error"))
    return out


# ── Main loop ────────────────────────────────────────────────────────

def run(args):
    data_dir = Path(args.data_dir).resolve()
    queries, qrels = load_tier(data_dir, args.tier)
    signal_map = load_signal_map(data_dir)

    # Build {query_id: [gold_filepaths]} and {query_id: (repo, commit)}
    golds_by_q: dict[str, list[str]] = defaultdict(list)
    snap_of_q: dict[str, tuple[str, str]] = {}
    for r in qrels:
        qid = r["query-id"]
        repo, commit, fp = r["corpus-id"].split(":", 2)
        golds_by_q[qid].append(fp)
        snap_of_q.setdefault(qid, (repo, commit))

    queries = [q for q in queries if q["id"] in golds_by_q]
    if args.limit > 0:
        queries = queries[: args.limit]

    # Group by snapshot so we build each tree only once
    by_snap: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for q in queries:
        by_snap[snap_of_q[q["id"]]].append(q)

    # Output dir
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir or f"bench/runs/{ts}__{args.tier}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Config snapshot
    cfg = {
        "timestamp": ts,
        "tier": args.tier,
        "model": args.model,
        "provider": args.provider,
        "top_k": args.top_k,
        "strategy": args.strategy,
        "ranker": args.ranker if args.strategy == "block" else None,
        "limit": args.limit,
        "num_queries": len(queries),
        "num_snapshots": len(by_snap),
        "data_dir": str(data_dir),
    }
    (out_dir / "config.json").write_text(json.dumps(cfg, indent=2))
    print(f"[cfg] {cfg}")
    print(f"[out] {out_dir}")

    # ConDB
    db_path = out_dir / "bench.sqlite"
    db = ConDB(str(db_path))
    db.set_llm(provider=args.provider, model=args.model)

    per_query_path = out_dir / "per_query.jsonl"
    per_query_f = per_query_path.open("w", encoding="utf-8")
    records: list[dict] = []

    try:
        done = 0
        for (repo, commit), qs in sorted(by_snap.items()):
            slug = repo.replace("/", "__")
            fs_json = data_dir / "filesystems" / f"{slug}__{commit}.json"
            if not fs_json.exists():
                print(f"[skip] missing snapshot {fs_json.name}")
                continue

            try:
                tree_id = build_tree_for_snapshot(db, fs_json)
            except Exception as e:
                print(f"[ingest-err] {slug}__{commit}: {e}")
                continue
            info = db.tree_info(tree_id)
            retriever = make_block_retriever(db, args) if args.strategy == "block" else None

            for q in qs:
                qid = q["id"]
                gold = set(golds_by_q[qid])
                t1 = time.time()
                try:
                    res = db.query(
                        tree_id,
                        question=q["text"],
                        strategy=args.strategy,
                        select_k=args.top_k,
                        max_turns=args.max_turns,
                        max_parallel_blocks=args.max_parallel_blocks,
                        retriever=retriever,
                    )
                    node_ids = res.node_ids
                    preds = extract_retrieved_paths(db, tree_id, node_ids)
                    err = None
                except Exception as e:
                    preds, res = [], None
                    err = f"{type(e).__name__}: {e}"
                    traceback.print_exc()

                dt_ms = int((time.time() - t1) * 1000)
                rec = {
                    "query_id": qid,
                    "repo": repo,
                    "commit": commit,
                    "snapshot_size": info["node_count"],
                    "path_signal_level": signal_map.get(qid, -1),
                    "gold": sorted(gold),
                    "top_k_preds": preds[: args.top_k],
                    "num_preds": len(preds),
                    "latency_ms": dt_ms,
                    "llm_calls": getattr(res, "llm_calls", 0) if res else 0,
                    "turns": getattr(res, "turns", 0) if res else 0,
                    "error": err,
                }
                for k in K_VALUES:
                    rec[f"hit@{k}"] = hit_at_k(preds, gold, k)
                rec["rr"] = reciprocal_rank(preds, gold)
                rec["ndcg@10"] = ndcg_at_k(preds, gold, 10)
                per_query_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                per_query_f.flush()
                records.append(rec)
                done += 1
                mark = "✓" if rec["hit@10"] else ("x" if not err else "E")
                print(f"[{done:4d}/{len(queries)}] {mark} {qid:40s} "
                      f"h@1={rec['hit@1']} h@5={rec['hit@5']} "
                      f"nodes={info['node_count']:5d} {dt_ms}ms")

            # cleanup this tree to keep sqlite small
            db.delete_tree(tree_id)

    finally:
        per_query_f.close()
        db.close()

    # ── Aggregate ──
    overall = aggregate(records)
    by_repo = {k: aggregate(v) for k, v in group_by(records, "repo").items()}
    by_signal = {k: aggregate(v) for k, v in group_by(records, "path_signal_level").items()}
    by_bucket = {k: aggregate(v) for k, v in group_by_bucket(records).items()}

    summary = {
        "config": cfg,
        "overall": overall,
        "per_repo": by_repo,
        "per_path_signal_level": by_signal,
        "per_snapshot_size_bucket": by_bucket,
    }
    # Re-ensure out_dir exists in case it was removed mid-run
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (out_dir / "report.md").write_text(render_report(summary, records))

    print(f"\n=== OVERALL ({overall['n']} queries) ===")
    for k in K_VALUES:
        print(f"  hit@{k:<2d}  {overall[f'hit@{k}']:.3f}")
    print(f"  mrr     {overall['mrr']:.3f}")
    print(f"  ndcg@10 {overall['ndcg@10']:.3f}")
    if overall.get("failed"):
        print(f"  failed  {overall['failed']}")
    print(f"\nreport: {out_dir}/report.md")


def group_by(records, key):
    out: dict = defaultdict(list)
    for r in records:
        out[r[key]].append(r)
    return out


def group_by_bucket(records):
    out: dict = defaultdict(list)
    for r in records:
        sz = r["snapshot_size"]
        if sz <= 50:
            b = "≤50"
        elif sz <= 200:
            b = "51–200"
        elif sz <= 500:
            b = "201–500"
        else:
            b = ">500"
        out[b].append(r)
    return out


def render_report(summary, records) -> str:
    cfg = summary["config"]
    o = summary["overall"]
    lines = [
        f"# SWEBench-FileTree run — {cfg['tier']}",
        "",
        f"- timestamp: {cfg['timestamp']}",
        f"- model: `{cfg['provider']}/{cfg['model']}`",
        f"- strategy: `{cfg['strategy']}`  top-k: {cfg['top_k']}",
    ]
    if cfg.get("ranker"):
        lines.append(f"- ranker: `{cfg['ranker']}`")
    lines += [
        f"- queries: {cfg['num_queries']}  snapshots: {cfg['num_snapshots']}",
        "",
        "## Overall",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for k in K_VALUES:
        lines.append(f"| hit@{k} | {o[f'hit@{k}']:.3f} |")
    lines += [
        f"| mrr | {o['mrr']:.3f} |",
        f"| ndcg@10 | {o['ndcg@10']:.3f} |",
        f"| failed | {o.get('failed',0)} |",
        "",
    ]

    def table(title, d, key_label):
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"| {key_label} | n | hit@1 | hit@5 | hit@10 | mrr |")
        lines.append("|---|---|---|---|---|---|")
        items = sorted(d.items(), key=lambda x: -x[1]["n"])
        for k, v in items:
            lines.append(f"| {k} | {v['n']} | {v['hit@1']:.3f} | "
                         f"{v['hit@5']:.3f} | {v['hit@10']:.3f} | {v['mrr']:.3f} |")
        lines.append("")

    table("Per repo", summary["per_repo"], "repo")
    table("Per path_signal_level", summary["per_path_signal_level"], "level")
    table("Per snapshot size", summary["per_snapshot_size_bucket"], "bucket")

    # failure samples
    failures = [r for r in records if r["hit@10"] == 0 and not r.get("error")]
    if failures:
        lines += ["## Failure samples (hit@10 = 0)", ""]
        import random
        random.seed(0)
        for r in random.sample(failures, min(5, len(failures))):
            lines += [
                f"### {r['query_id']}  ({r['repo']}@{r['commit']})",
                f"- signal level: {r['path_signal_level']}",
                f"- gold: {r['gold']}",
                f"- top10: {r['top_k_preds']}",
                "",
            ]
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tier", choices=["easy", "medium", "hard", "all"], default="medium")
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--provider", default="anthropic")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--strategy", choices=["auto", "beam", "block"], default="auto")
    p.add_argument("--ranker", choices=["heuristic", "bm25", "none"], default="heuristic",
                   help="Filesystem node ordering used with --strategy block")
    p.add_argument("--max-parallel-blocks", type=int, default=None)
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--limit", type=int, default=0, help="0 = all")
    p.add_argument("--output-dir", default=None)
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
