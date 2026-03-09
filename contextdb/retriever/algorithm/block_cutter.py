"""Block cutting algorithm for Block-level Beam Search."""

import hashlib
import json
from typing import Optional

from contextdb.core.storage import StorageProtocol
from contextdb.logger import get_logger
from contextdb.retriever.algorithm.block_types import (
    Block,
    BlockTreePlan,
    BlockType,
    HorizontalBlockGroup,
)
from contextdb.utils.token_counter import TokenCounter

log = get_logger(__name__)


class BlockCutter:
    """Cuts a document tree into Blocks for sequential LLM processing."""

    def __init__(
        self,
        storage: StorageProtocol,
        token_counter: TokenCounter,
        max_tokens_per_block: int = 16000,
        min_tokens_per_block: int = 0,
    ):
        self.storage = storage
        self.token_counter = token_counter
        self.max_tokens = max_tokens_per_block
        self.min_tokens = max(0, min(min_tokens_per_block, max_tokens_per_block))

    def cut_tree(self, tree_id: str) -> BlockTreePlan:
        """Generate complete block cutting plan for a tree."""
        root_id = self.storage.get_root_id(tree_id)
        if not root_id:
            return BlockTreePlan(tree_id=tree_id, max_tokens_per_block=self.max_tokens)

        # Precompute all node tokens
        self.token_counter.precompute_tree_tokens(self.storage, tree_id)

        # Get full tree token info
        tree_info = self.token_counter.count_subtree_tokens(self.storage, tree_id, root_id)

        plan = BlockTreePlan(
            tree_id=tree_id,
            max_tokens_per_block=self.max_tokens,
            total_tokens=tree_info.total_tokens,
        )

        log.debug(
            "BlockCutter: tree %s has %d tokens across %d nodes, max_depth=%d, block_range=[%d,%d]",
            tree_id[:8],
            tree_info.total_tokens,
            tree_info.node_count,
            tree_info.max_depth,
            self.min_tokens,
            self.max_tokens,
        )

        # Greedy vertical merging: start from depth 0, keep adding deeper
        # levels into the same block until hitting the token limit T.
        # When a single level exceeds T, use horizontal cutting for that level.
        max_depth = tree_info.max_depth
        current_depth = 0

        while current_depth <= max_depth:
            # Greedy: try to merge as many levels as possible into one vertical block
            depth_end = current_depth
            accumulated_tokens = tree_info.token_by_depth.get(current_depth, 0)

            while depth_end + 1 <= max_depth:
                next_depth_tokens = tree_info.token_by_depth.get(depth_end + 1, 0)
                if accumulated_tokens + next_depth_tokens <= self.max_tokens:
                    depth_end += 1
                    accumulated_tokens += next_depth_tokens
                else:
                    break

            log.debug(
                "  depth %d-%d: %d tokens (max=%d)",
                current_depth,
                depth_end,
                accumulated_tokens,
                self.max_tokens,
            )

            # Get nodes at these depths
            nodes_at_depths = self._get_nodes_at_depth_range(tree_id, current_depth, depth_end)

            if accumulated_tokens <= self.max_tokens:
                # Merged vertical block fits within budget
                block = self._create_vertical_block(tree_id, current_depth, depth_end, nodes_at_depths)
                plan.blocks.append(block)
                plan.block_map[block.block_id] = block
                log.debug("    -> vertical block %s (%d levels merged)", block.block_id, depth_end - current_depth + 1)
            else:
                # Single level exceeds T, need horizontal cutting
                h_group = self._create_horizontal_blocks(tree_id, current_depth, depth_end, nodes_at_depths)
                plan.horizontal_groups.append(h_group)
                for block in h_group.blocks:
                    plan.blocks.append(block)
                    plan.block_map[block.block_id] = block
                log.debug("    -> horizontal group %s with %d blocks", h_group.group_id, len(h_group.blocks))

            current_depth = depth_end + 1

        log.info(
            "BlockCutter: tree %s -> %d blocks (%d horizontal groups)",
            tree_id[:8],
            len(plan.blocks),
            len(plan.horizontal_groups),
        )

        return plan

    def _get_nodes_at_depth_range(self, tree_id: str, depth_start: int, depth_end: int) -> list[dict]:
        """Get all nodes within a depth range with their entities."""
        cursor = self.storage.conn.cursor()
        cursor.execute(
            """
            SELECT n.node_id, n.parent_id, n.slot, n.node_type, n.depth, n.path,
                   n.entity_type, n.entity_id, n.attrs_json,
                   e.payload_json
            FROM nodes n
            LEFT JOIN entities e ON n.entity_id = e.entity_id
            WHERE n.tree_id = ? AND n.depth >= ? AND n.depth <= ?
            ORDER BY n.path
        """,
            (tree_id, depth_start, depth_end),
        )

        nodes = []
        for row in cursor.fetchall():
            node_dict = {
                "node_id": row["node_id"],
                "parent_id": row["parent_id"],
                "slot": row["slot"],
                "node_type": row["node_type"],
                "depth": row["depth"],
                "path": row["path"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "attrs_json": row["attrs_json"],
            }
            if row["payload_json"]:
                node_dict["entity"] = {"payload": json.loads(row["payload_json"])}
            if row["attrs_json"]:
                node_dict["attrs"] = json.loads(row["attrs_json"])
            nodes.append(node_dict)

        return nodes

    def _create_vertical_block(
        self, tree_id: str, depth_start: int, depth_end: int, nodes: list[dict]
    ) -> Block:
        """Create a single vertical block."""
        block_id = f"v_{tree_id[:8]}_{depth_start}_{depth_end}"

        node_ids = [n["node_id"] for n in nodes]
        total_tokens = sum(self.token_counter.get_cached_count(nid) or 0 for nid in node_ids)

        # Generate cached content for prefix caching
        cached_content = self._generate_block_content(nodes)
        content_hash = hashlib.md5(cached_content.encode()).hexdigest()

        return Block(
            block_id=block_id,
            block_type=BlockType.VERTICAL,
            tree_id=tree_id,
            depth_start=depth_start,
            depth_end=depth_end,
            node_ids=node_ids,
            total_tokens=total_tokens,
            max_tokens=self.max_tokens,
            cached_content=cached_content,
            content_hash=content_hash,
        )

    def _create_horizontal_blocks(
        self, tree_id: str, depth_start: int, depth_end: int, nodes: list[dict]
    ) -> HorizontalBlockGroup:
        """Create horizontal blocks when depth range exceeds token limit."""
        group_id = f"h_{tree_id[:8]}_{depth_start}_{depth_end}"

        # Group nodes by parent to maintain tree structure
        parent_groups: dict[Optional[str], list[dict]] = {}
        for node in nodes:
            parent_id = node.get("parent_id")
            if parent_id not in parent_groups:
                parent_groups[parent_id] = []
            parent_groups[parent_id].append(node)

        ordered_nodes: list[dict] = []
        node_tokens: list[int] = []
        token_by_id: dict[str, int] = {}

        for children in parent_groups.values():
            for node in children:
                tokens = self.token_counter.get_cached_count(node["node_id"]) or self.token_counter.count_node_tokens(node)
                ordered_nodes.append(node)
                node_tokens.append(tokens)
                token_by_id[node["node_id"]] = tokens

        packs = self._pack_horizontal_nodes(ordered_nodes, node_tokens)
        packs = self._rebalance_small_tail_pack(packs, token_by_id)

        blocks = []
        for block_index, pack in enumerate(packs):
            blocks.append(
                self._create_horizontal_block(
                    tree_id, depth_start, depth_end, pack["nodes"], group_id, block_index
                )
            )

        return HorizontalBlockGroup(
            group_id=group_id,
            tree_id=tree_id,
            depth=depth_start,
            blocks=blocks,
            parent_node_ids=[p for p in parent_groups.keys() if p is not None],
        )

    def _pack_horizontal_nodes(self, nodes: list[dict], node_tokens: list[int]) -> list[dict[str, object]]:
        """Greedy pack nodes up to the max token budget."""
        if not nodes:
            return []

        packs: list[dict[str, object]] = []
        current_nodes: list[dict] = []
        current_tokens = 0
        i = 0

        while i < len(nodes):
            node = nodes[i]
            tokens = node_tokens[i]

            if current_nodes and current_tokens + tokens > self.max_tokens:
                packs.append({"nodes": current_nodes, "tokens": current_tokens})
                current_nodes = []
                current_tokens = 0
                continue

            current_nodes.append(node)
            current_tokens += tokens
            i += 1

        if current_nodes:
            packs.append({"nodes": current_nodes, "tokens": current_tokens})

        return packs

    def _rebalance_small_tail_pack(
        self, packs: list[dict[str, object]], token_by_id: dict[str, int]
    ) -> list[dict[str, object]]:
        """Rebalance the final two packs when the tail pack is too small."""
        if self.min_tokens <= 0 or len(packs) < 2:
            return packs

        last = packs[-1]
        prev = packs[-2]
        last_tokens = int(last["tokens"])
        prev_tokens = int(prev["tokens"])
        if last_tokens >= self.min_tokens:
            return packs

        prev_nodes = list(prev["nodes"])
        last_nodes = list(last["nodes"])

        while last_tokens < self.min_tokens and prev_nodes:
            candidate = prev_nodes[-1]
            candidate_tokens = token_by_id.get(candidate["node_id"], 0)
            if candidate_tokens <= 0:
                break
            if prev_tokens - candidate_tokens < self.min_tokens:
                break
            prev_nodes.pop()
            last_nodes.insert(0, candidate)
            prev_tokens -= candidate_tokens
            last_tokens += candidate_tokens

        prev["nodes"] = prev_nodes
        prev["tokens"] = prev_tokens
        last["nodes"] = last_nodes
        last["tokens"] = last_tokens
        return packs

    def _create_horizontal_block(
        self,
        tree_id: str,
        depth_start: int,
        depth_end: int,
        nodes: list[dict],
        group_id: str,
        index: int,
    ) -> Block:
        """Create a single horizontal block within a group."""
        block_id = f"{group_id}_b{index}"

        node_ids = [n["node_id"] for n in nodes]
        total_tokens = sum(self.token_counter.get_cached_count(nid) or 0 for nid in node_ids)

        cached_content = self._generate_block_content(nodes)
        content_hash = hashlib.md5(cached_content.encode()).hexdigest()

        return Block(
            block_id=block_id,
            block_type=BlockType.HORIZONTAL,
            tree_id=tree_id,
            depth_start=depth_start,
            depth_end=depth_end,
            node_ids=node_ids,
            total_tokens=total_tokens,
            max_tokens=self.max_tokens,
            cached_content=cached_content,
            content_hash=content_hash,
        )

    def _generate_block_content(self, nodes: list[dict]) -> str:
        """Generate cacheable content string for a block."""
        title_map: dict[str, str] = {}
        for node in nodes:
            nid = node.get("node_id")
            a = node.get("attrs") or {}
            if isinstance(a, str):
                try:
                    a = json.loads(a)
                except json.JSONDecodeError:
                    a = {}
            if nid and a.get("title"):
                title_map[nid] = a["title"]

        node_metadata: list[list[str]] = []
        node_texts: list[str] = []
        metadata_chars = 0

        for node in nodes:
            attrs = node.get("attrs") or {}
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs)
                except json.JSONDecodeError:
                    attrs = {}

            entity = node.get("entity", {})
            payload = entity.get("payload", {}) if isinstance(entity, dict) else {}

            meta_lines = []
            meta_lines.append(f"- id: {node['node_id']}")
            if attrs.get("title"):
                meta_lines.append(f"  title: {attrs['title']}")

            parent_id = node.get("parent_id")
            if parent_id and parent_id in title_map:
                meta_lines.append(f"  parent: {title_map[parent_id]}")

            if attrs.get("summary"):
                meta_lines.append(f"  summary: {attrs['summary']}")

            meta_lines.append(f"  depth: {node.get('depth', 0)}")

            page_start = attrs.get("page_start")
            page_end = attrs.get("page_end")
            if page_start is not None or page_end is not None:
                meta_lines.append(f"  range: {page_start}-{page_end}")

            node_metadata.append(meta_lines)
            metadata_chars += sum(len(l) for l in meta_lines)

            text = payload.get("text") or payload.get("content") or ""
            node_texts.append(text)

        chars_per_token = 4
        metadata_tokens_est = metadata_chars // chars_per_token
        remaining_tokens = max(0, self.max_tokens - metadata_tokens_est)
        nodes_with_text = sum(1 for t in node_texts if t)

        if nodes_with_text > 0:
            chars_per_node = max(200, (remaining_tokens * chars_per_token) // nodes_with_text)
        else:
            chars_per_node = 200

        lines = []
        for meta_lines, text in zip(node_metadata, node_texts):
            lines.extend(meta_lines)
            if text:
                lines.append(f"  text: {text[:chars_per_node]}")

        return "\n".join(lines)
