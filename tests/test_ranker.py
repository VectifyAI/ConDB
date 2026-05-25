from __future__ import annotations

from contextdb.adapter.filesystem import FileSystemAdapter
from contextdb.api.condb import ConDB
from contextdb.core.storage import TreeDB
from contextdb.retriever.algorithm.block_retriever import BlockRetriever
from contextdb.retriever.algorithm.ranker import BM25PathRanker, VectorPathRanker, make_ranker


class DummyLLM:
    def chat(self, messages, system="", tools=None, cache_key=None):
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank_1",
                    "name": "rank",
                    "input": {"ranked_ids": [], "done": False},
                }
            ]
        }


class KeywordEmbeddingClient:
    def embed(self, texts):
        vectors = []
        for text in texts:
            lower = text.lower()
            vectors.append([
                float("datetime" in lower or "timezone" in lower),
                float("conf" in lower or "config" in lower),
            ])
        return vectors


def test_bm25_path_ranker_orders_matching_paths_first():
    ranker = BM25PathRanker()
    candidates = [
        {"node_id": "a", "rel_path": "docs/conf.py", "title": "conf.py", "is_dir": False},
        {"node_id": "b", "rel_path": "django/db/models/query.py", "title": "query.py", "is_dir": False},
        {"node_id": "c", "rel_path": "tests/model_fields/test_query.py", "title": "test_query.py", "is_dir": False},
    ]

    ranked = ranker.rank("query model fields lookup", candidates, context={"mode": "filesystem"})
    ordered = sorted(ranked, key=lambda row: row[1], reverse=True)

    assert [c["node_id"] for c, _ in ordered] == ["c", "b", "a"]


def test_bm25_path_ranker_boosts_exact_path_mentions():
    ranker = BM25PathRanker()
    candidates = [
        {"node_id": "a", "rel_path": "django/db/models/base.py", "title": "base.py", "is_dir": False},
        {"node_id": "b", "rel_path": "django/db/models/sql/query.py", "title": "query.py", "is_dir": False},
    ]

    ranked = ranker.rank(
        "Traceback in /site-packages/django/db/models/sql/query.py while resolving lookup",
        candidates,
        context={"mode": "filesystem"},
    )
    ordered = sorted(ranked, key=lambda row: row[1], reverse=True)

    assert [c["node_id"] for c, _ in ordered] == ["b", "a"]


def test_filesystem_block_retriever_defaults_to_no_ranker():
    storage = TreeDB(":memory:")
    try:
        retriever = BlockRetriever(storage, DummyLLM(), mode="filesystem")

        assert retriever.ranker is None
    finally:
        storage.close()


def test_filesystem_block_ranker_preserves_block_local_order():
    storage = TreeDB(":memory:")
    try:
        tree = {
            "type": "object",
            "attrs": {"title": "root", "rel_path": "", "is_dir": True},
            "children": [
                {
                    "type": "leaf",
                    "attrs": {"rel_path": "django/db/models/query.py", "title": "query.py", "is_dir": False},
                },
                {
                    "type": "leaf",
                    "attrs": {"rel_path": "docs/conf.py", "title": "conf.py", "is_dir": False},
                },
            ],
        }
        tree_id = storage.ingest_tree(tree)
        root_id = storage.get_root_id(tree_id)
        node_ids = [node.node_id for node in storage.get_children(tree_id, root_id)]
        ranker = BM25PathRanker()
        retriever = BlockRetriever(storage, DummyLLM(), mode="filesystem", ranker=ranker)

        weak = retriever._order_fs_node_id_groups_for_query(tree_id, [node_ids], "unrelated issue")
        single_block = retriever._order_fs_node_id_groups_for_query(
            tree_id,
            [node_ids],
            "Traceback in docs/conf.py during configuration loading",
        )
        cross_block = retriever._order_fs_node_id_groups_for_query(
            tree_id,
            [[node_ids[0]], [node_ids[1]]],
            "Traceback in docs/conf.py during configuration loading",
        )

        assert weak == node_ids
        assert single_block == node_ids
        assert cross_block == [node_ids[1], node_ids[0]]
    finally:
        storage.close()


def test_vector_path_ranker_orders_semantic_path_matches_first():
    ranker = VectorPathRanker(KeywordEmbeddingClient())
    candidates = [
        {"node_id": "a", "rel_path": "docs/conf.py", "title": "conf.py", "is_dir": False},
        {
            "node_id": "b",
            "rel_path": "django/db/models/functions/datetime.py",
            "title": "datetime.py",
            "is_dir": False,
        },
    ]

    ranked = ranker.rank("timezone handling bug", candidates, context={"mode": "filesystem"})
    ordered = sorted(ranked, key=lambda row: row[1], reverse=True)

    assert [c["node_id"] for c, _ in ordered] == ["b", "a"]


def test_vector_path_ranker_keeps_exact_path_mentions_first():
    ranker = VectorPathRanker(KeywordEmbeddingClient())
    candidates = [
        {
            "node_id": "a",
            "rel_path": "django/db/models/functions/datetime.py",
            "title": "datetime.py",
            "is_dir": False,
        },
        {"node_id": "b", "rel_path": "docs/conf.py", "title": "conf.py", "is_dir": False},
    ]

    ranked = ranker.rank(
        "timezone handling bug while loading docs/conf.py",
        candidates,
        context={"mode": "filesystem"},
    )
    ordered = sorted(ranked, key=lambda row: row[1], reverse=True)

    assert [c["node_id"] for c, _ in ordered] == ["b", "a"]


def test_make_ranker_builds_vector_ranker_from_embedding_client():
    ranker = make_ranker("vector", embedding_client=KeywordEmbeddingClient())

    assert isinstance(ranker, VectorPathRanker)


def test_filesystem_block_vector_ranker_runs_without_path_evidence():
    storage = TreeDB(":memory:")
    try:
        tree = {
            "type": "object",
            "attrs": {"title": "root", "rel_path": "", "is_dir": True},
            "children": [
                {
                    "type": "leaf",
                    "attrs": {"rel_path": "docs/conf.py", "title": "conf.py", "is_dir": False},
                },
                {
                    "type": "leaf",
                    "attrs": {
                        "rel_path": "django/db/models/functions/datetime.py",
                        "title": "datetime.py",
                        "is_dir": False,
                    },
                },
            ],
        }
        tree_id = storage.ingest_tree(tree)
        root_id = storage.get_root_id(tree_id)
        node_ids = [node.node_id for node in storage.get_children(tree_id, root_id)]
        ranker = VectorPathRanker(KeywordEmbeddingClient())
        retriever = BlockRetriever(storage, DummyLLM(), mode="filesystem", ranker=ranker)

        ordered = retriever._order_fs_node_id_groups_for_query(
            tree_id,
            [[node_ids[0]], [node_ids[1]]],
            "timezone handling bug",
        )

        assert ordered == [node_ids[1], node_ids[0]]
    finally:
        storage.close()


def test_condb_auto_uses_filesystem_mode_for_filesystem_trees(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "target.py").write_text("body\n", encoding="utf-8")

    db = ConDB(":memory:")
    try:
        tree_id = FileSystemAdapter(str(tmp_path)).ingest(db.storage)
        retriever = db._make_retriever(tree_id, DummyLLM(), "auto")

        assert retriever.mode == "filesystem"
        assert getattr(retriever, "ranker", None) is None
    finally:
        db.close()
