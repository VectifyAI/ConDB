"""Tests for ConDB public API."""

from types import SimpleNamespace

import pytest

import contextdb
from contextdb import ConDB, LLMNotConfiguredError, QueryResult, TreeNotFoundError

# ── Fixtures ────────────────────────────────────────────────────────

SAMPLE_PAGEINDEX = {
    "doc_name": "Test Doc",
    "doc_description": "A test document",
    "structure": [
        {
            "node_id": "n1",
            "title": "Chapter 1",
            "summary": "First chapter",
            "text": "Hello world",
            "start_index": 1,
            "end_index": 2,
            "nodes": [
                {
                    "node_id": "n1_1",
                    "title": "Section 1.1",
                    "summary": "A section",
                    "text": "Section content",
                    "start_index": 1,
                    "end_index": 1,
                    "nodes": [],
                }
            ],
        },
        {
            "node_id": "n2",
            "title": "Chapter 2",
            "summary": "Second chapter",
            "text": "Goodbye world",
            "start_index": 3,
            "end_index": 4,
            "nodes": [],
        },
    ],
}

SAMPLE_CHATINDEX = {
    "conversation_id": "conv_1",
    "participants": ["user", "assistant"],
    "topics": [
        {
            "node_id": "topic_1",
            "title": "Onboarding",
            "summary": "Initial setup discussion",
            "msg_start": 0,
            "msg_end": 1,
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "How can I help?"},
            ],
            "subtopics": [],
        }
    ],
}


@pytest.fixture
def db():
    with contextdb.open(":memory:") as db:
        yield db


@pytest.fixture
def stored(db):
    """db with one tree already stored."""
    tree_id = db.store(SAMPLE_PAGEINDEX)
    return db, tree_id


# ── open / close ────────────────────────────────────────────────────

def test_open_close():
    db = contextdb.open(":memory:")
    assert isinstance(db, ConDB)
    db.close()


def test_context_manager():
    with contextdb.open(":memory:") as db:
        assert isinstance(db, ConDB)


# ── store ───────────────────────────────────────────────────────────

def test_store_pageindex(db):
    tree_id = db.store(SAMPLE_PAGEINDEX)
    assert isinstance(tree_id, str)
    assert len(tree_id) == 36  # UUID format


def test_store_document_alias(db):
    tree_id = db.store(SAMPLE_PAGEINDEX, format="document")
    assert db.has_tree(tree_id)


def test_store_with_meta(db):
    tree_id = db.store(SAMPLE_PAGEINDEX, meta={"source": "test.pdf"})
    info = db.tree_info(tree_id)
    assert info["meta"] == {"source": "test.pdf"}


def test_store_generic(db):
    data = {
        "title": "Root",
        "children": {"a": {"type": "leaf", "content": "hello", "title": "A"}},
    }
    tree_id = db.store(data, format="generic")
    assert db.has_tree(tree_id)


def test_store_chatindex(db):
    tree_id = db.store(SAMPLE_CHATINDEX, format="chatindex")
    info = db.tree_info(tree_id)
    root_id = info["root_node_id"]

    root_content = db.get_content(tree_id, root_id)
    assert root_content is not None
    assert root_content["conversation_id"] == "conv_1"
    assert root_content["title"] == "conv_1"

    children = db.get_children(tree_id, root_id)
    assert len(children) == 1
    assert children[0]["attrs"]["summary"] == "Initial setup discussion"

    topic_content = db.get_content(tree_id, children[0]["node_id"])
    assert topic_content is not None
    assert topic_content["title"] == "Onboarding"
    assert topic_content["messages"][0]["content"] == "Hello"


def test_delete_chatindex_tree_cleans_entities(db):
    tree_id = db.store(SAMPLE_CHATINDEX, format="chatindex")
    cur = db.storage.conn.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM entities")
    assert cur.fetchone()["c"] == 2

    db.delete_tree(tree_id)

    cur.execute("SELECT COUNT(*) AS c FROM entities")
    assert cur.fetchone()["c"] == 0


def test_store_bad_format(db):
    with pytest.raises(ValueError, match="Unknown format"):
        db.store({}, format="invalid")


# ── tree_info ───────────────────────────────────────────────────────

def test_tree_info(stored):
    db, tree_id = stored
    info = db.tree_info(tree_id)
    assert info["tree_id"] == tree_id
    assert info["node_count"] == 4  # root + n1 + n1_1 + n2
    assert info["max_depth"] == 2


def test_tree_info_not_found(db):
    with pytest.raises(TreeNotFoundError):
        db.tree_info("nonexistent")


# ── navigate ────────────────────────────────────────────────────────

def test_get_children(stored):
    db, tree_id = stored
    root_id = db.tree_info(tree_id)["root_node_id"]
    children = db.get_children(tree_id, root_id)
    assert len(children) == 2
    titles = {c.get("attrs", {}).get("title") for c in children}
    assert "Chapter 1" in titles
    assert "Chapter 2" in titles


def test_get_content(stored):
    db, tree_id = stored
    root_id = db.tree_info(tree_id)["root_node_id"]
    children = db.get_children(tree_id, root_id)
    content = db.get_content(tree_id, children[0]["node_id"])
    assert content is not None
    assert "title" in content


def test_get_subtree(stored):
    db, tree_id = stored
    root_id = db.tree_info(tree_id)["root_node_id"]
    subtree = db.get_subtree(tree_id, root_id, depth=1)
    assert len(subtree) == 3  # root + 2 children


def test_print_tree(stored):
    db, tree_id = stored
    text = db.print_tree(tree_id, depth=2)
    assert "Chapter 1" in text
    assert "Chapter 2" in text


# ── manage ──────────────────────────────────────────────────────────

def test_list_trees_empty(db):
    assert db.list_trees() == []


def test_list_trees(stored):
    db, tree_id = stored
    trees = db.list_trees()
    assert len(trees) == 1
    assert trees[0]["tree_id"] == tree_id
    assert trees[0]["node_count"] == 4


def test_has_tree(stored):
    db, tree_id = stored
    assert db.has_tree(tree_id) is True
    assert db.has_tree("nonexistent") is False


def test_delete_tree(stored):
    db, tree_id = stored
    db.delete_tree(tree_id)
    assert db.has_tree(tree_id) is False
    assert db.list_trees() == []


# ── query errors ────────────────────────────────────────────────────

def test_query_no_llm(stored):
    db, tree_id = stored
    with pytest.raises(LLMNotConfiguredError):
        db.query(tree_id, "test")


def test_query_tree_not_found(db):
    db.set_llm(llm=FakeLLM())
    with pytest.raises(TreeNotFoundError):
        db.query("nonexistent", "test")


# ── query with fake LLM ────────────────────────────────────────────

class FakeLLM:
    """Minimal LLM that always selects the first candidate."""
    def chat(self, messages, system="", tools=None, cache_key=None):
        # Extract candidate node IDs from the prompt text
        text = messages[0]["content"] if messages else ""
        import re
        ids = re.findall(r"id: ([0-9a-f-]+)", text)
        ranked_ids = ids[:1] if ids else []
        return {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "rank",
                 "input": {"ranked_ids": ranked_ids, "done": True}}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }


class DescendingLLM:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, system="", tools=None, cache_key=None):
        import re

        self.calls += 1
        text = messages[0]["content"] if messages else ""
        ids = re.findall(r"id: ([0-9a-f-]+)", text)
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "rank",
                    "input": {"ranked_ids": ids[:1], "done": self.calls >= 2},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }


class SpyCacheLLM:
    provider = "anthropic"
    model = "claude-sonnet-4-20250514"

    def __init__(self):
        self.chat_calls = []
        self.cache_calls = []

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
        self.chat_calls.append(
            {
                "messages": messages,
                "system": system,
                "tools": tools,
                "cache_key": cache_key,
            }
        )
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
        self.cache_calls.append(
            {
                "messages": messages,
                "system": system,
                "tools": tools,
                "cache_content": cache_content,
                "non_cached_content": non_cached_content,
                "cache_key": cache_key,
            }
        )
        return self._response()


def test_query_with_llm_object(stored):
    db, tree_id = stored
    result = db.query(tree_id, "What is chapter 1 about?", llm=FakeLLM())
    assert isinstance(result, QueryResult)
    assert result.turns >= 1


def test_query_set_llm(stored):
    db, tree_id = stored
    db.set_llm(llm=FakeLLM())
    result = db.query(tree_id, "test")
    assert isinstance(result, QueryResult)


def test_query_strategy_beam(stored):
    db, tree_id = stored
    result = db.query(tree_id, "test", llm=FakeLLM(), strategy="beam")
    assert isinstance(result, QueryResult)


def test_query_strategy_beam_default_reaches_leaf(stored):
    db, tree_id = stored

    result = db.query(tree_id, "section details", llm=DescendingLLM(), strategy="beam")

    assert len(result.node_ids) == 1
    assert result.contents[0]["content"]["title"] == "Section 1.1"
    assert result.turns == 2
    assert result.trace[0]["top_candidate_ids"] == 0


def test_query_strategy_block(stored):
    db, tree_id = stored
    result = db.query(tree_id, "test", llm=FakeLLM(), strategy="block")
    assert isinstance(result, QueryResult)


def test_query_strategy_block_passes_max_parallel_blocks_to_retriever(stored, monkeypatch):
    captured = {}

    class SpyBlockRetriever:
        def __init__(self, storage, llm, **kwargs):
            captured["init_kwargs"] = kwargs

        def retrieve(self, tree_id, question, **kwargs):
            captured["retrieve_kwargs"] = kwargs
            return SimpleNamespace(
                nodes=[],
                contents=[],
                turns=0,
                total_llm_calls=0,
                trace=[],
            )

    monkeypatch.setattr("contextdb.api.condb.BlockRetriever", SpyBlockRetriever)

    db, tree_id = stored
    result = db.query(
        tree_id,
        "test",
        llm=FakeLLM(),
        strategy="block",
        max_parallel_blocks=2,
    )

    assert isinstance(result, QueryResult)
    assert captured["init_kwargs"]["max_parallel_blocks"] == 2
    assert "max_parallel_blocks" not in captured["retrieve_kwargs"]


def test_query_strategy_block_cache_enabled_uses_cached_llm_path(stored):
    db, tree_id = stored
    llm = SpyCacheLLM()

    result = db.query(tree_id, "test", llm=llm, strategy="block", cache_enabled=True)

    assert isinstance(result, QueryResult)
    assert len(llm.cache_calls) == 1
    assert llm.cache_calls[0]["cache_content"]
    assert llm.cache_calls[0]["cache_key"] is not None
    assert llm.chat_calls == []


def test_query_strategy_block_cache_disabled_uses_plain_chat(stored):
    db, tree_id = stored
    llm = SpyCacheLLM()

    result = db.query(tree_id, "test", llm=llm, strategy="block", cache_enabled=False)

    assert isinstance(result, QueryResult)
    assert len(llm.chat_calls) == 1
    assert llm.chat_calls[0]["cache_key"] is None
    assert llm.cache_calls == []


def test_query_bad_strategy(stored):
    db, tree_id = stored
    with pytest.raises(ValueError, match="Unknown strategy"):
        db.query(tree_id, "test", llm=FakeLLM(), strategy="invalid")


def test_query_result_fields(stored):
    db, tree_id = stored
    result = db.query(tree_id, "test", llm=FakeLLM())
    assert hasattr(result, "node_ids")
    assert hasattr(result, "contents")
    assert hasattr(result, "turns")
    assert hasattr(result, "llm_calls")
    assert hasattr(result, "cache_hits")
    assert hasattr(result, "trace")
