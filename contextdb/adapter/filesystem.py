"""FileSystemAdapter — directory tree → ConDB tree."""

import fnmatch
import json
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
        stat = file_path.stat()
        ext = file_path.suffix.lower()
        rel = str(file_path.relative_to(self.root))

        if ext == ".json":
            try:
                raw = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw = ""
            parsed = self._try_parse_json(raw)
            if parsed is not None:
                expanded = self._scan_embedded_tree_json(file_path, rel, stat, entities, parsed_payload=parsed)
                if expanded is not None:
                    return expanded
                return self._build_json_tree_node(
                    value=parsed,
                    title=file_path.name,
                    rel_path=rel,
                    mtime=stat.st_mtime,
                    entities=entities,
                    virtual_source=rel,
                )

        expanded = self._scan_embedded_tree_json(file_path, rel, stat, entities)
        if expanded is not None:
            return expanded

        entity_id = str(uuid.uuid4())
        entities[entity_id] = {
            "type": "file",
            "title": file_path.name,
            "summary": "",
            "text": "",
        }

        attrs = {
            "title": file_path.name,
            "file_size": stat.st_size,
            "extension": ext,
            "mtime": stat.st_mtime,
            "is_dir": False,
            "rel_path": rel,
        }

        return {
            "type": "leaf",
            "attrs": attrs,
            "entity_id": entity_id,
        }

    @staticmethod
    def _try_parse_json(text: str) -> Any | None:
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return None

    def _build_json_tree_node(
        self,
        value: Any,
        title: str,
        rel_path: str,
        mtime: float,
        entities: dict[str, dict[str, Any]],
        virtual_source: str,
    ) -> dict[str, Any]:
        if isinstance(value, dict):
            children: dict[str, dict[str, Any]] = {}
            for key, child_value in value.items():
                slot = str(key)
                child_rel = f"{rel_path}/{slot}" if rel_path else slot
                children[slot] = self._build_json_tree_node(
                    value=child_value,
                    title=slot,
                    rel_path=child_rel,
                    mtime=mtime,
                    entities=entities,
                    virtual_source=virtual_source,
                )

            preview_keys = [str(k) for k in list(value.keys())[:20]]
            summary = f"json object keys: {', '.join(preview_keys)}" if preview_keys else "json object (empty)"
            entity_id = str(uuid.uuid4())
            entities[entity_id] = {
                "type": "directory",
                "title": title,
                "summary": summary,
            }
            return {
                "type": "object",
                "attrs": {
                    "title": title,
                    "is_dir": True,
                    "mtime": mtime,
                    "rel_path": rel_path,
                    "virtual": True,
                    "json_expanded": True,
                    "virtual_source": virtual_source,
                },
                "entity_id": entity_id,
                "children": children if children else None,
            }

        if isinstance(value, list):
            children = {}
            for idx, child_value in enumerate(value):
                slot = str(idx)
                child_rel = f"{rel_path}/{slot}" if rel_path else slot
                children[slot] = self._build_json_tree_node(
                    value=child_value,
                    title=slot,
                    rel_path=child_rel,
                    mtime=mtime,
                    entities=entities,
                    virtual_source=virtual_source,
                )

            entity_id = str(uuid.uuid4())
            entities[entity_id] = {
                "type": "directory",
                "title": title,
                "summary": f"json array length: {len(value)}",
            }
            return {
                "type": "object",
                "attrs": {
                    "title": title,
                    "is_dir": True,
                    "mtime": mtime,
                    "rel_path": rel_path,
                    "virtual": True,
                    "json_expanded": True,
                    "virtual_source": virtual_source,
                },
                "entity_id": entity_id,
                "children": children if children else None,
            }

        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        ext = Path(rel_path).suffix.lower()

        entity_id = str(uuid.uuid4())
        entities[entity_id] = {
            "type": "file",
            "title": title,
            "summary": "",
            "text": text,
        }
        return {
            "type": "leaf",
            "attrs": {
                "title": title,
                "file_size": len(text.encode("utf-8")),
                "extension": ext,
                "mtime": mtime,
                "is_dir": False,
                "rel_path": rel_path,
                "virtual_source": virtual_source,
            },
            "entity_id": entity_id,
        }

    def _scan_embedded_tree_json(
        self,
        file_path: Path,
        rel: str,
        stat,
        entities: dict[str, dict[str, Any]],
        parsed_payload: Any | None = None,
    ) -> dict[str, Any] | None:
        """Expand tree-style JSON payloads (docs/files) into virtual nodes."""
        if file_path.suffix.lower() != ".json":
            return None

        if parsed_payload is None:
            try:
                payload = json.loads(file_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                return None
        else:
            payload = parsed_payload

        if not isinstance(payload, dict):
            return None

        entry_kind = None
        entries = payload.get("files")
        if isinstance(entries, list):
            entry_kind = "files"
        else:
            entries = payload.get("docs")
            if isinstance(entries, list):
                entry_kind = "docs"
        if entry_kind is None:
            return None

        normalized: list[dict[str, str]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue

            item_path = item.get("path")
            if not isinstance(item_path, str):
                continue
            item_path = item_path.strip().lstrip("/")
            if not item_path:
                continue

            rel_path = Path(item_path).as_posix()
            if rel_path in {"", "."}:
                continue

            content = item.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(item.get("text"), str):
                text = item["text"]
            elif isinstance(item.get("abstract"), str):
                text = item["abstract"]
            else:
                text = ""

            title = item.get("title")
            if not isinstance(title, str) or not title.strip():
                title = Path(rel_path).name

            normalized.append({"path": rel_path, "title": title.strip(), "content": text})

        if not normalized:
            return None

        tree = {"dirs": {}, "files": []}
        for entry in normalized:
            parts = [p for p in entry["path"].split("/") if p]
            if not parts:
                continue
            node = tree
            for part in parts[:-1]:
                node = node["dirs"].setdefault(part, {"dirs": {}, "files": []})
            node["files"].append((parts[-1], entry))

        def build_virtual_children(node: dict, parent_rel: str) -> dict[str, dict[str, Any]]:
            children: dict[str, dict[str, Any]] = {}

            for dir_name in sorted(node["dirs"]):
                sub = node["dirs"][dir_name]
                child_rel = f"{parent_rel}/{dir_name}" if parent_rel else dir_name
                sub_children = build_virtual_children(sub, child_rel)

                subdir_names = [name + "/" for name in sorted(sub["dirs"])]
                file_names = [name for name, _ in sorted(sub["files"], key=lambda x: x[0].lower())]
                parts: list[str] = []
                if subdir_names:
                    parts.append(f"subdirs: {', '.join(subdir_names[:20])}")
                if file_names:
                    parts.append(f"files: {', '.join(file_names[:20])}")

                dir_entity_id = str(uuid.uuid4())
                entities[dir_entity_id] = {
                    "type": "directory",
                    "title": dir_name,
                    "summary": " | ".join(parts) if parts else "(empty)",
                }
                children[dir_name] = {
                    "type": "object",
                    "attrs": {
                        "title": dir_name,
                        "is_dir": True,
                        "mtime": stat.st_mtime,
                        "rel_path": child_rel,
                        "virtual": True,
                    },
                    "entity_id": dir_entity_id,
                    "children": sub_children if sub_children else None,
                }

            for slot_name, entry in sorted(node["files"], key=lambda x: x[0].lower()):
                virtual_rel = entry["path"]
                virtual_ext = Path(virtual_rel).suffix.lower()
                base_name = Path(virtual_rel).name or slot_name
                key = base_name
                if key in children:
                    key = f"{base_name}::{uuid.uuid4().hex[:8]}"

                if virtual_ext == ".json":
                    parsed_child = self._try_parse_json(entry["content"])
                else:
                    parsed_child = None

                if parsed_child is not None:
                    children[key] = self._build_json_tree_node(
                        value=parsed_child,
                        title=entry["title"],
                        rel_path=virtual_rel,
                        mtime=stat.st_mtime,
                        entities=entities,
                        virtual_source=rel,
                    )
                else:
                    text = entry["content"]

                    file_entity_id = str(uuid.uuid4())
                    entities[file_entity_id] = {
                        "type": "file",
                        "title": entry["title"],
                        "summary": "",
                        "text": text,
                    }

                    children[key] = {
                        "type": "leaf",
                        "attrs": {
                            "title": entry["title"],
                            "file_size": len(text.encode("utf-8")),
                            "extension": virtual_ext,
                            "mtime": stat.st_mtime,
                            "is_dir": False,
                            "rel_path": virtual_rel,
                            "virtual_source": rel,
                        },
                        "entity_id": file_entity_id,
                    }

            return children

        virtual_children = build_virtual_children(tree, parent_rel="")

        root_entity_id = str(uuid.uuid4())
        entities[root_entity_id] = {
            "type": "directory",
            "title": file_path.name,
            "summary": f"virtual entries: {len(normalized)} ({entry_kind})",
        }
        return {
            "type": "object",
            "attrs": {
                "title": file_path.name,
                "is_dir": True,
                "mtime": stat.st_mtime,
                "rel_path": rel,
                "virtual": True,
                "virtual_entries": len(normalized),
                "virtual_entry_kind": entry_kind,
            },
            "entity_id": root_entity_id,
            "children": virtual_children if virtual_children else None,
        }

    def ingest(self, storage: Any) -> str:
        tree, entities = self.convert()
        return storage.ingest_tree(
            tree, entities,
            meta={"source": "filesystem", "root_dir": str(self.root)},
        )
