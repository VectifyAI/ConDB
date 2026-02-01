"""Block-level beam search retriever with prefix caching."""

import json
from pathlib import Path
from typing import Any, Optional

from jinja2 import Template

from contextdb.config import get_llm_config, get_retriever_config
from contextdb.logger import get_logger
from contextdb.retriever.algorithm.base_retriever import BaseRetriever
from contextdb.retriever.algorithm.block_cutter import BlockCutter
from contextdb.retriever.algorithm.block_types import (
    Block,
    BlockResult,
    BlockRetrievalResult,
    BlockTreePlan,
    HorizontalBlockGroup,
)
from contextdb.utils.prefix_cache import PrefixCache
from contextdb.utils.token_counter import TokenCounter

log = get_logger(__name__)

# Load default config
_DEFAULT_CONFIG = get_retriever_config("block")

# Block-specific prompt template (dynamic part only)
BLOCK_PROMPT = Template(
    (Path(__file__).parent.parent.parent / "prompts/block.jinja").read_text()
    if (Path(__file__).parent.parent.parent / "prompts/block.jinja").exists()
    else """
You are ranking tree nodes to answer a user question.

Query: {{ query }}

Selected so far:
{% if selected %}
{{ selected }}
{% else %}
(none)
{% endif %}

{% if input_beams %}
Input beams (context from previous block):
{% for beam in input_beams %}
- {{ beam.node_id }}: {{ beam.title }} ({{ beam.path }})
{% endfor %}
{% endif %}

Pick up to {{ k }} candidates from the BLOCK CONTENT above, best first.

Return ONE tool call "rank" with:
- selected: list of ids in best-to-worst order
- done: true ONLY if you've reached leaf nodes or content is specific enough to answer. If nodes are high-level sections, set done=false to explore deeper.
"""
)

# Static prefix for block content (cacheable)
BLOCK_CONTENT_PREFIX = """=== BLOCK CONTENT (Document Tree Nodes) ===
The following are candidate nodes from this document block.
Each node has an id, title, and summary.

"""

# Tool schema for LLM
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


class BlockRetriever(BaseRetriever):
    """Block-level beam search retriever with prefix caching."""

    def __init__(
        self,
        storage,
        llm,
        max_tokens_per_block: int = None,
        levels_per_block: int = None,
        cache_enabled: bool = None,
        parallel_horizontal: bool = None,
        max_parallel_workers: int = None,
        reduce_rerank_threshold: int = None,
        enable_beam_pruning: bool = None,
    ):
        super().__init__(storage, llm)

        # Load from config, override with explicit parameters
        self.max_tokens_per_block = (
            max_tokens_per_block if max_tokens_per_block is not None
            else _DEFAULT_CONFIG.get("max_tokens_per_block", 16000)
        )
        self.levels_per_block = (
            levels_per_block if levels_per_block is not None
            else _DEFAULT_CONFIG.get("levels_per_block", 1)
        )
        self.parallel_horizontal = (
            parallel_horizontal if parallel_horizontal is not None
            else _DEFAULT_CONFIG.get("parallel_horizontal", False)
        )

        # Get max_parallel_workers, constrained by LLM's max_concurrent
        config_max_workers = _DEFAULT_CONFIG.get("max_parallel_workers", 4)
        if max_parallel_workers is not None:
            config_max_workers = max_parallel_workers

        # Get LLM's max_concurrent limit from config
        llm_max_concurrent = self._get_llm_max_concurrent(llm)
        self.max_parallel_workers = min(config_max_workers, llm_max_concurrent)
        self.reduce_rerank_threshold = (
            reduce_rerank_threshold if reduce_rerank_threshold is not None
            else _DEFAULT_CONFIG.get("reduce_rerank_threshold", 10)
        )

        cache_enabled_final = (
            cache_enabled if cache_enabled is not None
            else _DEFAULT_CONFIG.get("cache_enabled", True)
        )

        # Beam pruning: filter nodes by parent-child relationship
        self.enable_beam_pruning = (
            enable_beam_pruning if enable_beam_pruning is not None
            else _DEFAULT_CONFIG.get("enable_beam_pruning", True)
        )

        self.token_counter = TokenCounter()
        self.block_cutter = BlockCutter(
            storage=storage,
            token_counter=self.token_counter,
            max_tokens_per_block=self.max_tokens_per_block,
            levels_per_block=self.levels_per_block,
        )

        if cache_enabled_final:
            cache_max_entries = _DEFAULT_CONFIG.get("cache_max_entries", 1000)
            cache_ttl = _DEFAULT_CONFIG.get("cache_ttl_seconds", 3600)
            cache_max_memory = _DEFAULT_CONFIG.get("cache_max_memory_mb", 100)
            self.prefix_cache = PrefixCache(
                max_size=cache_max_entries,
                ttl_seconds=cache_ttl,
                max_memory_mb=cache_max_memory,
            )
        else:
            self.prefix_cache = None

        # Cache for tree plans (can be reused across queries)
        self._plan_cache: dict[str, BlockTreePlan] = {}

    def retrieve(
        self,
        tree_id: str,
        query: str,
        beam_size: int = None,
        max_turns: int = None,
        select_k: int = 1,
    ) -> BlockRetrievalResult:
        """
        Run block-level beam search on a tree.

        Args:
            tree_id: ID of the tree to search
            query: Search query
            beam_size: Number of beams to keep (None = keep all)
            max_turns: Maximum blocks to process (None = all blocks)
            select_k: Top-k to select per block

        Returns:
            BlockRetrievalResult with nodes, contents, and block metrics
        """
        root_id = self.storage.get_root_id(tree_id)
        if not root_id:
            return self._empty_result()

        # Clear token counter cache for this retrieval
        self.token_counter.clear_cache()

        # Get or create block plan
        plan = self._get_or_create_plan(tree_id)

        if not plan.blocks:
            return self._empty_result()

        log.debug(
            "BlockRetriever: %d blocks, %d horizontal groups, %d total tokens",
            len(plan.blocks),
            len(plan.horizontal_groups),
            plan.total_tokens,
        )

        # Initialize beam search state
        beams = [{"node_id": root_id, "title": "root", "path": "root"}]
        selected: list[str] = []
        trace: list[dict[str, Any]] = []
        block_traces: list[dict[str, Any]] = []

        total_llm_calls = 0
        cache_hits = 0
        blocks_processed = 0
        horizontal_groups_processed = 0
        nodes_pruned = 0  # Track pruning statistics

        k = beam_size if beam_size else select_k

        # Determine max blocks to process
        max_blocks = max_turns if max_turns else len(plan.blocks)

        # Track which horizontal groups we've processed
        processed_groups: set[str] = set()

        # Process blocks in order
        block_index = 0
        while block_index < len(plan.blocks) and blocks_processed < max_blocks:
            block = plan.blocks[block_index]

            # Get current beam node IDs for pruning
            beam_ids = {b["node_id"] for b in beams if b["node_id"]}

            # Early termination: check if beams have no children to expand
            if block_index > 0 and len(beams) > 0:
                has_children = False
                for beam in beams:
                    if beam["node_id"] and self.storage.get_children(tree_id, beam["node_id"]):
                        has_children = True
                        break
                if not has_children:
                    log.debug("All beams are leaves, stopping before block %s", block.block_id)
                    break

            # Check if this is part of a horizontal group
            if block.horizontal_group_id and block.horizontal_group_id not in processed_groups:
                # Find all blocks in this group
                h_group = next(
                    (g for g in plan.horizontal_groups if g.group_id == block.horizontal_group_id),
                    None,
                )

                if h_group:
                    # Process entire horizontal group with Map-Reduce
                    result, group_llm_calls, group_cache_hits, group_pruned = self._process_horizontal_group(
                        h_group, query, beams, selected, k, tree_id, beam_ids
                    )
                    nodes_pruned += group_pruned

                    total_llm_calls += group_llm_calls
                    cache_hits += group_cache_hits
                    horizontal_groups_processed += 1
                    blocks_processed += len(h_group.blocks)
                    processed_groups.add(h_group.group_id)

                    # Update beams with merged results
                    beams = self._update_beams(result.ranked_node_ids, tree_id, beam_size)

                    # Update selected
                    for node_id in result.selected_node_ids:
                        if node_id not in selected:
                            selected.append(node_id)

                    block_traces.append(
                        {
                            "type": "horizontal_group",
                            "group_id": h_group.group_id,
                            "blocks": len(h_group.blocks),
                            "llm_calls": group_llm_calls,
                            "result_count": len(result.ranked_node_ids),
                        }
                    )

                    trace.append(
                        {
                            "turn": len(trace),
                            "block_id": h_group.group_id,
                            "candidates": sum(len(b.node_ids) for b in h_group.blocks),
                            "kept": len(result.ranked_node_ids),
                            "done": result.done,
                        }
                    )

                    # Skip all blocks in this group
                    while block_index < len(plan.blocks) and plan.blocks[block_index].horizontal_group_id == h_group.group_id:
                        block_index += 1

                    if result.done:
                        log.debug("Horizontal group signaled done")
                        break

                    continue

            # Skip blocks from already processed horizontal groups
            if block.horizontal_group_id and block.horizontal_group_id in processed_groups:
                block_index += 1
                continue

            # Apply beam pruning for vertical blocks
            filtered_node_ids = None
            filtered_content = None
            if self.enable_beam_pruning and beam_ids:
                filtered_node_ids = self._filter_block_nodes(block, tree_id, beam_ids)
                if filtered_node_ids and len(filtered_node_ids) < len(block.node_ids):
                    nodes_pruned += len(block.node_ids) - len(filtered_node_ids)
                    filtered_content = self._generate_filtered_content(tree_id, filtered_node_ids)

            # Process single vertical block
            result, llm_called, cache_hit = self._process_block(
                block, query, beams, selected, k, filtered_node_ids, filtered_content
            )

            total_llm_calls += llm_called
            cache_hits += 1 if cache_hit else 0
            blocks_processed += 1

            # Update beams
            beams = self._update_beams(result.ranked_node_ids, tree_id, beam_size)

            # Update selected
            for node_id in result.selected_node_ids:
                if node_id not in selected:
                    selected.append(node_id)

            # Track actual candidates processed
            actual_candidates = len(filtered_node_ids) if filtered_node_ids is not None else len(block.node_ids)

            block_traces.append(
                {
                    "type": "vertical",
                    "block_id": block.block_id,
                    "depth_range": f"{block.depth_start}-{block.depth_end}",
                    "result_count": len(result.ranked_node_ids),
                    "cache_hit": cache_hit,
                    "original_nodes": len(block.node_ids),
                    "filtered_nodes": actual_candidates,
                }
            )

            trace.append(
                {
                    "turn": len(trace),
                    "block_id": block.block_id,
                    "candidates": actual_candidates,
                    "original_candidates": len(block.node_ids),
                    "kept": len(result.ranked_node_ids),
                    "done": result.done,
                }
            )

            if result.done:
                log.debug("Block %s signaled done", block.block_id)
                break

            block_index += 1

        # Gather final results
        contents = self._gather_contents(tree_id, selected)

        log.debug(
            "BlockRetriever complete: %d nodes, %d blocks, %d LLM calls, %d cache hits, %d nodes pruned",
            len(selected),
            blocks_processed,
            total_llm_calls,
            cache_hits,
            nodes_pruned,
        )

        return BlockRetrievalResult(
            nodes=selected,
            contents=contents,
            trace=trace,
            turns=len(trace),
            blocks_processed=blocks_processed,
            horizontal_groups_processed=horizontal_groups_processed,
            total_llm_calls=total_llm_calls,
            cache_hits=cache_hits,
            block_traces=block_traces,
            nodes_pruned=nodes_pruned,
        )

    def _get_or_create_plan(self, tree_id: str) -> BlockTreePlan:
        """Get cached plan or create new one."""
        if tree_id not in self._plan_cache:
            self._plan_cache[tree_id] = self.block_cutter.cut_tree(tree_id)
        return self._plan_cache[tree_id]

    def _process_block(
        self,
        block: Block,
        query: str,
        beams: list[dict],
        selected: list[str],
        k: int,
        filtered_node_ids: list[str] = None,
        filtered_content: str = None,
    ) -> tuple[BlockResult, int, bool]:
        """
        Process a single block with LLM.
        Returns (BlockResult, llm_calls, cache_hit).

        Uses Anthropic prompt caching: Block content is static and cached,
        only query/beams/selected change between calls.

        Args:
            filtered_node_ids: If provided, only consider these nodes (beam pruning)
            filtered_content: If provided, use this content instead of block.cached_content
        """
        # Use filtered nodes if provided
        node_ids = filtered_node_ids if filtered_node_ids is not None else block.node_ids
        content = filtered_content if filtered_content is not None else block.cached_content

        # If no nodes to process, return empty result
        if not node_ids:
            log.debug("Block %s has no relevant nodes after filtering", block.block_id)
            return (
                BlockResult(
                    block_id=block.block_id,
                    ranked_node_ids=[],
                    selected_node_ids=[],
                    done=False,
                ),
                0,
                False,
            )

        # Check application-level cache first (for exact same query + block)
        # Include filtered node count in cache key for beam pruning
        cache_key = None
        content_hash = block.content_hash if filtered_content is None else hash(content)
        if self.prefix_cache and content_hash:
            cache_key = f"{content_hash}:{hash(query)}:{len(node_ids)}"
            cached_result = self.prefix_cache.get(cache_key)
            if cached_result:
                log.debug("App cache hit for block %s", block.block_id)
                return (
                    BlockResult(
                        block_id=block.block_id,
                        ranked_node_ids=cached_result["ranked_node_ids"],
                        selected_node_ids=cached_result["selected_node_ids"],
                        done=cached_result["done"],
                    ),
                    0,
                    True,
                )

        # Build static cache content (Block nodes - this gets cached by Anthropic)
        cache_content = BLOCK_CONTENT_PREFIX + content

        # Build dynamic prompt (changes per query)
        dynamic_prompt = BLOCK_PROMPT.render(
            query=query,
            selected=selected,
            input_beams=beams,
            k=k,
        )

        # Call LLM with cache support
        # cache_content is static (cacheable), dynamic_prompt changes per query
        if hasattr(self.llm, "chat_with_cache"):
            resp = self.llm.chat_with_cache(
                [{"role": "user", "content": dynamic_prompt}],
                tools=TOOLS,
                cache_content=cache_content,
            )
            # Log cache metrics if available
            usage = resp.get("usage", {})
            if usage.get("cache_read_input_tokens"):
                log.debug(
                    "Block %s: cache_read=%d tokens",
                    block.block_id,
                    usage["cache_read_input_tokens"],
                )
        else:
            # Fallback for LLMs without cache support
            full_prompt = cache_content + "\n\n" + dynamic_prompt
            resp = self.llm.chat([{"role": "user", "content": full_prompt}], tools=TOOLS)

        # Parse response - use filtered node_ids for validation
        ranked_ids, done = self._parse_llm_response(resp, node_ids)

        result = BlockResult(
            block_id=block.block_id,
            ranked_node_ids=ranked_ids,
            selected_node_ids=ranked_ids[: max(1, k)],
            done=done,
            usage=resp.get("usage"),
        )

        # Cache result at application level
        if self.prefix_cache and cache_key:
            self.prefix_cache.set(
                cache_key,
                {
                    "ranked_node_ids": result.ranked_node_ids,
                    "selected_node_ids": result.selected_node_ids,
                    "done": result.done,
                },
            )

        return result, 1, False

    def _process_horizontal_group(
        self,
        group: HorizontalBlockGroup,
        query: str,
        beams: list[dict],
        selected: list[str],
        k: int,
        tree_id: str = None,
        beam_ids: set[str] = None,
    ) -> tuple[BlockResult, int, int, int]:
        """
        Process horizontal block group using Map-Reduce.

        Map: Process each block (sequentially or in parallel)
        Reduce: Merge results from all blocks

        Returns (merged_result, total_llm_calls, cache_hits, nodes_pruned).
        """
        # MAP phase: process each block
        block_results: list[BlockResult] = []
        total_calls = 0
        cache_hits = 0
        nodes_pruned = 0

        # Prepare filtered data for each block if beam pruning is enabled
        block_filter_data: dict[str, tuple[list[str], str]] = {}
        if self.enable_beam_pruning and beam_ids and tree_id:
            for block in group.blocks:
                filtered_node_ids = self._filter_block_nodes(block, tree_id, beam_ids)
                if filtered_node_ids is not None and len(filtered_node_ids) < len(block.node_ids):
                    nodes_pruned += len(block.node_ids) - len(filtered_node_ids)
                    if filtered_node_ids:
                        filtered_content = self._generate_filtered_content(tree_id, filtered_node_ids)
                        block_filter_data[block.block_id] = (filtered_node_ids, filtered_content)
                    else:
                        block_filter_data[block.block_id] = ([], "")

        def process_block_with_filter(block: Block):
            if block.block_id in block_filter_data:
                f_ids, f_content = block_filter_data[block.block_id]
                return self._process_block(block, query, beams, selected, k, f_ids, f_content)
            return self._process_block(block, query, beams, selected, k)

        if self.parallel_horizontal:
            # Parallel processing with concurrent.futures
            import concurrent.futures

            max_workers = min(self.max_parallel_workers, len(group.blocks))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(process_block_with_filter, block): block
                    for block in group.blocks
                }
                for future in concurrent.futures.as_completed(futures):
                    result, calls, hit = future.result()
                    block_results.append(result)
                    total_calls += calls
                    cache_hits += 1 if hit else 0
        else:
            # Sequential processing
            for block in group.blocks:
                result, calls, hit = process_block_with_filter(block)
                block_results.append(result)
                total_calls += calls
                cache_hits += 1 if hit else 0

        # REDUCE phase: merge results
        merged_result = self._reduce_horizontal_results(block_results, group, query, k)

        return merged_result, total_calls, cache_hits, nodes_pruned

    def _reduce_horizontal_results(
        self,
        results: list[BlockResult],
        group: HorizontalBlockGroup,
        query: str,
        k: int,
    ) -> BlockResult:
        """
        Reduce/merge results from horizontal block processing.

        Strategy: Re-rank combined top candidates with another LLM call
        if total candidates exceed threshold, otherwise just combine.
        """
        # Collect all ranked candidates
        all_ranked: list[str] = []
        for result in results:
            all_ranked.extend(result.ranked_node_ids)

        # Check if any block signaled done
        any_done = any(r.done for r in results)

        # If total within budget, no need for extra ranking
        if len(all_ranked) <= self.reduce_rerank_threshold:
            return BlockResult(
                block_id=group.group_id,
                ranked_node_ids=all_ranked[:k] if k else all_ranked,
                selected_node_ids=all_ranked[: max(1, k // max(1, len(results)))],
                done=any_done,
            )

        # Need to re-rank: get top candidates from each block
        top_per_block = max(1, k // max(1, len(results)))
        candidates_for_rerank = []
        for result in results:
            candidates_for_rerank.extend(result.ranked_node_ids[: top_per_block * 2])

        # Deduplicate while preserving order
        seen = set()
        unique_candidates = []
        for c in candidates_for_rerank:
            if c not in seen:
                seen.add(c)
                unique_candidates.append(c)

        # Build minimal reduce prompt
        reduce_prompt = f"""
Re-rank these candidates for: {query}

Candidates (from multiple parallel searches):
{', '.join(unique_candidates[:k*3])}

Return top {k} in order of relevance.
"""

        resp = self.llm.chat([{"role": "user", "content": reduce_prompt}], tools=TOOLS)
        final_ranked, done = self._parse_llm_response(resp, unique_candidates)

        return BlockResult(
            block_id=group.group_id,
            ranked_node_ids=final_ranked,
            selected_node_ids=final_ranked[: max(1, k)],
            done=done or any_done,
        )

    def _parse_llm_response(self, resp: dict, valid_node_ids: list[str]) -> tuple[list[str], bool]:
        """Parse LLM response to extract ranked ids and done flag."""
        valid_set = set(valid_node_ids)

        for block in resp.get("content", []):
            if block.get("type") != "tool_use":
                continue
            if block.get("name") != "rank":
                continue

            ranked_ids = block.get("input", {}).get("selected", []) or []
            done = bool(block.get("input", {}).get("done", False))

            # Filter to valid node ids
            ranked = [nid for nid in ranked_ids if nid in valid_set]
            return ranked, done

        raise ValueError("LLM did not return a rank tool call")

    def _update_beams(
        self,
        ranked_ids: list[str],
        tree_id: str,
        beam_size: Optional[int],
    ) -> list[dict]:
        """Update beam set based on ranked results."""
        new_beams = []

        for node_id in ranked_ids:
            node = self.storage.get_node(tree_id, node_id)

            # Build beam info
            attrs = {}
            if node and node.attrs_json:
                try:
                    attrs = json.loads(node.attrs_json)
                except json.JSONDecodeError:
                    pass

            beam = {
                "node_id": node_id,
                "title": attrs.get("title", ""),
                "path": node.path if node else "",
            }
            new_beams.append(beam)

            if beam_size and len(new_beams) >= beam_size:
                break

        return new_beams if new_beams else [{"node_id": "", "title": "", "path": ""}]

    def _gather_contents(self, tree_id: str, selected: list[str]) -> list[dict[str, Any]]:
        """Gather entity contents for selected nodes."""
        contents = []
        for node_id in selected:
            entity = self.storage.get_entity(tree_id, node_id)
            if entity:
                payload = json.loads(entity.payload_json)
                contents.append({"node_id": node_id, "content": payload})
        return contents

    def _empty_result(self) -> BlockRetrievalResult:
        """Return empty result."""
        return BlockRetrievalResult(
            nodes=[],
            contents=[],
            trace=[],
            turns=0,
            blocks_processed=0,
            horizontal_groups_processed=0,
            total_llm_calls=0,
            cache_hits=0,
            block_traces=[],
        )

    def clear_cache(self):
        """Clear all caches."""
        self._plan_cache.clear()
        if self.prefix_cache:
            self.prefix_cache.clear()

    def clear_plan_cache(self, tree_id: str = None):
        """Clear cached block plans."""
        if tree_id:
            self._plan_cache.pop(tree_id, None)
        else:
            self._plan_cache.clear()

    def clear_prefix_cache(self):
        """Clear prefix cache."""
        if self.prefix_cache:
            self.prefix_cache.clear()

    def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        return {
            "plan_cache_size": len(self._plan_cache),
            "prefix_cache": self.prefix_cache.stats() if self.prefix_cache else None,
        }

    def _get_llm_max_concurrent(self, llm) -> int:
        """Get LLM's max_concurrent limit from config."""
        # Try to get provider and model from LLM client
        provider = getattr(llm, "provider", None)
        model = getattr(llm, "model", None)

        if provider and model:
            try:
                llm_config = get_llm_config(provider, model)
                return llm_config.get("max_concurrent", 10)
            except Exception as e:
                log.debug("Failed to get LLM config for %s/%s: %s", provider, model, e)

        return 10

    def _get_children_of(self, tree_id: str, parent_ids: set[str]) -> set[str]:
        """Get all direct children of the given parent nodes."""
        if not parent_ids:
            return set()

        cursor = self.storage.conn.cursor()
        placeholders = ",".join("?" * len(parent_ids))
        cursor.execute(
            f"""
            SELECT node_id FROM nodes
            WHERE tree_id = ? AND parent_id IN ({placeholders})
            """,
            (tree_id, *parent_ids),
        )
        return {row[0] for row in cursor.fetchall()}

    def _filter_block_nodes(
        self,
        block: Block,
        tree_id: str,
        beam_ids: set[str],
    ) -> list[str]:
        """
        Filter block nodes to only include children of current beams.

        This implements beam-guided pruning: after selecting beams at depth N,
        only their children at depth N+1 are relevant.

        Returns filtered node_ids list.
        """
        if not beam_ids or not self.enable_beam_pruning:
            return block.node_ids

        # For root block (depth 0), no filtering
        if block.depth_start == 0:
            return block.node_ids

        # Get all children of beam nodes
        children = self._get_children_of(tree_id, beam_ids)

        # Filter block nodes
        filtered = [nid for nid in block.node_ids if nid in children]

        if filtered:
            log.debug(
                "Beam pruning: block %s filtered from %d to %d nodes (%.1f%% pruned)",
                block.block_id,
                len(block.node_ids),
                len(filtered),
                (1 - len(filtered) / len(block.node_ids)) * 100,
            )

        return filtered

    def _generate_filtered_content(self, tree_id: str, node_ids: list[str]) -> str:
        """Generate block content for filtered nodes."""
        lines = []
        cursor = self.storage.conn.cursor()

        placeholders = ",".join("?" * len(node_ids))
        cursor.execute(
            f"""
            SELECT n.node_id, n.depth, n.attrs_json, e.payload_json
            FROM nodes n
            LEFT JOIN entities e ON n.entity_id = e.entity_id
            WHERE n.tree_id = ? AND n.node_id IN ({placeholders})
            ORDER BY n.path
            """,
            (tree_id, *node_ids),
        )

        for row in cursor.fetchall():
            node_id, depth, attrs_json, payload_json = row

            attrs = {}
            if attrs_json:
                try:
                    attrs = json.loads(attrs_json)
                except json.JSONDecodeError:
                    pass

            payload = {}
            if payload_json:
                try:
                    payload = json.loads(payload_json)
                except json.JSONDecodeError:
                    pass

            lines.append(f"- id: {node_id}")
            if attrs.get("title"):
                lines.append(f"  title: {attrs['title']}")
            if attrs.get("summary"):
                lines.append(f"  summary: {attrs['summary']}")

            text = payload.get("text") or payload.get("content") or ""
            if text:
                lines.append(f"  text: {text[:200]}")

            lines.append(f"  depth: {depth}")

            # Add page range if available
            page_start = attrs.get("page_start")
            page_end = attrs.get("page_end")
            if page_start is not None or page_end is not None:
                lines.append(f"  range: {page_start}-{page_end}")

        return "\n".join(lines)
