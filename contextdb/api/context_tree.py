import asyncio
import inspect
import json
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Callable, Optional

from contextdb.adapter.base import ChatIndexAdapter, DocumentTreeAdapter, GenericAdapter
from contextdb.api._shared import (
    build_context_tree_block_retriever,
    namespace_entities,
    resolve_context_tree_query,
)
from contextdb.core.storage import StorageProtocol, TreeDB
from contextdb.llm import LLMProtocol
from contextdb.retriever import (
    BaseRetriever,
    BeamRetriever,
    BlockRetrievalResult,
    BlockRetriever,
    LegacyBlockRetriever,
    ManualRetriever,
    RetrievalResult,
    TreeFormatter,
)

TreeBuilder = Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]]


class ContextTree:
    def __init__(self, db_path: str = "context.sqlite", storage: StorageProtocol = None, llm: LLMProtocol = None):
        self.storage = storage or TreeDB(db_path)
        self.llm = llm
        self.formatter = TreeFormatter(self.storage)
        document_adapter = DocumentTreeAdapter()
        self.adapters = {
            "document": document_adapter,
            "pageindex": document_adapter,
            "chatindex": ChatIndexAdapter(),
            "generic": GenericAdapter(),
        }

    def index_markdown_file(self, md_path: str, *, tree_builder: Optional[TreeBuilder] = None, **builder_kwargs) -> str:
        return self._index_source_file(
            md_path,
            source_type="markdown",
            tree_builder=tree_builder,
            builder_kwargs=builder_kwargs,
        )

    def index_pdf_file(self, pdf_path: str, *, tree_builder: Optional[TreeBuilder] = None, **builder_kwargs) -> str:
        return self._index_source_file(
            pdf_path,
            source_type="pdf",
            tree_builder=tree_builder,
            builder_kwargs=builder_kwargs,
        )

    def _index_source_file(
        self,
        file_path: str,
        *,
        source_type: str,
        tree_builder: Optional[TreeBuilder],
        builder_kwargs: Optional[dict[str, Any]] = None,
    ) -> str:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        document_tree = self._build_document_tree(
            path,
            source_type=source_type,
            tree_builder=tree_builder,
            builder_kwargs=builder_kwargs or {},
        )
        return self.index_document_tree(document_tree)

    def _build_document_tree(
        self,
        path: Path,
        *,
        source_type: str,
        tree_builder: Optional[TreeBuilder],
        builder_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        if tree_builder is None:
            raise ValueError(
                f"No {source_type} tree_builder provided. "
                "ConDB does not import pageindex directly. "
                "Generate a pageindex-compatible tree externally and call index_document_tree()/index_pageindex(), "
                "or pass tree_builder=..."
            )

        result = tree_builder(str(path), **builder_kwargs)
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                result = asyncio.run(result)
            else:
                raise RuntimeError(
                    "Cannot run an async tree_builder inside an active event loop. "
                    "Await it yourself and pass the resulting tree to index_document_tree()."
                )

        if not isinstance(result, dict):
            raise TypeError(f"{source_type} tree_builder must return a dict, got {type(result).__name__}")
        return result

    def index_document_tree(self, data: dict[str, Any]) -> str:
        tree, entities = self.adapters["document"].convert(data)
        tree, entities = namespace_entities(tree, entities)
        return self.storage.ingest_tree(tree, entities=entities)

    def index_pageindex(self, data: dict[str, Any]) -> str:
        return self.index_document_tree(data)

    def index_chatindex(self, data: dict[str, Any]) -> str:
        tree, entities = self.adapters["chatindex"].convert(data)
        tree, entities = namespace_entities(tree, entities)
        return self.storage.ingest_tree(tree, entities=entities)

    def index_generic(self, data: dict[str, Any], adapter: str = "generic") -> str:
        adapter_instance = self.adapters.get(adapter, self.adapters["generic"])
        tree, entities = adapter_instance.convert(data)
        tree, entities = namespace_entities(tree, entities)
        return self.storage.ingest_tree(tree, entities=entities)

    def query(
        self,
        tree_id: str,
        question: str,
        retriever: BaseRetriever = None,
        use_block_retriever: bool = False,
        use_legacy_block_retriever: bool = False,
        **kwargs,
    ) -> RetrievalResult:
        """
        Query the tree with LLM-based retrieval.

        Args:
            tree_id: ID of the tree to query
            question: The question to answer
            retriever: Custom retriever instance (optional)
            use_block_retriever: Use BlockRetriever for large documents (default: False)
            use_legacy_block_retriever: Use LegacyBlockRetriever when use_block_retriever=True
            **kwargs: Additional arguments passed to retriever.retrieve()
                For BeamRetriever: beam_size, max_turns, select_k
                For BlockRetriever: beam_size, max_turns, select_k, max_tokens_per_block,
                    cache_enabled, max_parallel_blocks

        Returns:
            RetrievalResult or BlockRetrievalResult with selected nodes and contents
        """
        if not self.llm:
            raise ValueError("LLM client not provided")

        retriever, query_kwargs = resolve_context_tree_query(
            self.storage,
            self.llm,
            retriever=retriever,
            use_block_retriever=use_block_retriever,
            use_legacy_block_retriever=use_legacy_block_retriever,
            beam_cls=BeamRetriever,
            block_cls=BlockRetriever,
            legacy_block_cls=LegacyBlockRetriever,
            kwargs=kwargs,
        )
        return retriever.retrieve(tree_id, question, **query_kwargs)

    def query_with_blocks(
        self,
        tree_id: str,
        question: str,
        max_tokens_per_block: int = 16000,
        **kwargs,
    ) -> BlockRetrievalResult:
        if not self.llm:
            raise ValueError("LLM client not provided")

        kwargs.pop("view_depth", None)
        retriever = build_context_tree_block_retriever(
            self.storage,
            self.llm,
            max_tokens_per_block=max_tokens_per_block,
            cache_enabled=kwargs.pop("cache_enabled", True),
            max_parallel_blocks=kwargs.pop("max_parallel_blocks", None),
            block_cls=BlockRetriever,
        )
        return retriever.retrieve(tree_id, question, **kwargs)

    def query_with_legacy_blocks(
        self,
        tree_id: str,
        question: str,
        max_tokens_per_block: int = 16000,
        **kwargs,
    ) -> BlockRetrievalResult:
        if not self.llm:
            raise ValueError("LLM client not provided")

        kwargs.pop("view_depth", None)
        kwargs.pop("cache_enabled", None)
        kwargs.pop("max_parallel_blocks", None)
        retriever = build_context_tree_block_retriever(
            self.storage,
            self.llm,
            max_tokens_per_block=max_tokens_per_block,
            legacy=True,
            legacy_block_cls=LegacyBlockRetriever,
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
        return namespace_entities(tree, entities, namespace=namespace)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
