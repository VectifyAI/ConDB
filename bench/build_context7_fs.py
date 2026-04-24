#!/usr/bin/env python3
"""Build Context7 filesystem corpus as one JSON tree file."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent / "filesystem" / "context7"
LLMS_TXT_URL = "https://context7.com/docs/llms.txt"
TREE_FILENAME = "tree.json"
# 0 means "all docs found in llms.txt".
MAX_DOCS = int(os.environ.get("CONTEXT7_MAX_DOCS", "0"))
TIMEOUT = 15


def fetch_text(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "ConDB-context7-builder/1.0"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_doc_urls(llms_txt: str) -> list[str]:
    urls = re.findall(r"\((https://context7\.com/docs/[^)]+)\)", llms_txt)
    dedup: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        dedup.append(url)
    return dedup


def logical_path_for_url(url: str) -> str:
    path = urlparse(url).path.lstrip("/")
    if not path:
        path = "index.md"
    if path.endswith("/"):
        path += "index.md"
    if "." not in Path(path).name:
        path += ".md"
    return path


def write_tree_file(out_path: Path, docs: list[dict[str, str]]) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_index": LLMS_TXT_URL,
        "docs_count": len(docs),
        "docs": docs,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    llms_txt = fetch_text(LLMS_TXT_URL)
    all_doc_urls = parse_doc_urls(llms_txt)
    doc_urls = [url for url in all_doc_urls if url.endswith(".md")]
    if not doc_urls:
        raise RuntimeError(f"no markdown doc urls found from {LLMS_TXT_URL}")
    if MAX_DOCS > 0:
        doc_urls = doc_urls[:MAX_DOCS]

    docs: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for url in doc_urls:
        try:
            content = fetch_text(url)
            path = logical_path_for_url(url)
            docs.append({"url": url, "path": path, "content": content})
            print(f"+ {url} -> {TREE_FILENAME}::{path}")
        except Exception as e:  # noqa: BLE001
            errors.append({"url": url, "error": str(e)})
            print(f"! failed: {url} ({e})")

    if not docs:
        raise RuntimeError(f"failed to fetch any context7 docs; errors={len(errors)}")

    tree_path = BASE_DIR / TREE_FILENAME
    write_tree_file(tree_path, docs)

    print(
        f"done: docs={len(docs)} errors={len(errors)} "
        f"tree={tree_path}"
    )
    if errors:
        print("error_summary:")
        for err in errors:
            print(f"- {err['url']} :: {err['error']}")


if __name__ == "__main__":
    main()
