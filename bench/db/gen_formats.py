#!/usr/bin/env python3
"""Generate synthetic data for the JSON shapes ConDB ingests/produces.

Formats: chatindex, filesystem, generic, embeddings, corpus (and "all").
Tree formats are validated against the adapter that consumes them.
Content is random; the shape is what matters.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import deque
from pathlib import Path

WORDS = (
    "system model retrieval index document hierarchy embedding vector node tree "
    "query latency throughput storage schema benchmark optimization traversal "
    "metadata chunk relationship reasoning trace token context graph cluster "
    "partition compression serialization scalability ingestion update page section "
    "chapter paragraph summary analysis evaluation method result framework pipeline "
    "distributed parallel concurrent cache memory disk replica shard collection "
    "database aggregation projection filter sort scan lookup neural attention "
    "transformer gradient inference dataset corpus annotation precision recall "
    "ranking relevance semantic lexical sparse dense fusion"
).split()

ROLES = ["user", "assistant"]
EXTS = [".py", ".md", ".json", ".txt", ".yaml", ".cfg", ".rst", ".toml"]
REPOS = ["acme/core", "acme/api", "globex/engine", "initech/svc", "umbrella/lib",
         "stark/mesh", "wayne/graph", "wonka/store", "hooli/index", "piedpiper/ml"]

SCALES = {"tiny": 1_000, "small": 10_000, "medium": 100_000, "large": 1_000_000}


def words(n, rng):
    return " ".join(rng.choice(WORDS) for _ in range(n))


def sentence(n, rng):
    return words(n, rng).capitalize() + "."


def gen_chatindex(target_topics, max_depth, fanout_min, fanout_max,
                  msgs_min, msgs_max, msg_words, seed):
    rng = random.Random(seed)
    counter = [0]
    msg_cursor = [0]

    def topic(depth):
        tid = counter[0]
        counter[0] += 1
        n_msgs = rng.randint(msgs_min, msgs_max)
        start = msg_cursor[0]
        messages = [{"role": ROLES[(start + i) % 2], "content": sentence(msg_words, rng)}
                    for i in range(n_msgs)]
        msg_cursor[0] += n_msgs
        return {
            "node_id": f"topic_{tid:06d}",
            "title": words(rng.randint(2, 5), rng).title(),
            "summary": sentence(rng.randint(12, 30), rng),
            "msg_start": start,
            "msg_end": msg_cursor[0] - 1,
            "messages": messages,
            "subtopics": [],
        }

    roots, queue = [], deque()
    for _ in range(rng.randint(fanout_min, fanout_max)):
        if counter[0] >= target_topics:
            break
        n = topic(1)
        roots.append(n)
        queue.append((n, 1))
    while queue and counter[0] < target_topics:
        parent, d = queue.popleft()
        if d >= max_depth:
            continue
        for _ in range(rng.randint(fanout_min, fanout_max)):
            if counter[0] >= target_topics:
                break
            c = topic(d + 1)
            parent["subtopics"].append(c)
            queue.append((c, d + 1))

    data = {
        "conversation_id": f"conv_{seed:04d}",
        "participants": ["user", "assistant"],
        "topics": roots,
    }
    return data, {"topics": counter[0], "messages": msg_cursor[0]}


def gen_filesystem(target_nodes, max_depth, fanout_min, fanout_max,
                   files_per_dir, content_words, seed):
    rng = random.Random(seed)
    counter = [0]

    def make_dir(name, depth):
        counter[0] += 1
        node = {"name": name, "type": "directory", "children": []}
        for _ in range(rng.randint(0, files_per_dir)):
            if counter[0] >= target_nodes:
                break
            counter[0] += 1
            fname = f"{words(1, rng)}_{counter[0]:06d}{rng.choice(EXTS)}"
            node["children"].append({
                "name": fname,
                "type": "file",
                "content": words(content_words, rng),
            })
        if depth < max_depth:
            for _ in range(rng.randint(fanout_min, fanout_max)):
                if counter[0] >= target_nodes:
                    break
                node["children"].append(make_dir(f"{words(1, rng)}_{counter[0]:06d}", depth + 1))
        return node

    root = make_dir("synthetic-root", 1)
    return root, {"nodes": counter[0]}


def gen_generic(target_nodes, max_depth, fanout_min, fanout_max, text_words, seed):
    rng = random.Random(seed)
    extra_keys = ["author", "version", "tags", "priority", "status", "score",
                  "lang", "region", "kind", "owner", "weight"]
    counter = [0]

    def attrs():
        node = {"summary": sentence(rng.randint(6, 16), rng)}
        for k in rng.sample(extra_keys, rng.randint(0, 4)):
            node[k] = rng.choice([
                rng.randint(1, 1000),
                round(rng.random(), 4),
                rng.choice(["alpha", "beta", "gamma", "stable", "draft"]),
                [words(1, rng) for _ in range(rng.randint(1, 5))],
            ])
        return node

    def new_node():
        counter[0] += 1
        return attrs()

    root = new_node()
    root["type"] = "object"
    queue = deque([(root, 1)])
    while queue and counter[0] < target_nodes:
        parent, depth = queue.popleft()
        if depth >= max_depth:
            continue
        children = []
        for _ in range(rng.randint(fanout_min, fanout_max)):
            if counter[0] >= target_nodes:
                break
            c = new_node()
            children.append(c)
            queue.append((c, depth + 1))
        # children are alternately a dict or a list; both are valid generic input
        if rng.random() < 0.5:
            parent["children"] = children
        else:
            parent["children"] = {f"k{i}": c for i, c in enumerate(children)}
        parent["type"] = "object"

    def finalize(node):
        kids = node.get("children")
        if not kids:
            node["type"] = "leaf"
            node["content"] = words(text_words, rng)
            return
        for c in (kids.values() if isinstance(kids, dict) else kids):
            finalize(c)

    finalize(root)
    return root, {"nodes": counter[0]}


def gen_embeddings(count, dim, seed):
    rng = random.Random(seed)

    def vec():
        v = [rng.gauss(0, 1) for _ in range(dim)]
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [round(x / norm, 5) for x in v]

    data = [{"node_id": f"{i:06d}", "vector": vec()} for i in range(count)]
    return data, {"vectors": count, "dim": dim}


def gen_corpus(count, seed):
    rng = random.Random(seed)
    rows = []
    for _ in range(count):
        repo = rng.choice(REPOS)
        commit = f"{rng.randrange(16 ** 8):08x}"
        depth = rng.randint(1, 5)
        path = "/".join(words(1, rng) for _ in range(depth)) + rng.choice(EXTS)
        rows.append({
            "_id": f"{repo}:{commit}:{path}",
            "id": f"{repo}:{commit}:{path}",
            "repo": repo,
            "commit": commit,
            "filepath": path,
            "title": Path(path).name,
            "text": "",
            "summary": sentence(rng.randint(8, 20), rng),
            "node_type": "file",
        })
    return rows, {"records": count}


def validate(fmt, data):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        if fmt == "chatindex":
            from contextdb.adapter.base import ChatIndexAdapter
            ts, ents = ChatIndexAdapter().convert(data)
            return f"ChatIndexAdapter OK: {len(ents)} entities, {len(ts['children'])} root topics"
        if fmt in ("filesystem", "generic"):
            from contextdb.adapter.base import GenericAdapter
            _, ents = GenericAdapter().convert(data)
            return f"GenericAdapter OK: {len(ents)} entities"
    except Exception as e:
        return f"VALIDATION FAILED: {type(e).__name__}: {e}"
    return "(no adapter)"


def write_out(data, out_path, jsonl=False):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with out_path.open("w") as f:
        if jsonl:
            for row in data:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            json.dump(data, f, ensure_ascii=False)
    return time.time() - t0, out_path.stat().st_size


def auto_depth(target, fanout_min, fanout_max, branch_multiplier=1.0):
    """Depth a balanced tree of this fanout needs to reach `target` nodes."""
    avg = max(1.5, (fanout_min + fanout_max) / 2 * branch_multiplier)
    return max(2, math.ceil(math.log(max(target, 2)) / math.log(avg)) + 1)


def run_one(fmt, args, out_path):
    n = args.count or SCALES.get(args.scale, 10_000)
    t0 = time.time()
    jsonl = False
    if fmt == "chatindex":
        fmin, fmax = args.fanout_min or 4, args.fanout_max or 8
        depth = args.max_depth or auto_depth(n, fmin, fmax)
        data, stats = gen_chatindex(n, depth, fmin, fmax, args.msgs_min, args.msgs_max,
                                    args.msg_words, args.seed)
    elif fmt == "filesystem":
        fmin, fmax = args.fanout_min or 3, args.fanout_max or 6
        depth = args.max_depth or auto_depth(n, fmin, fmax, branch_multiplier=1.6)
        data, stats = gen_filesystem(n, depth, fmin, fmax, args.files_per_dir,
                                     args.content_words, args.seed)
    elif fmt == "generic":
        fmin, fmax = args.fanout_min or 2, args.fanout_max or 5
        depth = args.max_depth or auto_depth(n, fmin, fmax)
        data, stats = gen_generic(n, depth, fmin, fmax, args.content_words, args.seed)
    elif fmt == "embeddings":
        data, stats = gen_embeddings(args.count or 50_000, args.dim, args.seed)
    elif fmt == "corpus":
        data, stats = gen_corpus(args.count or 500_000, args.seed)
        jsonl = True
    else:
        raise ValueError(fmt)

    gen_s = time.time() - t0
    write_s, size = write_out(data, out_path, jsonl=jsonl)
    report = {
        "format": fmt,
        "out": str(out_path),
        "size_bytes": size,
        "size_human": f"{size / 1e6:.1f} MB" if size < 1e9 else f"{size / 1e9:.2f} GB",
        "gen_seconds": round(gen_s, 2),
        "write_seconds": round(write_s, 2),
        **stats,
    }
    if fmt in ("chatindex", "filesystem", "generic"):
        report["validation"] = validate(fmt, data)
    print(json.dumps(report, indent=2))
    return report


def main():
    ap = argparse.ArgumentParser(description="Generate synthetic ConDB JSON formats.")
    ap.add_argument("format", choices=["chatindex", "filesystem", "generic", "embeddings", "corpus", "all"])
    ap.add_argument("--scale", choices=sorted(SCALES), default="small")
    ap.add_argument("--count", type=int, help="explicit node/record/vector count")
    ap.add_argument("--max-depth", type=int)
    ap.add_argument("--fanout-min", type=int)
    ap.add_argument("--fanout-max", type=int)
    ap.add_argument("--msgs-min", type=int, default=2)
    ap.add_argument("--msgs-max", type=int, default=12)
    ap.add_argument("--msg-words", type=int, default=25)
    ap.add_argument("--files-per-dir", type=int, default=8)
    ap.add_argument("--content-words", type=int, default=60)
    ap.add_argument("--dim", type=int, default=1536)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out")
    ap.add_argument("--out-dir")
    args = ap.parse_args()

    if args.format == "all":
        out_dir = Path(args.out_dir or "bench/db/data")
        exts = {"corpus": "jsonl"}
        for fmt in ["chatindex", "filesystem", "generic", "embeddings", "corpus"]:
            run_one(fmt, args, out_dir / f"{fmt}.{exts.get(fmt, 'json')}")
            print()
    else:
        if not args.out:
            ap.error("--out is required for a single format")
        run_one(args.format, args, args.out)


if __name__ == "__main__":
    main()
