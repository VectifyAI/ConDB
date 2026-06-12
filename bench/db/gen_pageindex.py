#!/usr/bin/env python3
"""Generate synthetic PageIndex document trees for benchmarking.

Output matches the format consumed by ContextTree.index_pageindex
(doc_name / doc_description / recursive structure). Content is random;
the tree shape is what matters.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import deque
from pathlib import Path

WORDS = (
    "system model retrieval index document hierarchy embedding vector node tree "
    "query latency throughput storage schema benchmark optimization traversal "
    "metadata chunk relationship reasoning trace token context graph cluster "
    "partition compression serialization scalability ingestion update page "
    "section chapter paragraph summary analysis evaluation method result "
    "framework pipeline distributed parallel concurrent cache memory disk replica "
    "shard collection database aggregation projection filter sort scan lookup "
    "neural attention transformer gradient inference dataset corpus annotation "
    "precision recall ranking relevance semantic lexical sparse dense fusion"
).split()

SECTION_KINDS = (
    "Introduction", "Overview", "Background", "Methodology", "Architecture",
    "Implementation", "Evaluation", "Results", "Discussion", "Analysis",
    "Appendix", "Preliminaries", "Related Work", "Experiments", "Conclusion",
    "Design", "System", "Module", "Component", "Pipeline", "Benchmark",
)

# (nodes, max_depth, fanout_min, fanout_max, summary_words, text_words)
SCALES = {
    "tiny": (1_000, 4, 4, 8, 25, 60),
    "small": (10_000, 4, 5, 10, 30, 80),
    "medium": (100_000, 5, 6, 12, 35, 100),
    "large": (1_000_000, 6, 6, 14, 40, 120),
}


def make_words(n, rng):
    return " ".join(rng.choice(WORDS) for _ in range(n))


def make_title(rng):
    return f"{rng.choice(SECTION_KINDS)}: {make_words(rng.randint(2, 5), rng).capitalize()}"


def build_tree(target_nodes, max_depth, fanout_min, fanout_max, summary_words, text_words, seed):
    rng = random.Random(seed)
    counter = 0
    all_nodes = []

    def new_node():
        nonlocal counter
        node = {
            "node_id": f"{counter:06d}",
            "title": make_title(rng),
            "summary": make_words(summary_words, rng),
            "start_index": 0,
            "end_index": 0,
        }
        counter += 1
        all_nodes.append(node)
        return node

    roots = []
    queue = deque()
    for _ in range(rng.randint(fanout_min, fanout_max)):
        if counter >= target_nodes:
            break
        n = new_node()
        roots.append(n)
        queue.append((n, 1))

    while queue and counter < target_nodes:
        parent, depth = queue.popleft()
        if depth >= max_depth:
            continue
        children = []
        for _ in range(rng.randint(fanout_min, fanout_max)):
            if counter >= target_nodes:
                break
            c = new_node()
            children.append(c)
            queue.append((c, depth + 1))
        if children:
            parent["nodes"] = children

    leaf_count = 0
    for node in all_nodes:
        if "nodes" not in node:
            node["text"] = make_words(text_words, rng)
            leaf_count += 1

    page = [1]

    def assign_pages(node):
        if "nodes" in node:
            for child in node["nodes"]:
                assign_pages(child)
            node["start_index"] = node["nodes"][0]["start_index"]
            node["end_index"] = node["nodes"][-1]["end_index"]
        else:
            span = rng.randint(1, 4)
            node["start_index"] = page[0]
            node["end_index"] = page[0] + span - 1
            page[0] += span

    for r in roots:
        assign_pages(r)

    depth_hist = {}

    def walk(node, d):
        depth_hist[d] = depth_hist.get(d, 0) + 1
        for c in node.get("nodes", []):
            walk(c, d + 1)

    for r in roots:
        walk(r, 1)

    stats = {
        "total_nodes": counter,
        "leaf_nodes": leaf_count,
        "internal_nodes": counter - leaf_count,
        "max_depth": max(depth_hist) if depth_hist else 0,
        "top_level_sections": len(roots),
        "depth_histogram": dict(sorted(depth_hist.items())),
    }
    return roots, stats


def main():
    ap = argparse.ArgumentParser(description="Generate a synthetic PageIndex tree.")
    ap.add_argument("--scale", choices=sorted(SCALES), help="preset scale")
    ap.add_argument("--nodes", type=int, help="target node count (overrides --scale)")
    ap.add_argument("--max-depth", type=int)
    ap.add_argument("--fanout-min", type=int)
    ap.add_argument("--fanout-max", type=int)
    ap.add_argument("--summary-words", type=int)
    ap.add_argument("--text-words", type=int)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.scale:
        nodes, max_depth, fmin, fmax, sw, tw = SCALES[args.scale]
    else:
        nodes, max_depth, fmin, fmax, sw, tw = 10_000, 4, 5, 10, 30, 80

    nodes = args.nodes or nodes
    max_depth = args.max_depth or max_depth
    fmin = args.fanout_min or fmin
    fmax = args.fanout_max or fmax
    sw = args.summary_words if args.summary_words is not None else sw
    tw = args.text_words if args.text_words is not None else tw

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Generating ~{nodes:,} nodes -> {out_path}", file=sys.stderr)
    t0 = time.time()
    structure, stats = build_tree(nodes, max_depth, fmin, fmax, sw, tw, args.seed)
    gen_s = time.time() - t0

    doc = {
        "doc_name": f"synthetic_pageindex_{args.scale or nodes}.pdf",
        "doc_description": (
            f"Synthetic PageIndex tree. {stats['total_nodes']} nodes, "
            f"max depth {stats['max_depth']}."
        ),
        "structure": structure,
    }

    t1 = time.time()
    with out_path.open("w") as f:
        json.dump(doc, f, ensure_ascii=False)
    write_s = time.time() - t1

    size = out_path.stat().st_size
    print(json.dumps({
        "out": str(out_path),
        "size_bytes": size,
        "size_human": f"{size / 1e6:.1f} MB" if size < 1e9 else f"{size / 1e9:.2f} GB",
        "gen_seconds": round(gen_s, 2),
        "write_seconds": round(write_s, 2),
        **stats,
    }, indent=2))


if __name__ == "__main__":
    main()
