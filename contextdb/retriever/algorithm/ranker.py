"""Rankers for filesystem retrieval candidates."""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Any

from contextdb.retriever.algorithm.embeddings import (
    EmbeddingClient,
    LiteLLMEmbeddingClient,
    cosine_similarity,
)

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_path(path: str) -> str:
    return str(path or "").strip("/").replace("\\", "/").lower()


def tokenize_path_text(text: str) -> list[str]:
    if not text:
        return []
    split_text = _CAMEL_BOUNDARY_RE.sub(" ", str(text).replace("_", " ").replace("-", " "))
    tokens = _TOKEN_RE.findall(split_text.lower())
    expanded = list(tokens)
    for token in tokens:
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            expanded.append(token[:-1])
    return expanded


def path_matches_query(path: str, query: str, *, is_dir: bool = False) -> bool:
    rel_path = normalize_path(path)
    if not rel_path:
        return False

    query_text = str(query or "").replace("\\", "/").lower()
    if is_dir:
        return f"{rel_path.rstrip('/')}/" in query_text
    return "/" in rel_path and rel_path in query_text


def has_path_evidence(candidates: list[dict[str, Any]], query: str) -> bool:
    query_tokens = set(tokenize_path_text(query))
    if not query_tokens:
        return False

    return any(
        query_tokens.intersection(tokenize_path_text(_candidate_path(candidate)))
        for candidate in candidates
    )


def _candidate_is_dir(candidate: dict[str, Any]) -> bool:
    if "is_dir" in candidate:
        return bool(candidate["is_dir"])
    return not bool(candidate.get("is_leaf", False))


def _candidate_path(candidate: dict[str, Any]) -> str:
    return str(candidate.get("rel_path") or candidate.get("path") or "").strip("/")


class Ranker(ABC):
    def should_rank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> bool:
        return bool(candidates)

    @abstractmethod
    def rank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> list[tuple[dict[str, Any], float]]:
        """Return [(candidate, score), ...]. Higher = higher priority."""


class BM25PathRanker(Ranker):
    """Path-aware BM25 over filesystem candidates."""

    def __init__(
        self,
        *,
        k1: float = 1.2,
        b: float = 0.75,
        basename_weight: float = 3.0,
        parent_weight: float = 1.5,
        full_path_weight: float = 1.0,
    ) -> None:
        self.k1 = k1
        self.b = b
        self.field_weights = {
            "basename": basename_weight,
            "parent": parent_weight,
            "full_path": full_path_weight,
        }

    def should_rank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> bool:
        return has_path_evidence(candidates, query)

    def rank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> list[tuple[dict[str, Any], float]]:
        if not candidates:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return [(c, 0.0) for c in candidates]

        docs = [self._candidate_fields(c) for c in candidates]
        scores = self._fielded_bm25_scores(query_tokens, docs)
        return [
            (candidate, score + self._prior_score(candidate, query))
            for candidate, score in zip(candidates, scores)
        ]

    def _fielded_bm25_scores(
        self,
        query_tokens: list[str],
        docs: list[dict[str, list[str]]],
    ) -> list[float]:
        scores = [0.0 for _ in docs]
        for field_name, weight in self.field_weights.items():
            if weight <= 0:
                continue
            field_docs = [doc.get(field_name, []) for doc in docs]
            field_scores = self._bm25_scores(query_tokens, field_docs)
            for idx, score in enumerate(field_scores):
                scores[idx] += weight * score
        return scores

    def _bm25_scores(self, query_tokens: list[str], docs: list[list[str]]) -> list[float]:
        n_docs = len(docs)
        avgdl = sum(len(d) for d in docs) / n_docs if n_docs else 0.0
        if avgdl <= 0:
            return [0.0 for _ in docs]

        dfs: Counter[str] = Counter()
        for doc in docs:
            dfs.update(set(doc))

        query_counts = Counter(query_tokens)
        scores: list[float] = []
        for doc in docs:
            tf = Counter(doc)
            dl = len(doc)
            score = 0.0
            for token, qf in query_counts.items():
                freq = tf.get(token, 0)
                if freq <= 0:
                    continue
                df = dfs[token]
                idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
                denom = freq + self.k1 * (1.0 - self.b + self.b * dl / avgdl)
                score += qf * idf * (freq * (self.k1 + 1.0) / denom)
            scores.append(score)
        return scores

    def _candidate_fields(self, candidate: dict[str, Any]) -> dict[str, list[str]]:
        rel_path = _candidate_path(candidate)
        basename = rel_path.rsplit("/", 1)[-1] if rel_path else ""
        parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        return {
            "basename": self._tokenize(basename),
            "parent": self._tokenize(parent),
            "full_path": self._tokenize(rel_path),
        }

    def _prior_score(self, candidate: dict[str, Any], query: str = "") -> float:
        rel_path = str(candidate.get("rel_path") or candidate.get("path") or "").strip("/")
        is_dir = self._is_dir(candidate)
        return 20.0 if path_matches_query(rel_path, query, is_dir=is_dir) else 0.0

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        return tokenize_path_text(text)

    @staticmethod
    def _is_dir(candidate: dict[str, Any]) -> bool:
        return _candidate_is_dir(candidate)


class VectorPathRanker(Ranker):
    """Embedding-based path ranker over the current filesystem candidates."""

    def __init__(
        self,
        embedding_client: EmbeddingClient,
        *,
        basename_weight: float = 3.0,
        parent_weight: float = 1.5,
        full_path_weight: float = 1.0,
        exact_path_boost: float = 20.0,
        cache_embeddings: bool = True,
    ) -> None:
        self.embedding_client = embedding_client
        self.field_weights = {
            "basename": basename_weight,
            "parent": parent_weight,
            "full_path": full_path_weight,
        }
        self.exact_path_boost = exact_path_boost
        self.cache_embeddings = cache_embeddings
        self._embedding_cache: dict[str, list[float]] = {}

    def should_rank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> bool:
        return bool(tokenize_path_text(query)) and any(_candidate_path(candidate) for candidate in candidates)

    def rank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> list[tuple[dict[str, Any], float]]:
        if not candidates:
            return []

        query_vector = self._embed_one(self._query_text(query))
        field_texts_by_candidate = [self._candidate_field_texts(candidate) for candidate in candidates]
        all_field_texts = [
            fields[field_name]
            for fields in field_texts_by_candidate
            for field_name in self.field_weights
            if fields[field_name]
        ]
        self._embed_texts(all_field_texts)

        ranked: list[tuple[dict[str, Any], float]] = []
        for candidate, fields in zip(candidates, field_texts_by_candidate):
            score = 0.0
            for field_name, weight in self.field_weights.items():
                if weight <= 0:
                    continue
                score += weight * cosine_similarity(query_vector, self._embed_one(fields[field_name]))
            score += self._prior_score(candidate, query)
            ranked.append((candidate, score))
        return ranked

    def _embed_one(self, text: str) -> list[float]:
        return self._embed_texts([text])[0] if text else []

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        normalized = [self._normalize_embedding_text(text) for text in texts]
        if not normalized:
            return []

        if not self.cache_embeddings:
            non_empty = [text for text in normalized if text]
            embedded = self.embedding_client.embed(non_empty) if non_empty else []
            vectors_by_text: dict[str, list[float]] = {}
            for text, vector in zip(non_empty, embedded):
                vectors_by_text[text] = vector
            return [vectors_by_text.get(text, []) for text in normalized]

        missing: list[str] = []
        seen_missing: set[str] = set()
        for text in normalized:
            if not text:
                continue
            if text in self._embedding_cache:
                continue
            if text in seen_missing:
                continue
            seen_missing.add(text)
            missing.append(text)

        if missing:
            embedded = self.embedding_client.embed(missing)
            if len(embedded) != len(missing):
                raise ValueError(
                    f"Embedding client returned {len(embedded)} vectors for {len(missing)} texts"
                )
            for text, vector in zip(missing, embedded):
                self._embedding_cache[text] = vector

        vectors: list[list[float]] = []
        for text in normalized:
            if not text:
                vectors.append([])
            else:
                vectors.append(self._embedding_cache[text])
        return vectors

    def _candidate_field_texts(self, candidate: dict[str, Any]) -> dict[str, str]:
        rel_path = _candidate_path(candidate)
        basename = rel_path.rsplit("/", 1)[-1] if rel_path else ""
        parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        return {
            "basename": self._path_text(basename),
            "parent": self._path_text(parent),
            "full_path": self._path_text(rel_path),
        }

    def _prior_score(self, candidate: dict[str, Any], query: str = "") -> float:
        rel_path = _candidate_path(candidate)
        return (
            self.exact_path_boost
            if path_matches_query(rel_path, query, is_dir=_candidate_is_dir(candidate))
            else 0.0
        )

    @staticmethod
    def _query_text(query: str) -> str:
        return str(query or "").strip()

    @staticmethod
    def _path_text(path: str) -> str:
        return " ".join(tokenize_path_text(path))

    @staticmethod
    def _normalize_embedding_text(text: str) -> str:
        return " ".join(str(text or "").strip().split())


def make_ranker(
    ranker: str | Ranker | None,
    *,
    embedding_client: EmbeddingClient | None = None,
    embedding_provider: str = "openai",
    embedding_model: str | None = None,
    embedding_api_key: str | None = None,
) -> Ranker | None:
    if ranker is None or ranker == "none":
        return None
    if isinstance(ranker, Ranker):
        return ranker
    if not isinstance(ranker, str):
        if hasattr(ranker, "rank"):
            return ranker
        raise TypeError(f"Unsupported ranker: {ranker!r}")

    name = ranker.lower()
    if name == "bm25":
        return BM25PathRanker()
    if name == "vector":
        client = embedding_client or LiteLLMEmbeddingClient(
            provider=embedding_provider,
            model=embedding_model,
            api_key=embedding_api_key,
        )
        return VectorPathRanker(client)
    raise ValueError(f"Unknown ranker: {ranker!r}")
