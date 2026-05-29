#!/usr/bin/env python3
"""
Run ConDB filesystem retrieval on SWEBench-FileTree.

Usage:
  python bench/run_swebench_filetree.py --tier medium
  python bench/run_swebench_filetree.py --tier easy --limit 20
  python bench/run_swebench_filetree.py --tier all --model claude-sonnet-4-6

Outputs to bench/runs/<timestamp>__<tier>/:
  config.json         run metadata
  per_query.jsonl     per-query gold-cutoff metrics and returned paths
  summary.json        aggregated metrics (overall, per-repo, per-signal)
  report.md           human-readable summary
"""
from __future__ import annotations

import argparse
import json
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
from contextdb.retriever.algorithm.beam_retriever import BeamRetriever
from contextdb.retriever.algorithm.block_retriever import BlockRetriever
from contextdb.retriever.algorithm.ranker import make_ranker
from contextdb.retriever.algorithm.vertical_retriever import VerticalRetriever

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_DATA_DIR = Path("data/swebench_pathonly")


def make_filesystem_retriever(db: ConDB, args, node_count: int):
    strategy = args.strategy
    if strategy == "auto":
        strategy = "beam" if node_count <= 50 else "block"
    if strategy == "beam":
        return BeamRetriever(db.storage, db._llm, mode="filesystem")
    if strategy in ("block", "vertical"):
        ranker = make_ranker(
            args.ranker,
            embedding_provider=args.embedding_provider,
            embedding_model=args.embedding_model,
            embedding_api_key=args.embedding_api_key,
        )
        cls = VerticalRetriever if strategy == "vertical" else BlockRetriever
        return cls(
            db.storage,
            db._llm,
            mode="filesystem",
            ranker=ranker,
            max_parallel_blocks=args.max_parallel_blocks,
        )
    return None


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
# Set-based: the retriever returns what it thinks are the right files; no
# artificial top-K truncation. Reported as precision / recall / F1 over the
# returned set, plus a strict exact-match flag.


def set_metrics(preds: list[str], golds: set[str]) -> dict[str, float | int]:
    pset = set(preds)
    if not golds:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "exact_match": 0, "hit": 0}
    hit = len(pset & golds)
    precision = hit / len(pset) if pset else 0.0
    recall = hit / len(golds)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "exact_match": int(pset == golds),
        "hit": hit,
    }


def reciprocal_rank(preds: list[str], golds: set[str]) -> float:
    for i, p in enumerate(preds, 1):
        if p in golds:
            return 1.0 / i
    return 0.0


def aggregate(records: list[dict]) -> dict:
    n = len(records)
    if n == 0:
        return {"n": 0}
    metrics = [set_metrics(r.get("preds", []), set(r.get("gold", []))) for r in records]
    return {
        "n": n,
        "precision": sum(m["precision"] for m in metrics) / n,
        "recall": sum(m["recall"] for m in metrics) / n,
        "f1": sum(m["f1"] for m in metrics) / n,
        "exact_match": sum(m["exact_match"] for m in metrics) / n,
        "mrr": sum(reciprocal_rank(r.get("preds", []), set(r.get("gold", []))) for r in records) / n,
        "avg_gold": sum(len(set(r.get("gold", []))) for r in records) / n,
        "avg_returned": sum(r.get("num_preds", len(r.get("preds", []))) for r in records) / n,
        "failed": sum(1 for r in records if r.get("error")),
    }


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
        "max_results": args.max_results,
        "strategy": args.strategy,
        "ranker": args.ranker,
        "embedding_provider": args.embedding_provider if args.ranker == "vector" else None,
        "embedding_model": args.embedding_model if args.ranker == "vector" else None,
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
            retriever = make_filesystem_retriever(db, args, info["node_count"])

            for q in qs:
                qid = q["id"]
                gold = set(golds_by_q[qid])
                t1 = time.time()
                try:
                    res = db.query(
                        tree_id,
                        question=q["text"],
                        strategy=args.strategy,
                        select_k=args.max_results,
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
                    "gold_count": len(gold),
                    "preds": preds,
                    "num_preds": len(preds),
                    "latency_ms": dt_ms,
                    "llm_calls": getattr(res, "llm_calls", 0) if res else 0,
                    "turns": getattr(res, "turns", 0) if res else 0,
                    "error": err,
                }
                rec.update(set_metrics(preds, gold))
                rec["rr"] = reciprocal_rank(preds, gold)
                per_query_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                per_query_f.flush()
                records.append(rec)
                done += 1
                if rec["exact_match"]:
                    mark = "✓"
                elif rec["hit"]:
                    mark = "~"
                else:
                    mark = "E" if err else "x"
                print(f"[{done:4d}/{len(queries)}] {mark} {qid:40s} "
                      f"hit={rec['hit']}/{rec['gold_count']} "
                      f"P={rec['precision']:.2f} R={rec['recall']:.2f} F1={rec['f1']:.2f} "
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
    by_gold_count = {k: aggregate(v) for k, v in group_by_gold_count(records).items()}

    summary = {
        "config": cfg,
        "overall": overall,
        "per_repo": by_repo,
        "per_path_signal_level": by_signal,
        "per_snapshot_size_bucket": by_bucket,
        "per_gold_count": by_gold_count,
    }
    # Re-ensure out_dir exists in case it was removed mid-run
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (out_dir / "report.md").write_text(render_report(summary, records))

    print(f"\n=== OVERALL ({overall['n']} queries) ===")
    print(f"  precision   {overall['precision']:.3f}")
    print(f"  recall      {overall['recall']:.3f}")
    print(f"  f1          {overall['f1']:.3f}")
    print(f"  exact_match {overall['exact_match']:.3f}")
    print(f"  mrr         {overall['mrr']:.3f}")
    print(f"  avg_gold    {overall['avg_gold']:.2f}")
    print(f"  avg_returned {overall['avg_returned']:.2f}")
    if overall.get("failed"):
        print(f"  failed      {overall['failed']}")
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


def group_by_gold_count(records):
    out: dict = defaultdict(list)
    for r in records:
        gold_count = int(r.get("gold_count", len(set(r.get("gold", [])))))
        bucket = str(gold_count) if gold_count <= 5 else "6+"
        out[bucket].append(r)
    return out


def gold_bucket_sort_key(bucket) -> int:
    text = str(bucket)
    if text.endswith("+"):
        return int(text[:-1])
    return int(text)


def render_report(summary, records) -> str:
    cfg = summary["config"]
    o = summary["overall"]
    lines = [
        f"# SWEBench-FileTree run — {cfg['tier']}",
        "",
        f"- timestamp: {cfg['timestamp']}",
        f"- model: `{cfg['provider']}/{cfg['model']}`",
        f"- strategy: `{cfg['strategy']}`",
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
        f"| precision | {o['precision']:.3f} |",
        f"| recall | {o['recall']:.3f} |",
        f"| f1 | {o['f1']:.3f} |",
        f"| exact_match | {o['exact_match']:.3f} |",
        f"| mrr | {o['mrr']:.3f} |",
        f"| avg_gold | {o['avg_gold']:.2f} |",
        f"| avg_returned | {o['avg_returned']:.2f} |",
        "",
    ]

    def table(title, d, key_label):
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"| {key_label} | n | precision | recall | f1 | exact_match | avg gold | avg returned |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")

        def sort_key(item):
            return gold_bucket_sort_key(item[0]) if title == "Per gold count" else -item[1]["n"]

        for k, v in sorted(d.items(), key=sort_key):
            lines.append(
                f"| {k} | {v['n']} | {v['precision']:.3f} | {v['recall']:.3f} | "
                f"{v['f1']:.3f} | {v['exact_match']:.3f} | "
                f"{v['avg_gold']:.2f} | {v['avg_returned']:.2f} |"
            )
        lines.append("")

    table("Per gold count", summary.get("per_gold_count", {}), "gold files")
    table("Per repo", summary["per_repo"], "repo")
    table("Per path_signal_level", summary["per_path_signal_level"], "level")
    table("Per snapshot size", summary["per_snapshot_size_bucket"], "bucket")

    misses = [r for r in records if r.get("recall", 0.0) < 1.0 and not r.get("error")]
    if misses:
        lines += ["## Miss samples", ""]
        for r in misses[:5]:
            lines += [
                f"### {r['query_id']}  ({r['repo']}@{r['commit']})",
                f"- signal level: {r['path_signal_level']}",
                f"- gold: {r['gold']}",
                f"- returned: {r['preds']}",
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
    p.add_argument("--max-results", type=int, default=50,
                   help="Safety cap on retriever output. Retriever stops naturally on LLM done=true.")
    p.add_argument("--strategy", choices=["auto", "beam", "block", "vertical"], default="auto")
    p.add_argument("--ranker", choices=["bm25", "vector", "none"], default="none",
                   help="Optional path ordering for Block merge results")
    p.add_argument("--embedding-provider", default="openai")
    p.add_argument("--embedding-model", default="text-embedding-3-small")
    p.add_argument("--embedding-api-key", default=None)
    p.add_argument("--max-parallel-blocks", type=int, default=None)
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--limit", type=int, default=0, help="0 = all")
    p.add_argument("--output-dir", default=None)
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
