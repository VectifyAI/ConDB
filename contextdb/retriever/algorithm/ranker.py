"""Rankers for filesystem retrieval candidates."""

from __future__ import annotations

import json
import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Any


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
    """BM25 over file paths, with optional subtree path tokens for directories."""

    _CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
    _TOKEN_RE = re.compile(r"[a-z0-9]+")

    _SOURCE_DIRS = frozenset({"app", "apps", "core", "lib", "libs", "package", "packages", "src"})
    _TEST_DIRS = frozenset({"spec", "specs", "test", "tests"})
    _LOW_VALUE_DIRS = frozenset({"build", "dist", "docs", "examples", "vendor"})

    def __init__(
        self,
        storage=None,
        *,
        k1: float = 1.2,
        b: float = 0.75,
        use_subtree_paths: bool = True,
        subtree_max_depth: int = 100,
    ) -> None:
        self.storage = storage
        self.k1 = k1
        self.b = b
        self.use_subtree_paths = use_subtree_paths
        self.subtree_max_depth = subtree_max_depth
        self._subtree_token_cache: dict[tuple[str, str], list[str]] = {}

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

        context = context or {}
        docs = [self._candidate_tokens(c, context) for c in candidates]
        scores = self._bm25_scores(query_tokens, docs)
        return [
            (candidate, score + self._prior_score(candidate, query_tokens))
            for candidate, score in zip(candidates, scores)
        ]

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

    def _candidate_tokens(self, candidate: dict[str, Any], context: dict[str, Any]) -> list[str]:
        parts = [
            candidate.get("rel_path", ""),
            candidate.get("path", ""),
            candidate.get("title", ""),
            candidate.get("tag", ""),
            candidate.get("summary", ""),
        ]
        tokens = self._tokenize(" ".join(str(p) for p in parts if p))

        if not self.use_subtree_paths or not self._is_dir(candidate):
            return tokens

        tree_id = context.get("tree_id")
        node_id = candidate.get("node_id")
        if not self.storage or not tree_id or not node_id:
            return tokens

        return tokens + self._subtree_tokens(str(tree_id), str(node_id))

    def _subtree_tokens(self, tree_id: str, node_id: str) -> list[str]:
        key = (tree_id, node_id)
        cached = self._subtree_token_cache.get(key)
        if cached is not None:
            return cached

        tokens: list[str] = []
        for node in self.storage.get_subtree(tree_id, node_id, max_depth=self.subtree_max_depth):
            attrs = self._attrs_from_node_dict(node)
            if attrs.get("is_dir"):
                continue
            rel_path = attrs.get("rel_path") or node.get("path") or attrs.get("title") or ""
            tokens.extend(self._tokenize(str(rel_path)))

        self._subtree_token_cache[key] = tokens
        return tokens

    def _prior_score(self, candidate: dict[str, Any], query_tokens: list[str]) -> float:
        rel_path = str(candidate.get("rel_path") or candidate.get("path") or "").strip("/")
        parts = [p.lower() for p in rel_path.split("/") if p]
        first = parts[0] if parts else ""
        tag = str(candidate.get("tag") or "").lower()
        title = str(candidate.get("title") or "").lower()
        basename = rel_path.rsplit("/", 1)[-1].lower() if rel_path else title

        score = 0.0
        if basename and basename in query_tokens:
            score += 1.5
        if first in self._SOURCE_DIRS or tag == "[src]":
            score += 0.4

        asks_for_tests = bool(set(query_tokens).intersection(self._TEST_DIRS | {"testing", "pytest"}))
        is_test = tag == "[test]" or any(part in self._TEST_DIRS for part in parts)
        if asks_for_tests and is_test:
            score += 0.8
        elif is_test:
            score -= 2.0

        if first in self._LOW_VALUE_DIRS and "docs" not in query_tokens and "readme" not in query_tokens:
            score -= 0.3

        return score

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        if not text:
            return []
        split_text = cls._CAMEL_BOUNDARY_RE.sub(" ", str(text).replace("_", " ").replace("-", " "))
        tokens = cls._TOKEN_RE.findall(split_text.lower())
        expanded = list(tokens)
        for token in tokens:
            if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
                expanded.append(token[:-1])
        return expanded

    @staticmethod
    def _is_dir(candidate: dict[str, Any]) -> bool:
        if "is_dir" in candidate:
            return bool(candidate["is_dir"])
        return not bool(candidate.get("is_leaf", False))

    @staticmethod
    def _attrs_from_node_dict(node: dict[str, Any]) -> dict[str, Any]:
        attrs = node.get("attrs") or {}
        if isinstance(attrs, dict):
            return attrs
        if isinstance(attrs, str):
            try:
                parsed = json.loads(attrs)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}
