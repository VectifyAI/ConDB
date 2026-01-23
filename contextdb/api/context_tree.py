import asyncio
import json
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional
from contextdb.core.storage import StorageProtocol, TreeDB
from contextdb.adapter.base import PageIndexAdapter, ChatIndexAdapter, GenericAdapter
from contextdb.retriever import BeamRetriever, ManualRetriever, RetrievalResult, TreeFormatter, BaseRetriever
from contextdb.llm import LLMProtocol, LLMClient


class ContextTree:
    def __init__(self, db_path: str = "context.sqlite", storage: StorageProtocol = None, llm: LLMProtocol = None):
        self.storage = storage or TreeDB(db_path)
        self.llm = llm
        self.formatter = TreeFormatter(self.storage)
        self.adapters = {
            "pageindex": PageIndexAdapter(),
            "chatindex": ChatIndexAdapter(),
            "generic": GenericAdapter()
        }

    def index_markdown_file(self, md_path: str) -> str:
        try:
            from pageindex import md_to_tree
        except ImportError:
            raise ImportError("Install pageindex: pip install pageindex")

        path = Path(md_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {md_path}")

        result = asyncio.run(md_to_tree(str(path)))
        return self.index_pageindex(result)

    def index_pdf_file(self, pdf_path: str) -> str:
        try:
            from pageindex import page_index_main
            from pageindex.utils import ConfigLoader
        except ImportError:
            raise ImportError("Install pageindex: pip install pageindex")

        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {pdf_path}")

        config = ConfigLoader().load({
            "toc_check_page_num": 20,
            "max_page_num_each_node": 10,
            "max_token_num_each_node": 20000,
            "if_add_node_id": "yes",
            "if_add_node_summary": "yes",
            "if_add_doc_description": "no",
            "if_add_node_text": "no"
        })
        result = page_index_main(str(path), config)
        return self.index_pageindex(result)

    def index_pageindex(self, data: Dict[str, Any]) -> str:
        tree, entities = self.adapters["pageindex"].convert(data)
        tree, entities = self._namespace_entities(tree, entities)
        return self.storage.ingest_tree(tree, entities=entities)

    def index_chatindex(self, data: Dict[str, Any]) -> str:
        tree, entities = self.adapters["chatindex"].convert(data)
        tree, entities = self._namespace_entities(tree, entities)
        return self.storage.ingest_tree(tree, entities=entities)

    def index_generic(self, data: Dict[str, Any], adapter: str = "generic") -> str:
        adapter = self.adapters.get(adapter, self.adapters["generic"])
        tree, entities = adapter.convert(data)
        tree, entities = self._namespace_entities(tree, entities)
        return self.storage.ingest_tree(tree, entities=entities)

    def query(self, tree_id: str, question: str, retriever: BaseRetriever = None, **kwargs) -> RetrievalResult:
        if not self.llm:
            raise ValueError("LLM client not provided")
        if retriever is None:
            retriever = BeamRetriever(self.storage, self.llm)
        return retriever.retrieve(tree_id, question, **kwargs)

    def query_manual(self, tree_id: str, query: str, actions: List[Dict]) -> RetrievalResult:
        retriever = ManualRetriever(self.storage)
        return retriever.retrieve(tree_id, query, actions)

    def expand(self, tree_id: str, node_id: str, depth: int = 1) -> List[Dict[str, Any]]:
        return self.storage.get_subtree(tree_id, node_id, max_depth=depth)

    def get_content(self, tree_id: str, node_id: str) -> Optional[Dict[str, Any]]:
        entity = self.storage.get_entity(tree_id, node_id)
        if entity:
            return json.loads(entity.payload_json)
        return None

    def get_children(self, tree_id: str, node_id: str) -> List[Dict[str, Any]]:
        nodes = self.storage.get_children(tree_id, node_id)
        return [n.to_dict() for n in nodes]

    def get_node(self, tree_id: str, node_id: str) -> Dict[str, Any]:
        node = self.storage.get_node(tree_id, node_id)
        return node.to_dict() if node else {}

    def format_tree_view(self, tree_id: str, node_id: str = None, depth: int = 2) -> str:
        if node_id is None:
            node_id = self.storage.get_root_id(tree_id)
        if not node_id:
            return ""
        return self.formatter.format_view(tree_id, node_id, depth)

    def format_tree_json(self, tree_id: str, node_id: str = None, depth: int = 2) -> Dict[str, Any]:
        if node_id is None:
            node_id = self.storage.get_root_id(tree_id)
        if not node_id:
            return {}
        return self.formatter.format_json(tree_id, node_id, depth)

    def close(self):
        self.storage.close()

    @staticmethod
    def _namespace_entities(tree: Dict[str, Any],
                            entities: Optional[Dict[str, Dict[str, Any]]],
                            namespace: Optional[str] = None):
        if not entities:
            return tree, entities

        ns = namespace or uuid.uuid4().hex
        mapping = {eid: f"{ns}:{eid}" for eid in entities.keys()}

        def remap(node: Dict[str, Any]):
            eid = node.get("entity_id")
            if eid in mapping:
                node["entity_id"] = mapping[eid]
            children = node.get("children")
            if isinstance(children, dict):
                for child in children.values():
                    remap(child)
            elif isinstance(children, list):
                for child in children:
                    remap(child)

        remap(tree)
        new_entities = {mapping[eid]: payload for eid, payload in entities.items()}
        return tree, new_entities

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
