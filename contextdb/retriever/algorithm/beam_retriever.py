"""Beam search retriever — keep top-k paths instead of a single path."""

import json
from pathlib import Path
from typing import Any

from jinja2 import Template

from contextdb.logger import get_logger
from contextdb.retriever.algorithm.base_retriever import BaseRetriever
from contextdb.retriever.algorithm.ranker import BM25PathRanker, Ranker, has_path_evidence
from contextdb.retriever.base import RetrievalResult

log = get_logger(__name__)

# Prompt template for LLM ranking (kept in prompts/ for easy editing).
PROMPT = Template((Path(__file__).parent.parent.parent / "prompts/beam.jinja").read_text())
PROMPT_FS = Template((Path(__file__).parent.parent.parent / "prompts/beam_fs.jinja").read_text())

# Tool schema: LLM returns ranked node ids and can signal "done".
TOOLS = [
    {
        "name": "rank",
        "description": "Rank candidate node ids for the query",
        "input_schema": {
            "type": "object",
            "properties": {"ranked_ids": {"type": "array", "items": {"type": "string"}}, "done": {"type": "boolean"}},
            "required": ["ranked_ids"],
        },
    }
]


class BeamRetriever(BaseRetriever):
    """Beam search over the tree with LLM as the ranking judge."""

    def __init__(self, storage, llm, mode: str = "document", ranker: Ranker | None = None, fs_ranker: str = "none"):
        super().__init__(storage, llm)
        if fs_ranker not in {"bm25", "none"}:
            raise ValueError(f"Unknown filesystem ranker: {fs_ranker!r}")
        if mode == "filesystem" and fs_ranker == "bm25" and ranker is None:
            ranker = BM25PathRanker(storage)
        self.mode = mode
        self.ranker = ranker
        self.fs_ranker = fs_ranker
        self._entity_cache: dict[str, dict[str, Any]] = {}
        self._cached_tree_id: str = ""

    def retrieve(
        self, tree_id: str, query: str, beam_size: int = None, max_turns: int = None, select_k: int = 1
    ) -> RetrievalResult:
        """
        Run beam search on a tree.
        - beam_size: how many frontier paths to keep each step
        - max_turns: optional cap; when None, use full tree depth as upper bound
        - select_k: final number of top file results to return
        """
        root_id = self.storage.get_root_id(tree_id)
        if not root_id:
            return RetrievalResult([], [], [], 0)

        if tree_id != self._cached_tree_id:
            self._entity_cache.clear()
            self._cached_tree_id = tree_id

        if max_turns is None:
            max_turns = self._tree_max_depth(tree_id)

        log.debug("start beam_size=%s max_turns=%s select_k=%s query=%s", beam_size, max_turns, select_k, query[:50])

        result_limit = max(1, int(select_k or 1))
        frontier = [{"node_id": root_id, "titles": [], "parent_summary": ""}]
        top_candidate_ids: list[str] = []
        trace: list[dict[str, Any]] = []

        for turn in range(max_turns):
            candidate_set = []

            # Expand frontier nodes into the candidate set shown to the LLM.
            log.debug("turn %d: expanding %d frontier nodes", turn, len(frontier))
            for frontier_node in frontier:
                # Get parent node's summary for context carrying
                parent_summary = frontier_node.get("parent_summary", "")
                children = self.storage.get_children(tree_id, frontier_node["node_id"])
                if not children:
                    # Leaf node: mark is_leaf=True to exclude from next frontier.
                    candidate_set.append(self._candidate_from_node(
                        tree_id, frontier_node["node_id"], frontier_node["titles"], parent_summary, is_leaf=True))
                    log.debug("  leaf: %s", frontier_node["node_id"][:8])
                    continue

                for child in children:
                    candidate_set.append(self._candidate_from_child(
                        tree_id, child, frontier_node["titles"], parent_summary))

            if not candidate_set:
                log.debug("turn %d: no candidates, stopping", turn)
                break

            # Check if all frontier nodes are leaves (no children to expand)
            # If so, stop early - no point asking LLM whether to continue
            all_leaves = all(
                not self.storage.get_children(tree_id, frontier_node["node_id"]) for frontier_node in frontier
            )
            if all_leaves and len(frontier) > 0:
                log.debug("turn %d: all frontier nodes are leaves, stopping early", turn)
                # Add leaf nodes to top candidates before stopping.
                for frontier_node in frontier:
                    if len(top_candidate_ids) < result_limit and frontier_node["node_id"] not in top_candidate_ids:
                        top_candidate_ids.append(frontier_node["node_id"])
                break

            # Show candidate set
            candidate_set = self._order_fs_candidates(candidate_set, query, tree_id)
            log.debug("turn %d: %d candidates:", turn, len(candidate_set))
            for c in candidate_set[:10]:
                log.debug("  [%s] %s", c["node_id"][:8], c["title"] or c["path"])

            pick_limit = len(candidate_set) if beam_size is None else max(beam_size, result_limit)
            ranked_ids, done = self._rank_with_llm(
                query,
                candidate_set,
                top_candidate_ids,
                pick_limit=pick_limit,
            )

            # Build lookup map for O(1) access
            candidate_map = {c["node_id"]: c for c in candidate_set}

            # Guard against premature termination in filesystem mode: the LLM must
            # commit to at least one non-directory before we accept done=True.
            # Otherwise we'd stop after picking a directory and return no files.
            if done and self.mode == "filesystem":
                picked_file = any(
                    not candidate_map.get(nid, {}).get("is_dir", False)
                    for nid in ranked_ids
                )
                if not picked_file:
                    log.debug("turn %d: overriding done=True (all ranked are directories)", turn)
                    done = False

            # Show LLM decision
            log.debug("turn %d: LLM ranked top-%d, done=%s", turn, len(ranked_ids), done)
            for i, nid in enumerate(ranked_ids[:5]):
                c = candidate_map.get(nid)
                if c:
                    log.debug("  #%d [%s] %s", i + 1, nid[:8], c["title"])

            # Add file-like ranked ids until the final result limit is reached.
            remaining_result_slots = max(0, result_limit - len(top_candidate_ids))
            if self.mode == "filesystem":
                ranked_file_ids = [
                    node_id
                    for node_id in ranked_ids
                    if not candidate_map.get(node_id, {}).get("is_dir", False)
                ]
                top_ids_this_turn = ranked_file_ids[:remaining_result_slots]
            else:
                top_ids_this_turn = ranked_ids[:remaining_result_slots]
            for node_id in top_ids_this_turn:
                if node_id not in top_candidate_ids:
                    top_candidate_ids.append(node_id)

            if len(top_candidate_ids) >= result_limit:
                next_frontier = []
                done = True
            else:
                # Keep the next frontier set (directories / expandable nodes only).
                next_frontier = []
                seen = set()
                for node_id in ranked_ids:
                    if node_id in seen:
                        continue
                    seen.add(node_id)
                    cand = candidate_map[node_id]
                    if cand.get("is_leaf") or (self.mode == "filesystem" and not cand.get("is_dir", False)):
                        # Leaf/file nodes go to top candidates, not next frontier.
                        if self.mode != "filesystem" and node_id not in top_candidate_ids:
                            top_candidate_ids.append(node_id)
                        continue
                    next_frontier.append({
                        "node_id": cand["node_id"],
                        "titles": cand["path_titles"],
                        "parent_summary": cand.get("summary", ""),
                    })
                    if beam_size is not None and len(next_frontier) >= max(1, beam_size):
                        break
                if beam_size is None:
                    for cand in candidate_set:
                        if cand["node_id"] in seen or cand.get("is_leaf"):
                            continue
                        if self.mode == "filesystem" and not cand.get("is_dir", False):
                            continue
                        seen.add(cand["node_id"])
                        next_frontier.append({
                            "node_id": cand["node_id"],
                            "titles": cand["path_titles"],
                            "parent_summary": cand.get("summary", ""),
                        })

            trace.append({
                "turn": turn,
                "candidates": len(candidate_set),
                "top_candidate_ids": len(top_candidate_ids),
                "frontier": len(next_frontier),
                "kept": len(next_frontier),
                "done": done,
            })

            frontier = next_frontier
            if done:
                log.debug("turn %d: done=True, stopping", turn)
                break

        # In filesystem mode, top candidates should already be files; keep this as
        # a defensive filter for old traces or unusual adapters.
        if self.mode == "filesystem":
            file_top_candidate_ids = []
            for node_id in top_candidate_ids:
                node = self.storage.get_node(tree_id, node_id)
                if node and node.attrs_json:
                    try:
                        attrs = json.loads(node.attrs_json)
                    except json.JSONDecodeError:
                        attrs = {}
                    if attrs.get("is_dir", False):
                        continue
                file_top_candidate_ids.append(node_id)
            top_candidate_ids = file_top_candidate_ids if file_top_candidate_ids else top_candidate_ids

        # Final results
        contents = []
        top_candidate_ids = top_candidate_ids[:result_limit]

        log.debug("=== retrieval complete: %d top candidates ===", len(top_candidate_ids))
        for node_id in top_candidate_ids:
            entity = self.storage.get_entity(tree_id, node_id)
            if entity:
                payload = json.loads(entity.payload_json)
                contents.append({"node_id": node_id, "content": payload})
                title = payload.get("title", "")
                text = (payload.get("text") or payload.get("summary") or "")[:100]
                log.debug("  [%s] %s: %s...", node_id[:8], title, text)

        return RetrievalResult(top_candidate_ids, contents, trace, len(trace))

    def _rank_with_llm(
        self, query: str, candidates: list[dict[str, Any]], top_candidate_ids: list[str], pick_limit: int
    ) -> tuple[list[str], bool]:
        """
        Ask the LLM to rank candidates. Returns (ranked_ids, done).
        This is the core "LLM as judge" step.
        """
        tmpl = PROMPT_FS if self.mode == "filesystem" else PROMPT
        prompt = tmpl.render(
            query=query,
            candidates=candidates,
            top_candidate_ids=top_candidate_ids,
            pick_limit=pick_limit,
        )
        resp = self.llm.chat([{"role": "user", "content": prompt}], tools=TOOLS)
        for block in resp.get("content", []):
            if block.get("type") != "tool_use":
                continue
            if block.get("name") != "rank":
                continue
            tool_input = block.get("input", {}) or {}
            ranked_ids = tool_input.get("ranked_ids", []) or []
            done = bool(tool_input.get("done", False))
            # Keep only ids that actually exist in candidates.
            known = {c["node_id"] for c in candidates}
            ranked = [nid for nid in ranked_ids if nid in known]
            return ranked, done
        raise ValueError("LLM did not return a rank tool call")

    def _order_fs_candidates(
        self,
        candidates: list[dict[str, Any]],
        query: str,
        tree_id: str,
    ) -> list[dict[str, Any]]:
        if self.mode != "filesystem" or not candidates:
            return candidates
        if self.fs_ranker == "none":
            return list(candidates)
        if not has_path_evidence(candidates, query):
            return list(candidates)

        scores = self.ranker.rank(
            query,
            candidates,
            context={"mode": "filesystem", "tree_id": tree_id},
        )
        score_by_id = {candidate["node_id"]: score for candidate, score in scores}
        ordered = sorted(
            enumerate(candidates),
            key=lambda item: (
                -score_by_id.get(item[1].get("node_id", ""), 0.0),
                self._fs_ranker_tie_key(item[1], item[0]),
            ),
        )
        return [candidate for _, candidate in ordered]

    @staticmethod
    def _fs_ranker_tie_key(candidate: dict[str, Any], original_index: int) -> tuple[int, int, str, int]:
        is_file = 0 if candidate.get("is_dir", False) else 1
        depth = int(candidate.get("depth", 0) or 0)
        rel_path = str(candidate.get("rel_path") or candidate.get("path") or "").lower()
        return (is_file, depth, rel_path, original_index)

    def _candidate_from_child(
        self, tree_id: str, child, parent_titles: list[str], parent_summary: str = "", is_leaf: bool = False
    ) -> dict[str, Any]:
        """Build a candidate dict from a child node (for the LLM prompt)."""
        attrs = self._node_attrs(child)
        title = attrs.get("title") or ""
        summary = attrs.get("summary") or ""
        text = "" if self.mode == "filesystem" else self._node_text(tree_id, child.node_id)
        path_titles = parent_titles + ([title] if title else [])
        cand = {
            "node_id": child.node_id,
            "title": title,
            "summary": summary,
            "text": text[:200] if text else "",
            "parent_summary": parent_summary,
            "page_start": attrs.get("page_start"),
            "page_end": attrs.get("page_end"),
            "depth": child.depth,
            "path": " > ".join(path_titles),
            "path_titles": path_titles,
            "is_leaf": is_leaf,
        }
        if self.mode == "filesystem":
            cand["rel_path"] = attrs.get("rel_path", "")
            cand["is_dir"] = attrs.get("is_dir", False)
            cand["summary"] = ""
        return cand

    def _candidate_from_node(
        self, tree_id: str, node_id: str, parent_titles: list[str], parent_summary: str = "", is_leaf: bool = False
    ) -> dict[str, Any]:
        """Build a candidate dict from an existing node id (leaf case)."""
        node = self.storage.get_node(tree_id, node_id)
        if not node:
            return {
                "node_id": node_id,
                "title": "",
                "summary": "",
                "text": "",
                "path": " > ".join(parent_titles),
                "path_titles": parent_titles,
                "parent_summary": parent_summary,
                "is_leaf": is_leaf,
            }
        return self._candidate_from_child(tree_id, node, parent_titles, parent_summary, is_leaf)

    def _node_attrs(self, node) -> dict[str, Any]:
        """Parse attrs_json from Node into a dict (safe for None)."""
        if not getattr(node, "attrs_json", None):
            return {}
        try:
            return json.loads(node.attrs_json) if node.attrs_json else {}
        except json.JSONDecodeError:
            return {}

    def _node_text(self, tree_id: str, node_id: str) -> str:
        """Fetch text/content from entity payload if present (cached)."""
        if node_id in self._entity_cache:
            payload = self._entity_cache[node_id]
        else:
            entity = self.storage.get_entity(tree_id, node_id)
            payload = json.loads(entity.payload_json) if entity else {}
            self._entity_cache[node_id] = payload
        return payload.get("text") or payload.get("content") or ""

    def _tree_max_depth(self, tree_id: str) -> int:
        """Fetch the deepest node depth in this tree (upper bound for steps)."""
        cursor = self.storage.conn.cursor()
        cursor.execute("SELECT MAX(depth) AS max_depth FROM nodes WHERE tree_id=?", (tree_id,))
        row = cursor.fetchone()
        return row["max_depth"] if row and row["max_depth"] is not None else 0
