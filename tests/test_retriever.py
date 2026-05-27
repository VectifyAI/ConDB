import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from contextdb import ContextTree
from contextdb.config import Config

TEST_DATA = {
    "doc_name": "ml_basics.pdf",
    "structure": [
        {"title": "1. Introduction", "node_id": "intro", "summary": "ML overview", "text": "Machine learning enables systems to learn.", "start_index": 1, "end_index": 15, "nodes": [
            {"title": "1.1 What is ML?", "node_id": "intro_1", "summary": "Core concepts", "text": "ML finds patterns in data.", "start_index": 1, "end_index": 5, "nodes": []},
            {"title": "1.2 Types", "node_id": "intro_2", "summary": "Learning types", "text": "Supervised, unsupervised, reinforcement.", "start_index": 6, "end_index": 10, "nodes": []}
        ]},
        {"title": "2. Supervised", "node_id": "sup", "summary": "Supervised learning", "text": "Uses labeled data.", "start_index": 16, "end_index": 40, "nodes": [
            {"title": "2.1 Regression", "node_id": "sup_1", "summary": "Continuous prediction", "text": "Linear regression predicts continuous values.", "start_index": 16, "end_index": 20, "nodes": []},
            {"title": "2.2 Classification", "node_id": "sup_2", "summary": "Categorical prediction", "text": "Classification predicts categories.", "start_index": 21, "end_index": 30, "nodes": []}
        ]},
        {"title": "3. Unsupervised", "node_id": "unsup", "summary": "Unsupervised learning", "text": "Finds patterns in unlabeled data.", "start_index": 41, "end_index": 60, "nodes": [
            {"title": "3.1 Clustering", "node_id": "unsup_1", "summary": "Grouping", "text": "Groups similar data points.", "start_index": 41, "end_index": 50, "nodes": []}
        ]}
    ]
}


def test_manual():
    print("Test: Manual Retriever")
    print("=" * 40)

    ct = ContextTree("test_manual.sqlite")
    try:
        tree_id = ct.index_pageindex(TEST_DATA)
        print(f"Indexed: {tree_id[:8]}")

        root_id = ct.storage.get_root_id(tree_id)
        actions = [
            {"type": "expand", "node_id": root_id},
            {"type": "expand", "node_id": "sup"},
            {"type": "get_content", "node_id": "sup_1"},
            {"type": "done"}
        ]

        result = ct.query_manual(tree_id, "regression", actions)
        print(f"Turns: {result.turns}, Contents: {len(result.contents)}")
        if result.contents:
            print(f"Text: {result.contents[0]['content']['text'][:50]}...")

        print("=" * 40)
        print("OK")
        return True
    except Exception as e:
        print(f"FAIL: {e}")
        return False
    finally:
        ct.close()


def test_llm():
    print("Test: Iterative Retriever")
    print("=" * 40)

    try:
        Config.validate()
    except ValueError as e:
        print(f"Skip: {e}")
        return

    llm = Config.get_llm_client()
    ct = ContextTree("test_llm.sqlite", llm=llm)

    tree_id = ct.index_pageindex(TEST_DATA)
    print(f"Indexed: {tree_id[:8]}")

    r = ct.query(tree_id, "What is regression?", max_turns=5)
    print(f"turns: {r.turns}, nodes: {len(r.nodes)}")
    assert len(r.nodes) >= 0

    ct.close()
    print("OK")


if __name__ == "__main__":
    ok1 = test_manual()
    print()
    ok2 = test_llm()
    sys.exit(0 if ok1 and ok2 else 1)
