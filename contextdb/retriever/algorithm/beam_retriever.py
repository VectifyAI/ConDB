"""Beam search retriever — keep top-k paths instead of a single path."""

import json
from pathlib import Path
from typing import Any

from jinja2 import Template

from contextdb.logger import get_logger
from contextdb.retriever.algorithm.base_retriever import BaseRetriever
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
            "properties": {"selected": {"type": "array", "items": {"type": "string"}}, "done": {"type": "boolean"}},
            "required": ["selected"],
        },
    }
]


class BeamRetriever(BaseRetriever):
    """Beam search over the tree with LLM as the ranking judge."""

    def __init__(self, storage, llm, mode: str = "document"):
        super().__init__(storage, llm)
        self.mode = mode
        self._entity_cache: dict[str, dict[str, Any]] = {}
        self._cached_tree_id: str = ""

    def retrieve(
        self, tree_id: str, query: str, beam_size: int = None, max_turns: int = None, select_k: int = 1
    ) -> RetrievalResult:
        """
        Run beam search on a tree.
        - beam_size: how many paths to keep each step (k in beam search)
        - max_turns: optional cap; when None, use full tree depth as upper bound
        - select_k: how many top candidates to keep as "answers" each step
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

        beams = [{"node_id": root_id, "titles": [], "parent_summary": ""}]
        selected: list[str] = []
        trace: list[dict[str, Any]] = []

        for turn in range(max_turns):
            candidates = []

            # Expand beams
            log.debug("turn %d: expanding %d beams", turn, len(beams))
            for beam in beams:
                # Get parent node's summary for context carrying
                parent_summary = beam.get("parent_summary", "")
                children = self.storage.get_children(tree_id, beam["node_id"])
                if not children:
                    # Leaf node: mark is_leaf=True to exclude from next beams
                    candidates.append(self._candidate_from_node(
                        tree_id, beam["node_id"], beam["titles"], parent_summary, is_leaf=True))
                    log.debug("  leaf: %s", beam["node_id"][:8])
                    continue

                for child in children:
                    candidates.append(self._candidate_from_child(
                        tree_id, child, beam["titles"], parent_summary))

            if not candidates:
                log.debug("turn %d: no candidates, stopping", turn)
                break

            # Check if all beams are leaves (no children to expand)
            # If so, stop early - no point asking LLM whether to continue
            all_leaves = all(
                not self.storage.get_children(tree_id, beam["node_id"]) for beam in beams
            )
            if all_leaves and len(beams) > 0:
                log.debug("turn %d: all beams are leaves, stopping early", turn)
                # Add leaf nodes to selected before stopping
                for beam in beams:
                    if beam["node_id"] not in selected:
                        selected.append(beam["node_id"])
                break

            # Show candidates
            log.debug("turn %d: %d candidates:", turn, len(candidates))
            for c in candidates[:10]:
                log.debug("  [%s] %s", c["node_id"][:8], c["title"] or c["path"])

            k = len(candidates) if beam_size is None else max(beam_size, select_k)
            ranked_ids, done = self._rank_with_llm(query, candidates, selected, k=k)

            # Build lookup map for O(1) access
            candidates_map = {c["node_id"]: c for c in candidates}

            # Show LLM decision
            log.debug("turn %d: LLM ranked top-%d, done=%s", turn, len(ranked_ids), done)
            for i, nid in enumerate(ranked_ids[:5]):
                c = candidates_map.get(nid)
                if c:
                    log.debug("  #%d [%s] %s", i + 1, nid[:8], c["title"])

            # Pick top candidates as "selected" (answers).
            # In fs mode when done, keep all ranked files (not just top-k)
            keep_n = len(ranked_ids) if (self.mode == "filesystem" and done) else max(1, select_k)
            for node_id in ranked_ids[:keep_n]:
                if node_id not in selected:
                    selected.append(node_id)

            # Keep the next beam set (exclude leaf nodes)
            next_beams = []
            seen = set()
            for node_id in ranked_ids:
                if node_id in seen:
                    continue
                seen.add(node_id)
                cand = candidates_map[node_id]
                if cand.get("is_leaf"):
                    # Leaf nodes go to selected, not next_beams
                    if node_id not in selected:
                        selected.append(node_id)
                    continue
                next_beams.append({
                    "node_id": cand["node_id"],
                    "titles": cand["path_titles"],
                    "parent_summary": cand.get("summary", ""),
                })
                if beam_size is not None and len(next_beams) >= max(1, beam_size):
                    break
            if beam_size is None:
                for cand in candidates:
                    if cand["node_id"] in seen or cand.get("is_leaf"):
                        continue
                    seen.add(cand["node_id"])
                    next_beams.append({
                        "node_id": cand["node_id"],
                        "titles": cand["path_titles"],
                        "parent_summary": cand.get("summary", ""),
                    })

            trace.append({"turn": turn, "candidates": len(candidates), "kept": len(next_beams), "done": done})

            beams = next_beams
            if done:
                log.debug("turn %d: done=True, stopping", turn)
                break

        # In filesystem mode, filter directories and keep only files
        if self.mode == "filesystem":
            file_selected = []
            for node_id in selected:
                node = self.storage.get_node(tree_id, node_id)
                if node and node.attrs_json:
                    try:
                        attrs = json.loads(node.attrs_json)
                    except json.JSONDecodeError:
                        attrs = {}
                    if attrs.get("is_dir", False):
                        continue
                file_selected.append(node_id)
            selected = file_selected if file_selected else selected

        # Final results
        contents = []
        log.debug("=== retrieval complete: %d nodes selected ===", len(selected))
        for node_id in selected:
            entity = self.storage.get_entity(tree_id, node_id)
            if entity:
                payload = json.loads(entity.payload_json)
                contents.append({"node_id": node_id, "content": payload})
                title = payload.get("title", "")
                text = (payload.get("text") or payload.get("summary") or "")[:100]
                log.debug("  [%s] %s: %s...", node_id[:8], title, text)

        return RetrievalResult(selected, contents, trace, len(trace))

    def _rank_with_llm(
        self, query: str, candidates: list[dict[str, Any]], selected: list[str], k: int
    ) -> tuple[list[str], bool]:
        """
        Ask the LLM to rank candidates. Returns (ranked_ids, done).
        This is the core "LLM as judge" step.
        """
        tmpl = PROMPT_FS if self.mode == "filesystem" else PROMPT
        prompt = tmpl.render(query=query, candidates=candidates, selected=selected, k=k)
        resp = self.llm.chat([{"role": "user", "content": prompt}], tools=TOOLS)
        for block in resp.get("content", []):
            if block.get("type") != "tool_use":
                continue
            if block.get("name") != "rank":
                continue
            ranked_ids = block.get("input", {}).get("selected", []) or []
            done = bool(block.get("input", {}).get("done", False))
            # Keep only ids that actually exist in candidates.
            known = {c["node_id"] for c in candidates}
            ranked = [nid for nid in ranked_ids if nid in known]
            return ranked, done
        raise ValueError("LLM did not return a rank tool call")

    def _candidate_from_child(
        self, tree_id: str, child, parent_titles: list[str], parent_summary: str = "", is_leaf: bool = False
    ) -> dict[str, Any]:
        """Build a candidate dict from a child node (for the LLM prompt)."""
        attrs = self._node_attrs(child)
        title = attrs.get("title") or ""
        summary = attrs.get("summary") or ""
        text = self._node_text(tree_id, child.node_id)
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
            cand["tag"] = attrs.get("tag", "")
            cand["is_dir"] = attrs.get("is_dir", False)
            if not summary:
                cand["summary"] = text[:200] if text else ""
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
