#!/usr/bin/env python3
"""Build local repository filesystem corpus as one JSON file."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
OUT_DIR = SCRIPT_DIR / "filesystem" / "repo"
OUT_FILE = OUT_DIR / "tree.json"

MAX_FILE_SIZE = 512 * 1024  # 512KB per file

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    "node_modules",
}

SKIP_PREFIXES = {
    "bench/filesystem/",
}

ALLOWED_EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".jinja",
    ".sh",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".sql",
    ".rst",
}

ALLOWED_FILENAMES = {
    "Makefile",
    "Dockerfile",
}


def should_skip(rel_path: Path) -> bool:
    rel_posix = rel_path.as_posix()
    if any(rel_posix.startswith(prefix) for prefix in SKIP_PREFIXES):
        return True
    if any(part in SKIP_DIRS for part in rel_path.parts):
        return True
    return False


def should_include_file(file_path: Path, rel_path: Path) -> bool:
    if should_skip(rel_path):
        return False
    if not file_path.is_file():
        return False
    if file_path.suffix.lower() not in ALLOWED_EXTENSIONS and file_path.name not in ALLOWED_FILENAMES:
        return False
    try:
        if file_path.stat().st_size > MAX_FILE_SIZE:
            return False
    except OSError:
        return False
    return True


def main() -> None:
    docs: list[dict[str, str | int]] = []
    skipped = 0

    for file_path in sorted(PROJECT_ROOT.rglob("*")):
        rel_path = file_path.relative_to(PROJECT_ROOT)
        if not should_include_file(file_path, rel_path):
            skipped += 1
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue
        docs.append(
            {
                "path": rel_path.as_posix(),
                "size": len(content.encode("utf-8")),
                "content": content,
            }
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(PROJECT_ROOT),
        "files_count": len(docs),
        "files": docs,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"done: files={len(docs)} skipped={skipped} output={OUT_FILE}")


if __name__ == "__main__":
    main()
