import json
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Protocol, runtime_checkable
from contextdb.core.storage import StorageProtocol, TreeDB
from contextdb.llm import LLMProtocol


@dataclass
class RetrievalResult:
    nodes: List[str]
    contents: List[Dict[str, Any]]
    trace: List[Dict[str, Any]]
    turns: int


@runtime_checkable
class RetrieverProtocol(Protocol):
    def retrieve(self, tree_id: str, query: str, max_turns: int = 10) -> RetrievalResult: ...


class TreeFormatter:
    def __init__(self, storage: StorageProtocol):
        self.storage = storage

    def format_view(self, tree_id: str, node_id: str, depth: int = 2, show_summary: bool = True) -> str:
        subtree = self.storage.get_subtree(tree_id, node_id, max_depth=depth)
        if not subtree:
            return ""

        children_map = {}
        for node in subtree:
            pid = node.get('parent_id')
            if pid:
                children_map.setdefault(pid, []).append(node)

        def fmt(node, indent=0):
            prefix = "  " * indent
            attrs = node.get("attrs") or {}
            title = attrs.get("title", "untitled")
            summary = attrs.get("summary")
            nid = node.get("node_id", "?")[:8]
            line = f"{prefix}[{nid}] {title}"
            if show_summary and summary:
                line += f" - {summary}"
            lines = [line]
            for child in children_map.get(node['node_id'], []):
                lines.extend(fmt(child, indent + 1))
            return lines

        return "\n".join(fmt(subtree[0]))

    def format_json(self, tree_id: str, node_id: str, depth: int = 2) -> Dict[str, Any]:
        subtree = self.storage.get_subtree(tree_id, node_id, max_depth=depth, with_entities=True)
        if not subtree:
            return {}

        children_map = {}
        for node in subtree:
            pid = node.get('parent_id')
            if pid:
                children_map.setdefault(pid, []).append(node)

        def to_dict(node):
            attrs = node.get("attrs", {})
            result = {
                "node_id": node.get("node_id"),
                "title": attrs.get("title"),
                "summary": attrs.get("summary"),
                "type": ["object", "array", "leaf"][node.get("node_type", 0)]
            }
            if node.get("entity"):
                result["entity"] = node["entity"]["payload"]
            children = children_map.get(node.get("node_id"), [])
            if children:
                result["children"] = [to_dict(c) for c in children]
            return result

        return to_dict(subtree[0])


class LLMRetriever:
    def __init__(self, storage: StorageProtocol, llm: LLMProtocol):
        self.storage = storage
        self.llm = llm
        self.formatter = TreeFormatter(storage)

    def _resolve_node(self, tree_id: str, node_ref: str) -> Optional[str]:
        if hasattr(self.storage, 'conn'):
            cursor = self.storage.conn.cursor()
            # exact match
            cursor.execute("SELECT node_id FROM nodes WHERE tree_id = ? AND node_id = ?", (tree_id, node_ref))
            row = cursor.fetchone()
            if row:
                return row['node_id']
            # entity_id match
            cursor.execute("SELECT node_id FROM nodes WHERE tree_id = ? AND entity_id = ?", (tree_id, node_ref))
            row = cursor.fetchone()
            if row:
                return row['node_id']
            # prefix match
            if len(node_ref) < 36:
                cursor.execute("SELECT node_id FROM nodes WHERE tree_id = ? AND node_id LIKE ?", (tree_id, f"{node_ref}%"))
                row = cursor.fetchone()
                if row:
                    return row['node_id']
        return None

    def retrieve(self, tree_id: str, query: str, max_turns: int = 10) -> RetrievalResult:
        root_id = self.storage.get_root_id(tree_id)
        if not root_id:
            return RetrievalResult([], [], [], 0)

        nodes, contents, trace = [], [], []
        current = root_id
        conversation = []

        tools = [
            {"name": "expand_node", "description": "Expand a tree node to see children", "input_schema": {"type": "object", "properties": {"node_id": {"type": "string"}, "depth": {"type": "integer", "default": 1}}, "required": ["node_id"]}},
            {"name": "get_content", "description": "Get full content of a node", "input_schema": {"type": "object", "properties": {"node_id": {"type": "string"}}, "required": ["node_id"]}},
            {"name": "done", "description": "Finish retrieval", "input_schema": {"type": "object", "properties": {}}}
        ]

        system = f"""You are a tree navigation assistant.

Task: {query}

Strategy:
1. Use expand_node to explore nodes
2. Use get_content to retrieve relevant content
3. Call done() when finished"""

        for turn in range(max_turns):
            view = self.formatter.format_view(tree_id, current, depth=2)
            conversation.append({"role": "user", "content": f"Current view:\n{view}\n\nNext action?"})

            try:
                resp = self.llm.chat(conversation, system=system, tools=tools)

                tool_results = []
                for block in resp.get("content", []):
                    if block.get("type") == "tool_use":
                        name = block["name"]
                        inp = block.get("input", {})
                        tid = block["id"]

                        if name == "expand_node":
                            nid = inp.get("node_id", current)
                            current = nid
                            trace.append({"turn": turn, "action": "expand", "node_id": nid})
                            tool_results.append({"type": "tool_result", "tool_use_id": tid, "content": f"Expanded {nid}"})

                        elif name == "get_content":
                            nref = inp.get("node_id", current)
                            resolved = self._resolve_node(tree_id, nref) or nref
                            entity = self.storage.get_entity(tree_id, resolved)
                            if entity:
                                contents.append({"node_id": resolved, "type": entity.entity_type, "content": json.loads(entity.payload_json)})
                                nodes.append(resolved)
                                tool_results.append({"type": "tool_result", "tool_use_id": tid, "content": f"Got content from {resolved}"})
                            else:
                                tool_results.append({"type": "tool_result", "tool_use_id": tid, "content": f"No content for {resolved}"})
                            trace.append({"turn": turn, "action": "get_content", "node_id": resolved})

                        elif name == "done":
                            trace.append({"turn": turn, "action": "done"})
                            return RetrievalResult(nodes, contents, trace, len(trace))

                if tool_results:
                    conversation.append({"role": "assistant", "content": resp.get("content", [])})
                    conversation.append({"role": "user", "content": tool_results})

            except Exception as e:
                trace.append({"turn": turn, "action": "error", "error": str(e)})
                break

        trace.append({"turn": max_turns, "action": "max_turns"})
        return RetrievalResult(nodes, contents, trace, len(trace))


class ManualRetriever:
    def __init__(self, storage: StorageProtocol):
        self.storage = storage

    def _resolve_node(self, tree_id: str, node_ref: str) -> Optional[str]:
        if hasattr(self.storage, 'conn'):
            cursor = self.storage.conn.cursor()
            cursor.execute("SELECT node_id FROM nodes WHERE tree_id = ? AND node_id = ?", (tree_id, node_ref))
            row = cursor.fetchone()
            if row:
                return row['node_id']
            cursor.execute("SELECT node_id FROM nodes WHERE tree_id = ? AND entity_id = ?", (tree_id, node_ref))
            row = cursor.fetchone()
            if row:
                return row['node_id']
            if len(node_ref) < 36:
                cursor.execute("SELECT node_id FROM nodes WHERE tree_id = ? AND node_id LIKE ?", (tree_id, f"{node_ref}%"))
                row = cursor.fetchone()
                if row:
                    return row['node_id']
        return None

    def retrieve(self, tree_id: str, query: str, actions: List[Dict], max_turns: int = 10) -> RetrievalResult:
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
                    contents.append({"node_id": resolved, "type": entity.entity_type, "content": json.loads(entity.payload_json)})
                    nodes.append(resolved)
                trace.append({"turn": i, "action": "get_content", "node_id": resolved})

            elif atype == "done":
                trace.append({"turn": i, "action": "done"})
                break

        return RetrievalResult(nodes, contents, trace, len(trace))
