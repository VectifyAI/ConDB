import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from contextdb.adapter.base import ChatIndexAdapter, GenericAdapter, PageIndexAdapter
from contextdb.core.storage import StorageProtocol, TreeDB
from contextdb.llm import LLMProtocol
from contextdb.retriever import (
    BaseRetriever,
    BeamRetriever,
    BlockRetriever,
    BlockRetrievalResult,
    ManualRetriever,
    RetrievalResult,
    TreeFormatter,
)


class ContextTree:
    def __init__(self, db_path: str = "context.sqlite", storage: StorageProtocol = None, llm: LLMProtocol = None):
        self.storage = storage or TreeDB(db_path)
        self.llm = llm
        self.formatter = TreeFormatter(self.storage)
        self.adapters = {"pageindex": PageIndexAdapter(), "chatindex": ChatIndexAdapter(), "generic": GenericAdapter()}

    def index_markdown_file(self, md_path: str) -> str:
        try:
            from pageindex import md_to_tree
        except ImportError as e:
            raise ImportError("Install pageindex: pip install pageindex") from e

        path = Path(md_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {md_path}")

        result = asyncio.run(md_to_tree(str(path)))
        return self.index_pageindex(result)

    def index_pdf_file(self, pdf_path: str) -> str:
        try:
            from pageindex import page_index_main
            from pageindex.utils import ConfigLoader
        except ImportError as e:
            raise ImportError("Install pageindex: pip install pageindex") from e

        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {pdf_path}")

        config = ConfigLoader().load(
            {
                "toc_check_page_num": 20,
                "max_page_num_each_node": 10,
                "max_token_num_each_node": 20000,
                "if_add_node_id": "yes",
                "if_add_node_summary": "yes",
                "if_add_doc_description": "no",
                "if_add_node_text": "no",
            }
        )
        result = page_index_main(str(path), config)
        return self.index_pageindex(result)

    def index_pageindex(self, data: dict[str, Any]) -> str:
        tree, entities = self.adapters["pageindex"].convert(data)
        tree, entities = self._namespace_entities(tree, entities)
        return self.storage.ingest_tree(tree, entities=entities)

    def index_chatindex(self, data: dict[str, Any]) -> str:
        tree, entities = self.adapters["chatindex"].convert(data)
        tree, entities = self._namespace_entities(tree, entities)
        return self.storage.ingest_tree(tree, entities=entities)

    def index_generic(self, data: dict[str, Any], adapter: str = "generic") -> str:
        adapter_instance = self.adapters.get(adapter, self.adapters["generic"])
        tree, entities = adapter_instance.convert(data)
        tree, entities = self._namespace_entities(tree, entities)
        return self.storage.ingest_tree(tree, entities=entities)

    def query(
        self,
        tree_id: str,
        question: str,
        retriever: BaseRetriever = None,
        use_block_retriever: bool = False,
        **kwargs,
    ) -> RetrievalResult:
        """
        Query the tree with LLM-based retrieval.

        Args:
            tree_id: ID of the tree to query
            question: The question to answer
            retriever: Custom retriever instance (optional)
            use_block_retriever: Use BlockRetriever for large documents (default: False)
            **kwargs: Additional arguments passed to retriever.retrieve()
                For BeamRetriever: beam_size, max_turns, select_k
                For BlockRetriever: beam_size, max_turns, select_k, max_tokens_per_block

        Returns:
            RetrievalResult or BlockRetrievalResult with selected nodes and contents
        """
        if not self.llm:
            raise ValueError("LLM client not provided")

        if retriever is None:
            if use_block_retriever:
                block_kwargs = {}
                for key in ["max_tokens_per_block"]:
                    if key in kwargs:
                        block_kwargs[key] = kwargs.pop(key)
                retriever = BlockRetriever(self.storage, self.llm, **block_kwargs)
            else:
                retriever = BeamRetriever(self.storage, self.llm)

        return retriever.retrieve(tree_id, question, **kwargs)

    def query_with_blocks(
        self,
        tree_id: str,
        question: str,
        max_tokens_per_block: int = 16000,
        **kwargs,
    ) -> BlockRetrievalResult:
        if not self.llm:
            raise ValueError("LLM client not provided")

        retriever = BlockRetriever(
            self.storage,
            self.llm,
            max_tokens_per_block=max_tokens_per_block,
        )
        return retriever.retrieve(tree_id, question, **kwargs)

    def query_manual(self, tree_id: str, query: str, actions: list[dict]) -> RetrievalResult:
        retriever = ManualRetriever(self.storage)
        return retriever.retrieve(tree_id, query, actions)

    def expand(self, tree_id: str, node_id: str, depth: int = 1) -> list[dict[str, Any]]:
        return self.storage.get_subtree(tree_id, node_id, max_depth=depth)

    def get_content(self, tree_id: str, node_id: str) -> Optional[dict[str, Any]]:
        entity = self.storage.get_entity(tree_id, node_id)
        if entity:
            return json.loads(entity.payload_json)
        return None

    def get_children(self, tree_id: str, node_id: str) -> list[dict[str, Any]]:
        nodes = self.storage.get_children(tree_id, node_id)
        return [n.to_dict() for n in nodes]

    def get_node(self, tree_id: str, node_id: str) -> dict[str, Any]:
        node = self.storage.get_node(tree_id, node_id)
        return node.to_dict() if node else {}

    def format_tree_view(self, tree_id: str, node_id: str = None, depth: int = 2) -> str:
        if node_id is None:
            node_id = self.storage.get_root_id(tree_id)
        if not node_id:
            return ""
        return self.formatter.format_view(tree_id, node_id, depth)

    def format_tree_json(self, tree_id: str, node_id: str = None, depth: int = 2) -> dict[str, Any]:
        if node_id is None:
            node_id = self.storage.get_root_id(tree_id)
        if not node_id:
            return {}
        return self.formatter.format_json(tree_id, node_id, depth)

    def close(self):
        self.storage.close()

    @staticmethod
    def _namespace_entities(
        tree: dict[str, Any], entities: Optional[dict[str, dict[str, Any]]], namespace: Optional[str] = None
    ):
        if not entities:
            return tree, entities

        ns = namespace or uuid.uuid4().hex
        mapping = {eid: f"{ns}:{eid}" for eid in entities.keys()}

        def remap(node: dict[str, Any]):
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
