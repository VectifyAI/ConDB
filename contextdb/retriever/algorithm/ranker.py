"""Rankers for filesystem retrieval candidates."""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Any


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
