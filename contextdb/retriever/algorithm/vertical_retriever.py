"""Block retriever variant with per-beam vertical split in filesystem mode.

This variant expands each beam independently in FS mode:
- parent A with beams [B, C]
- evaluate subtree blocks for [A->B] and [A->C] separately
- merge ranked outputs for next-step beams
"""

from collections import deque
import json
from typing import Any

from contextdb.retriever.algorithm.block_retriever import BlockRetriever
from contextdb.retriever.algorithm.block_types import BlockRetrievalResult


class VerticalRetriever(BlockRetriever):
    """Filesystem retrieval with per-parent branch blocks (a->b, a->c)."""

    def _retrieve_fs(
        self, tree_id: str, query: str, beam_size: int, max_turns: int, select_k: int,
    ) -> BlockRetrievalResult:
        root_id = self.storage.get_root_id(tree_id)
        if not root_id:
            return self._empty_result()

        if tree_id != self._precomputed_tree_id:
            self.token_counter.clear_cache()
            self.token_counter.precompute_tree_tokens(self.storage, tree_id)
            self._precomputed_tree_id = tree_id

        beams = [{"node_id": root_id, "title": "root", "path": "root"}]
        top_specs = self._create_top_block_specs_fs(tree_id, beams[0])
        if not top_specs:
            return self._empty_result()
        top_block = top_specs[0]["block"]
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
        max_calls = max_turns if max_turns else 20

        # Step 1: top block
        allowed_top = [nid for nid in top_block.node_ids if nid != root_id]
        if not allowed_top:
            allowed_top = top_block.node_ids

        top_cache_segments = self._build_cache_segments(cache_window)
        top_result, llm_called, cache_metrics = self._process_block(
            block=top_block,
            query=query,
            input_beams=beams,
            previous_selected=previous_selected,
            allowed_node_ids=allowed_top,
            k=k,
            cache_segments=top_cache_segments,
            current_block_content=top_block.cached_content or "",
            cache_current_block=self.cache_current_block,
        )
        total_llm_calls += llm_called
        cache_read_tokens += cache_metrics.get("cache_read_tokens", 0)
        cache_creation_tokens += cache_metrics.get("cache_creation_tokens", 0)
        blocks_processed += 1
        cache_window_tokens = self._append_to_cache_window(
            cache_window,
            cache_window_tokens,
            top_block,
            cache_block=self.cache_current_block,
            pin_block=True,
        )

        beams = self._update_beams(top_result.ranked_node_ids, tree_id, beam_size)
        for nid in top_result.selected_node_ids:
            if nid not in selected:
                selected.append(nid)
        previous_selected = list(top_result.selected_node_ids)

        block_traces.append({
            "type": "top",
            "block_id": top_block.block_id,
            "depth_range": f"{top_block.depth_start}-{top_block.depth_end}",
            "nodes": len(top_block.node_ids),
            "allowed": len(allowed_top),
        })
        done = bool(top_result.done)
        if done and self._beams_have_children(tree_id, beams):
            done = False
        trace.append({
            "turn": 0,
            "block_id": top_block.block_id,
            "candidates": len(allowed_top),
            "kept": len(top_result.ranked_node_ids),
            "done": done,
        })

        # Step 2: split by beam branch (a->b, a->c, ...)
        turn = 1
        while not done and total_llm_calls < max_calls:
            if not self._beams_have_children(tree_id, beams):
                break

            branch_rows = []
            for beam in beams:
                beam_id = beam.get("node_id")
                if not beam_id:
                    continue

                subtree_block = self._create_subtree_block_fs(tree_id, [beam_id])
                if subtree_block is None:
                    continue

                allowed_sub = [nid for nid in subtree_block.node_ids if nid != beam_id]
                if not allowed_sub:
                    allowed_sub = subtree_block.node_ids

                sub_cache_segments = self._build_cache_segments(cache_window)
                result, branch_calls, branch_cache_metrics = self._process_block(
                    block=subtree_block,
                    query=query,
                    input_beams=[beam],
                    previous_selected=previous_selected,
                    allowed_node_ids=allowed_sub,
                    k=k,
                    cache_segments=sub_cache_segments,
                    current_block_content=subtree_block.cached_content or "",
                    cache_current_block=self.cache_subtree_block,
                )
                total_llm_calls += branch_calls
                cache_read_tokens += branch_cache_metrics.get("cache_read_tokens", 0)
                cache_creation_tokens += branch_cache_metrics.get("cache_creation_tokens", 0)
                blocks_processed += 1
                cache_window_tokens = self._append_to_cache_window(
                    cache_window, cache_window_tokens, subtree_block, cache_block=self.cache_subtree_block,
                )

                for nid in result.selected_node_ids:
                    if nid not in selected:
                        selected.append(nid)

                branch_rows.append({
                    "beam_id": beam_id,
                    "result": result,
                    "allowed_count": len(allowed_sub),
                    "subtree_block": subtree_block,
                })

                block_traces.append({
                    "type": "branch_subtree",
                    "beam_id": beam_id,
                    "block_id": subtree_block.block_id,
                    "nodes": len(subtree_block.node_ids),
                    "allowed": len(allowed_sub),
                    "tokens": subtree_block.total_tokens,
                })

                if total_llm_calls >= max_calls:
                    break

            if not branch_rows:
                break

            merged_ranked: list[str] = []
            seen: set[str] = set()
            for row in branch_rows:
                for nid in row["result"].ranked_node_ids:
                    if nid in seen:
                        continue
                    seen.add(nid)
                    merged_ranked.append(nid)

            if not merged_ranked:
                break

            previous_selected = merged_ranked[:max(1, k)]
            for nid in previous_selected:
                if nid not in selected:
                    selected.append(nid)
            beams = self._update_beams(merged_ranked, tree_id, beam_size)

            done = all(row["result"].done for row in branch_rows)
            if done and self._beams_have_children(tree_id, beams):
                done = False

            trace.append({
                "turn": turn,
                "type": "branch_split",
                "branch_blocks": len(branch_rows),
                "candidates": sum(row["allowed_count"] for row in branch_rows),
                "kept": len(merged_ranked),
                "done": done,
            })
            turn += 1

        # Keep files only in filesystem mode
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
