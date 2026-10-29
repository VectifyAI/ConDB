import pytest

from contextdb.config import Config


def test_validate_requires_key_for_configured_anthropic_provider(monkeypatch):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(Config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "openai-key")

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        Config.validate()


def test_validate_requires_key_for_configured_openai_provider(monkeypatch):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(Config, "ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setattr(Config, "OPENAI_API_KEY", None)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        Config.validate()


def test_validate_rejects_unsupported_provider(monkeypatch):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "unknown")
    monkeypatch.setattr(Config, "ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "openai-key")

    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        Config.validate()


def test_llm_config_resolves_litellm_model_names():
    from contextdb.config import get_llm_config

    bare = get_llm_config("anthropic", "claude-sonnet-4-6")
    prefixed = get_llm_config("anthropic", "anthropic/claude-sonnet-4-6")

    assert prefixed == bare
    assert prefixed["context_limit"] == 200000


def test_block_retriever_sizes_window_and_budget_from_model_context():
    from types import SimpleNamespace

    from contextdb.core.storage import TreeDB
    from contextdb.retriever.algorithm.block_retriever import BlockRetriever

    llm = SimpleNamespace(provider="anthropic", model="anthropic/claude-sonnet-4-6")
    with TreeDB(":memory:") as db:
        retriever = BlockRetriever(db, llm, max_tokens_per_block=16000)

    # 200k context, real/estimated ratio 1.3, 4096 reserve, 16k current block.
    assert retriever.prompt_token_budget == 153846 - 4096
    assert retriever.cache_window_tokens == 153846 - 4096 - 16000


def _sonnet_retriever(db, **kwargs):
    from types import SimpleNamespace

    from contextdb.retriever.algorithm.block_retriever import BlockRetriever

    return BlockRetriever(db, SimpleNamespace(provider="anthropic", model="anthropic/claude-sonnet-4-6"), **kwargs)


def test_fit_cache_window_drops_oldest_unpinned_blocks_first():
    from contextdb.core.storage import TreeDB

    with TreeDB(":memory:") as db:
        retriever = _sonnet_retriever(db)
        static = retriever.token_counter.count_text_tokens(retriever._cache_static_segment())
        window = [
            {"block_id": "top", "tokens": 50, "pinned": True},
            {"block_id": "old", "tokens": 40, "pinned": False},
            {"block_id": "mid", "tokens": 30, "pinned": False},
            {"block_id": "new", "tokens": 20, "pinned": False},
        ]

        retriever.prompt_token_budget = static + 100
        assert [e["block_id"] for e in retriever._fit_cache_window(window, 0, "b")] == ["top", "mid", "new"]

        retriever.prompt_token_budget = static + 140
        assert retriever._fit_cache_window(window, 0, "b") == window

        retriever.prompt_token_budget = static + 15
        assert retriever._fit_cache_window(window, 0, "b") == []

        retriever.prompt_token_budget = static + 50
        assert [e["block_id"] for e in retriever._fit_cache_window(window, 0, "b")] == ["top"]


@pytest.mark.parametrize("checkpoints", [4, 1])
def test_every_block_prompt_stays_within_budget(checkpoints):
    import contextdb

    class MeasuringLLM:
        provider = "anthropic"
        model = "anthropic/claude-sonnet-4-6"

        def __init__(self):
            self.totals = []

        def chat_with_cache(self, messages, system="", tools=None, cache_content=None, non_cached_content=None,
                            cache_key=None):
            parts = cache_content if isinstance(cache_content, list) else [cache_content or ""]
            self.totals.append(sum(self.counter.count_text_tokens(p) for p in [*parts, non_cached_content or ""])
                               + self.counter.count_text_tokens(messages[0]["content"]))
            ids = [line[2:] for line in messages[0]["content"].splitlines() if line.startswith("- ")][-3:]
            return {"content": [{"type": "tool_use", "id": "t", "name": "rank", "input": {"ranked_ids": ids}}]}

    doc = {"structure": [
        {"title": f"Chapter {c}", "node_id": f"{c:04d}", "summary": "chapter " * 20, "nodes": [
            {"title": f"Section {c}.{s}", "node_id": f"{c:04d}{s:04d}", "summary": "section " * 20,
             "nodes": [{"title": f"Para {c}.{s}.{p}", "node_id": f"{c:04d}{s:04d}{p:04d}", "summary": "para " * 20}
                       for p in range(4)]}
            for s in range(4)]}
        for c in range(6)
    ]}
    db = contextdb.open(":memory:")
    try:
        tree_id = db.store(doc, format="pageindex")
        llm = MeasuringLLM()
        retriever = _sonnet_retriever(db.storage, max_tokens_per_block=400)
        llm.counter = retriever.token_counter
        retriever.llm = llm
        retriever.prompt_token_budget = 2200
        retriever.cache_window_checkpoints = checkpoints
        retriever.retrieve(tree_id, "find the paragraph", beam_size=3, select_k=3)
    finally:
        db.close()

    assert len(llm.totals) >= 3
    assert max(llm.totals) <= 2200 + 50  # segments are counted apart; joins add a few tokens
