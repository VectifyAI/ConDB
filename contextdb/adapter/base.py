from abc import ABC, abstractmethod
from typing import Any

# Keys to skip when extracting attrs from node dicts
_SKIP_KEYS = frozenset({"content", "text", "children", "nodes"})


class BaseAdapter(ABC):
    @abstractmethod
    def convert(self, source_json: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        pass


class DocumentTreeAdapter(BaseAdapter):
    """Adapter for pageindex-compatible hierarchical document trees."""

    def convert(self, document_json: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        entities = {}

        def extract_node(node: dict[str, Any]) -> dict[str, Any]:
            node_id = node.get("node_id")
            entity_content = {
                "type": "section",
                "title": node.get("title"),
                "summary": node.get("summary", ""),
                "structure": node.get("structure", ""),
            }
            if node.get("text"):
                entity_content["text"] = node["text"]
            else:
                entity_content["metadata"] = {
                    "line_num": node.get("line_num"),
                    "start_index": node.get("start_index"),
                    "end_index": node.get("end_index"),
                }
            entities[node_id] = entity_content

            children = None
            if node.get("nodes"):
                children = {n["node_id"]: extract_node(n) for n in node["nodes"]}

            return {
                "type": "object" if children else "leaf",
                "attrs": {
                    "title": node.get("title"),
                    "summary": node.get("summary"),
                    "structure": node.get("structure"),
                    "page_start": node.get("start_index"),
                    "page_end": node.get("end_index"),
                    "line_num": node.get("line_num"),
                },
                "entity_id": node_id,
                "children": children,
            }

        root_entity_id = "__root__"
        entities[root_entity_id] = {
            "type": "document",
            "title": document_json.get("doc_name", "Document"),
            "description": document_json.get("doc_description", ""),
            "metadata": {
                "doc_name": document_json.get("doc_name"),
                "doc_description": document_json.get("doc_description"),
            },
        }

        tree_structure = {
            "type": "object",
            "attrs": {
                "doc_name": document_json.get("doc_name"),
                "doc_description": document_json.get("doc_description"),
            },
            "entity_id": root_entity_id,
            "children": {n["node_id"]: extract_node(n) for n in document_json.get("structure", [])},
        }

        return tree_structure, entities


# Backward-compatible alias for the existing schema name.
PageIndexAdapter = DocumentTreeAdapter


class ChatIndexAdapter(BaseAdapter):
    def convert(self, chatindex_json: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        entities = {}
        root_entity_id = "__root__"
        conversation_title = chatindex_json.get("conversation_id") or "Conversation"
        entities[root_entity_id] = {
            "type": "conversation",
            "title": conversation_title,
            "conversation_id": chatindex_json.get("conversation_id"),
            "participants": chatindex_json.get("participants"),
            "metadata": {
                "conversation_id": chatindex_json.get("conversation_id"),
                "participants": chatindex_json.get("participants"),
            },
        }

        def extract_topic(topic: dict[str, Any]) -> dict[str, Any]:
            topic_id = topic.get("node_id")
            messages = topic.get("messages", [])

            entities[topic_id] = {
                "type": "topic",
                "title": topic.get("title"),
                "summary": topic.get("summary", ""),
                "messages": messages,
                "msg_start": topic.get("msg_start"),
                "msg_end": topic.get("msg_end"),
            }

            children = None
            subtopics = topic.get("subtopics", [])
            if subtopics:
                children = {st.get("node_id"): extract_topic(st) for st in subtopics}

            return {
                "type": "object" if children else "leaf",
                "attrs": {
                    "title": topic.get("title"),
                    "summary": topic.get("summary"),
                    "msg_start": topic.get("msg_start"),
                    "msg_end": topic.get("msg_end"),
                },
                "entity_id": topic_id,
                "children": children,
            }

        tree_structure = {
            "type": "object",
            "attrs": {
                "title": conversation_title,
                "conversation_id": chatindex_json.get("conversation_id"),
                "participants": chatindex_json.get("participants"),
            },
            "entity_id": root_entity_id,
            "children": {t["node_id"]: extract_topic(t) for t in chatindex_json.get("topics", [])},
        }

        return tree_structure, entities


class GenericAdapter(BaseAdapter):
    def convert(self, generic_json: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        entities = {}

        def extract_value(value: Any, path: str = "") -> dict[str, Any]:
            if isinstance(value, dict):
                if "content" in value or "text" in value:
                    entity_id = f"entity_{path}".replace("/", "_")
                    entities[entity_id] = {
                        "type": value.get("type", "content"),
                        "content": value.get("content") or value.get("text"),
                    }
                    return {
                        "type": "leaf",
                        "summary": value.get("summary"),
                        "attrs": {k: v for k, v in value.items() if k not in _SKIP_KEYS},
                        "entity_id": entity_id,
                    }

                children_dict = value.get("children") or value.get("nodes")
                if children_dict:
                    children = {}
                    if isinstance(children_dict, dict):
                        children = {k: extract_value(v, f"{path}/{k}") for k, v in children_dict.items()}
                    elif isinstance(children_dict, list):
                        children = {str(i): extract_value(v, f"{path}/{i}") for i, v in enumerate(children_dict)}

                    return {
                        "type": "object",
                        "summary": value.get("summary"),
                        "attrs": {k: v for k, v in value.items() if k not in _SKIP_KEYS},
                        "children": children,
                    }

            return {"type": "leaf"}

        tree_structure = extract_value(generic_json, "root")
        return tree_structure, entities
