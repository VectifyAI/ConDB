"""Adaptive depth-jumping retriever with dynamic layer merging."""

import json
from pathlib import Path
from typing import Any, Optional

from jinja2 import Template

from contextdb.config import get_llm_config, get_retriever_config
from contextdb.logger import get_logger
from contextdb.retriever.algorithm.base_retriever import BaseRetriever
from contextdb.retriever.algorithm.block_types import BlockRetrievalResult
from contextdb.utils.prefix_cache import PrefixCache
from contextdb.utils.token_counter import TokenCounter

log = get_logger(__name__)

# Load default config (fallback to block config if adaptive not found)
try:
    _DEFAULT_CONFIG = get_retriever_config("adaptive")
except Exception as e:
    log.debug("Failed to load adaptive config, using block config: %s", e)
    _DEFAULT_CONFIG = get_retriever_config("block")

# Prompt template for adaptive retriever
ADAPTIVE_PROMPT = Template("""
You are ranking tree nodes to answer a user question.

Query: {{ query }}

Selected so far:
{% if selected %}
{{ selected }}
{% else %}
(none)
{% endif %}

{% if input_beams %}
Input beams (context from previous depth):
{% for beam in input_beams %}
- {{ beam.node_id }}: {{ beam.title }} (depth={{ beam.depth }})
{% endfor %}
{% endif %}

Pick up to {{ k }} candidates from the NODES above, best first.

Return ONE tool call "rank" with:
- selected: list of ids in best-to-worst order
- done: true ONLY if you've reached leaf nodes or content is specific enough to answer
""")

# Static prefix for node content
NODE_CONTENT_PREFIX = """=== DOCUMENT TREE NODES ===
The following are candidate nodes from multiple depths.
Each node has an id, title, summary, and depth.

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


class AdaptiveRetriever(BaseRetriever):
    """Adaptive depth-jumping retriever that dynamically merges multiple depths into one LLM call."""

    def __init__(
        self,
        storage,
        llm,
        max_tokens_per_call: int = None,
        fill_threshold: float = None,
        cache_enabled: bool = None,
        enable_beam_pruning: bool = None,
        max_pregenerated_depth: int = None,
    ):
        super().__init__(storage, llm)

        # Load from config, override with explicit parameters
        self.max_tokens_per_call = (
            max_tokens_per_call if max_tokens_per_call is not None
            else _DEFAULT_CONFIG.get("max_tokens_per_call", 16000)
        )
        self.fill_threshold = (
            fill_threshold if fill_threshold is not None
            else _DEFAULT_CONFIG.get("fill_threshold", 0.7)
        )
        # Hybrid caching: only pregenerate content for depths <= this value
        # Deeper depths use filtered content (reduces cache_read tokens)
        self.max_pregenerated_depth = (
            max_pregenerated_depth if max_pregenerated_depth is not None
            else _DEFAULT_CONFIG.get("max_pregenerated_depth", 1)
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

        # Cache for depth info
        self._depth_cache: dict[str, dict] = {}
        # Cache for pre-generated depth content (for Anthropic prompt caching)
        self._depth_content_cache: dict[str, dict[int, str]] = {}

    def retrieve(
        self,
        tree_id: str,
        query: str,
        beam_size: int = None,
        max_turns: int = None,
        select_k: int = 1,
    ) -> BlockRetrievalResult:
        """
        Run adaptive depth-jumping search on a tree.

        Args:
            tree_id: ID of the tree to search
            query: Search query
            beam_size: Number of beams to keep (None = keep all)
            max_turns: Maximum LLM calls (None = auto)
            select_k: Top-k to select per turn

        Returns:
            BlockRetrievalResult with nodes, contents, and metrics
        """
        root_id = self.storage.get_root_id(tree_id)
        if not root_id:
            return self._empty_result()

        # Clear token counter cache for this retrieval
        self.token_counter.clear_cache()

        # Clear depth cache when token cache is cleared (tokens need recomputing)
        self._depth_cache.pop(tree_id, None)

        # Precompute tokens for the tree
        self.token_counter.precompute_tree_tokens(self.storage, tree_id)

        # Get tree depth info
        depth_info = self._get_depth_info(tree_id)
        max_depth = depth_info["max_depth"]

        # Pre-generate depth content for caching
        self._pregenerate_depth_content(tree_id, depth_info)

        log.debug(
            "AdaptiveRetriever: tree %s has max_depth=%d, depths=%s",
            tree_id[:8],
            max_depth,
            {d: len(nodes) for d, nodes in depth_info["nodes_by_depth"].items()},
        )

        # Initialize beam search state
        beams = [{"node_id": root_id, "title": "root", "path": "root", "depth": 0}]
        selected: list[str] = []
        trace: list[dict[str, Any]] = []

        total_llm_calls = 0
        cache_hits = 0
        nodes_pruned = 0
        depths_processed = 0

        k = beam_size if beam_size else select_k
        max_llm_calls = max_turns if max_turns else 10  # Default max

        current_depth = 0

        while current_depth <= max_depth and total_llm_calls < max_llm_calls:
            # Early termination: check if beams have no children
            if current_depth > 0 and beams:
                has_children = False
                for beam in beams:
                    if beam["node_id"] and self.storage.get_children(tree_id, beam["node_id"]):
                        has_children = True
                        break
                if not has_children:
                    log.debug("All beams are leaves, stopping at depth %d", current_depth)
                    break

            # Determine depths to process in this call
            depths_to_process, accumulated_tokens = self._plan_depth_jump(
                tree_id, current_depth, max_depth, depth_info, beams
            )

            if not depths_to_process:
                break

            log.debug(
                "Adaptive: processing depths %s (%d tokens)",
                depths_to_process, accumulated_tokens
            )

            # Get nodes for these depths
            beam_ids = {b["node_id"] for b in beams if b["node_id"]}
            nodes_to_process, pruned = self._get_nodes_for_depths(
                tree_id, depths_to_process, beam_ids, depth_info
            )
            nodes_pruned += pruned

            if not nodes_to_process:
                current_depth = depths_to_process[-1] + 1
                continue

            # Process with LLM
            result, llm_called, cache_hit = self._process_nodes(
                tree_id, nodes_to_process, query, beams, selected, k,
                depths=depths_to_process
            )

            total_llm_calls += llm_called
            cache_hits += 1 if cache_hit else 0
            depths_processed += len(depths_to_process)

            # Update beams
            beams = self._update_beams(result["ranked_ids"], tree_id, beam_size)

            # Update selected
            for node_id in result["selected_ids"]:
                if node_id not in selected:
                    selected.append(node_id)

            trace.append({
                "turn": len(trace),
                "depths": depths_to_process,
                "candidates": len(nodes_to_process),
                "kept": len(result["ranked_ids"]),
                "done": result["done"],
            })

            if result["done"]:
                log.debug("LLM signaled done at depths %s", depths_to_process)
                break

            # Move to next depth
            current_depth = depths_to_process[-1] + 1

        # Gather final results
        contents = self._gather_contents(tree_id, selected)

        log.debug(
            "AdaptiveRetriever complete: %d nodes, %d LLM calls, %d depths, %d nodes pruned",
            len(selected),
            total_llm_calls,
            depths_processed,
            nodes_pruned,
        )

        return BlockRetrievalResult(
            nodes=selected,
            contents=contents,
            trace=trace,
            turns=len(trace),
            blocks_processed=depths_processed,  # Reuse field for depths
            horizontal_groups_processed=0,
            total_llm_calls=total_llm_calls,
            cache_hits=cache_hits,
            nodes_pruned=nodes_pruned,
            block_traces=[],
        )

    def _get_depth_info(self, tree_id: str) -> dict:
        """Get precomputed depth information for a tree."""
        if tree_id in self._depth_cache:
            return self._depth_cache[tree_id]

        cursor = self.storage.conn.cursor()
        cursor.execute(
            """
            SELECT node_id, parent_id, depth, attrs_json
            FROM nodes
            WHERE tree_id = ?
            ORDER BY depth, path
            """,
            (tree_id,),
        )

        nodes_by_depth: dict[int, list[dict]] = {}
        tokens_by_depth: dict[int, int] = {}
        max_depth = 0

        for row in cursor.fetchall():
            depth = row["depth"]
            max_depth = max(max_depth, depth)

            node_info = {
                "node_id": row["node_id"],
                "parent_id": row["parent_id"],
                "depth": depth,
                "attrs_json": row["attrs_json"],
            }

            if depth not in nodes_by_depth:
                nodes_by_depth[depth] = []
                tokens_by_depth[depth] = 0

            nodes_by_depth[depth].append(node_info)

            # Get token count
            tokens = self.token_counter.get_cached_count(row["node_id"]) or 0
            tokens_by_depth[depth] += tokens

        info = {
            "max_depth": max_depth,
            "nodes_by_depth": nodes_by_depth,
            "tokens_by_depth": tokens_by_depth,
        }

        self._depth_cache[tree_id] = info
        return info

    def _pregenerate_depth_content(self, tree_id: str, depth_info: dict) -> None:
        """
        Pre-generate content for shallow depths for stable caching.

        Hybrid strategy:
        - Depths <= max_pregenerated_depth: Pre-generate for cache stability
        - Depths > max_pregenerated_depth: Use filtered content (cheaper)

        This balances cache hit rate (shallow depths) vs token cost (deep depths).
        """
        if tree_id in self._depth_content_cache:
            return

        depth_content: dict[int, str] = {}

        for depth, nodes in depth_info["nodes_by_depth"].items():
            # Only pregenerate for shallow depths
            if depth > self.max_pregenerated_depth:
                log.debug("Skipping pregeneration for depth %d (> max_pregenerated_depth=%d)",
                         depth, self.max_pregenerated_depth)
                continue

            lines = []
            for n in nodes:
                enriched = self._enrich_node(tree_id, n)
                attrs = enriched.get("attrs", {})
                payload = enriched.get("payload", {})

                lines.append(f"- id: {n['node_id']}")
                lines.append(f"  parent: {n.get('parent_id', 'none')}")
                if attrs.get("title"):
                    lines.append(f"  title: {attrs['title']}")
                if attrs.get("summary"):
                    lines.append(f"  summary: {attrs['summary']}")

                text = payload.get("text") or payload.get("content") or ""
                if text:
                    lines.append(f"  text: {text[:200]}")

                lines.append(f"  depth: {depth}")

            depth_content[depth] = "\n".join(lines)

        self._depth_content_cache[tree_id] = depth_content
        log.debug("Pre-generated content for %d depths (max_pregenerated_depth=%d) in tree %s",
                 len(depth_content), self.max_pregenerated_depth, tree_id[:8])

    def _plan_depth_jump(
        self,
        tree_id: str,
        start_depth: int,
        max_depth: int,
        depth_info: dict,
        beams: list[dict],
    ) -> tuple[list[int], int]:
        """
        Plan which depths to process in one LLM call.

        Strategy:
        - Accumulate depths until token limit or fill threshold
        - For deeper levels, apply beam pruning estimate
        - Simulate cascading pruning: after each depth, update candidate beam_ids

        Returns (depths_to_process, estimated_tokens)
        """
        depths = []
        accumulated_tokens = 0
        beam_ids = {b["node_id"] for b in beams if b["node_id"]}

        for depth in range(start_depth, max_depth + 1):
            # Estimate tokens for this depth
            depth_tokens = depth_info["tokens_by_depth"].get(depth, 0)
            depth_nodes = depth_info["nodes_by_depth"].get(depth, [])

            if depth == 0:
                # Root level - no pruning, include all
                estimated_node_count = len(depth_nodes)
                # Update beam_ids to all depth-0 nodes for next depth estimation
                next_beam_ids = {n["node_id"] for n in depth_nodes}
            elif self.enable_beam_pruning and beam_ids:
                # Estimate: only children of beam nodes are relevant
                relevant_nodes = [n for n in depth_nodes if n["parent_id"] in beam_ids]
                estimated_node_count = len(relevant_nodes)
                if depth_nodes and estimated_node_count > 0:
                    prune_ratio = estimated_node_count / len(depth_nodes)
                    depth_tokens = int(depth_tokens * prune_ratio)
                else:
                    depth_tokens = 0
                # Update beam_ids to relevant nodes for next depth estimation
                next_beam_ids = {n["node_id"] for n in relevant_nodes}
            else:
                # No pruning
                estimated_node_count = len(depth_nodes)
                next_beam_ids = {n["node_id"] for n in depth_nodes}

            # Check if adding this depth exceeds limit
            if accumulated_tokens + depth_tokens > self.max_tokens_per_call and depths:
                break

            # Only add depth if there are nodes to process
            if estimated_node_count > 0 or depth == start_depth:
                depths.append(depth)
                accumulated_tokens += depth_tokens

            # Update beam_ids for next depth
            beam_ids = next_beam_ids

            # Check fill threshold
            if accumulated_tokens >= self.max_tokens_per_call * self.fill_threshold:
                break

        return depths, accumulated_tokens

    def _get_nodes_for_depths(
        self,
        tree_id: str,
        depths: list[int],
        beam_ids: set[str],
        depth_info: dict,
    ) -> tuple[list[dict], int]:
        """
        Get nodes for specified depths with beam pruning.

        Beam pruning logic:
        - beam_ids contains the node IDs selected by LLM in previous turn
        - For each depth, only include nodes whose parent is in beam_ids
        - After processing each depth, update beam_ids to current depth's included nodes
          (so that next depth can use them for pruning)

        Returns (nodes_list, nodes_pruned)
        """
        nodes = []
        nodes_pruned = 0

        # Track which nodes we've included at each depth for cascading pruning
        included_at_current_depth = set()

        for depth in depths:
            depth_nodes = depth_info["nodes_by_depth"].get(depth, [])

            if depth == 0:
                # Root level - no pruning, include all
                for n in depth_nodes:
                    nodes.append(self._enrich_node(tree_id, n))
                    included_at_current_depth.add(n["node_id"])
            elif self.enable_beam_pruning and beam_ids:
                # Apply beam pruning: only include nodes whose parent is a beam
                included_at_current_depth = set()
                for n in depth_nodes:
                    if n["parent_id"] in beam_ids:
                        nodes.append(self._enrich_node(tree_id, n))
                        included_at_current_depth.add(n["node_id"])
                    else:
                        nodes_pruned += 1
            else:
                # No pruning - include all nodes at this depth
                included_at_current_depth = set()
                for n in depth_nodes:
                    nodes.append(self._enrich_node(tree_id, n))
                    included_at_current_depth.add(n["node_id"])

            # Update beam_ids for next depth:
            # Next depth should only include children of nodes we included at current depth
            if self.enable_beam_pruning and included_at_current_depth:
                beam_ids = included_at_current_depth

        return nodes, nodes_pruned

    def _enrich_node(self, tree_id: str, node_info: dict) -> dict:
        """Add entity data to node info."""
        node_id = node_info["node_id"]

        # Get entity payload
        entity = self.storage.get_entity(tree_id, node_id)
        payload = {}
        if entity and entity.payload_json:
            try:
                payload = json.loads(entity.payload_json)
            except json.JSONDecodeError:
                pass

        # Parse attrs
        attrs = {}
        if node_info.get("attrs_json"):
            try:
                attrs = json.loads(node_info["attrs_json"])
            except json.JSONDecodeError:
                pass

        return {
            **node_info,
            "attrs": attrs,
            "payload": payload,
        }

    def _process_nodes(
        self,
        tree_id: str,
        nodes: list[dict],
        query: str,
        beams: list[dict],
        selected: list[str],
        k: int,
        depths: list[int] = None,
    ) -> tuple[dict, int, bool]:
        """
        Process nodes with LLM.

        Uses pre-generated depth content for stable caching when content fits
        within token limits. Falls back to filtered content when pre-generated
        content would exceed the limit.

        Returns (result_dict, llm_calls, cache_hit)
        """
        node_ids = [n["node_id"] for n in nodes]
        node_id_set = set(node_ids)

        # Hybrid caching strategy:
        # - Use pre-generated content for shallow depths (for cache stability)
        # - Use filtered content for deep depths (for cost efficiency)
        # - Fall back to filtered content if pre-generated would exceed token limit
        pregenerated_depths = []
        filtered_depths = []

        # Max tokens for LLM context (leave room for prompt and response)
        max_context_tokens = 180000  # Conservative limit for 200K context window

        if depths and tree_id in self._depth_content_cache:
            # Estimate pre-generated content size
            pregen_tokens = 0
            for depth in depths:
                if depth in self._depth_content_cache[tree_id]:
                    depth_content = self._depth_content_cache[tree_id][depth]
                    depth_tokens = self.token_counter.count_text_tokens(depth_content)
                    if pregen_tokens + depth_tokens <= max_context_tokens:
                        pregenerated_depths.append(depth)
                        pregen_tokens += depth_tokens
                    else:
                        # Would exceed limit, use filtered instead
                        log.debug("Depth %d pre-gen content (%d tokens) would exceed limit, using filtered",
                                 depth, depth_tokens)
                        filtered_depths.append(depth)
                else:
                    filtered_depths.append(depth)
        else:
            filtered_depths = depths if depths else []

        # Build cache content
        cache_content_parts = [NODE_CONTENT_PREFIX]

        # Add pre-generated content for shallow depths
        if pregenerated_depths:
            for depth in sorted(pregenerated_depths):
                cache_content_parts.append(f"=== DEPTH {depth} ===\n{self._depth_content_cache[tree_id][depth]}")

        # Add filtered content for deep depths
        filtered_nodes = [n for n in nodes if n.get("depth", 0) in filtered_depths]
        if filtered_nodes:
            filtered_content = self._generate_content(filtered_nodes)
            if filtered_content:
                cache_content_parts.append(f"=== DEPTHS {filtered_depths} (filtered) ===\n{filtered_content}")

        cache_content = "\n\n".join(cache_content_parts)

        # Cache key: stable part (pregenerated depths) + variable part (filtered node ids)
        if pregenerated_depths:
            stable_key = f"{tree_id}:{tuple(sorted(pregenerated_depths))}"
        else:
            stable_key = tree_id
        filtered_node_ids = tuple(sorted(n["node_id"] for n in filtered_nodes))
        cache_content_key = f"{stable_key}:{hash(filtered_node_ids)}"

        log.debug("Hybrid caching: pregenerated depths=%s, filtered depths=%s (%d nodes)",
                 pregenerated_depths, filtered_depths, len(filtered_nodes))

        # Check app-level cache
        cache_key = None
        if self.prefix_cache:
            # Include node_ids in cache key since results depend on active nodes
            cache_key = f"{cache_content_key}:{hash(query)}:{hash(tuple(sorted(node_ids)))}"
            cached_result = self.prefix_cache.get(cache_key)
            if cached_result:
                log.debug("App cache hit for %d nodes", len(nodes))
                return cached_result, 0, True

        # Build dynamic prompt with active node indication
        active_node_ids_str = ", ".join(node_ids) if len(node_ids) <= 20 else f"{len(node_ids)} nodes"
        dynamic_prompt = ADAPTIVE_PROMPT.render(
            query=query,
            selected=selected,
            input_beams=beams,
            k=k,
        )
        # Add active nodes hint when using pre-generated content (which includes pruned nodes)
        if pregenerated_depths:
            dynamic_prompt = f"ACTIVE CANDIDATES (only rank these): {active_node_ids_str}\n\n" + dynamic_prompt

        # Final safety check: ensure total content doesn't exceed limit
        total_content = cache_content + "\n\n" + dynamic_prompt
        total_tokens = self.token_counter.count_text_tokens(total_content)
        if total_tokens > max_context_tokens:
            log.warning(
                "Total content (%d tokens) exceeds limit (%d), falling back to filtered-only mode",
                total_tokens, max_context_tokens
            )
            # Rebuild with filtered content only
            filtered_content = self._generate_content(nodes)
            cache_content = NODE_CONTENT_PREFIX + f"=== FILTERED NODES ===\n{filtered_content}"
            # Remove pregenerated depths from cache key
            pregenerated_depths = []
            cache_content_key = f"{tree_id}:{hash(tuple(sorted(node_ids)))}"

            # Re-check after fallback
            total_content = cache_content + "\n\n" + dynamic_prompt
            total_tokens = self.token_counter.count_text_tokens(total_content)
            if total_tokens > max_context_tokens:
                # Still too large - need to truncate content
                log.warning(
                    "Filtered content still exceeds limit (%d > %d), truncating",
                    total_tokens, max_context_tokens
                )
                # Estimate how much to keep (leave 20K for prompt overhead)
                available_tokens = max_context_tokens - 20000
                # chars per token estimate is ~4
                max_chars = available_tokens * 4
                if len(cache_content) > max_chars:
                    cache_content = cache_content[:max_chars] + "\n... [truncated due to size limit]"

        # Call LLM with cache support
        if hasattr(self.llm, "chat_with_cache"):
            resp = self.llm.chat_with_cache(
                [{"role": "user", "content": dynamic_prompt}],
                tools=TOOLS,
                cache_content=cache_content,
            )
        else:
            full_prompt = cache_content + "\n\n" + dynamic_prompt
            resp = self.llm.chat([{"role": "user", "content": full_prompt}], tools=TOOLS)

        # Parse response
        ranked_ids, done = self._parse_llm_response(resp, node_ids)

        result = {
            "ranked_ids": ranked_ids,
            "selected_ids": ranked_ids[: max(1, k)],
            "done": done,
        }

        # Cache result
        if self.prefix_cache and cache_key:
            self.prefix_cache.set(cache_key, result)

        return result, 1, False

    def _generate_content(self, nodes: list[dict]) -> str:
        """Generate content string for nodes."""
        lines = []
        for node in nodes:
            attrs = node.get("attrs", {})
            payload = node.get("payload", {})

            lines.append(f"- id: {node['node_id']}")
            lines.append(f"  parent: {node.get('parent_id', 'none')}")
            if attrs.get("title"):
                lines.append(f"  title: {attrs['title']}")
            if attrs.get("summary"):
                lines.append(f"  summary: {attrs['summary']}")

            text = payload.get("text") or payload.get("content") or ""
            if text:
                lines.append(f"  text: {text[:200]}")

            lines.append(f"  depth: {node.get('depth', 0)}")

        return "\n".join(lines)

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
                "depth": node.depth if node else 0,
            }
            new_beams.append(beam)

            if beam_size and len(new_beams) >= beam_size:
                break

        return new_beams if new_beams else [{"node_id": "", "title": "", "path": "", "depth": 0}]

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
        self._depth_cache.clear()
        self._depth_content_cache.clear()
        if self.prefix_cache:
            self.prefix_cache.clear()

    def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        return {
            "depth_cache_size": len(self._depth_cache),
            "depth_content_cache_size": len(self._depth_content_cache),
            "prefix_cache": self.prefix_cache.stats() if self.prefix_cache else None,
        }
