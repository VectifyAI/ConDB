from __future__ import annotations

from contextdb.adapter.filesystem import FileSystemAdapter
from contextdb.api.condb import ConDB
from contextdb.core.storage import TreeDB
from contextdb.retriever.algorithm.block_retriever import BlockRetriever
from contextdb.retriever.algorithm.ranker import BM25PathRanker


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


def test_condb_auto_uses_filesystem_mode_for_filesystem_trees(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "target.py").write_text("body\n", encoding="utf-8")

    db = ConDB(":memory:")
    try:
        tree_id = FileSystemAdapter(str(tmp_path)).ingest(db.storage)
        retriever = db._make_retriever(tree_id, DummyLLM(), "auto")

        assert retriever.mode == "filesystem"
        assert retriever.ranker is None
    finally:
        db.close()
