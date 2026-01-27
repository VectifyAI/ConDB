"""Beam search retriever - keep top-k paths instead of a single path."""

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

    def __init__(self, storage, llm):
        # storage must provide get_root_id/get_children/get_entity
        super().__init__(storage, llm)
        # llm must follow LLMProtocol.chat(messages, tools=...)
        self._entity_cache: dict[str, dict[str, Any]] = {}

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

        # Clear cache to prevent memory leak across multiple retrieve calls
        self._entity_cache.clear()

        if max_turns is None:
            max_turns = self._tree_max_depth(tree_id)

        log.debug("start beam_size=%s max_turns=%s query=%s", beam_size, max_turns, query[:50])

        beams = [{"node_id": root_id, "titles": []}]
        selected: list[str] = []
        trace: list[dict[str, Any]] = []

        for turn in range(max_turns):
            candidates = []

            # Expand beams
            log.debug("turn %d: expanding %d beams", turn, len(beams))
            for beam in beams:
                children = self.storage.get_children(tree_id, beam["node_id"])
                if not children:
                    candidates.append(self._candidate_from_node(tree_id, beam["node_id"], beam["titles"]))
                    log.debug("  leaf: %s", beam["node_id"][:8])
                    continue

                for child in children:
                    candidates.append(self._candidate_from_child(tree_id, child, beam["titles"]))

            if not candidates:
                log.debug("turn %d: no candidates, stopping", turn)
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
            for node_id in ranked_ids[: max(1, select_k)]:
                if node_id not in selected:
                    selected.append(node_id)

            # Keep the next beam set.
            next_beams = []
            seen = set()
            for node_id in ranked_ids:
                if node_id in seen:
                    continue
                seen.add(node_id)
                cand = candidates_map[node_id]
                next_beams.append({"node_id": cand["node_id"], "titles": cand["path_titles"]})
                if beam_size is not None and len(next_beams) >= max(1, beam_size):
                    break
            if beam_size is None:
                for cand in candidates:
                    if cand["node_id"] in seen:
                        continue
                    seen.add(cand["node_id"])
                    next_beams.append({"node_id": cand["node_id"], "titles": cand["path_titles"]})

            trace.append({"turn": turn, "candidates": len(candidates), "kept": len(next_beams), "done": done})

            beams = next_beams
            if done:
                log.debug("turn %d: done=True, stopping", turn)
                break

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
        prompt = PROMPT.render(query=query, candidates=candidates, selected=selected, k=k)
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

    def _candidate_from_child(self, tree_id: str, child, parent_titles: list[str]) -> dict[str, Any]:
        """Build a candidate dict from a child node (for the LLM prompt)."""
        attrs = self._node_attrs(child)
        title = attrs.get("title") or ""
        summary = attrs.get("summary") or ""
        text = self._node_text(tree_id, child.node_id)
        path_titles = parent_titles + ([title] if title else [])
        return {
            "node_id": child.node_id,
            "title": title,
            "summary": summary,
            "text": text[:200] if text else "",
            "page_start": attrs.get("page_start"),
            "page_end": attrs.get("page_end"),
            "depth": child.depth,
            "path": " > ".join(path_titles),
            "path_titles": path_titles,
        }

    def _candidate_from_node(self, tree_id: str, node_id: str, parent_titles: list[str]) -> dict[str, Any]:
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
            }
        return self._candidate_from_child(tree_id, node, parent_titles)

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
