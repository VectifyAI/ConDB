"""Block-level beam search retriever with fixed-block prefix caching."""

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from jinja2 import Template

from contextdb.config import get_retriever_config
from contextdb.logger import get_logger
from contextdb.retriever.algorithm.base_retriever import BaseRetriever
from contextdb.retriever.algorithm.block_cutter import BlockCutter
from contextdb.retriever.algorithm.block_types import (
    Block,
    BlockResult,
    BlockRetrievalResult,
    BlockTreePlan,
    BlockType,
)
from contextdb.utils.token_counter import TokenCounter

log = get_logger(__name__)

_DEFAULT_CONFIG = get_retriever_config("block")

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
BLOCK_PROMPT = Template((_PROMPTS_DIR / "block.jinja").read_text(encoding="utf-8"))
BLOCK_CACHE_PREFIX_PROMPT = Template((_PROMPTS_DIR / "block_cache_prefix.jinja").read_text(encoding="utf-8"))
BLOCK_FS_PROMPT = Template((_PROMPTS_DIR / "block_fs.jinja").read_text(encoding="utf-8"))
BLOCK_FS_CACHE_PREFIX_PROMPT = Template((_PROMPTS_DIR / "block_fs_cache_prefix.jinja").read_text(encoding="utf-8"))

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

    def __init__(
        self,
        storage,
        llm,
        max_tokens_per_block: int = None,
        mode: str = "document",
    ):
        super().__init__(storage, llm)
        self.mode = mode

        self.max_tokens_per_block = (
            max_tokens_per_block if max_tokens_per_block is not None
            else _DEFAULT_CONFIG.get("max_tokens_per_block", 16000)
        )
        self.min_tokens_per_block = _DEFAULT_CONFIG.get("min_tokens_per_block", 0)

        provider = getattr(llm, "provider", None)
        model = getattr(llm, "model", None)
        self.token_counter = TokenCounter(provider=provider, model=model)
        self.block_cutter = BlockCutter(
            storage,
            self.token_counter,
            self.max_tokens_per_block,
            self.min_tokens_per_block,
        )
        self._plan_cache: dict[str, BlockTreePlan] = {}
        self._precomputed_tree_id: str = ""

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

        plan = self._get_or_create_plan(tree_id)
        if not plan.blocks:
            return self._empty_result()

        top_block = plan.blocks[0]

        # Regenerate top block content with fs-aware format
        top_nodes = self._get_nodes_by_ids(tree_id, top_block.node_ids)
        if top_nodes:
            fs_content = self._generate_fs_block_content(top_nodes)
            top_block = Block(
                block_id=top_block.block_id,
                block_type=top_block.block_type,
                tree_id=top_block.tree_id,
                depth_start=top_block.depth_start,
                depth_end=top_block.depth_end,
                node_ids=top_block.node_ids,
                total_tokens=top_block.total_tokens,
                max_tokens=top_block.max_tokens,
                cached_content=fs_content,
                content_hash=hashlib.md5(fs_content.encode()).hexdigest(),
            )

        beams = [{"node_id": root_id, "title": "root", "path": "root"}]
        selected: list[str] = []
        previous_selected: list[str] = []
        trace: list[dict[str, Any]] = []
        block_traces: list[dict[str, Any]] = []

        total_llm_calls = 0
        cache_read_tokens = 0
        cache_creation_tokens = 0
        blocks_processed = 0

        k = beam_size if beam_size else select_k
        max_calls = max_turns if max_turns else 20

        # Step 1: Process top block
        allowed_top = [nid for nid in top_block.node_ids if nid != root_id]
        if not allowed_top:
            allowed_top = top_block.node_ids

        result, llm_called, cache_metrics = self._process_block(
            block=top_block, query=query, input_beams=beams,
            previous_selected=previous_selected, allowed_node_ids=allowed_top, k=k,
        )
        total_llm_calls += llm_called
        cache_read_tokens += cache_metrics.get("cache_read_tokens", 0)
        cache_creation_tokens += cache_metrics.get("cache_creation_tokens", 0)
        blocks_processed += 1

        beams = self._update_beams(result.ranked_node_ids, tree_id, beam_size)
        for nid in result.selected_node_ids:
            if nid not in selected:
                selected.append(nid)
        previous_selected = list(result.selected_node_ids)

        block_traces.append({
            "type": "top", "block_id": top_block.block_id,
            "depth_range": f"{top_block.depth_start}-{top_block.depth_end}",
            "nodes": len(top_block.node_ids), "allowed": len(allowed_top),
        })
        trace.append({
            "turn": 0, "block_id": top_block.block_id,
            "candidates": len(allowed_top),
            "kept": len(result.ranked_node_ids), "done": result.done,
        })

        result = self._override_done_if_dirs(result, tree_id, beams)

        # Step 2: Iteratively process subtree blocks
        turn = 1
        while not result.done and total_llm_calls < max_calls:
            if not self._beams_have_children(tree_id, beams):
                break

            beam_ids = [b["node_id"] for b in beams if b["node_id"]]
            subtree_block = self._create_subtree_block_fs(tree_id, beam_ids)
            if subtree_block is None:
                break

            allowed_sub = [nid for nid in subtree_block.node_ids if nid not in set(beam_ids)]
            if not allowed_sub:
                allowed_sub = subtree_block.node_ids

            result, llm_called, cache_metrics = self._process_block(
                block=subtree_block, query=query, input_beams=beams,
                previous_selected=previous_selected, allowed_node_ids=allowed_sub, k=k,
            )
            total_llm_calls += llm_called
            cache_read_tokens += cache_metrics.get("cache_read_tokens", 0)
            cache_creation_tokens += cache_metrics.get("cache_creation_tokens", 0)
            blocks_processed += 1

            beams = self._update_beams(result.ranked_node_ids, tree_id, beam_size)
            for nid in result.selected_node_ids:
                if nid not in selected:
                    selected.append(nid)
            previous_selected = list(result.selected_node_ids)

            result = self._override_done_if_dirs(result, tree_id, beams)

            block_traces.append({
                "type": "subtree", "block_id": subtree_block.block_id,
                "nodes": len(subtree_block.node_ids),
                "allowed": len(allowed_sub),
                "tokens": subtree_block.total_tokens,
            })
            trace.append({
                "turn": turn, "block_id": subtree_block.block_id,
                "candidates": len(allowed_sub),
                "kept": len(result.ranked_node_ids), "done": result.done,
            })
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

    def _create_subtree_block_fs(self, tree_id: str, beam_node_ids: list[str]) -> Optional[Block]:
        """Create a subtree block from children of beam nodes."""
        all_children = []
        for nid in beam_node_ids:
            all_children.extend(self._get_direct_children_nodes(tree_id, nid))

        if not all_children:
            return None

        def _count(node):
            return self.token_counter.get_cached_count(node["node_id"]) or self.token_counter.count_node_tokens(node)

        total_tokens = sum(_count(c) for c in all_children)

        # If all children fit, use them all; otherwise pack greedily
        if total_tokens <= self.max_tokens_per_block:
            nodes_to_pack = all_children
        else:
            nodes_to_pack = []
            packed_tokens = 0
            for child in all_children:
                ct = _count(child)
                if packed_tokens + ct <= self.max_tokens_per_block:
                    nodes_to_pack.append(child)
                    packed_tokens += ct
                elif not nodes_to_pack:
                    nodes_to_pack.append(child)
                    break
            total_tokens = sum(_count(c) for c in nodes_to_pack)

        if not nodes_to_pack:
            return None

        content = self._generate_fs_block_content(nodes_to_pack)
        node_ids = [c["node_id"] for c in nodes_to_pack]
        depths = [c.get("depth", 0) for c in nodes_to_pack]
        block_id = f"fs_sub_{'_'.join(bid[:8] for bid in beam_node_ids[:3])}"
        return Block(
            block_id=block_id,
            block_type=BlockType.VERTICAL,
            tree_id=tree_id,
            depth_start=min(depths) if depths else 0,
            depth_end=max(depths) if depths else 0,
            node_ids=node_ids,
            total_tokens=total_tokens,
            max_tokens=self.max_tokens_per_block,
            cached_content=content,
            content_hash=hashlib.md5(content.encode()).hexdigest(),
        )

    def _generate_fs_block_content(self, nodes: list[dict]) -> str:
        """Generate fs-aware block content with rel_path and tags."""
        lines = []
        for node in nodes:
            attrs = node.get("attrs") or {}
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs)
                except json.JSONDecodeError:
                    attrs = {}

            nid = node["node_id"]
            rel_path = attrs.get("rel_path", "")
            is_dir = attrs.get("is_dir", False)
            tag = attrs.get("tag", "")

            lines.append(f"- id: {nid}")
            if rel_path:
                lines.append(f"  path: {rel_path}")
            if is_dir:
                lines.append(f"  type: directory")
            else:
                lines.append(f"  type: file")
            if tag:
                lines.append(f"  tag: {tag}")

            # Get summary from entity (directory listing)
            entity = node.get("entity", {})
            payload = entity.get("payload", {}) if isinstance(entity, dict) else {}
            summary = payload.get("summary", "")
            if summary:
                lines.append(f"  summary: {summary[:200]}")

        return "\n".join(lines)

    @staticmethod
    def _row_to_node_dict(row) -> dict:
        node = {
            "node_id": row["node_id"],
            "parent_id": row["parent_id"],
            "depth": row["depth"],
            "path": row["path"],
            "attrs_json": row["attrs_json"],
        }
        if row["attrs_json"]:
            node["attrs"] = json.loads(row["attrs_json"])
        if row["payload_json"]:
            node["entity"] = {"payload": json.loads(row["payload_json"])}
        return node

    _NODE_QUERY = """SELECT n.node_id, n.parent_id, n.depth, n.path, n.attrs_json, e.payload_json
                     FROM nodes n LEFT JOIN entities e ON n.entity_id = e.entity_id"""

    def _get_nodes_by_ids(self, tree_id: str, node_ids: list[str]) -> list[dict]:
        if not node_ids:
            return []
        cursor = self.storage.conn.cursor()
        results = []
        for i in range(0, len(node_ids), 500):
            chunk = node_ids[i:i + 500]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(
                f"{self._NODE_QUERY} WHERE n.tree_id = ? AND n.node_id IN ({placeholders}) ORDER BY n.path",
                (tree_id, *chunk),
            )
            results.extend(self._row_to_node_dict(row) for row in cursor.fetchall())
        return results

    def _get_direct_children_nodes(self, tree_id: str, node_id: str) -> list[dict]:
        cursor = self.storage.conn.cursor()
        cursor.execute(
            f"{self._NODE_QUERY} WHERE n.tree_id = ? AND n.parent_id = ? ORDER BY n.path",
            (tree_id, node_id),
        )
        return [self._row_to_node_dict(row) for row in cursor.fetchall()]

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

                group_results = self._process_horizontal_group(
                    group_blocks, tree_id, query, beams, previous_selected, k,
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
                continue

            if group_id and group_id in processed_groups:
                continue

            allowed_node_ids = self._collect_allowed_node_ids(tree_id, block, beams)
            if not allowed_node_ids:
                continue

            result, llm_called, cache_metrics = self._process_block(
                block=block,
                query=query,
                input_beams=beams,
                previous_selected=previous_selected,
                allowed_node_ids=allowed_node_ids,
                k=k,
            )
            total_llm_calls += llm_called
            cache_read_tokens += cache_metrics.get("cache_read_tokens", 0)
            cache_creation_tokens += cache_metrics.get("cache_creation_tokens", 0)
            blocks_processed += 1

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

    def _process_horizontal_group(self, group_blocks, tree_id, query, beams, previous_selected, k):
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
            )
            return [(result, llm_called, cache_metrics, blk)]

        results = []
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {
                pool.submit(
                    self._process_block, blk, query, beams, previous_selected, allowed, k,
                ): blk
                for blk, allowed in tasks
            }
            for future in as_completed(futures):
                blk = futures[future]
                result, llm_called, cache_metrics = future.result()
                results.append((result, llm_called, cache_metrics, blk))

        return results

    # ---- LLM interaction ----

    def _process_block(self, block, query, input_beams, previous_selected, allowed_node_ids, k):
        """Process one fixed block with dynamic beam filter."""
        empty_metrics = {"cache_read_tokens": 0, "cache_creation_tokens": 0}
        if not allowed_node_ids:
            return BlockResult(block_id=block.block_id, ranked_node_ids=[], selected_node_ids=[], done=False), 0, empty_metrics

        cache_key = self._build_block_cache_key(block)
        if self.mode == "filesystem":
            cache_part = BLOCK_FS_CACHE_PREFIX_PROMPT.render(block_content=block.cached_content or "")
            dynamic_prompt = BLOCK_FS_PROMPT.render(
                query=query,
                previous_selected=previous_selected,
                input_beams=input_beams,
                allowed_node_ids=allowed_node_ids,
                k=k,
            )
        else:
            cache_part = BLOCK_CACHE_PREFIX_PROMPT.render(block_content=block.cached_content or "")
            dynamic_prompt = BLOCK_PROMPT.render(
                query=query,
                previous_selected=previous_selected,
                input_beams=input_beams,
                allowed_node_ids=allowed_node_ids,
                k=k,
            )
        prefix_tokens = self.token_counter.count_text_tokens(cache_part)
        dynamic_tokens = self.token_counter.count_text_tokens(dynamic_prompt)
        est_total_tokens = prefix_tokens + dynamic_tokens
        log.info(
            "prompt_tokens block=%s prefix=%d dynamic=%d est_total=%d allowed=%d",
            block.block_id,
            prefix_tokens,
            dynamic_tokens,
            est_total_tokens,
            len(allowed_node_ids),
        )

        if hasattr(self.llm, "chat_with_cache"):
            call_started = time.perf_counter()
            cache_call_kwargs = {
                "tools": TOOLS,
                "cache_content": cache_part,
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
            full_prompt = cache_part + "\n\n" + dynamic_prompt
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

        ranked_ids, done = self._parse_llm_response(resp, allowed_node_ids)

        result = BlockResult(
            block_id=block.block_id,
            ranked_node_ids=ranked_ids,
            selected_node_ids=ranked_ids[:max(1, k)],
            done=done,
            usage=usage,
        )
        return result, 1, cache_metrics

    def _build_block_cache_key(self, block: Block) -> str:
        identity = block.content_hash or block.block_id
        return f"condb:block:{identity}"

    def _parse_llm_response(self, resp, valid_node_ids):
        valid_set = set(valid_node_ids)
        for block in resp.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "rank":
                ranked_ids = block.get("input", {}).get("selected", []) or []
                done = bool(block.get("input", {}).get("done", False))
                return [nid for nid in ranked_ids if nid in valid_set], done
        raise ValueError("LLM did not return a rank tool call")

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

            new_beams.append({
                "node_id": node_id,
                "title": attrs.get("title", ""),
                "path": node.path if node else "",
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
