"""Legacy block retriever (subtree-driven prefix caching)."""

import hashlib
import json
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
    BlockType,
)
from contextdb.utils.token_counter import TokenCounter

log = get_logger(__name__)

_DEFAULT_CONFIG = get_retriever_config("block")

BLOCK_PROMPT = Template(
    """
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

Pick up to {{ k }} candidates from the CURRENT BLOCK above, best first.
Previous blocks are provided for context only — do NOT select nodes from them.

Return ONE tool call "rank" with:
- selected: list of ids from the CURRENT BLOCK in best-to-worst order
- done: true ONLY if you've reached leaf nodes or content is specific enough to answer. If nodes are high-level sections, set done=false to explore deeper.
"""
)

BLOCK_CONTENT_PREFIX = """=== DOCUMENT TREE NODES ===

"""

CURRENT_BLOCK_MARKER = """
=== CURRENT BLOCK (rank these nodes) ===
"""

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


class LegacyBlockRetriever(BaseRetriever):

    def __init__(
        self,
        storage,
        llm,
        max_tokens_per_block: int = None,
    ):
        super().__init__(storage, llm)

        self.max_tokens_per_block = (
            max_tokens_per_block if max_tokens_per_block is not None
            else _DEFAULT_CONFIG.get("max_tokens_per_block", 16000)
        )

        self.token_counter = TokenCounter()
        self.block_cutter = BlockCutter(storage, self.token_counter, self.max_tokens_per_block)

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
        Subtree-driven block beam search.

        1. Process top block (greedy-merged shallow layers) → select beams
        2. For each beam, create subtree block dynamically (greedy packing by token budget)
        3. Repeat until done or max_turns reached
        """
        root_id = self.storage.get_root_id(tree_id)
        if not root_id:
            return self._empty_result()

        self.token_counter.clear_cache()
        self.token_counter.precompute_tree_tokens(self.storage, tree_id)

        plan = self._get_or_create_plan(tree_id)
        if not plan.blocks:
            return self._empty_result()

        top_block = plan.blocks[0]
        top_prefix = top_block.cached_content or ""

        beams = [{"node_id": root_id, "title": "root", "path": "root"}]
        selected: list[str] = []
        trace: list[dict[str, Any]] = []
        block_traces: list[dict[str, Any]] = []

        total_llm_calls = 0
        cache_read_tokens = 0
        cache_creation_tokens = 0
        blocks_processed = 0

        k = beam_size if beam_size else select_k
        max_calls = max_turns if max_turns else 20

        # Step 1: Process top block
        result, llm_called, cache_metrics = self._process_block(
            top_block, query, beams, selected, k, prefix="",
        )
        total_llm_calls += llm_called
        cache_read_tokens += cache_metrics.get("cache_read_tokens", 0)
        cache_creation_tokens += cache_metrics.get("cache_creation_tokens", 0)
        blocks_processed += 1

        beams = self._update_beams(result.ranked_node_ids, tree_id, beam_size)
        for node_id in result.selected_node_ids:
            if node_id not in selected:
                selected.append(node_id)

        block_traces.append({
            "type": "top",
            "block_id": top_block.block_id,
            "depth_range": f"{top_block.depth_start}-{top_block.depth_end}",
            "nodes": len(top_block.node_ids),
        })
        trace.append({
            "turn": 0,
            "block_id": top_block.block_id,
            "candidates": len(top_block.node_ids),
            "kept": len(result.ranked_node_ids),
            "done": result.done,
        })

        # Step 2: Iteratively process subtree blocks
        turn = 1
        while not result.done and total_llm_calls < max_calls:
            has_children = any(
                b["node_id"] and self.storage.get_children(tree_id, b["node_id"])
                for b in beams
            )
            if not has_children:
                break

            beam_ids = [b["node_id"] for b in beams if b["node_id"]]
            subtree_block = self._create_subtree_block(tree_id, beam_ids)
            if subtree_block is None:
                break

            result, llm_called, cache_metrics = self._process_block(
                subtree_block, query, beams, selected, k, prefix=top_prefix,
            )
            total_llm_calls += llm_called
            cache_read_tokens += cache_metrics.get("cache_read_tokens", 0)
            cache_creation_tokens += cache_metrics.get("cache_creation_tokens", 0)
            blocks_processed += 1

            beams = self._update_beams(result.ranked_node_ids, tree_id, beam_size)
            for node_id in result.selected_node_ids:
                if node_id not in selected:
                    selected.append(node_id)

            block_traces.append({
                "type": "subtree",
                "block_id": subtree_block.block_id,
                "nodes": len(subtree_block.node_ids),
                "tokens": subtree_block.total_tokens,
            })
            trace.append({
                "turn": turn,
                "block_id": subtree_block.block_id,
                "candidates": len(subtree_block.node_ids),
                "kept": len(result.ranked_node_ids),
                "done": result.done,
            })
            turn += 1

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

    # ---- subtree block creation ----

    def _create_subtree_block(self, tree_id: str, beam_node_ids: list[str]) -> Optional[Block]:
        """Create a block by greedy-packing beam subtrees within the token budget T.

        Uses dynamic content detail: full content for nodes that fit,
        compact (title+summary only) for overflow nodes to maximize coverage.
        """
        all_descendants = []
        for node_id in beam_node_ids:
            all_descendants.extend(self._get_subtree_nodes(tree_id, node_id))

        if not all_descendants:
            return None

        total_tokens = sum(
            self.token_counter.get_cached_count(d["node_id"])
            or self.token_counter.count_node_tokens(d)
            for d in all_descendants
        )

        if total_tokens <= self.max_tokens_per_block:
            return self._make_block(tree_id, beam_node_ids, all_descendants, total_tokens, "subtree")

        # Greedy pack children's subtrees, with dynamic detail level
        packed_full = []     # nodes with full content (title + summary + text)
        packed_compact = []  # nodes with compact content (title + summary only)
        packed_tokens = 0

        for beam_id in beam_node_ids:
            for child in self._get_direct_children_nodes(tree_id, beam_id):
                child_subtree = self._get_subtree_nodes(tree_id, child["node_id"])
                child_subtree_tokens = sum(
                    self.token_counter.get_cached_count(n["node_id"])
                    or self.token_counter.count_node_tokens(n)
                    for n in child_subtree
                )
                child_node_tokens = (
                    self.token_counter.get_cached_count(child["node_id"])
                    or self.token_counter.count_node_tokens(child)
                )

                if packed_tokens + child_node_tokens + child_subtree_tokens <= self.max_tokens_per_block:
                    packed_full.append(child)
                    packed_full.extend(child_subtree)
                    packed_tokens += child_node_tokens + child_subtree_tokens
                else:
                    packed_compact.append(child)
                    packed_tokens += child_node_tokens

        all_packed = packed_full + packed_compact
        if not all_packed:
            return None

        # Generate content: full detail for packed_full, compact for packed_compact
        full_ids = {n["node_id"] for n in packed_full}
        content = self._generate_content_from_nodes(all_packed, compact_ids={
            n["node_id"] for n in packed_compact
        })
        node_ids = [n["node_id"] for n in all_packed]
        depths = [n.get("depth", 0) for n in all_packed]

        return Block(
            block_id=f"packed_{'_'.join(bid[:8] for bid in beam_node_ids[:3])}",
            block_type=BlockType.VERTICAL,
            tree_id=tree_id,
            depth_start=min(depths) if depths else 0,
            depth_end=max(depths) if depths else 0,
            node_ids=node_ids,
            total_tokens=packed_tokens,
            max_tokens=self.max_tokens_per_block,
            cached_content=content,
            content_hash=hashlib.md5(content.encode()).hexdigest(),
        )

    def _make_block(self, tree_id, beam_ids, nodes, total_tokens, prefix):
        node_ids = [n["node_id"] for n in nodes]
        content = self._generate_content_from_nodes(nodes)
        depths = [n.get("depth", 0) for n in nodes]
        return Block(
            block_id=f"{prefix}_{'_'.join(bid[:8] for bid in beam_ids[:3])}",
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

    # ---- LLM interaction ----

    def _process_block(self, block, query, beams, selected, k, prefix=""):
        """Process a block with LLM. Returns (BlockResult, llm_calls, cache_metrics)."""
        empty_metrics = {"cache_read_tokens": 0, "cache_creation_tokens": 0}

        node_ids = block.node_ids
        content = block.cached_content

        if not node_ids:
            return BlockResult(block_id=block.block_id, ranked_node_ids=[], selected_node_ids=[], done=False), 0, empty_metrics

        # Cached prefix (reusable across queries) vs non-cached current block (one-time)
        if prefix:
            cache_part = BLOCK_CONTENT_PREFIX + prefix
            non_cached_part = CURRENT_BLOCK_MARKER + content
        else:
            cache_part = BLOCK_CONTENT_PREFIX + content
            non_cached_part = None

        dynamic_prompt = BLOCK_PROMPT.render(query=query, selected=selected, input_beams=beams, k=k)

        if hasattr(self.llm, "chat_with_cache"):
            resp = self.llm.chat_with_cache(
                [{"role": "user", "content": dynamic_prompt}],
                tools=TOOLS,
                cache_content=cache_part,
                non_cached_content=non_cached_part,
            )
        else:
            full_prompt = cache_part + "\n\n" + (non_cached_part or "") + "\n\n" + dynamic_prompt
            resp = self.llm.chat([{"role": "user", "content": full_prompt}], tools=TOOLS)

        usage = resp.get("usage") or {}
        cache_metrics = {
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
            "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
        }

        ranked_ids, done = self._parse_llm_response(resp, node_ids)

        result = BlockResult(
            block_id=block.block_id,
            ranked_node_ids=ranked_ids,
            selected_node_ids=ranked_ids[:max(1, k)],
            done=done,
            usage=usage,
        )
        return result, 1, cache_metrics

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
                    pass
            new_beams.append({
                "node_id": node_id,
                "title": attrs.get("title", ""),
                "path": node.path if node else "",
            })
            if beam_size and len(new_beams) >= beam_size:
                break
        return new_beams if new_beams else [{"node_id": "", "title": "", "path": ""}]

    # ---- DB helpers ----

    def _get_subtree_nodes(self, tree_id, node_id):
        cursor = self.storage.conn.cursor()
        cursor.execute("SELECT path FROM nodes WHERE tree_id = ? AND node_id = ?", (tree_id, node_id))
        row = cursor.fetchone()
        if not row:
            return []
        cursor.execute(
            """SELECT n.node_id, n.parent_id, n.depth, n.path, n.attrs_json, e.payload_json
               FROM nodes n LEFT JOIN entities e ON n.entity_id = e.entity_id
               WHERE n.tree_id = ? AND n.path LIKE ? AND n.node_id != ?
               ORDER BY n.path""",
            (tree_id, row[0] + "%", node_id),
        )
        return [self._row_to_node(r) for r in cursor.fetchall()]

    def _get_direct_children_nodes(self, tree_id, node_id):
        cursor = self.storage.conn.cursor()
        cursor.execute(
            """SELECT n.node_id, n.parent_id, n.depth, n.path, n.attrs_json, e.payload_json
               FROM nodes n LEFT JOIN entities e ON n.entity_id = e.entity_id
               WHERE n.tree_id = ? AND n.parent_id = ?
               ORDER BY n.path""",
            (tree_id, node_id),
        )
        return [self._row_to_node(r) for r in cursor.fetchall()]

    def _row_to_node(self, row):
        node = {"node_id": row["node_id"], "parent_id": row["parent_id"],
                "depth": row["depth"], "path": row["path"], "attrs_json": row["attrs_json"]}
        if row["attrs_json"]:
            node["attrs"] = json.loads(row["attrs_json"])
        if row["payload_json"]:
            node["entity"] = {"payload": json.loads(row["payload_json"])}
        return node

    def _generate_content_from_nodes(self, nodes, compact_ids=None):
        """Generate block content. Nodes in compact_ids get title+summary only."""
        compact_ids = compact_ids or set()
        lines = []
        for node in nodes:
            attrs = node.get("attrs") or {}
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs)
                except json.JSONDecodeError:
                    attrs = {}

            lines.append(f"- id: {node['node_id']}")
            if attrs.get("title"):
                lines.append(f"  title: {attrs['title']}")
            if attrs.get("summary"):
                lines.append(f"  summary: {attrs['summary']}")

            if node["node_id"] not in compact_ids:
                entity = node.get("entity", {})
                payload = entity.get("payload", {}) if isinstance(entity, dict) else {}
                text = payload.get("text") or payload.get("content") or ""
                if text:
                    lines.append(f"  text: {text[:200]}")

            lines.append(f"  depth: {node.get('depth', 0)}")
            page_start = attrs.get("page_start")
            page_end = attrs.get("page_end")
            if page_start is not None or page_end is not None:
                lines.append(f"  range: {page_start}-{page_end}")
        return "\n".join(lines)

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
            nodes=[], contents=[], trace=[], turns=0,
            blocks_processed=0, total_llm_calls=0,
            cache_read_tokens=0, cache_creation_tokens=0, block_traces=[],
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

    def _get_llm_max_concurrent(self, llm):
        provider = getattr(llm, "provider", None)
        model = getattr(llm, "model", None)
        if provider and model:
            try:
                return get_llm_config(provider, model).get("max_concurrent", 10)
            except Exception:
                pass
        return 10


BlockRetrieverLegacy = LegacyBlockRetriever
