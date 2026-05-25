"""Embedding adapters used by retrieval rankers."""

from __future__ import annotations

import math
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
        ...


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    norm_a = math.sqrt(sum(v * v for v in a[:n]))
    norm_b = math.sqrt(sum(v * v for v in b[:n]))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _resolve_embedding_model(provider: str, model: str | None) -> str:
    model = model or "text-embedding-3-small"
    if "/" in model:
        return model
    return f"{provider}/{model}"


class LiteLLMEmbeddingClient:
    """Thin adapter around litellm.embedding."""

    def __init__(
        self,
        *,
        provider: str = "openai",
        model: str | None = None,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        import litellm

        self._litellm = litellm
        self.model = _resolve_embedding_model(provider, model)
        self.api_key = api_key
        self.kwargs = kwargs
        litellm.suppress_debug_info = True

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        kwargs = {"model": self.model, "input": texts, **self.kwargs}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        response = self._litellm.embedding(**kwargs)
        rows = list(getattr(response, "data", None) or response["data"])
        rows.sort(key=lambda row: _row_value(row, "index", 0))
        return [list(_row_value(row, "embedding", [])) for row in rows]


def _row_value(row: Any, key: str, default: Any) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)
