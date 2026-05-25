"""ConDB public API — store hierarchical JSON, query with LLM."""

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from contextdb.adapter.base import ChatIndexAdapter, DocumentTreeAdapter, GenericAdapter
from contextdb.api._shared import build_strategy_retriever, namespace_entities
from contextdb.core.storage import TreeDB
from contextdb.llm import LLMClient, LLMProtocol
from contextdb.retriever import (
    BaseRetriever,
    BeamRetriever,
    BlockRetriever,
    TreeFormatter,
)
from contextdb.retriever.algorithm.ranker import Ranker, make_ranker

# ── Errors ──────────────────────────────────────────────────────────

class ConDBError(Exception):
    """Base exception."""


class TreeNotFoundError(ConDBError):
    """Tree ID does not exist."""


class LLMNotConfiguredError(ConDBError):
    """query() called without LLM."""


# ── Result ──────────────────────────────────────────────────────────

@dataclass
class QueryResult:
    node_ids: list[str]
    contents: list[dict[str, Any]]
    turns: int
    llm_calls: int = 0
    cache_hits: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)


# ── Adapters ────────────────────────────────────────────────────────

_DOCUMENT_ADAPTER = DocumentTreeAdapter()

_ADAPTERS = {
    "document": _DOCUMENT_ADAPTER,
    "pageindex": _DOCUMENT_ADAPTER,
    "chatindex": ChatIndexAdapter(),
    "generic": GenericAdapter(),
}


# ── ConDB ───────────────────────────────────────────────────────────

class ConDB:
    """Main entry point. Storage + query over hierarchical documents."""

    def __init__(self, db_path: str = "contextdb.sqlite"):
        self.storage = TreeDB(db_path)
        self._llm: Optional[LLMProtocol] = None
        self._formatter = TreeFormatter(self.storage)

    # ── Store ───────────────────────────────────────────────────────

    def store(self, data: dict, *, format: str = "pageindex", meta: dict = None) -> str:
        adapter = _ADAPTERS.get(format)
        if adapter is None:
            raise ValueError(f"Unknown format: {format!r}. Use: {list(_ADAPTERS)}")
        tree, entities = adapter.convert(data)
        tree, entities = namespace_entities(tree, entities)
        return self.storage.ingest_tree(tree, entities=entities, meta=meta)

    # ── LLM ─────────────────────────────────────────────────────────

    def set_llm(self, *, provider: str = "anthropic", api_key: str = None,
                model: str = None, llm: LLMProtocol = None):
        self._llm = llm or LLMClient(provider=provider, api_key=api_key, model=model)

    def _resolve_llm(self, llm=None, provider=None, api_key=None, model=None) -> LLMProtocol:
        """per-query llm= > per-query provider= > set_llm() default."""
        if llm is not None:
            return llm
        if provider is not None:
            return LLMClient(provider=provider, api_key=api_key, model=model)
        if self._llm is not None:
            return self._llm
        raise LLMNotConfiguredError("No LLM configured. Call set_llm() or pass provider=/llm= to query().")

    # ── Query ───────────────────────────────────────────────────────

    def query(
        self,
        tree_id: str,
        question: str,
        *,
        llm: LLMProtocol = None,
        provider: str = None,
        api_key: str = None,
        model: str = None,
        strategy: str = "auto",
        beam_size: int = 3,
        max_turns: int = None,
        select_k: int = 1,
        max_tokens_per_block: int = 16000,
        cache_enabled: bool = True,
        max_parallel_blocks: int = None,
        ranker: str | Ranker | None = None,
        embedding_provider: str = "openai",
        embedding_model: str = None,
        embedding_api_key: str = None,
        retriever: BaseRetriever = None,
    ) -> QueryResult:
        self._check_tree(tree_id)
        resolved_llm = self._resolve_llm(llm, provider, api_key, model)

        if retriever is None:
            retriever = self._make_retriever(
                tree_id, resolved_llm, strategy,
                max_tokens_per_block=max_tokens_per_block,
                cache_enabled=cache_enabled,
                max_parallel_blocks=max_parallel_blocks,
                ranker=ranker,
                embedding_provider=embedding_provider,
                embedding_model=embedding_model,
                embedding_api_key=embedding_api_key,
            )

        result = retriever.retrieve(tree_id, question,
                                    beam_size=beam_size, max_turns=max_turns, select_k=select_k)

        return QueryResult(
            node_ids=result.nodes,
            contents=result.contents,
            turns=result.turns,
            llm_calls=getattr(result, "total_llm_calls", result.turns),
            cache_hits=getattr(result, "cache_hits", 0),
            trace=result.trace,
        )

    def _make_retriever(self, tree_id, llm, strategy, **kwargs) -> BaseRetriever:
        if strategy == "auto":
            strategy = self._pick_strategy(tree_id)
        mode = self._tree_mode(tree_id)
        ranker = (
            make_ranker(
                kwargs.get("ranker"),
                embedding_provider=kwargs.get("embedding_provider", "openai"),
                embedding_model=kwargs.get("embedding_model"),
                embedding_api_key=kwargs.get("embedding_api_key"),
            )
            if strategy == "block"
            else None
        )
        return build_strategy_retriever(
            self.storage,
            llm,
            strategy,
            beam_cls=BeamRetriever,
            block_cls=BlockRetriever,
            max_tokens_per_block=kwargs.get("max_tokens_per_block", 16000),
            cache_enabled=kwargs.get("cache_enabled", True),
            max_parallel_blocks=kwargs.get("max_parallel_blocks"),
            mode=mode,
            ranker=ranker,
        )

    def _tree_mode(self, tree_id: str) -> str:
        cur = self.storage.conn.cursor()
        cur.execute("SELECT meta_json FROM trees WHERE tree_id = ?", (tree_id,))
        row = cur.fetchone()
        if not row or not row["meta_json"]:
            return "document"
        try:
            meta = json.loads(row["meta_json"])
        except json.JSONDecodeError:
            return "document"
        return "filesystem" if meta.get("source") == "filesystem" else "document"

    def _pick_strategy(self, tree_id: str) -> str:
        cur = self.storage.conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM nodes WHERE tree_id = ?", (tree_id,))
        count = cur.fetchone()["c"]
        if count <= 50:
            return "beam"
        return "block"

    # ── Navigate ────────────────────────────────────────────────────

    def tree_info(self, tree_id: str) -> dict[str, Any]:
        self._check_tree(tree_id)
        cur = self.storage.conn.cursor()
        cur.execute("SELECT * FROM trees WHERE tree_id = ?", (tree_id,))
        tree = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS c, MAX(depth) AS d FROM nodes WHERE tree_id = ?", (tree_id,))
        stats = cur.fetchone()
        return {
            "tree_id": tree_id,
            "root_node_id": tree["root_node_id"],
            "node_count": stats["c"],
            "max_depth": stats["d"] or 0,
            "meta": json.loads(tree["meta_json"]) if tree["meta_json"] else None,
            "created_at": tree["created_at"],
        }

    def get_content(self, tree_id: str, node_id: str) -> Optional[dict[str, Any]]:
        entity = self.storage.get_entity(tree_id, node_id)
        return json.loads(entity.payload_json) if entity else None

    def get_children(self, tree_id: str, node_id: str) -> list[dict[str, Any]]:
        return [n.to_dict() for n in self.storage.get_children(tree_id, node_id)]

    def get_subtree(self, tree_id: str, node_id: str, depth: int = 2) -> list[dict[str, Any]]:
        return self.storage.get_subtree(tree_id, node_id, max_depth=depth)

    def print_tree(self, tree_id: str, depth: int = 3) -> str:
        root_id = self.storage.get_root_id(tree_id)
        return self._formatter.format_view(tree_id, root_id, depth) if root_id else ""

    # ── Manage ──────────────────────────────────────────────────────

    def list_trees(self) -> list[dict[str, Any]]:
        cur = self.storage.conn.cursor()
        cur.execute("""
            SELECT t.tree_id, t.meta_json, t.created_at, COUNT(n.node_id) AS node_count
            FROM trees t LEFT JOIN nodes n ON t.tree_id = n.tree_id
            GROUP BY t.tree_id ORDER BY t.created_at DESC
        """)
        return [
            {
                "tree_id": r["tree_id"],
                "meta": json.loads(r["meta_json"]) if r["meta_json"] else None,
                "node_count": r["node_count"],
                "created_at": r["created_at"],
            }
            for r in cur.fetchall()
        ]

    def has_tree(self, tree_id: str) -> bool:
        cur = self.storage.conn.cursor()
        cur.execute("SELECT 1 FROM trees WHERE tree_id = ?", (tree_id,))
        return cur.fetchone() is not None

    def delete_tree(self, tree_id: str):
        self.storage.delete_tree(tree_id)

    # ── Lifecycle ───────────────────────────────────────────────────

    def close(self):
        self.storage.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Internal ────────────────────────────────────────────────────

    def _check_tree(self, tree_id: str):
        if not self.has_tree(tree_id):
            raise TreeNotFoundError(f"Tree not found: {tree_id}")

def _namespace_entities(tree, entities):
    """Backward-compatible wrapper around the shared namespacing helper."""
    return namespace_entities(tree, entities)


# ── Module-level convenience ────────────────────────────────────────

def open(db_path: str = "contextdb.sqlite") -> ConDB:
    return ConDB(db_path)
