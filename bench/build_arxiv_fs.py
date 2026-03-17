#!/usr/bin/env python3
"""Build ArXiv filesystem benchmark as one final JSON + one config."""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent / "filesystem" / "arxiv"
OUTPUT_JSON = BASE_DIR / "tree.json"
OUTPUT_CONFIG = BASE_DIR / "queries.json"
ARXIV_API = "http://export.arxiv.org/api/query"

CATEGORIES = {
    "cs.DB": ["database indexing", "query optimization"],
    "cs.IR": ["information retrieval", "retrieval augmented generation"],
    "cs.CL": ["transformer language model", "alignment RLHF"],
    "cs.CV": ["object detection", "image generation diffusion"],
    "cs.AI": ["reasoning planning AI", "automated planning"],
}


def fetch_feed(category: str, query: str, max_results: int, timeout_s: int) -> str:
    params = urllib.parse.urlencode(
        {
            "search_query": f"cat:{category} AND all:{query}",
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
        }
    )
    url = f"{ARXIV_API}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "ConDB-arxiv-builder/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_entries(xml_text: str, category: str) -> list[dict]:
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_text)
    entries: list[dict] = []

    for entry in root.findall("atom:entry", ns):
        title_node = entry.find("atom:title", ns)
        summary_node = entry.find("atom:summary", ns)
        id_node = entry.find("atom:id", ns)
        if title_node is None or summary_node is None or id_node is None:
            continue
        arxiv_id = id_node.text.strip().split("/abs/")[-1]
        title = title_node.text.strip().replace("\n", " ")
        abstract = summary_node.text.strip().replace("\n", " ")
        entries.append(
            {
                "id": arxiv_id,
                "category": category,
                "title": title,
                "abstract": abstract,
            }
        )
    return entries


def build_doc_text(item: dict) -> str:
    return (
        f"# {item['title']}\n\n"
        f"- arxiv_id: {item['id']}\n"
        f"- primary_category: {item['category']}\n\n"
        "## Abstract\n\n"
        f"{item['abstract']}\n"
    )


def build_queries(paths_by_category: dict[str, list[str]]) -> dict:
    def category_paths(category: str) -> list[str]:
        paths = sorted(set(paths_by_category.get(category, [])))
        if not paths:
            raise RuntimeError(f"missing documents for category: {category}")
        return paths

    return {
        "queries": [
            {
                "query": "Where are papers about database indexing and query optimization?",
                "ground_truth": category_paths("cs.DB"),
            },
            {
                "query": "Where are transformer language model and alignment papers?",
                "ground_truth": category_paths("cs.CL"),
            },
            {
                "query": "Where are retrieval augmented generation papers?",
                "ground_truth": category_paths("cs.IR"),
            },
        ]
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build ArXiv FS benchmark assets")
    parser.add_argument("--max-results-per-query", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    by_id: dict[str, dict] = {}
    for category, queries in CATEGORIES.items():
        for q in queries:
            try:
                feed = fetch_feed(category, q, args.max_results_per_query, args.timeout)
                for entry in parse_entries(feed, category):
                    by_id.setdefault(entry["id"], entry)
                print(f"+ {category} :: {q}")
            except Exception as e:  # noqa: BLE001
                print(f"! failed {category} :: {q} ({e})")

    docs = []
    paths_by_category: dict[str, list[str]] = {}
    for arxiv_id in sorted(by_id):
        item = by_id[arxiv_id]
        doc_path = f"papers/{arxiv_id}.md"
        docs.append(
            {
                "path": doc_path,
                "title": item["title"],
                "content": build_doc_text(item),
            }
        )
        paths_by_category.setdefault(item["category"], []).append(doc_path)

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "arxiv_api",
        "docs_count": len(docs),
        "docs": docs,
    }
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_CONFIG.write_text(
        json.dumps(build_queries(paths_by_category), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"done: docs={len(docs)} json={OUTPUT_JSON} config={OUTPUT_CONFIG}")


if __name__ == "__main__":
    main()
