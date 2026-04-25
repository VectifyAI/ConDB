"""Filesystem-specific helpers for BlockRetriever."""

import hashlib
import json
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Optional

from contextdb.retriever.algorithm.block_types import Block, BlockResult, BlockType


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
        root_beam: dict[str, str],
        query: str = "",
    ) -> list[dict[str, Any]]:
        root_id = root_beam.get("node_id")
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
            beam=root_beam,
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
        beams: list[dict[str, str]],
        query: str = "",
    ) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for beam in beams:
            beam_id = beam.get("node_id")
            if not beam_id:
                continue

            children = self._order_fs_nodes_for_query(
                self._get_direct_children_nodes(tree_id, beam_id),
                query,
            )
            if not children:
                continue

            specs.extend(
                self._build_fs_block_specs(
                    tree_id=tree_id,
                    beam=beam,
                    nodes=children,
                    base_block_id=f"fs_sub_{beam_id[:8]}",
                    render_options=_FS_DEFAULT_RENDER_OPTIONS,
                    always_suffix=True,
                )
            )
        return specs

    def _build_fs_block_specs(
        self,
        tree_id: str,
        beam: dict[str, str],
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
                "beam": beam,
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

    def _append_fs_selected_blocks(
        self,
        cache_window: deque[dict[str, Any]],
        total_tokens: int,
        block_rows: list[tuple[dict[str, Any], BlockResult, int, dict[str, int]]],
        beams: list[dict[str, str]],
        cache_block: bool,
        pin_block: bool = False,
    ) -> int:
        if not self.cache_enabled or not block_rows:
            return total_tokens

        selected_node_ids = {
            beam.get("node_id")
            for beam in beams
            if beam.get("node_id")
        }
        if not selected_node_ids:
            return total_tokens

        appended_block_ids: set[str] = set()
        for spec, _, _, _ in block_rows:
            block = spec["block"]
            if block.block_id in appended_block_ids:
                continue
            if not selected_node_ids.intersection(block.node_ids):
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
        previous_selected: list[str],
        k: int,
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
                input_beams=[spec["beam"]],
                previous_selected=previous_selected,
                allowed_node_ids=block.node_ids,
                k=k,
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
                        [spec["beam"]],
                        previous_selected,
                        block.node_ids,
                        k,
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

    _FS_QUERY_TOKEN_RE = re.compile(r"[a-z0-9]+")
    _FS_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
    _FS_SOURCE_DIRS = frozenset({
        "app",
        "apps",
        "contextdb",
        "lib",
        "libs",
        "package",
        "packages",
        "src",
    })
    _FS_TEST_DIRS = frozenset({"spec", "specs", "test", "tests"})
    _FS_LOW_VALUE_DIRS = frozenset({
        ".cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        "docs",
        "examples",
        "vendor",
    })

    def _order_fs_nodes_for_query(self, nodes: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
        """Order exchangeable filesystem siblings by query-dependent priority."""
        if not nodes:
            return []

        fs_ranker = getattr(self, "fs_ranker", "heuristic")
        if fs_ranker == "none":
            return list(nodes)

        if fs_ranker == "bm25":
            candidates = [self._fs_ranker_candidate(node) for node in nodes]
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

        query_tokens = self._tokenize_fs_query(query)
        if not query_tokens:
            return list(nodes)

        ordered = sorted(
            enumerate(nodes),
            key=lambda item: self._fs_node_sort_key(item[1], query_tokens, item[0]),
        )
        return [node for _, node in ordered]

    def _fs_ranker_candidate(self, node: dict[str, Any]) -> dict[str, Any]:
        attrs = self._fs_node_attrs(node)
        rel_path = (attrs.get("rel_path") or node.get("path") or "").strip("/")
        title = (attrs.get("title") or rel_path.rsplit("/", 1)[-1]).strip()
        is_dir = bool(attrs.get("is_dir", False))
        return {
            **node,
            "title": title,
            "rel_path": rel_path,
            "tag": attrs.get("tag", ""),
            "summary": attrs.get("summary", ""),
            "is_dir": is_dir,
            "is_leaf": not is_dir,
        }

    def _fs_ranker_tie_key(self, node: dict[str, Any], original_index: int) -> tuple[int, int, str, int]:
        attrs = self._fs_node_attrs(node)
        rel_path = (attrs.get("rel_path") or node.get("path") or "").strip("/")
        is_file = 0 if attrs.get("is_dir", False) else 1
        depth = int(node.get("depth", 0) or 0)
        return (is_file, depth, rel_path.lower(), original_index)

    def _fs_node_sort_key(
        self,
        node: dict[str, Any],
        query_tokens: set[str],
        original_index: int,
    ) -> tuple[float, int, int, str, int]:
        attrs = self._fs_node_attrs(node)
        score = self._score_fs_node_for_query(node, attrs, query_tokens)
        rel_path = (attrs.get("rel_path") or node.get("path") or "").strip("/")
        is_file = 0 if attrs.get("is_dir", False) else 1
        depth = int(node.get("depth", 0) or 0)
        return (-score, is_file, depth, rel_path.lower(), original_index)

    def _score_fs_node_for_query(
        self,
        node: dict[str, Any],
        attrs: dict[str, Any],
        query_tokens: set[str],
    ) -> float:
        rel_path = (attrs.get("rel_path") or node.get("path") or "").strip("/")
        title = (attrs.get("title") or rel_path.rsplit("/", 1)[-1]).strip()
        tag = (attrs.get("tag") or "").lower()
        is_dir = bool(attrs.get("is_dir", False))

        entity = node.get("entity", {})
        payload = entity.get("payload", {}) if isinstance(entity, dict) else {}
        summary = str(payload.get("summary") or "") if is_dir else ""

        path_text = f"{rel_path} {title}"
        path_tokens = self._tokenize_fs_query(path_text)
        summary_tokens = self._tokenize_fs_query(summary)
        path_lower = path_text.lower()
        summary_lower = summary.lower()
        basename = title.lower()

        score = 0.0
        matched_tokens = query_tokens.intersection(path_tokens)
        score += 40.0 * len(matched_tokens)

        for token in query_tokens:
            if token == basename:
                score += 60.0
            if token and token in path_lower:
                score += 15.0
            if token and token in summary_lower:
                score += 5.0

        if query_tokens and query_tokens.issubset(path_tokens):
            score += 80.0
        if query_tokens.intersection(summary_tokens):
            score += 8.0 * len(query_tokens.intersection(summary_tokens))

        path_parts = [part.lower() for part in rel_path.split("/") if part]
        first_part = path_parts[0] if path_parts else ""
        asks_for_tests = bool(query_tokens.intersection({"spec", "specs", "test", "tests", "testing", "pytest"}))
        asks_for_docs = bool(query_tokens.intersection({"doc", "docs", "documentation", "readme"}))

        if tag == "[src]" or first_part in self._FS_SOURCE_DIRS:
            score += 10.0
        if not asks_for_tests and (tag == "[test]" or any(part in self._FS_TEST_DIRS for part in path_parts)):
            score -= 30.0
        if asks_for_tests and (tag == "[test]" or any(part in self._FS_TEST_DIRS for part in path_parts)):
            score += 25.0
        if asks_for_docs and (tag == "[doc]" or first_part == "docs"):
            score += 25.0
        if not asks_for_docs and first_part in self._FS_LOW_VALUE_DIRS:
            score -= 8.0

        if is_dir and matched_tokens:
            score += 12.0
        elif is_dir:
            score += 2.0

        return score

    @classmethod
    def _tokenize_fs_query(cls, text: str) -> set[str]:
        if not text:
            return set()
        split_text = cls._FS_CAMEL_BOUNDARY_RE.sub(" ", str(text))
        return set(cls._FS_QUERY_TOKEN_RE.findall(split_text.lower()))

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
            tag = attrs.get("tag", "")
            display_path = self._strip_fs_path_prefix(rel_path, path_prefix)

            lines.append(f"- id: {alias}")
            if display_path:
                lines.append(f"  path: {display_path}")
            if is_dir:
                lines.append("  type: directory")
            else:
                lines.append("  type: file")
            if tag:
                lines.append(f"  tag: {tag}")

            entity = node.get("entity", {})
            payload = entity.get("payload", {}) if isinstance(entity, dict) else {}
            summary = payload.get("summary", "")
            if summary and (
                (is_dir and render_options.include_dir_summaries)
                or ((not is_dir) and render_options.include_file_summaries)
            ):
                lines.append(f"  summary: {summary[:render_options.summary_max_chars]}")

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
        input_beams: list[dict[str, str]],
        previous_selected: list[str],
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
            "previous_selected": self._format_fs_node_refs(tree_id, previous_selected),
            "input_beams": self._format_fs_beam_refs(input_beams),
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

    def _format_fs_beam_refs(self, beams: list[dict[str, str]]) -> list[str]:
        refs: list[str] = []
        for beam in beams or []:
            refs.append(self._format_fs_beam_ref(beam))
        return refs

    def _collapse_fs_beams(self, tree_id: str, beams: list[dict[str, str]]) -> list[dict[str, str]]:
        collapsed: list[dict[str, str]] = []
        seen_node_ids: set[str] = set()
        for beam in beams or []:
            collapsed_beam = self._collapse_fs_single_child_beam(tree_id, beam)
            node_id = collapsed_beam.get("node_id", "")
            if node_id and node_id in seen_node_ids:
                continue
            if node_id:
                seen_node_ids.add(node_id)
            collapsed.append(collapsed_beam)
        return collapsed if collapsed else [{"node_id": "", "title": "", "path": ""}]

    def _collapse_fs_single_child_beam(self, tree_id: str, beam: dict[str, str]) -> dict[str, str]:
        current = dict(beam)
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

            current = self._make_fs_beam(child)

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
    def _format_fs_beam_ref(beam: dict[str, str]) -> str:
        path = (beam.get("path") or "").strip()
        title = (beam.get("title") or "").strip()
        if path and path != "root" and not path.startswith("/r/"):
            return path
        if title:
            return title
        if path:
            return path
        return beam.get("node_id", "")

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
    def _make_fs_beam(node: dict[str, Any]) -> dict[str, str]:
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
FsRenderOptions = FilesystemRenderOptions
