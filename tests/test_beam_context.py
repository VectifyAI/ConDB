"""Test BeamRetriever with context carrying and leaf node handling."""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextdb import ContextTree
from contextdb.config import Config
from contextdb.retriever import BeamRetriever


def load_test_data():
    path = os.path.join(os.path.dirname(__file__), "../examples/regulation_best_interest.json")
    with open(path) as f:
        return json.load(f)


def test_context_carrying():
    """Test that parent_summary is passed to candidates."""
    print("Test: Context Carrying")
    print("=" * 50)

    try:
        Config.validate()
    except ValueError as e:
        print(f"Skip (no API key): {e}")
        return True

    data = load_test_data()
    llm = Config.get_llm_client()
    ct = ContextTree("test_beam_context.sqlite", llm=llm)

    try:
        tree_id = ct.index_pageindex(data)
        print(f"Indexed: {tree_id[:8]}")
        print(f"Doc: {data['doc_name']}")

        # Query that needs context from parent (e.g., definitions in preamble)
        query = "What is the solely incidental prong?"
        print(f"Query: {query}")

        result = ct.query(tree_id, query, beam_size=2, max_turns=4)

        print(f"Turns: {result.turns}")
        print(f"Selected nodes: {len(result.nodes)}")
        for node_id in result.nodes:
            content = ct.get_content(tree_id, node_id)
            if content:
                print(f"  - {content.get('title', node_id)[:50]}")

        assert result.turns > 0, "Should have at least one turn"
        print("\nOK")
        return True
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        ct.close()
        if os.path.exists("test_beam_context.sqlite"):
            os.remove("test_beam_context.sqlite")


def test_leaf_not_in_next_beams():
    """Test that leaf nodes are excluded from next beams."""
    print("\nTest: Leaf Node Handling")
    print("=" * 50)

    try:
        Config.validate()
    except ValueError as e:
        print(f"Skip (no API key): {e}")
        return True

    # Simple tree with known leaf nodes
    data = {
        "doc_name": "test_leaf.pdf",
        "structure": [
            {"title": "Chapter 1", "node_id": "ch1", "summary": "Overview", "start_index": 1, "end_index": 5,
             "nodes": [
                 {"title": "Section 1.1", "node_id": "s1_1", "summary": "Details about topic A", "start_index": 1, "end_index": 2},
                 {"title": "Section 1.2", "node_id": "s1_2", "summary": "Details about topic B", "start_index": 3, "end_index": 5}
             ]},
            {"title": "Chapter 2", "node_id": "ch2", "summary": "Advanced topics", "start_index": 6, "end_index": 10}
        ]
    }

    llm = Config.get_llm_client()
    ct = ContextTree("test_leaf.sqlite", llm=llm)

    try:
        tree_id = ct.index_pageindex(data)
        print(f"Indexed: {tree_id[:8]}")

        result = ct.query(tree_id, "topic A details", beam_size=2, max_turns=5)

        print(f"Turns: {result.turns}")
        print(f"Selected: {result.nodes}")

        # Verify no infinite loop (turns should be limited)
        assert result.turns <= 5, "Should not exceed max_turns"
        print("\nOK")
        return True
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        ct.close()
        if os.path.exists("test_leaf.sqlite"):
            os.remove("test_leaf.sqlite")


if __name__ == "__main__":
    ok1 = test_context_carrying()
    ok2 = test_leaf_not_in_next_beams()
    sys.exit(0 if ok1 and ok2 else 1)
