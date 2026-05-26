"""Prompt-cache helpers for BlockRetriever."""

from __future__ import annotations

import hashlib
import os
from collections import deque
from pathlib import Path
from typing import Any

from jinja2 import Template

from contextdb.logger import get_logger
from contextdb.retriever.algorithm.block_types import Block


class _RetrieverSupportBase:
    def __init__(self, retriever):
        self._retriever = retriever

    def __getattr__(self, name: str):
        return getattr(self._retriever, name)

log = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
BLOCK_CURRENT_BLOCK_PROMPT = Template((_PROMPTS_DIR / "block_current_block.jinja").read_text(encoding="utf-8"))

DOC_CACHE_STATIC_SEGMENT = (
    "You are ranking tree nodes to answer a user question.\n"
    "Previous blocks are provided for context only — do NOT select nodes from them.\n"
)

FS_CACHE_STATIC_SEGMENT = (
    "You are navigating a source code repository's directory tree to find files relevant to a query.\n"
    "Previous blocks are provided for context only.\n"
    "RULES:\n"
    "- Select directories to drill deeper into, or files if they directly match the query\n"
    "- set done=false if returned directories need further exploration\n"
)


class BlockRetrieverPromptCacheSupport(_RetrieverSupportBase):
    """Prompt-cache support methods for BlockRetriever."""

    def _build_block_cache_key(self, block: Block, cache_payload: str | list[str] = "") -> str:
        cache_text = self._cache_payload_to_text(cache_payload)
        identity = hashlib.md5(cache_text.encode()).hexdigest() if cache_text else (block.content_hash or block.block_id)
        salt = os.getenv("CONDB_CACHE_KEY_SALT", "").strip()
        suffix = f":{salt}" if salt else ""
        return f"condb:block:{self.mode}:{identity}{suffix}"

    def _build_cache_payload(self, cache_segments: list[str]) -> str | list[str]:
        if not cache_segments:
            return ""
        if getattr(self.llm, "provider", None) == "anthropic":
            return cache_segments
        return self._cache_payload_to_text(cache_segments)

    @staticmethod
    def _cache_payload_to_text(cache_payload: str | list[str]) -> str:
        if isinstance(cache_payload, list):
            return "\n\n".join(s for s in cache_payload if s)
        return cache_payload or ""

    def _cache_static_segment(self) -> str:
        return FS_CACHE_STATIC_SEGMENT if self.mode == "filesystem" else DOC_CACHE_STATIC_SEGMENT

    def _build_cache_segments(self, cache_window: deque[dict[str, Any]]) -> list[str]:
        if not self.cache_enabled:
            return []

        static_segment = self._cache_static_segment()
        entries = [entry["content"] for entry in cache_window if entry.get("content")]
        if not entries:
            return [static_segment]

        max_checkpoints = max(1, self.cache_window_checkpoints)
        max_entry_segments = max(0, max_checkpoints - 1)

        if max_entry_segments == 0:
            return [static_segment + "\n\n" + "\n\n".join(entries)]

        if len(entries) <= max_entry_segments:
            return [static_segment, *entries]

        merge_count = len(entries) - max_entry_segments + 1
        merged_head = "\n\n".join(entries[:merge_count])
        tail = entries[merge_count:]
        return [static_segment, merged_head, *tail]

    def _append_to_cache_window(
        self,
        cache_window: deque[dict[str, Any]],
        total_tokens: int,
        block: Block,
        cache_block: bool = True,
        pin_block: bool = False,
    ) -> int:
        if not cache_block or not self.cache_enabled or not block.cached_content:
            return total_tokens

        entry_content = self._render_block_cache_segment(block.block_id, block.cached_content)
        entry_tokens = self.token_counter.count_text_tokens(entry_content)
        cache_window.append({
            "block_id": block.block_id,
            "content": entry_content,
            "tokens": entry_tokens,
            "pinned": pin_block,
        })
        total_tokens += entry_tokens

        dropped: list[str] = []
        while len(cache_window) > 1 and total_tokens > self.cache_window_tokens:
            remove_index = next((idx for idx, entry in enumerate(cache_window) if not entry.get("pinned")), None)
            if remove_index is None:
                break
            cache_window.rotate(-remove_index)
            removed = cache_window.popleft()
            cache_window.rotate(remove_index)
            total_tokens -= int(removed["tokens"])
            dropped.append(removed["block_id"])

        if dropped:
            log.info(
                "cache_window_trim dropped=%s remaining=%d total_tokens=%d limit=%d",
                dropped,
                len(cache_window),
                total_tokens,
                self.cache_window_tokens,
            )
        return total_tokens

    @staticmethod
    def _render_block_cache_segment(block_id: str, block_content: str) -> str:
        return BLOCK_CURRENT_BLOCK_PROMPT.render(
            block_id=block_id,
            block_content=block_content,
        )

    def _parse_llm_response(self, resp, valid_node_ids, response_id_map: dict[str, str] | None = None):
        valid_set = set(valid_node_ids)
        for block in resp.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "rank":
                tool_input = block.get("input", {}) or {}
                ranked_ids = tool_input.get("ranked_ids", []) or []
                done = bool(tool_input.get("done", False))
                normalized: list[str] = []
                for node_id in ranked_ids:
                    mapped_node_id = response_id_map.get(node_id, node_id) if response_id_map else node_id
                    if mapped_node_id in valid_set and mapped_node_id not in normalized:
                        normalized.append(mapped_node_id)
                return normalized, done
        raise ValueError("LLM did not return a rank tool call")
