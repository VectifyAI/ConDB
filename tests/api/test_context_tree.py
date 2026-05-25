"""Tests for ContextTree compatibility behavior."""

from types import SimpleNamespace

import pytest

from contextdb import ContextTree

SAMPLE_PAGEINDEX = {
    "doc_name": "Compat Test Doc",
    "doc_description": "Compatibility checks",
    "structure": [
        {
            "node_id": "n1",
            "title": "Section 1",
            "summary": "Overview",
            "text": "Hello world",
            "start_index": 1,
            "end_index": 1,
            "nodes": [],
        }
    ],
}


class FakeLLM:
    @staticmethod
    def _response():
        return {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "rank", "input": {"ranked_ids": [], "done": True}}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }

    def chat(self, messages, system="", tools=None, cache_key=None):
        return self._response()

    def chat_with_cache(
        self,
        messages,
        system="",
        tools=None,
        cache_content=None,
        non_cached_content=None,
        cache_key=None,
    ):
        return self._response()


@pytest.mark.parametrize("use_block_retriever", [False, True])
def test_query_accepts_legacy_view_depth_kwarg(use_block_retriever):
    ct = ContextTree(":memory:", llm=FakeLLM())
    try:
        tree_id = ct.index_pageindex(SAMPLE_PAGEINDEX)
        result = ct.query(
            tree_id,
            "test",
            use_block_retriever=use_block_retriever,
            max_turns=2,
            view_depth=2,
        )
        assert result.turns >= 0
    finally:
        ct.close()


def test_query_accepts_cache_enabled_kwarg_on_beam_path():
    ct = ContextTree(":memory:", llm=FakeLLM())
    try:
        tree_id = ct.index_pageindex(SAMPLE_PAGEINDEX)
        result = ct.query(
            tree_id,
            "test",
            max_turns=2,
            cache_enabled=False,
        )
        assert result.turns >= 0
    finally:
        ct.close()


def test_query_passes_max_parallel_blocks_to_block_retriever(monkeypatch):
    captured = {}

    class SpyBlockRetriever:
        def __init__(self, storage, llm, **kwargs):
            captured["init_kwargs"] = kwargs

        def retrieve(self, tree_id, question, **kwargs):
            captured["retrieve_kwargs"] = kwargs
            return SimpleNamespace(turns=0)

    monkeypatch.setattr("contextdb.api.context_tree.BlockRetriever", SpyBlockRetriever)

    ct = ContextTree(":memory:", llm=FakeLLM())
    try:
        tree_id = ct.index_pageindex(SAMPLE_PAGEINDEX)
        result = ct.query(
            tree_id,
            "test",
            use_block_retriever=True,
            max_parallel_blocks=2,
        )
        assert result.turns == 0
        assert captured["init_kwargs"]["max_parallel_blocks"] == 2
        assert "max_parallel_blocks" not in captured["retrieve_kwargs"]
    finally:
        ct.close()


def test_query_accepts_max_parallel_blocks_kwarg_on_legacy_block_path():
    ct = ContextTree(":memory:", llm=FakeLLM())
    try:
        tree_id = ct.index_pageindex(SAMPLE_PAGEINDEX)
        result = ct.query(
            tree_id,
            "test",
            use_block_retriever=True,
            use_legacy_block_retriever=True,
            max_parallel_blocks=2,
        )
        assert result.turns >= 0
    finally:
        ct.close()


def test_query_with_blocks_passes_max_parallel_blocks_to_retriever(monkeypatch):
    captured = {}

    class SpyBlockRetriever:
        def __init__(self, storage, llm, **kwargs):
            captured["init_kwargs"] = kwargs

        def retrieve(self, tree_id, question, **kwargs):
            captured["retrieve_kwargs"] = kwargs
            return SimpleNamespace(turns=0)

    monkeypatch.setattr("contextdb.api.context_tree.BlockRetriever", SpyBlockRetriever)

    ct = ContextTree(":memory:", llm=FakeLLM())
    try:
        tree_id = ct.index_pageindex(SAMPLE_PAGEINDEX)
        result = ct.query_with_blocks(
            tree_id,
            "test",
            max_parallel_blocks=3,
        )
        assert result.turns == 0
        assert captured["init_kwargs"]["max_parallel_blocks"] == 3
        assert "max_parallel_blocks" not in captured["retrieve_kwargs"]
    finally:
        ct.close()


def test_index_document_tree_alias():
    ct = ContextTree(":memory:")
    try:
        tree_id = ct.index_document_tree(SAMPLE_PAGEINDEX)
        root_id = ct.storage.get_root_id(tree_id)
        assert root_id is not None
        root_content = ct.get_content(tree_id, root_id)
        assert root_content["title"] == "Compat Test Doc"
    finally:
        ct.close()


def test_index_markdown_file_requires_tree_builder(tmp_path):
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Title\n", encoding="utf-8")

    ct = ContextTree(":memory:")
    try:
        with pytest.raises(ValueError, match="tree_builder"):
            ct.index_markdown_file(str(md_path))
    finally:
        ct.close()


def test_index_markdown_file_uses_sync_tree_builder(tmp_path):
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Title\n", encoding="utf-8")
    captured = {}

    def build_tree(path: str, **kwargs):
        captured["path"] = path
        captured["kwargs"] = kwargs
        return SAMPLE_PAGEINDEX

    ct = ContextTree(":memory:")
    try:
        tree_id = ct.index_markdown_file(str(md_path), tree_builder=build_tree, parser="external")
        root_id = ct.storage.get_root_id(tree_id)
        assert captured["path"] == str(md_path)
        assert captured["kwargs"] == {"parser": "external"}
        assert root_id is not None
    finally:
        ct.close()


def test_index_markdown_file_uses_async_tree_builder(tmp_path):
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Title\n", encoding="utf-8")

    async def build_tree(path: str, **kwargs):
        assert path == str(md_path)
        assert kwargs == {"parser": "external"}
        return SAMPLE_PAGEINDEX

    ct = ContextTree(":memory:")
    try:
        tree_id = ct.index_markdown_file(str(md_path), tree_builder=build_tree, parser="external")
        assert ct.storage.get_root_id(tree_id) is not None
    finally:
        ct.close()


def test_index_pdf_file_uses_tree_builder(tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    def build_tree(path: str, **kwargs):
        assert path == str(pdf_path)
        assert kwargs == {"mode": "external"}
        return SAMPLE_PAGEINDEX

    ct = ContextTree(":memory:")
    try:
        tree_id = ct.index_pdf_file(str(pdf_path), tree_builder=build_tree, mode="external")
        assert ct.storage.get_root_id(tree_id) is not None
    finally:
        ct.close()
