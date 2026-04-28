"""Filesystem-specific helpers for BlockRetriever."""

import hashlib
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Optional

from contextdb.retriever.algorithm.block_types import Block, BlockResult, BlockType
from contextdb.retriever.algorithm.ranker import has_path_evidence


class _RetrieverSupportBase:
    def __init__(self, retriever):
        self._retriever = retriever

    def __getattr__(self, name: str):
        return getattr(self._retriever, name)


@dataclass(frozen=True)
class FilesystemRenderOptions:
    include_dir_summaries: bool = True
    include_file_summaries: bool = False
    summary_max_chars: int = 200


_FS_DEFAULT_RENDER_OPTIONS = FilesystemRenderOptions()
_FS_VIRTUAL_TOP_RENDER_OPTIONS = FilesystemRenderOptions(
    include_dir_summaries=True,
    include_file_summaries=False,
    summary_max_chars=96,
)


class BlockRetrieverFilesystemSupport(_RetrieverSupportBase):
    """Filesystem-specific support methods for BlockRetriever."""

    def _create_top_block_specs_fs(
        self,
        tree_id: str,
        root_frontier: dict[str, str],
        query: str = "",
    ) -> list[dict[str, Any]]:
        root_id = root_frontier.get("node_id")
        if not root_id:
            return []

        is_virtual_root = self._is_virtual_fs_directory_id(tree_id, root_id)
        nodes = (
            self._get_virtual_fs_top_nodes(tree_id, root_id)
            if is_virtual_root
            else self._get_direct_children_nodes(tree_id, root_id)
        )
        nodes = self._order_fs_nodes_for_query(nodes, query)
        render_options = self._get_fs_top_render_options(is_virtual_root)
        return self._build_fs_block_specs(
            tree_id=tree_id,
            frontier_node=root_frontier,
            nodes=nodes,
            base_block_id=self._get_fs_top_block_id(tree_id),
            render_options=render_options,
        )

    def _get_virtual_fs_top_nodes(self, tree_id: str, root_id: str, max_depth: int = 4) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        seen_node_ids: set[str] = set()

        def append_node(node: dict[str, Any]) -> None:
            node_id = node.get("node_id", "")
            if not node_id or node_id in seen_node_ids:
                return
            seen_node_ids.add(node_id)
            nodes.append(node)

        def walk(parent_id: str, remaining_depth: int) -> None:
            if remaining_depth <= 0:
                return

            for child in self._get_direct_children_nodes(tree_id, parent_id):
                collapsed = child
                collapsed_depth = remaining_depth

                while collapsed_depth > 1 and self._is_virtual_fs_directory(collapsed):
                    grandchildren = self._get_direct_children_nodes(tree_id, collapsed["node_id"])
                    if len(grandchildren) != 1:
                        break
                    collapsed = grandchildren[0]
                    collapsed_depth -= 1

                append_node(collapsed)

                if collapsed_depth <= 1:
                    continue
                if not self.storage.get_children(tree_id, collapsed["node_id"]):
                    continue
                walk(collapsed["node_id"], collapsed_depth - 1)

        walk(root_id, max_depth)
        return nodes

    @staticmethod
    def _get_fs_top_render_options(is_virtual_root: bool) -> FilesystemRenderOptions:
        return _FS_VIRTUAL_TOP_RENDER_OPTIONS if is_virtual_root else _FS_DEFAULT_RENDER_OPTIONS

    def _create_subtree_block_specs_fs(
        self,
        tree_id: str,
        frontier: list[dict[str, str]],
        query: str = "",
    ) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for frontier_node in frontier:
            frontier_id = frontier_node.get("node_id")
            if not frontier_id:
                continue

            children = self._order_fs_nodes_for_query(
                self._get_direct_children_nodes(tree_id, frontier_id),
                query,
            )
            if not children:
                continue

            specs.extend(
                self._build_fs_block_specs(
                    tree_id=tree_id,
                    frontier_node=frontier_node,
                    nodes=children,
                    base_block_id=f"fs_sub_{frontier_id[:8]}",
                    render_options=_FS_DEFAULT_RENDER_OPTIONS,
                    always_suffix=True,
                )
            )
        return specs

    def _build_fs_block_specs(
        self,
        tree_id: str,
        frontier_node: dict[str, str],
        nodes: list[dict[str, Any]],
        base_block_id: str,
        render_options: FilesystemRenderOptions = _FS_DEFAULT_RENDER_OPTIONS,
        always_suffix: bool = False,
    ) -> list[dict[str, Any]]:
        if not nodes:
            return []

        packs = self._pack_fs_children(nodes, render_options=render_options)
        if not packs:
            return []

        specs: list[dict[str, Any]] = []
        for idx, pack in enumerate(packs):
            block_id = (
                f"{base_block_id}_b{idx}"
                if always_suffix or len(packs) > 1
                else base_block_id
            )
            specs.append({
                "frontier": frontier_node,
                "block": self._make_fs_block(
                    tree_id,
                    pack,
                    block_id,
                    render_options=render_options,
                ),
                "block_index": idx,
                "block_count": len(packs),
            })
        return specs

    def _pack_fs_children(
        self,
        children: list[dict[str, Any]],
        render_options: FilesystemRenderOptions = _FS_DEFAULT_RENDER_OPTIONS,
    ) -> list[list[dict[str, Any]]]:
        if not children:
            return []

        packs: list[list[dict[str, Any]]] = []
        current_pack: list[dict[str, Any]] = []

        for child in children:
            candidate_pack = current_pack + [child]
            candidate_tokens = self._count_fs_block_tokens(
                candidate_pack,
                render_options=render_options,
            )
            if current_pack and candidate_tokens > self.max_tokens_per_block:
                packs.append(current_pack)
                current_pack = [child]
                continue

            current_pack = candidate_pack

        if current_pack:
            packs.append(current_pack)

        return packs

    def _build_fs_subtree_block(
        self,
        tree_id: str,
        beam_node_id: str,
        nodes_to_pack: list[dict[str, Any]],
        block_index: int,
    ) -> Block:
        block_id = f"fs_sub_{beam_node_id[:8]}_b{block_index}"
        return self._make_fs_block(
            tree_id,
            nodes_to_pack,
            block_id,
            render_options=_FS_DEFAULT_RENDER_OPTIONS,
        )

    def _build_fs_candidate_block(
        self,
        tree_id: str,
        node_ids: list[str],
        block_id: str,
    ) -> tuple[Block, list[str]]:
        nodes = self._get_nodes_by_ids(tree_id, node_ids)
        allowed_node_ids = [node["node_id"] for node in nodes]
        block = self._make_fs_block(
            tree_id,
            nodes,
            block_id,
            render_options=_FS_DEFAULT_RENDER_OPTIONS,
        )
        return block, allowed_node_ids

    @staticmethod
    def _merge_unique_ids(node_ids: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for node_id in node_ids:
            if node_id in seen:
                continue
            seen.add(node_id)
            merged.append(node_id)
        return merged

    def _append_fs_frontier_blocks(
        self,
        cache_window: deque[dict[str, Any]],
        total_tokens: int,
        block_rows: list[tuple[dict[str, Any], BlockResult, int, dict[str, int]]],
        frontier: list[dict[str, str]],
        cache_block: bool,
        pin_block: bool = False,
    ) -> int:
        if not self.cache_enabled or not block_rows:
            return total_tokens

        frontier_node_ids = {
            frontier_node.get("node_id")
            for frontier_node in frontier
            if frontier_node.get("node_id")
        }
        if not frontier_node_ids:
            return total_tokens

        appended_block_ids: set[str] = set()
        for spec, _, _, _ in block_rows:
            block = spec["block"]
            if block.block_id in appended_block_ids:
                continue
            if not frontier_node_ids.intersection(block.node_ids):
                continue
            total_tokens = self._append_to_cache_window(
                cache_window,
                total_tokens,
                block,
                cache_block=cache_block,
                pin_block=pin_block,
            )
            appended_block_ids.add(block.block_id)
        return total_tokens

    def _process_fs_block_specs(
        self,
        subtree_specs: list[dict[str, Any]],
        query: str,
        previous_top_candidate_ids: list[str],
        pick_limit: int,
        cache_segments: list[str] = None,
        cache_current_block: bool = True,
    ) -> list[tuple[dict[str, Any], BlockResult, int, dict[str, int]]]:
        if not subtree_specs:
            return []

        if len(subtree_specs) == 1:
            spec = subtree_specs[0]
            block = spec["block"]
            result, llm_called, cache_metrics = self._process_block(
                block=block,
                query=query,
                input_frontier=[spec["frontier"]],
                previous_top_candidate_ids=previous_top_candidate_ids,
                allowed_node_ids=block.node_ids,
                pick_limit=pick_limit,
                cache_segments=cache_segments or [],
                current_block_content=block.cached_content or "",
                cache_current_block=cache_current_block,
            )
            return [(spec, result, llm_called, cache_metrics)]

        ordered_results: list[Optional[tuple[dict[str, Any], BlockResult, int, dict[str, int]]]] = [None] * len(subtree_specs)
        max_workers = self._max_parallel_workers(len(subtree_specs))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for idx, spec in enumerate(subtree_specs):
                block = spec["block"]
                futures[
                    pool.submit(
                        self._process_block,
                        block,
                        query,
                        [spec["frontier"]],
                        previous_top_candidate_ids,
                        block.node_ids,
                        pick_limit,
                        cache_segments=cache_segments or [],
                        current_block_content=block.cached_content or "",
                        cache_current_block=cache_current_block,
                    )
                ] = (idx, spec)

            for future in as_completed(futures):
                idx, spec = futures[future]
                result, llm_called, cache_metrics = future.result()
                ordered_results[idx] = (spec, result, llm_called, cache_metrics)

        return [row for row in ordered_results if row is not None]

    def _create_subtree_block_fs(
        self,
        tree_id: str,
        beam_node_ids: list[str],
        query: str = "",
    ) -> Optional[Block]:
        """Create a subtree block from children of beam nodes."""
        all_children = []
        for nid in beam_node_ids:
            all_children.extend(self._get_direct_children_nodes(tree_id, nid))

        if not all_children:
            return None
        all_children = self._order_fs_nodes_for_query(all_children, query)

        packs = self._pack_fs_children(
            all_children,
            render_options=_FS_DEFAULT_RENDER_OPTIONS,
        )
        if not packs:
            return None

        block_id = f"fs_sub_{'_'.join(bid[:8] for bid in beam_node_ids[:3])}"
        return self._make_fs_vertical_block(tree_id, packs[0], block_id)

    def _order_fs_nodes_for_query(self, nodes: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
        """Order exchangeable filesystem siblings when BM25 has exact path evidence."""
        if not nodes:
            return []

        fs_ranker = getattr(self, "fs_ranker", "bm25")
        if fs_ranker == "none":
            return list(nodes)

        if fs_ranker == "bm25":
            candidates = [self._fs_ranker_candidate(node) for node in nodes]
            if not has_path_evidence(candidates, query):
                return list(nodes)
            scores = self.ranker.rank(
                query,
                candidates,
                context={"mode": "filesystem", "tree_id": getattr(self, "_precomputed_tree_id", "")},
            )
            score_by_id = {candidate["node_id"]: score for candidate, score in scores}
            ordered = sorted(
                enumerate(nodes),
                key=lambda item: (
                    -score_by_id.get(item[1].get("node_id", ""), 0.0),
                    self._fs_ranker_tie_key(item[1], item[0]),
                ),
            )
            return [node for _, node in ordered]

        return list(nodes)

    def _fs_ranker_candidate(self, node: dict[str, Any]) -> dict[str, Any]:
        attrs = self._fs_node_attrs(node)
        rel_path = (attrs.get("rel_path") or node.get("path") or "").strip("/")
        title = (attrs.get("title") or rel_path.rsplit("/", 1)[-1]).strip()
        is_dir = bool(attrs.get("is_dir", False))
        return {
            **node,
            "title": title,
            "rel_path": rel_path,
            "is_dir": is_dir,
            "is_leaf": not is_dir,
        }

    def _fs_ranker_tie_key(self, node: dict[str, Any], original_index: int) -> tuple[int, int, str, int]:
        attrs = self._fs_node_attrs(node)
        rel_path = (attrs.get("rel_path") or node.get("path") or "").strip("/")
        is_file = 0 if attrs.get("is_dir", False) else 1
        depth = int(node.get("depth", 0) or 0)
        return (is_file, depth, rel_path.lower(), original_index)

    @staticmethod
    def _fs_node_attrs(node: dict[str, Any]) -> dict[str, Any]:
        attrs = node.get("attrs") or {}
        if isinstance(attrs, str):
            try:
                return json.loads(attrs)
            except json.JSONDecodeError:
                return {}
        return attrs if isinstance(attrs, dict) else {}

    @staticmethod
    def _build_fs_alias_maps(node_ids: list[str]) -> tuple[dict[str, str], dict[str, str]]:
        node_id_to_alias: dict[str, str] = {}
        alias_to_node_id: dict[str, str] = {}
        for idx, node_id in enumerate(node_ids, start=1):
            alias = f"n{idx}"
            node_id_to_alias[node_id] = alias
            alias_to_node_id[alias] = node_id
        return node_id_to_alias, alias_to_node_id

    def _build_fs_block_content(
        self,
        nodes: list[dict],
        path_prefix: str = "",
        node_id_to_alias: dict[str, str] | None = None,
        render_options: FilesystemRenderOptions = _FS_DEFAULT_RENDER_OPTIONS,
    ) -> str:
        if node_id_to_alias is None:
            node_id_to_alias, _ = self._build_fs_alias_maps([node["node_id"] for node in nodes])
        lines = []
        if path_prefix:
            lines.append(f"Path prefix: {path_prefix}")
            lines.append("")
        for node in nodes:
            attrs = node.get("attrs") or {}
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs)
                except json.JSONDecodeError:
                    attrs = {}

            nid = node["node_id"]
            alias = node_id_to_alias.get(nid, nid)
            rel_path = attrs.get("rel_path", "")
            is_dir = attrs.get("is_dir", False)
            display_path = self._strip_fs_path_prefix(rel_path, path_prefix)

            lines.append(f"- id: {alias}")
            if display_path:
                lines.append(f"  path: {display_path}")
            if is_dir:
                lines.append("  type: directory")
            else:
                lines.append("  type: file")

        return "\n".join(lines)

    def _render_fs_block_content(
        self,
        nodes: list[dict[str, Any]],
        render_options: FilesystemRenderOptions = _FS_DEFAULT_RENDER_OPTIONS,
    ) -> tuple[str, int]:
        node_id_to_alias, _ = self._build_fs_alias_maps([node["node_id"] for node in nodes])
        plain_content = self._build_fs_block_content(
            nodes,
            path_prefix="",
            node_id_to_alias=node_id_to_alias,
            render_options=render_options,
        )
        plain_tokens = self.token_counter.count_text_tokens(plain_content)

        path_prefix = self._shared_fs_path_prefix(nodes)
        if not path_prefix:
            return plain_content, plain_tokens

        compressed_content = self._build_fs_block_content(
            nodes,
            path_prefix=path_prefix,
            node_id_to_alias=node_id_to_alias,
            render_options=render_options,
        )
        compressed_tokens = self.token_counter.count_text_tokens(compressed_content)
        if compressed_tokens < plain_tokens:
            return compressed_content, compressed_tokens
        return plain_content, plain_tokens

    def _count_fs_block_tokens(
        self,
        nodes: list[dict[str, Any]],
        render_options: FilesystemRenderOptions = _FS_DEFAULT_RENDER_OPTIONS,
    ) -> int:
        if not nodes:
            return 0
        return self._render_fs_block_content(
            nodes,
            render_options=render_options,
        )[1]

    def _make_fs_vertical_block(
        self,
        tree_id: str,
        nodes: list[dict[str, Any]],
        block_id: str,
        render_options: FilesystemRenderOptions = _FS_DEFAULT_RENDER_OPTIONS,
    ) -> Block:
        content, total_tokens = self._render_fs_block_content(
            nodes,
            render_options=render_options,
        )
        node_ids = [node["node_id"] for node in nodes]
        depths = [node.get("depth", 0) for node in nodes]
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

    def _make_fs_block(
        self,
        tree_id: str,
        nodes: list[dict[str, Any]],
        block_id: str,
        render_options: FilesystemRenderOptions = _FS_DEFAULT_RENDER_OPTIONS,
    ) -> Block:
        content, total_tokens = self._render_fs_block_content(
            nodes,
            render_options=render_options,
        )
        node_ids = [node["node_id"] for node in nodes]
        depths = [node.get("depth", 0) for node in nodes]
        return Block(
            block_id=block_id,
            block_type=BlockType.HORIZONTAL,
            tree_id=tree_id,
            depth_start=min(depths) if depths else 0,
            depth_end=max(depths) if depths else 0,
            node_ids=node_ids,
            total_tokens=total_tokens,
            max_tokens=self.max_tokens_per_block,
            cached_content=content,
            content_hash=hashlib.md5(content.encode()).hexdigest(),
        )

    def _get_fs_top_block_id(self, tree_id: str) -> str:
        return f"fs_top_{tree_id[:8]}"

    def _build_fs_prompt_context(
        self,
        tree_id: str,
        block: Block,
        allowed_node_ids: list[str],
        input_frontier: list[dict[str, str]],
        previous_top_candidate_ids: list[str],
    ) -> dict[str, Any]:
        node_id_to_alias, alias_to_node_id = self._build_fs_alias_maps(list(block.node_ids))
        block_node_set = set(block.node_ids)
        allowed_node_set = {node_id for node_id in allowed_node_ids if node_id in block_node_set}
        allowed_aliases = [
            node_id_to_alias[node_id]
            for node_id in block.node_ids
            if node_id in allowed_node_set
        ]
        blocked_aliases = [
            node_id_to_alias[node_id]
            for node_id in block.node_ids
            if node_id not in allowed_node_set
        ]

        if not blocked_aliases:
            selection_mode = "all"
            selection_aliases: list[str] = []
        elif len(blocked_aliases) <= len(allowed_aliases):
            selection_mode = "all_except"
            selection_aliases = blocked_aliases
        else:
            selection_mode = "subset"
            selection_aliases = allowed_aliases

        return {
            "alias_to_node_id": alias_to_node_id,
            "selection_mode": selection_mode,
            "selection_aliases": selection_aliases,
            "previous_top_candidate_ids": self._format_fs_node_refs(tree_id, previous_top_candidate_ids),
            "input_frontier": self._format_fs_frontier_refs(input_frontier),
        }

    def _format_fs_node_refs(self, tree_id: str, node_ids: list[str]) -> list[str]:
        if not node_ids:
            return []
        nodes = {node["node_id"]: node for node in self._get_nodes_by_ids(tree_id, node_ids)}
        labels: list[str] = []
        for node_id in node_ids:
            node = nodes.get(node_id)
            if not node:
                labels.append(node_id)
                continue
            labels.append(self._format_fs_node_ref(node, fallback=node_id))
        return labels

    def _format_fs_frontier_refs(self, frontier: list[dict[str, str]]) -> list[str]:
        refs: list[str] = []
        for frontier_node in frontier or []:
            refs.append(self._format_fs_frontier_ref(frontier_node))
        return refs

    def _format_fs_beam_refs(self, beams: list[dict[str, str]]) -> list[str]:
        return self._format_fs_frontier_refs(beams)

    def _collapse_fs_frontier(self, tree_id: str, frontier: list[dict[str, str]]) -> list[dict[str, str]]:
        collapsed: list[dict[str, str]] = []
        seen_node_ids: set[str] = set()
        for frontier_node in frontier or []:
            collapsed_frontier_node = self._collapse_fs_single_child_frontier_node(tree_id, frontier_node)
            node_id = collapsed_frontier_node.get("node_id", "")
            if node_id and node_id in seen_node_ids:
                continue
            if node_id:
                seen_node_ids.add(node_id)
            collapsed.append(collapsed_frontier_node)
        return collapsed if collapsed else [{"node_id": "", "title": "", "path": ""}]

    def _collapse_fs_beams(self, tree_id: str, beams: list[dict[str, str]]) -> list[dict[str, str]]:
        return self._collapse_fs_frontier(tree_id, beams)

    def _collapse_fs_single_child_frontier_node(self, tree_id: str, frontier_node: dict[str, str]) -> dict[str, str]:
        current = dict(frontier_node)
        seen_node_ids: set[str] = set()

        while True:
            node_id = current.get("node_id", "")
            if not node_id or node_id in seen_node_ids:
                return current
            seen_node_ids.add(node_id)

            children = self._get_direct_children_nodes(tree_id, node_id)
            if len(children) != 1:
                return current

            child = children[0]
            child_id = child.get("node_id", "")
            if not child_id or not self.storage.get_children(tree_id, child_id):
                return current
            if not self._is_virtual_fs_directory(child):
                return current

            current = self._make_fs_frontier_node(child)

    def _collapse_fs_single_child_beam(self, tree_id: str, beam: dict[str, str]) -> dict[str, str]:
        return self._collapse_fs_single_child_frontier_node(tree_id, beam)

    def _is_virtual_fs_directory_id(self, tree_id: str, node_id: str) -> bool:
        node = self.storage.get_node(tree_id, node_id)
        if not node:
            return False
        node_dict = node.to_dict()
        return self._is_virtual_fs_directory(node_dict)

    @staticmethod
    def _is_virtual_fs_directory(node: dict[str, Any]) -> bool:
        attrs = node.get("attrs") or {}
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except json.JSONDecodeError:
                attrs = {}
        if not attrs.get("is_dir", False):
            return False
        return bool(
            attrs.get("virtual")
            or attrs.get("json_expanded")
            or attrs.get("virtual_source")
        )

    @staticmethod
    def _format_fs_frontier_ref(frontier_node: dict[str, str]) -> str:
        path = (frontier_node.get("path") or "").strip()
        title = (frontier_node.get("title") or "").strip()
        if path and path != "root" and not path.startswith("/r/"):
            return path
        if title:
            return title
        if path:
            return path
        return frontier_node.get("node_id", "")

    @staticmethod
    def _format_fs_beam_ref(beam: dict[str, str]) -> str:
        return BlockRetrieverFilesystemSupport._format_fs_frontier_ref(beam)

    @staticmethod
    def _format_fs_node_ref(node: dict[str, Any], fallback: str) -> str:
        attrs = node.get("attrs") or {}
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except json.JSONDecodeError:
                attrs = {}
        rel_path = (attrs.get("rel_path") or "").strip()
        if rel_path:
            return rel_path
        path = (node.get("path") or "").strip()
        if path:
            return path
        return fallback

    @staticmethod
    def _make_fs_frontier_node(node: dict[str, Any]) -> dict[str, str]:
        attrs = node.get("attrs") or {}
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except json.JSONDecodeError:
                attrs = {}
        return {
            "node_id": node.get("node_id", ""),
            "title": attrs.get("title", ""),
            "path": attrs.get("rel_path", ""),
        }

    @staticmethod
    def _make_fs_beam(node: dict[str, Any]) -> dict[str, str]:
        return BlockRetrieverFilesystemSupport._make_fs_frontier_node(node)

    @staticmethod
    def _shared_fs_path_prefix(nodes: list[dict[str, Any]]) -> str:
        paths = []
        for node in nodes:
            attrs = node.get("attrs") or {}
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs)
                except json.JSONDecodeError:
                    attrs = {}
            rel_path = (attrs.get("rel_path") or "").strip("/")
            if rel_path:
                paths.append(rel_path.split("/"))

        if len(paths) < 2:
            return ""

        prefix_parts: list[str] = []
        for parts in zip(*paths):
            head = parts[0]
            if all(part == head for part in parts[1:]):
                prefix_parts.append(head)
                continue
            break

        if not prefix_parts:
            return ""
        return "/".join(prefix_parts) + "/"

    @staticmethod
    def _strip_fs_path_prefix(rel_path: str, path_prefix: str) -> str:
        rel_path = (rel_path or "").strip("/")
        if not rel_path:
            return ""
        if path_prefix and rel_path.startswith(path_prefix):
            stripped = rel_path[len(path_prefix):]
            if stripped:
                return stripped
        return rel_path

    @staticmethod
    def _row_to_node_dict(row) -> dict:
        node = {
            "node_id": row["node_id"],
            "parent_id": row["parent_id"],
            "slot": row["slot"],
            "depth": row["depth"],
            "path": row["path"],
            "attrs_json": row["attrs_json"],
        }
        if row["attrs_json"]:
            node["attrs"] = json.loads(row["attrs_json"])
        if row["payload_json"]:
            node["entity"] = {"payload": json.loads(row["payload_json"])}
        return node

    _NODE_QUERY = """SELECT n.node_id, n.parent_id, n.slot, n.depth, n.path, n.attrs_json, e.payload_json
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
                f"{self._NODE_QUERY} WHERE n.tree_id = ? AND n.node_id IN ({placeholders})",
                (tree_id, *chunk),
            )
            rows_by_id = {row["node_id"]: self._row_to_node_dict(row) for row in cursor.fetchall()}
            results.extend(rows_by_id[node_id] for node_id in chunk if node_id in rows_by_id)
        return results

    def _get_direct_children_nodes(self, tree_id: str, node_id: str) -> list[dict]:
        cursor = self.storage.conn.cursor()
        cursor.execute(
            f"{self._NODE_QUERY} WHERE n.tree_id = ? AND n.parent_id = ? ORDER BY n.slot",
            (tree_id, node_id),
        )
        return [self._row_to_node_dict(row) for row in cursor.fetchall()]
FsRenderOptions = FilesystemRenderOptions
