"""Block-level beam search retriever with fixed-block prefix caching."""

import json
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from jinja2 import Template

from contextdb.config import get_llm_config, get_retriever_config
from contextdb.logger import get_logger
from contextdb.retriever.algorithm.base_retriever import BaseRetriever
from contextdb.retriever.algorithm.block_cutter import BlockCutter
from contextdb.retriever.algorithm.block_retriever_filesystem import (
    BlockRetrieverFilesystemSupport,
    FilesystemRenderOptions,
)
from contextdb.retriever.algorithm.block_retriever_prompt_cache import (
    DOC_CACHE_STATIC_SEGMENT,
    FS_CACHE_STATIC_SEGMENT,
    BlockRetrieverPromptCacheSupport,
)
from contextdb.retriever.algorithm.block_types import (
    Block,
    BlockResult,
    BlockRetrievalResult,
    BlockTreePlan,
)
from contextdb.utils.token_counter import TokenCounter

log = get_logger(__name__)

_DEFAULT_CONFIG = get_retriever_config("block")

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
BLOCK_PROMPT = Template((_PROMPTS_DIR / "block.jinja").read_text(encoding="utf-8"))
BLOCK_FS_PROMPT = Template((_PROMPTS_DIR / "block_fs.jinja").read_text(encoding="utf-8"))

TOOLS = [
    {
        "name": "rank",
        "description": "Rank candidate node ids for the query",
        "input_schema": {
            "type": "object",
            "properties": {
                "selected": {"type": "array", "items": {"type": "string"}},
                "done": {"type": "boolean"},
            },
            "required": ["selected"],
        },
    }
]

__all__ = [
    "BlockRetriever",
    "DOC_CACHE_STATIC_SEGMENT",
    "FS_CACHE_STATIC_SEGMENT",
    "FilesystemRenderOptions",
    "FsRenderOptions",
]

FsRenderOptions = FilesystemRenderOptions


class BlockRetriever(BaseRetriever):

    def __init__(
        self,
        storage,
        llm,
        max_tokens_per_block: int = None,
        cache_current_block: bool = None,
        cache_subtree_block: bool = None,
        cache_enabled: bool = True,
        max_parallel_blocks: int = None,
        mode: str = "document",
    ):
        super().__init__(storage, llm)
        self.mode = mode
        self.cache_enabled = bool(cache_enabled)

        self.max_tokens_per_block = (
            max_tokens_per_block if max_tokens_per_block is not None
            else _DEFAULT_CONFIG.get("max_tokens_per_block", 16000)
        )
        self.min_tokens_per_block = _DEFAULT_CONFIG.get("min_tokens_per_block", 0)
        self.cache_current_block = (
            cache_current_block if cache_current_block is not None
            else bool(_DEFAULT_CONFIG.get("cache_current_block", True))
        )
        self.cache_subtree_block = (
            cache_subtree_block if cache_subtree_block is not None
            else bool(_DEFAULT_CONFIG.get("cache_subtree_block", True))
        )

        provider = getattr(llm, "provider", None)
        model = getattr(llm, "model", None)
        llm_config = get_llm_config(provider, model) if provider and model else {}
        context_limit = int(llm_config.get("context_limit", 100000))
        llm_max_concurrent = max(1, int(llm_config.get("max_concurrent", 10) or 10))
        reserve_tokens = int(_DEFAULT_CONFIG.get("cache_window_reserve_tokens", 4096))
        configured_window = int(_DEFAULT_CONFIG.get("cache_window_tokens", 0) or 0)
        configured_parallel = (
            max_parallel_blocks
            if max_parallel_blocks is not None
            else int(_DEFAULT_CONFIG.get("max_parallel_blocks", 4) or 4)
        )
        self.max_parallel_blocks = max(1, min(int(configured_parallel), llm_max_concurrent))
        if configured_window > 0:
            self.cache_window_tokens = configured_window
        else:
            # Use model window minus a safety reserve for non-cached + dynamic prompt.
            self.cache_window_tokens = max(self.max_tokens_per_block, context_limit - reserve_tokens)
        self.cache_window_checkpoints = max(1, int(_DEFAULT_CONFIG.get("cache_window_checkpoints", 4) or 4))

        self.token_counter = TokenCounter(provider=provider, model=model)
        self.block_cutter = BlockCutter(
            storage,
            self.token_counter,
            self.max_tokens_per_block,
            self.min_tokens_per_block,
        )
        self._filesystem_support = BlockRetrieverFilesystemSupport(self)
        self._prompt_cache_support = BlockRetrieverPromptCacheSupport(self)
        self._plan_cache: dict[str, BlockTreePlan] = {}
        self._precomputed_tree_id: str = ""

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        for support_name in ("_filesystem_support", "_prompt_cache_support"):
            support = self.__dict__.get(support_name)
            if support is None:
                continue
            if hasattr(type(support), name):
                return getattr(support, name)
        raise AttributeError(f"{type(self).__name__!s} object has no attribute {name!r}")

    def retrieve(
        self,
        tree_id: str,
        query: str,
        beam_size: int = None,
        max_turns: int = None,
        select_k: int = 1,
    ) -> BlockRetrievalResult:
        if self.mode == "filesystem":
            return self._retrieve_fs(tree_id, query, beam_size, max_turns, select_k)
        return self._retrieve_doc(tree_id, query, beam_size, max_turns, select_k)

    # ── Filesystem mode: subtree-driven (like Legacy) ───────────────

    def _retrieve_fs(
        self, tree_id: str, query: str, beam_size: int, max_turns: int, select_k: int,
    ) -> BlockRetrievalResult:
        """Subtree-driven retrieval for filesystem navigation.

        1. Process top block (shallow layers) → select beams
        2. Dynamically build subtree blocks from beam nodes → repeat
        """
        root_id = self.storage.get_root_id(tree_id)
        if not root_id:
            return self._empty_result()

        if tree_id != self._precomputed_tree_id:
            self.token_counter.clear_cache()
            self.token_counter.precompute_tree_tokens(self.storage, tree_id)
            self._precomputed_tree_id = tree_id

        beams = self._collapse_fs_beams(
            tree_id,
            [{"node_id": root_id, "title": "root", "path": "root"}],
        )
        selected: list[str] = []
        previous_selected: list[str] = []
        trace: list[dict[str, Any]] = []
        block_traces: list[dict[str, Any]] = []

        total_llm_calls = 0
        cache_read_tokens = 0
        cache_creation_tokens = 0
        blocks_processed = 0
        cache_window: deque[dict[str, Any]] = deque()
        cache_window_tokens = 0

        k = beam_size if beam_size else select_k
        max_rounds = max(1, max_turns) if max_turns else 20

        result = BlockResult(block_id="", ranked_node_ids=[], selected_node_ids=[], done=False)

        turn = 0
        while not result.done and turn < max_rounds:
            if turn > 0 and not self._beams_have_children(tree_id, beams):
                break

            round_label = "top" if turn == 0 else "subtree"
            block_specs = (
                self._create_top_block_specs_fs(tree_id, beams[0])
                if turn == 0
                else self._create_subtree_block_specs_fs(tree_id, beams)
            )
            if not block_specs:
                break

            cache_segments = self._build_cache_segments(cache_window)
            local_k = k if len(block_specs) <= 1 else max(k, 2)
            cache_current_block = self.cache_current_block if turn == 0 else self.cache_subtree_block
            block_rows = self._process_fs_block_specs(
                subtree_specs=block_specs,
                query=query,
                previous_selected=previous_selected,
                k=local_k,
                cache_segments=cache_segments,
                cache_current_block=cache_current_block,
            )
            if not block_rows:
                break

            merged_ranked: list[str] = []
            candidate_count = 0

            for spec, block_result, llm_called, cache_metrics in block_rows:
                block = spec["block"]
                total_llm_calls += llm_called
                cache_read_tokens += cache_metrics.get("cache_read_tokens", 0)
                cache_creation_tokens += cache_metrics.get("cache_creation_tokens", 0)
                blocks_processed += 1

                candidate_count += len(block.node_ids)
                merged_ranked.extend(block_result.ranked_node_ids)

                block_trace = {
                    "type": f"{round_label}_horizontal" if spec["block_count"] > 1 else round_label,
                    "block_id": block.block_id,
                    "block_index": spec["block_index"],
                    "block_count": spec["block_count"],
                    "nodes": len(block.node_ids),
                    "allowed": len(block.node_ids),
                    "tokens": block.total_tokens,
                }
                if round_label == "subtree":
                    block_trace["beam_id"] = spec["beam"]["node_id"]
                block_traces.append(block_trace)

            merged_ranked = self._merge_unique_ids(merged_ranked)
            result = BlockResult(
                block_id=f"fs_{round_label}_{turn}",
                ranked_node_ids=merged_ranked,
                selected_node_ids=merged_ranked[:max(1, k)],
                done=(not merged_ranked) or all(block_result.done for _, block_result, _, _ in block_rows),
            )

            if len(block_rows) > 1 and len(merged_ranked) > 1:
                rerank_cache_segments = self._build_cache_segments(cache_window)
                rerank_block, rerank_allowed_ids = self._build_fs_candidate_block(
                    tree_id=tree_id,
                    node_ids=merged_ranked,
                    block_id=f"fs_{round_label}_rerank_t{turn}",
                )
                result, llm_called, cache_metrics = self._process_block(
                    block=rerank_block,
                    query=query,
                    input_beams=beams,
                    previous_selected=previous_selected,
                    allowed_node_ids=rerank_allowed_ids,
                    k=k,
                    cache_segments=rerank_cache_segments,
                    current_block_content=rerank_block.cached_content or "",
                    cache_current_block=False,
                )
                total_llm_calls += llm_called
                cache_read_tokens += cache_metrics.get("cache_read_tokens", 0)
                cache_creation_tokens += cache_metrics.get("cache_creation_tokens", 0)
                blocks_processed += 1

                block_traces.append({
                    "type": f"{round_label}_global",
                    "block_id": rerank_block.block_id,
                    "nodes": len(rerank_block.node_ids),
                    "allowed": len(rerank_allowed_ids),
                    "tokens": rerank_block.total_tokens,
                })

            previous_selected = list(result.selected_node_ids)
            for nid in previous_selected:
                if nid not in selected:
                    selected.append(nid)
            beams = self._collapse_fs_beams(
                tree_id,
                self._update_beams(result.ranked_node_ids, tree_id, beam_size),
            )
            cache_window_tokens = self._append_fs_selected_blocks(
                cache_window=cache_window,
                total_tokens=cache_window_tokens,
                block_rows=block_rows,
                beams=beams,
                cache_block=cache_current_block,
                pin_block=(turn == 0),
            )
            result = self._override_done_if_dirs(result, tree_id, beams)

            trace_entry = {
                "turn": turn,
                "type": f"{round_label}_split" if len(block_rows) > 1 else round_label,
                "candidates": candidate_count,
                "reranked_candidates": len(merged_ranked),
                "kept": len(result.ranked_node_ids),
                "done": result.done,
            }
            if len(block_rows) > 1:
                trace_entry["blocks"] = len(block_rows)
            else:
                trace_entry["block_id"] = block_rows[0][0]["block"].block_id
            trace.append(trace_entry)
            turn += 1

        # Filter out directory nodes — only return files
        file_selected = []
        for nid in selected:
            node = self.storage.get_node(tree_id, nid)
            if node and node.attrs_json:
                try:
                    attrs = json.loads(node.attrs_json)
                except json.JSONDecodeError:
                    attrs = {}
                if attrs.get("is_dir", False):
                    continue
            file_selected.append(nid)
        selected = file_selected if file_selected else selected

        contents = self._gather_contents(tree_id, selected)
        return BlockRetrievalResult(
            nodes=selected, contents=contents, trace=trace, turns=len(trace),
            blocks_processed=blocks_processed, total_llm_calls=total_llm_calls,
            cache_read_tokens=cache_read_tokens, cache_creation_tokens=cache_creation_tokens,
            block_traces=block_traces,
        )

    # ── Document mode: fixed-block depth-slice (original) ───────────

    def _retrieve_doc(
        self, tree_id: str, query: str, beam_size: int, max_turns: int, select_k: int,
    ) -> BlockRetrievalResult:
        """
        Fixed-block beam retrieval:
        1. Use BlockCutter plan blocks as stable cacheable prefixes.
        2. Keep block content unchanged across queries.
        3. Use beam paths to dynamically filter allowed node ids per block.
        """
        root_id = self.storage.get_root_id(tree_id)
        if not root_id:
            return self._empty_result()

        if tree_id != self._precomputed_tree_id:
            self.token_counter.clear_cache()
            self.token_counter.precompute_tree_tokens(self.storage, tree_id)
            self._precomputed_tree_id = tree_id

        plan = self._get_or_create_plan(tree_id)
        if not plan.blocks:
            return self._empty_result()

        beams = [{"node_id": root_id, "title": "root", "path": "root"}]
        previous_selected: list[str] = []
        selected: list[str] = []
        trace: list[dict[str, Any]] = []
        block_traces: list[dict[str, Any]] = []

        total_llm_calls = 0
        cache_read_tokens = 0
        cache_creation_tokens = 0
        blocks_processed = 0
        cache_window: deque[dict[str, Any]] = deque()
        cache_window_tokens = 0

        k = beam_size if beam_size else select_k
        max_calls = max_turns if max_turns else len(plan.blocks)

        h_group_map: dict[str, str] = {}
        h_groups: dict[str, list[Block]] = {}
        for hg in plan.horizontal_groups:
            h_groups[hg.group_id] = hg.blocks
            for hb in hg.blocks:
                h_group_map[hb.block_id] = hg.group_id

        done = False
        processed_groups: set[str] = set()
        for block in plan.blocks:
            if total_llm_calls >= max_calls or done:
                break

            if total_llm_calls > 0 and not self._beams_have_children(tree_id, beams):
                break

            group_id = h_group_map.get(block.block_id)

            if group_id and group_id not in processed_groups:
                processed_groups.add(group_id)
                group_blocks = h_groups[group_id]
                group_cache_segments = self._build_cache_segments(cache_window)

                group_results = self._process_horizontal_group(
                    group_blocks, tree_id, query, beams, previous_selected, k,
                    cache_segments=group_cache_segments,
                    cache_current_block=self.cache_current_block,
                )

                for result, llm_called, cache_metrics, blk in group_results:
                    total_llm_calls += llm_called
                    cache_read_tokens += cache_metrics.get("cache_read_tokens", 0)
                    cache_creation_tokens += cache_metrics.get("cache_creation_tokens", 0)
                    blocks_processed += 1

                    for node_id in result.selected_node_ids:
                        if node_id not in selected:
                            selected.append(node_id)

                    block_traces.append({
                        "type": "horizontal",
                        "block_id": blk.block_id,
                        "depth_range": f"{blk.depth_start}-{blk.depth_end}",
                        "nodes": len(blk.node_ids),
                        "tokens": blk.total_tokens,
                    })

                all_ranked = []
                for result, _, _, _ in group_results:
                    all_ranked.extend(result.ranked_node_ids)
                seen = set()
                merged_ranked = []
                for nid in all_ranked:
                    if nid not in seen:
                        seen.add(nid)
                        merged_ranked.append(nid)

                previous_selected = merged_ranked[:max(1, k)]
                for nid in previous_selected:
                    if nid not in selected:
                        selected.append(nid)
                beams = self._update_beams(merged_ranked, tree_id, beam_size)

                trace.append({
                    "turn": len(trace),
                    "group_id": group_id,
                    "h_blocks": len(group_blocks),
                    "candidates": sum(len(r.ranked_node_ids) for r, _, _, _ in group_results),
                    "kept": len(merged_ranked),
                    "done": any(r.done for r, _, _, _ in group_results),
                })
                if any(r.done for r, _, _, _ in group_results):
                    done = True

                processed_ids = {blk.block_id for _, _, _, blk in group_results}
                for group_block in group_blocks:
                    if group_block.block_id in processed_ids:
                        cache_window_tokens = self._append_to_cache_window(
                            cache_window,
                            cache_window_tokens,
                            group_block,
                            cache_block=self.cache_current_block,
                        )
                continue

            if group_id and group_id in processed_groups:
                continue

            allowed_node_ids = self._collect_allowed_node_ids(tree_id, block, beams)
            if not allowed_node_ids:
                continue

            block_cache_segments = self._build_cache_segments(cache_window)
            result, llm_called, cache_metrics = self._process_block(
                block=block,
                query=query,
                input_beams=beams,
                previous_selected=previous_selected,
                allowed_node_ids=allowed_node_ids,
                k=k,
                cache_segments=block_cache_segments,
                current_block_content=block.cached_content or "",
                cache_current_block=self.cache_current_block,
            )
            total_llm_calls += llm_called
            cache_read_tokens += cache_metrics.get("cache_read_tokens", 0)
            cache_creation_tokens += cache_metrics.get("cache_creation_tokens", 0)
            blocks_processed += 1
            cache_window_tokens = self._append_to_cache_window(
                cache_window, cache_window_tokens, block, cache_block=self.cache_current_block,
            )

            for node_id in result.selected_node_ids:
                if node_id not in selected:
                    selected.append(node_id)

            previous_selected = list(result.selected_node_ids)
            beams = self._update_beams(result.ranked_node_ids, tree_id, beam_size)

            block_traces.append({
                "type": "vertical",
                "block_id": block.block_id,
                "depth_range": f"{block.depth_start}-{block.depth_end}",
                "nodes": len(block.node_ids),
                "allowed": len(allowed_node_ids),
                "tokens": block.total_tokens,
            })
            trace.append({
                "turn": len(trace),
                "block_id": block.block_id,
                "candidates": len(allowed_node_ids),
                "kept": len(result.ranked_node_ids),
                "done": result.done,
            })

            if result.done:
                break

        contents = self._gather_contents(tree_id, selected)

        return BlockRetrievalResult(
            nodes=selected,
            contents=contents,
            trace=trace,
            turns=len(trace),
            blocks_processed=blocks_processed,
            total_llm_calls=total_llm_calls,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            block_traces=block_traces,
        )

    def _process_horizontal_group(
        self,
        group_blocks,
        tree_id,
        query,
        beams,
        previous_selected,
        k,
        cache_segments: list[str] = None,
        cache_current_block: bool = True,
    ):
        """Process all blocks in a horizontal group concurrently."""
        tasks = []
        for blk in group_blocks:
            allowed = self._collect_allowed_node_ids(tree_id, blk, beams)
            if allowed:
                tasks.append((blk, allowed))

        if not tasks:
            return []

        if len(tasks) == 1:
            blk, allowed = tasks[0]
            result, llm_called, cache_metrics = self._process_block(
                blk, query, beams, previous_selected, allowed, k,
                cache_segments=cache_segments or [],
                current_block_content=blk.cached_content or "",
                cache_current_block=cache_current_block,
            )
            return [(result, llm_called, cache_metrics, blk)]

        results = []
        max_workers = self._max_parallel_workers(len(tasks))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    self._process_block,
                    blk,
                    query,
                    beams,
                    previous_selected,
                    allowed,
                    k,
                    cache_segments=cache_segments or [],
                    current_block_content=blk.cached_content or "",
                    cache_current_block=cache_current_block,
                ): blk
                for blk, allowed in tasks
            }
            for future in as_completed(futures):
                blk = futures[future]
                result, llm_called, cache_metrics = future.result()
                results.append((result, llm_called, cache_metrics, blk))

        return results

    def _max_parallel_workers(self, task_count: int) -> int:
        return max(1, min(task_count, self.max_parallel_blocks))

    # ---- LLM interaction ----

    def _process_block(
        self,
        block,
        query,
        input_beams,
        previous_selected,
        allowed_node_ids,
        k,
        cache_segments: list[str] = None,
        current_block_content: str = "",
        cache_current_block: bool = True,
    ):
        """Process one fixed block with dynamic beam filter."""
        empty_metrics = {"cache_read_tokens": 0, "cache_creation_tokens": 0}
        if not allowed_node_ids:
            return BlockResult(block_id=block.block_id, ranked_node_ids=[], selected_node_ids=[], done=False), 0, empty_metrics

        current_block_prompt = self._render_block_cache_segment(
            block.block_id,
            current_block_content or block.cached_content or "",
        )
        non_cached_parts: list[str] = []
        if self.cache_enabled:
            cache_segment_parts = list(cache_segments or [])
            if current_block_prompt:
                if cache_current_block:
                    cache_segment_parts.append(current_block_prompt)
                else:
                    non_cached_parts.append(current_block_prompt)
            cache_payload = self._build_cache_payload(cache_segment_parts)
            cache_key = self._build_block_cache_key(block, cache_payload)
        else:
            cache_payload = ""
            cache_key = None
            if current_block_prompt:
                non_cached_parts.append(current_block_prompt)
        non_cached_content = self._cache_payload_to_text(non_cached_parts)
        if not non_cached_content:
            non_cached_content = None
        response_id_map: dict[str, str] | None = None
        if self.mode == "filesystem":
            prompt_context = self._build_fs_prompt_context(
                tree_id=block.tree_id,
                block=block,
                allowed_node_ids=allowed_node_ids,
                input_beams=input_beams,
                previous_selected=previous_selected,
            )
            dynamic_prompt = BLOCK_FS_PROMPT.render(
                query=query,
                previous_selected=prompt_context["previous_selected"],
                input_beams=prompt_context["input_beams"],
                selection_mode=prompt_context["selection_mode"],
                selection_aliases=prompt_context["selection_aliases"],
                k=k,
            )
            response_id_map = prompt_context["alias_to_node_id"]
        else:
            dynamic_prompt = BLOCK_PROMPT.render(
                query=query,
                previous_selected=previous_selected,
                input_beams=input_beams,
                allowed_node_ids=allowed_node_ids,
                k=k,
            )
        prefix_tokens = self.token_counter.count_text_tokens(self._cache_payload_to_text(cache_payload))
        current_tokens = 0 if (self.cache_enabled and cache_current_block) else self.token_counter.count_text_tokens(current_block_prompt)
        dynamic_tokens = self.token_counter.count_text_tokens(dynamic_prompt)
        est_total_tokens = prefix_tokens + current_tokens + dynamic_tokens
        log.info(
            "prompt_tokens block=%s prefix=%d current=%d dynamic=%d est_total=%d allowed=%d",
            block.block_id,
            prefix_tokens,
            current_tokens,
            dynamic_tokens,
            est_total_tokens,
            len(allowed_node_ids),
        )

        if not self.cache_enabled:
            call_started = time.perf_counter()
            full_prompt_parts = []
            if non_cached_content:
                full_prompt_parts.append(non_cached_content)
            full_prompt_parts.append(dynamic_prompt)
            full_prompt = "\n\n".join(part for part in full_prompt_parts if part)
            resp = self.llm.chat(
                [{"role": "user", "content": full_prompt}],
                tools=TOOLS,
            )
        elif hasattr(self.llm, "chat_with_cache"):
            call_started = time.perf_counter()
            cache_call_kwargs = {
                "tools": TOOLS,
                "cache_content": cache_payload,
                "non_cached_content": non_cached_content,
                "cache_key": cache_key,
            }
            try:
                resp = self.llm.chat_with_cache(
                    [{"role": "user", "content": dynamic_prompt}],
                    **cache_call_kwargs,
                )
            except TypeError:
                cache_call_kwargs.pop("cache_key", None)
                resp = self.llm.chat_with_cache(
                    [{"role": "user", "content": dynamic_prompt}],
                    **cache_call_kwargs,
                )
        else:
            call_started = time.perf_counter()
            full_prompt_parts = [self._cache_payload_to_text(cache_payload)]
            if non_cached_content:
                full_prompt_parts.append(non_cached_content)
            full_prompt_parts.append(dynamic_prompt)
            full_prompt = "\n\n".join(part for part in full_prompt_parts if part)
            chat_kwargs = {"tools": TOOLS, "cache_key": cache_key}
            try:
                resp = self.llm.chat([{"role": "user", "content": full_prompt}], **chat_kwargs)
            except TypeError:
                chat_kwargs.pop("cache_key", None)
                resp = self.llm.chat([{"role": "user", "content": full_prompt}], **chat_kwargs)
        call_latency_s = time.perf_counter() - call_started

        usage = resp.get("usage") or {}
        cache_read_tokens = usage.get("cache_read_input_tokens", usage.get("cached_tokens", 0)) or 0
        cache_creation_tokens = usage.get("cache_creation_input_tokens", 0) or 0
        try:
            network_rtt_s = max(0.0, float(os.getenv("CONDB_NETWORK_RTT_EST_MS", "0")) / 1000.0)
        except (TypeError, ValueError):
            network_rtt_s = 0.0
        adjusted_wall_s = max(0.0, call_latency_s - network_rtt_s)
        cache_metrics = {
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
        }
        input_tokens = usage.get("input_tokens", 0) or 0
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
        effective_prefill_tokens = max(0, int(input_tokens) - int(cache_read_tokens))
        total_work_tokens = effective_prefill_tokens + int(output_tokens)
        if total_work_tokens > 0:
            prefill_est_s = adjusted_wall_s * effective_prefill_tokens / total_work_tokens
            decode_est_s = adjusted_wall_s * int(output_tokens) / total_work_tokens
        else:
            prefill_est_s = 0.0
            decode_est_s = 0.0

        log.info(
            "usage_tokens block=%s input=%d output=%d cache_read=%d cache_create=%d",
            block.block_id,
            int(input_tokens),
            int(output_tokens),
            int(cache_read_tokens),
            int(cache_creation_tokens),
        )
        log.info(
            "timing block=%s wall=%.3fs wall_adj=%.3fs net_rtt_est=%.3fs prefill_est=%.3fs decode_est=%.3fs prefill_tokens=%d decode_tokens=%d",
            block.block_id,
            call_latency_s,
            adjusted_wall_s,
            network_rtt_s,
            prefill_est_s,
            decode_est_s,
            effective_prefill_tokens,
            int(output_tokens),
        )

        ranked_ids, done = self._parse_llm_response(
            resp,
            allowed_node_ids,
            response_id_map=response_id_map,
        )

        result = BlockResult(
            block_id=block.block_id,
            ranked_node_ids=ranked_ids,
            selected_node_ids=ranked_ids[:max(1, k)],
            done=done,
            usage=usage,
        )
        return result, 1, cache_metrics

    # ---- beam management ----

    def _update_beams(self, ranked_ids, tree_id, beam_size):
        new_beams = []
        for node_id in ranked_ids:
            node = self.storage.get_node(tree_id, node_id)
            attrs = {}
            if node and node.attrs_json:
                try:
                    attrs = json.loads(node.attrs_json)
                except json.JSONDecodeError:
                    attrs = {}

            if self.mode == "filesystem":
                beam_path = attrs.get("rel_path", "")
            else:
                beam_path = node.path if node else ""
            new_beams.append({
                "node_id": node_id,
                "title": attrs.get("title", ""),
                "path": beam_path,
            })
            if beam_size and len(new_beams) >= beam_size:
                break

        return new_beams if new_beams else [{"node_id": "", "title": "", "path": ""}]

    def _beams_have_children(self, tree_id: str, beams: list[dict[str, str]]) -> bool:
        for beam in beams:
            node_id = beam.get("node_id", "")
            if node_id and self.storage.get_children(tree_id, node_id):
                return True
        return False

    def _override_done_if_dirs(self, result: BlockResult, tree_id: str, beams: list[dict]) -> BlockResult:
        """In fs mode, force-continue if beams still point to directories."""
        if result.done and self._beams_have_children(tree_id, beams):
            return BlockResult(
                block_id=result.block_id,
                ranked_node_ids=result.ranked_node_ids,
                selected_node_ids=result.selected_node_ids,
                done=False,
                usage=result.usage,
            )
        return result

    # ---- allowed node filtering (dynamic, but content stays fixed) ----

    def _collect_allowed_node_ids(self, tree_id: str, block: Block, beams: list[dict[str, str]]) -> list[str]:
        beam_ids = [b["node_id"] for b in beams if b.get("node_id")]
        if not beam_ids:
            return []

        beam_path_map = self._get_node_paths(tree_id, beam_ids)
        if not beam_path_map:
            return []

        block_path_map = self._get_node_paths(tree_id, block.node_ids)
        if not block_path_map:
            return []

        beam_id_set = set(beam_ids)
        beam_paths = list(beam_path_map.values())
        allowed: list[str] = []

        for node_id in block.node_ids:
            if node_id in beam_id_set:
                continue
            node_path = block_path_map.get(node_id)
            if not node_path:
                continue
            if any(node_path.startswith(f"{beam_path}/") for beam_path in beam_paths):
                allowed.append(node_id)

        return allowed

    def _get_node_paths(self, tree_id: str, node_ids: list[str]) -> dict[str, str]:
        if not node_ids:
            return {}

        cursor = self.storage.conn.cursor()
        path_map: dict[str, str] = {}
        chunk_size = 500

        for i in range(0, len(node_ids), chunk_size):
            chunk = node_ids[i:i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(
                f"SELECT node_id, path FROM nodes WHERE tree_id = ? AND node_id IN ({placeholders})",
                (tree_id, *chunk),
            )
            for row in cursor.fetchall():
                path_map[row["node_id"]] = row["path"]

        return path_map

    # ---- DB helpers ----

    def _gather_contents(self, tree_id, selected):
        contents = []
        for node_id in selected:
            entity = self.storage.get_entity(tree_id, node_id)
            if entity:
                contents.append({"node_id": node_id, "content": json.loads(entity.payload_json)})
        return contents

    # ---- plan & cache management ----

    def _get_or_create_plan(self, tree_id):
        if tree_id not in self._plan_cache:
            self._plan_cache[tree_id] = self.block_cutter.cut_tree(tree_id)
        return self._plan_cache[tree_id]

    def _empty_result(self):
        return BlockRetrievalResult(
            nodes=[],
            contents=[],
            trace=[],
            turns=0,
            blocks_processed=0,
            total_llm_calls=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            block_traces=[],
        )

    def clear_cache(self):
        self._plan_cache.clear()

    def clear_plan_cache(self, tree_id=None):
        if tree_id:
            self._plan_cache.pop(tree_id, None)
        else:
            self._plan_cache.clear()

    def get_cache_stats(self):
        return {"plan_cache_size": len(self._plan_cache)}
