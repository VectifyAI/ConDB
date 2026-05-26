#!/usr/bin/env python3
"""Build filesystem output from a blueprint.

Input: blueprint JSON (directory tree + file URLs)
Output: filesystem.json

Examples:
  python build_filesystem.py --blueprint filesystem_blueprint.json
  python build_filesystem.py --blueprint filesystem_blueprint.json --skip-download --skip-pageindex
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PDF_DIR = SCRIPT_DIR / "pdfs"
TREE_DIR = SCRIPT_DIR / "trees"
FS_OUTPUT = SCRIPT_DIR / "filesystem.json"
PAGEINDEX_DIR = Path(os.environ.get("PAGEINDEX_DIR", SCRIPT_DIR.parent.parent.parent / "PageIndex"))
PAGEINDEX_MODEL = os.environ.get("PAGEINDEX_MODEL", "gpt-4o")


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\s\-]", "", name)
    name = re.sub(r"\s+", "-", name.strip())
    return (name or "untitled")[:100].lower()


def safe_stem_from_node(name: str, url: str) -> str:
    raw = Path(name).stem if name else Path(urllib.request.url2pathname(url)).stem
    raw = raw.replace("/", "_")
    raw = re.sub(r"[^\w.\-]", "", raw)
    return (raw or "untitled").lower()


def load_blueprint(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Blueprint not found: {path}")
    with open(path) as f:
        bp = json.load(f)
    if not isinstance(bp, dict) or bp.get("type") != "directory":
        raise ValueError("Blueprint root must be a directory object")
    return bp


def iter_file_nodes(node: dict, parents: list[str] | None = None):
    parents = parents or []
    t = node.get("type")
    name = node.get("name", "")

    if t == "directory":
        for c in node.get("children", []):
            yield from iter_file_nodes(c, parents + ([name] if name else []))
    elif t == "file":
        url = node.get("url")
        if not url:
            return
        yield parents, node


def download_file(url: str, out_path: Path) -> bool:
    if out_path.exists() and out_path.stat().st_size > 1024:
        return True
    try:
        urllib.request.urlretrieve(url, out_path)
        return True
    except Exception as e:
        print(f"ERROR: download failed: {url} ({e})", flush=True)
        return False


def run_pageindex_on_pdf(pdf_path: Path, model: str) -> dict | None:
    safe_name = pdf_path.stem
    tree_path = TREE_DIR / f"{safe_name}_structure.json"

    if tree_path.exists():
        with open(tree_path) as f:
            return json.load(f)

    if not PAGEINDEX_DIR.exists():
        print(f"ERROR: PageIndex dir not found: {PAGEINDEX_DIR}", flush=True)
        return None

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CHATGPT_API_KEY", "")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set", flush=True)
        return None

    (PAGEINDEX_DIR / ".env").write_text(f"OPENAI_API_KEY={api_key}\nCHATGPT_API_KEY={api_key}\n")

    try:
        result = subprocess.run(
            [
                sys.executable,
                "run_pageindex.py",
                "--pdf_path",
                str(pdf_path),
                "--model",
                model,
                "--if-add-node-text",
                "yes",
                "--if-add-node-summary",
                "yes",
                "--if-add-node-id",
                "yes",
                "--if-add-doc-description",
                "no",
            ],
            cwd=str(PAGEINDEX_DIR),
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, "CHATGPT_API_KEY": api_key},
        )
    except subprocess.TimeoutExpired:
        print(f"ERROR: pageindex timeout: {safe_name}", flush=True)
        return None

    if result.returncode != 0:
        print(f"ERROR: pageindex failed: {safe_name}: {result.stderr[:300]}", flush=True)
        return None

    pi_output = PAGEINDEX_DIR / "results" / f"{safe_name}_structure.json"
    if not pi_output.exists():
        print(f"ERROR: pageindex output not found: {pi_output}", flush=True)
        return None

    with open(pi_output) as f:
        tree = json.load(f)

    shutil.copy2(pi_output, tree_path)
    return tree


def tree_node_to_files(node: dict) -> list[dict]:
    title = node.get("title", "untitled")
    fname = sanitize_filename(title)
    children_nodes = node.get("nodes", [])
    node_text = node.get("text", "")
    summary = node.get("summary", "")

    if children_nodes:
        dir_entry = {"name": fname, "type": "directory", "children": []}
        content = f"# {title}\n\n"
        if summary:
            content += f"**Summary:** {summary}\n\n"
        if node_text:
            content += node_text + "\n"
        if content.strip() != f"# {title}":
            dir_entry["children"].append({"name": "overview.md", "type": "file", "content": content})
        for child in children_nodes:
            dir_entry["children"].extend(tree_node_to_files(child))
        return [dir_entry]

    content = f"# {title}\n\n"
    if summary:
        content += f"**Summary:** {summary}\n\n"
    if node_text:
        content += node_text + "\n"
    return [{"name": f"{fname}.md", "type": "file", "content": content}]


def ensure_dir(parent: dict, name: str) -> dict:
    ex = next((c for c in parent.get("children", []) if c.get("type") == "directory" and c.get("name") == name), None)
    if ex:
        return ex
    d = {"name": name, "type": "directory", "children": []}
    parent.setdefault("children", []).append(d)
    return d


def build_filesystem_from_blueprint(blueprint: dict, tree_map: dict[str, dict], source_map: dict[str, dict]) -> dict:
    out_root = {"name": blueprint.get("name", "papers"), "type": "directory", "children": []}
    seen_stems: set[str] = set()

    for parents, file_node in iter_file_nodes(blueprint):
        url = file_node["url"]
        name = file_node.get("name", "paper.pdf")
        safe_stem = safe_stem_from_node(name, url)
        if safe_stem in seen_stems:
            continue
        seen_stems.add(safe_stem)

        tree = tree_map.get(safe_stem)

        cur = out_root
        root_name = blueprint.get("name")
        rel_parents = parents
        if rel_parents and root_name and rel_parents[0] == root_name:
            rel_parents = rel_parents[1:]

        for p in rel_parents:
            cur = ensure_dir(cur, p)

        paper_dir = ensure_dir(cur, safe_stem)

        source_entry = {
            "name": "source.json",
            "type": "file",
            "content": json.dumps(
                {
                    "name": name,
                    "url": url,
                    "local_pdf": source_map.get(safe_stem, {}).get("pdf_path"),
                },
                ensure_ascii=False,
                indent=2,
            ),
        }
        existing_source = next((c for c in paper_dir["children"] if c.get("type") == "file" and c.get("name") == "source.json"), None)
        if existing_source:
            existing_source["content"] = source_entry["content"]
        else:
            paper_dir["children"].append(source_entry)

        if tree:
            structure = tree.get("structure", tree if isinstance(tree, list) else [])
            if isinstance(structure, list) and structure:
                sec_dir = {"name": "sections", "type": "directory", "children": []}
                for i, node in enumerate(structure):
                    files = tree_node_to_files(node)
                    for f in files:
                        f["name"] = f"{i:02d}-{f['name']}"
                        sec_dir["children"].append(f)
                existing_sections = next((c for c in paper_dir["children"] if c.get("type") == "directory" and c.get("name") == "sections"), None)
                if existing_sections:
                    existing_sections["children"] = sec_dir["children"]
                else:
                    paper_dir["children"].append(sec_dir)

    return out_root


def count_nodes(node: dict) -> tuple[int, int]:
    if node.get("type") == "file":
        return 1, 0
    files, dirs = 0, 1
    for c in node.get("children", []):
        f, d = count_nodes(c)
        files += f
        dirs += d
    return files, dirs


def main():
    parser = argparse.ArgumentParser(description="Download + PageIndex build from filesystem blueprint")
    parser.add_argument("--blueprint", type=str, default=str(SCRIPT_DIR / "filesystem_blueprint.json"))
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-pageindex", action="store_true")
    parser.add_argument("--model", type=str, default=PAGEINDEX_MODEL)
    args = parser.parse_args()

    for d in [PDF_DIR, TREE_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    blueprint = load_blueprint(Path(args.blueprint))

    source_map: dict[str, dict] = {}
    tree_map: dict[str, dict] = {}

    files = list(iter_file_nodes(blueprint))
    print(f"INFO: input files from blueprint: {len(files)}", flush=True)

    unique_files: list[tuple[list[str], dict]] = []
    seen_stems: set[str] = set()
    for parents, node in files:
        url = node["url"]
        name = node.get("name", "paper.pdf")
        safe_stem = safe_stem_from_node(name, url)
        if safe_stem in seen_stems:
            continue
        seen_stems.add(safe_stem)
        unique_files.append((parents, node))

    print(f"INFO: unique papers after dedup: {len(unique_files)}", flush=True)

    for _parents, node in unique_files:
        url = node["url"]
        name = node.get("name", "paper.pdf")
        safe_stem = safe_stem_from_node(name, url)
        pdf_path = PDF_DIR / f"{safe_stem}.pdf"

        ok = True
        if not args.skip_download:
            ok = download_file(url, pdf_path)
        else:
            ok = pdf_path.exists()

        if not ok:
            continue

        source_map[safe_stem] = {"pdf_path": str(pdf_path), "url": url, "name": name}

        if args.skip_pageindex:
            tree_path = TREE_DIR / f"{safe_stem}_structure.json"
            if tree_path.exists():
                with open(tree_path) as f:
                    tree_map[safe_stem] = json.load(f)
            continue

        tree = run_pageindex_on_pdf(pdf_path, args.model)
        if tree:
            tree_map[safe_stem] = tree

    fs = build_filesystem_from_blueprint(blueprint, tree_map, source_map)
    with open(FS_OUTPUT, "w") as f:
        json.dump(fs, f, indent=2, ensure_ascii=False)

    fcnt, dcnt = count_nodes(fs)
    print(f"INFO: done: {fcnt} files, {dcnt} dirs", flush=True)
    print(f"INFO: output: {FS_OUTPUT}", flush=True)


if __name__ == "__main__":
    main()
