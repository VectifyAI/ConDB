"""FileSystemAdapter — directory tree → ConDB tree."""

import fnmatch
import uuid
from pathlib import Path
from typing import Any, Optional

from contextdb.adapter.base import BaseAdapter

DEFAULT_IGNORE_PATTERNS = [
    ".git", ".git/**", "node_modules", "node_modules/**",
    "__pycache__", "__pycache__/**", ".env", "*.pyc", "*.pyo",
    ".DS_Store", "Thumbs.db", ".idea", ".idea/**", ".vscode", ".vscode/**",
    "*.so", "*.dylib", "*.dll", "*.exe", "*.o", "*.a",
    ".tox", ".tox/**", ".mypy_cache", ".mypy_cache/**",
    ".pytest_cache", ".pytest_cache/**", "*.egg-info", "*.egg-info/**",
    "dist", "dist/**", "build", "build/**",
]

def _parse_gitignore(gitignore_path: Path) -> list[str]:
    patterns = []
    try:
        for line in gitignore_path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line.rstrip("/"))
            if not line.endswith("/**"):
                patterns.append(line.rstrip("/") + "/**")
    except OSError:
        pass
    return patterns


def _is_ignored(rel_path: str, name: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_path, pat):
            return True
    return False


class FileSystemAdapter(BaseAdapter):

    def __init__(
        self,
        root_dir: str,
        ignore_patterns: Optional[list[str]] = None,
    ):
        self.root = Path(root_dir).resolve()
        if not self.root.is_dir():
            raise ValueError(f"Not a directory: {self.root}")

        self.ignore_patterns = list(ignore_patterns or DEFAULT_IGNORE_PATTERNS)
        gitignore = self.root / ".gitignore"
        if gitignore.is_file():
            self.ignore_patterns.extend(_parse_gitignore(gitignore))

    def convert(self, source_json: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        entities: dict[str, dict[str, Any]] = {}
        tree = self._scan_dir(self.root, entities)
        return tree, entities

    @staticmethod
    def _get_file_tag(rel_path: str, ext: str, name: str) -> str:
        parts = rel_path.replace("\\", "/").split("/")
        if any(p in ("test", "tests") for p in parts):
            return "[test]"
        if ext in (".md", ".rst", ".txt") or "docs/" in rel_path.replace("\\", "/") + "/":
            return "[doc]"
        config_names = {"setup.py", "setup.cfg", "pyproject.toml", "Makefile", "Dockerfile"}
        config_exts = {".ini", ".cfg", ".yaml", ".yml", ".toml"}
        if name in config_names or ext in config_exts:
            return "[config]"
        return "[src]"

    def _scan_dir(self, dir_path: Path, entities: dict[str, dict[str, Any]]) -> dict[str, Any]:
        entity_id = str(uuid.uuid4())
        children: dict[str, dict[str, Any]] = {}
        child_names: list[str] = []

        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            entries = []

        for entry in entries:
            rel = str(entry.relative_to(self.root))
            if _is_ignored(rel, entry.name, self.ignore_patterns):
                continue

            if entry.is_dir():
                child_names.append(entry.name + "/")
                children[entry.name] = self._scan_dir(entry, entities)
            elif entry.is_file():
                child_names.append(entry.name)
                children[entry.name] = self._scan_file(entry, entities)

        dir_name = dir_path.name or str(self.root)
        rel_path = str(dir_path.relative_to(self.root))

        # Build directory summary from child names only
        subdirs = []
        files = []
        for cname, cnode in children.items():
            if cnode.get("type") == "object":
                subdirs.append(cname + "/")
            elif cnode.get("type") == "leaf":
                files.append(cname)

        parts = []
        if subdirs:
            parts.append(f"subdirs: {', '.join(subdirs[:20])}")
        if files:
            parts.append(f"files: {', '.join(files[:20])}")

        dir_summary = " | ".join(parts) if parts else ", ".join(child_names[:50]) or "(empty)"

        entities[entity_id] = {
            "type": "directory",
            "title": dir_name,
            "summary": dir_summary,
        }

        stat = dir_path.stat()
        return {
            "type": "object",
            "attrs": {
                "title": dir_name,
                "is_dir": True,
                "mtime": stat.st_mtime,
                "rel_path": rel_path,
            },
            "entity_id": entity_id,
            "children": children if children else None,
        }

    def _scan_file(self, file_path: Path, entities: dict[str, dict[str, Any]]) -> dict[str, Any]:
        entity_id = str(uuid.uuid4())
        stat = file_path.stat()
        ext = file_path.suffix.lower()
        rel = str(file_path.relative_to(self.root))

        entities[entity_id] = {
            "type": "file",
            "title": file_path.name,
            "summary": "",
            "text": "",
        }

        tag = self._get_file_tag(rel, ext, file_path.name)

        attrs = {
            "title": file_path.name,
            "file_size": stat.st_size,
            "extension": ext,
            "mtime": stat.st_mtime,
            "is_dir": False,
            "tag": tag,
            "rel_path": rel,
        }

        return {
            "type": "leaf",
            "attrs": attrs,
            "entity_id": entity_id,
        }

    def ingest(self, storage: Any) -> str:
        tree, entities = self.convert()
        return storage.ingest_tree(
            tree, entities,
            meta={"source": "filesystem", "root_dir": str(self.root)},
        )
