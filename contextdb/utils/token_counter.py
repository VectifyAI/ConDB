"""Token counting utilities for Block-level Beam Search."""

import json
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class TokenizerProtocol(Protocol):
    """Protocol for token counting."""

    def count_tokens(self, text: str) -> int: ...


@dataclass
class NodeTokenInfo:
    """Token count information for a single node."""

    node_id: str
    depth: int
    token_count: int
    children_token_count: int = 0  # Sum of all descendants' tokens


@dataclass
class SubtreeTokenInfo:
    """Aggregated token info for a subtree."""

    root_node_id: str
    total_tokens: int
    node_count: int
    max_depth: int
    token_by_depth: dict[int, int]  # depth -> total tokens at that depth


@dataclass
class TokenEstimateConfig:
    """Configuration for token estimation."""

    tokens_per_char: float = 0.25
    candidate_overhead_tokens: int = 50
    prompt_overhead_tokens: int = 200
    response_reserved_tokens: int = 500


class TiktokenCounter:
    """Tiktoken-based counter (OpenAI models)."""

    def __init__(self, encoding: str = "cl100k_base"):
        try:
            import tiktoken

            self.encoding = tiktoken.get_encoding(encoding)
        except ImportError:
            raise ImportError("tiktoken required: pip install tiktoken")

    def count_tokens(self, text: str) -> int:
        return len(self.encoding.encode(text))


class AnthropicTokenCounter:
    """Anthropic token counter. Uses cl100k_base when available, else char estimation."""

    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        self._counter = None
        try:
            import tiktoken
            self._counter = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            pass

    def count_tokens(self, text: str) -> int:
        if self._counter:
            return len(self._counter.encode(text))
        return int(len(text) * 0.28)


class CharEstimateCounter:
    """Character-based token estimation fallback."""

    def __init__(self, tokens_per_char: float = 0.25):
        self.tokens_per_char = tokens_per_char

    def count_tokens(self, text: str) -> int:
        return int(len(text) * self.tokens_per_char)


def make_tokenizer(provider: str = None, model: str = None) -> TokenizerProtocol:
    """Create a tokenizer for the given provider."""
    if provider == "openai":
        encoding = "o200k_base" if model and "4o" in model else "cl100k_base"
        try:
            return TiktokenCounter(encoding)
        except ImportError:
            pass

    if provider == "anthropic":
        try:
            return AnthropicTokenCounter(model or "claude-sonnet-4-20250514")
        except ImportError:
            pass

    return CharEstimateCounter()


class TokenCounter:
    """Token counter for nodes and subtrees."""

    def __init__(
        self,
        tokenizer: Optional[TokenizerProtocol] = None,
        config: Optional[TokenEstimateConfig] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.tokenizer = tokenizer or (make_tokenizer(provider, model) if provider else None)
        self.config = config or TokenEstimateConfig()
        self._cache: dict[str, int] = {}

    def count_text_tokens(self, text: str) -> int:
        """Count tokens for a text string."""
        if not text:
            return 0

        if self.tokenizer:
            return self.tokenizer.count_tokens(text)

        # Fast estimation: chars * tokens_per_char
        return int(len(text) * self.config.tokens_per_char)

    def count_node_tokens(self, node: dict[str, Any]) -> int:
        """Count tokens for a single node's content."""
        node_id = node.get("node_id")
        if node_id and node_id in self._cache:
            return self._cache[node_id]

        # Build candidate-like text representation
        text_parts = []

        # Handle attrs - could be dict or JSON string
        attrs = node.get("attrs")
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except json.JSONDecodeError:
                attrs = {}
        elif attrs is None:
            attrs_json = node.get("attrs_json")
            if attrs_json:
                try:
                    attrs = json.loads(attrs_json)
                except json.JSONDecodeError:
                    attrs = {}
            else:
                attrs = {}

        if attrs.get("title"):
            text_parts.append(f"title: {attrs['title']}")
        if attrs.get("summary"):
            text_parts.append(f"summary: {attrs['summary']}")

        # Add text preview (first 200 chars, matching current behavior)
        entity = node.get("entity", {})
        if isinstance(entity, dict):
            payload = entity.get("payload", {})
            if isinstance(payload, dict):
                entity_text = payload.get("text") or payload.get("content") or ""
            else:
                entity_text = ""
        else:
            entity_text = ""

        if entity_text:
            text_parts.append(f"text: {entity_text[:200]}")

        combined_text = "\n".join(text_parts)

        if self.tokenizer:
            token_count = self.tokenizer.count_tokens(combined_text)
        else:
            # Fast estimation: chars * tokens_per_char + overhead
            token_count = int(len(combined_text) * self.config.tokens_per_char + self.config.candidate_overhead_tokens)

        # Cache the result
        if node_id:
            self._cache[node_id] = token_count

        return token_count

    def count_subtree_tokens(
        self, storage, tree_id: str, node_id: str, max_depth: int = 100
    ) -> SubtreeTokenInfo:
        """Count total tokens for a subtree."""
        subtree = storage.get_subtree(tree_id, node_id, max_depth, with_entities=True)

        token_by_depth: dict[int, int] = {}
        total_tokens = 0
        max_seen_depth = 0

        for node in subtree:
            node_tokens = self.count_node_tokens(node)
            depth = node.get("depth", 0)

            token_by_depth[depth] = token_by_depth.get(depth, 0) + node_tokens
            total_tokens += node_tokens
            max_seen_depth = max(max_seen_depth, depth)

        return SubtreeTokenInfo(
            root_node_id=node_id,
            total_tokens=total_tokens,
            node_count=len(subtree),
            max_depth=max_seen_depth,
            token_by_depth=token_by_depth,
        )

    def get_cached_count(self, node_id: str) -> Optional[int]:
        """Get previously computed token count for a node."""
        return self._cache.get(node_id)

    def estimate_prompt_tokens(self, query: str, num_candidates: int, avg_candidate_tokens: int = 100) -> int:
        """Estimate total prompt tokens for a beam search step."""
        query_tokens = self.count_text_tokens(query)

        return self.config.prompt_overhead_tokens + query_tokens + num_candidates * avg_candidate_tokens

    def clear_cache(self):
        """Clear token count cache."""
        self._cache.clear()

    def precompute_tree_tokens(self, storage, tree_id: str) -> dict[str, int]:
        """Precompute token counts for all nodes in a tree."""
        root_id = storage.get_root_id(tree_id)
        if not root_id:
            return {}

        subtree = storage.get_subtree(tree_id, root_id, max_depth=1000, with_entities=True)

        for node in subtree:
            self.count_node_tokens(node)

        return dict(self._cache)
