"""Internal helpers shared by ConDB and ContextTree."""

import uuid
from typing import Any, Optional

from contextdb.retriever import BaseRetriever, BeamRetriever, BlockRetriever, LegacyBlockRetriever


def namespace_entities(
    tree: dict[str, Any],
    entities: Optional[dict[str, dict[str, Any]]],
    namespace: Optional[str] = None,
) -> tuple[dict[str, Any], Optional[dict[str, dict[str, Any]]]]:
    """Prefix entity ids so multiple trees can coexist in one storage backend."""
    if not entities:
        return tree, entities

    ns = namespace or uuid.uuid4().hex
    mapping = {entity_id: f"{ns}:{entity_id}" for entity_id in entities}

    def remap(node: dict[str, Any]) -> None:
        entity_id = node.get("entity_id")
        if entity_id in mapping:
            node["entity_id"] = mapping[entity_id]

        children = node.get("children")
        if isinstance(children, dict):
            for child in children.values():
                remap(child)
        elif isinstance(children, list):
            for child in children:
                remap(child)

    remap(tree)
    return tree, {mapping[entity_id]: payload for entity_id, payload in entities.items()}


def build_strategy_retriever(
    storage,
    llm,
    strategy: str,
    *,
    beam_cls=BeamRetriever,
    block_cls=BlockRetriever,
    max_tokens_per_block: int = 16000,
    cache_enabled: bool = True,
    max_parallel_blocks: int = None,
    mode: str = "document",
    ranker=None,
) -> BaseRetriever:
    """Create a retriever from the ConDB strategy selector."""
    if strategy == "beam":
        return beam_cls(storage, llm, mode=mode)
    if strategy == "block":
        return block_cls(
            storage,
            llm,
            max_tokens_per_block=max_tokens_per_block,
            cache_enabled=cache_enabled,
            max_parallel_blocks=max_parallel_blocks,
            mode=mode,
            ranker=ranker,
        )
    raise ValueError(f"Unknown strategy: {strategy!r}")


def resolve_context_tree_query(
    storage,
    llm,
    *,
    retriever: BaseRetriever = None,
    use_block_retriever: bool = False,
    use_legacy_block_retriever: bool = False,
    beam_cls=BeamRetriever,
    block_cls=BlockRetriever,
    legacy_block_cls=LegacyBlockRetriever,
    kwargs: dict[str, Any],
) -> tuple[BaseRetriever, dict[str, Any]]:
    """Normalize ContextTree query kwargs and return the concrete retriever."""
    query_kwargs = dict(kwargs)
    query_kwargs.pop("view_depth", None)

    if retriever is not None:
        return retriever, query_kwargs

    if not use_block_retriever:
        query_kwargs.pop("cache_enabled", None)
        query_kwargs.pop("max_parallel_blocks", None)
        return beam_cls(storage, llm), query_kwargs

    block_kwargs: dict[str, Any] = {}
    for key in ("max_tokens_per_block", "max_parallel_blocks"):
        if key in query_kwargs:
            block_kwargs[key] = query_kwargs.pop(key)

    if use_legacy_block_retriever:
        block_kwargs.pop("max_parallel_blocks", None)
        query_kwargs.pop("cache_enabled", None)
        return legacy_block_cls(storage, llm, **block_kwargs), query_kwargs

    if "cache_enabled" in query_kwargs:
        block_kwargs["cache_enabled"] = query_kwargs.pop("cache_enabled")
    else:
        query_kwargs.pop("cache_enabled", None)

    return block_cls(storage, llm, **block_kwargs), query_kwargs


def build_context_tree_block_retriever(
    storage,
    llm,
    *,
    max_tokens_per_block: int = 16000,
    cache_enabled: bool = True,
    max_parallel_blocks: int = None,
    legacy: bool = False,
    block_cls=BlockRetriever,
    legacy_block_cls=LegacyBlockRetriever,
):
    """Construct the explicit block retriever variants used by ContextTree."""
    if legacy:
        return legacy_block_cls(
            storage,
            llm,
            max_tokens_per_block=max_tokens_per_block,
        )
    return block_cls(
        storage,
        llm,
        max_tokens_per_block=max_tokens_per_block,
        cache_enabled=cache_enabled,
        max_parallel_blocks=max_parallel_blocks,
    )
