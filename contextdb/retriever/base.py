import json
from dataclasses import dataclass
from typing import Any, Optional

from contextdb.core.storage import StorageProtocol


@dataclass
class RetrievalResult:
    nodes: list[str]
    contents: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    turns: int


class TreeFormatter:
    def __init__(self, storage: StorageProtocol):
        self.storage = storage

    def _build_children_map(self, subtree: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Build a map of parent_id -> list of children nodes."""
        children_map = {}
        for node in subtree:
            pid = node.get("parent_id")
            if pid:
                children_map.setdefault(pid, []).append(node)
        return children_map

    def format_view(self, tree_id: str, node_id: str, depth: int = 2, show_summary: bool = True) -> str:
        subtree = self.storage.get_subtree(tree_id, node_id, max_depth=depth)
        if not subtree:
            return ""

        children_map = self._build_children_map(subtree)

        def fmt(node, indent=0):
            prefix = "  " * indent
            attrs = node.get("attrs") or {}
            title = attrs.get("title", "untitled")
            summary = attrs.get("summary")
            nid = node.get("node_id", "?")
            line = f"{prefix}[{nid}] {title}"
            if show_summary and summary:
                line += f" - {summary}"
            lines = [line]
            for child in children_map.get(node["node_id"], []):
                lines.extend(fmt(child, indent + 1))
            return lines

        return "\n".join(fmt(subtree[0]))

    def format_json(self, tree_id: str, node_id: str, depth: int = 2) -> dict[str, Any]:
        subtree = self.storage.get_subtree(tree_id, node_id, max_depth=depth, with_entities=True)
        if not subtree:
            return {}

        children_map = self._build_children_map(subtree)

        def to_dict(node):
            attrs = node.get("attrs", {})
            result = {
                "node_id": node.get("node_id"),
                "title": attrs.get("title"),
                "summary": attrs.get("summary"),
                "type": ["object", "array", "leaf"][node.get("node_type", 0)],
            }
            if node.get("entity"):
                result["entity"] = node["entity"]["payload"]
            children = children_map.get(node.get("node_id"), [])
            if children:
                result["children"] = [to_dict(c) for c in children]
            return result

        return to_dict(subtree[0])


# used for debugging and testing
class ManualRetriever:
    def __init__(self, storage: StorageProtocol):
        self.storage = storage

    def _resolve_node(self, tree_id: str, node_ref: str) -> Optional[str]:
        if hasattr(self.storage, "conn"):
            cursor = self.storage.conn.cursor()
            cursor.execute("SELECT node_id FROM nodes WHERE tree_id = ? AND node_id = ?", (tree_id, node_ref))
            row = cursor.fetchone()
            if row:
                return row["node_id"]
            cursor.execute("SELECT node_id FROM nodes WHERE tree_id = ? AND entity_id = ?", (tree_id, node_ref))
            row = cursor.fetchone()
            if row:
                return row["node_id"]
            cursor.execute("SELECT node_id FROM nodes WHERE tree_id = ? AND slot = ? LIMIT 2", (tree_id, node_ref))
            rows = cursor.fetchall()
            if len(rows) == 1:
                return rows[0]["node_id"]
        return None

    def retrieve(self, tree_id: str, query: str, actions: list[dict], max_turns: int = 10) -> RetrievalResult:
        root_id = self.storage.get_root_id(tree_id)
        if not root_id:
            return RetrievalResult([], [], [], 0)

        nodes, contents, trace = [], [], []
        current = root_id

        for i, action in enumerate(actions[:max_turns]):
            atype = action.get("type")

            if atype == "expand":
                nref = action.get("node_id", current)
                resolved = self._resolve_node(tree_id, nref) or nref
                current = resolved
                trace.append({"turn": i, "action": "expand", "node_id": resolved})

            elif atype == "get_content":
                nref = action.get("node_id", current)
                resolved = self._resolve_node(tree_id, nref) or nref
                entity = self.storage.get_entity(tree_id, resolved)
                if entity:
                    contents.append(
                        {"node_id": resolved, "type": entity.entity_type, "content": json.loads(entity.payload_json)}
                    )
                    nodes.append(resolved)
                trace.append({"turn": i, "action": "get_content", "node_id": resolved})

            elif atype == "done":
                trace.append({"turn": i, "action": "done"})
                break

        return RetrievalResult(nodes, contents, trace, len(trace))
