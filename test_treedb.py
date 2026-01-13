#!/usr/bin/env python3
"""
Simple tests for TreeDB v0.1
"""

import os
from treedb import TreeDB


def test_create_tree():
    """Test tree creation"""
    print("Test 1: Create tree... ", end="")
    db = TreeDB(":memory:")
    tree_id, root_id = db.create_tree(meta={"test": "value"})
    assert tree_id is not None
    assert root_id is not None
    db.close()
    print("PASS")


def test_get_node():
    """Test node retrieval"""
    print("Test 2: Get node... ", end="")
    db = TreeDB(":memory:")
    tree_id, root_id = db.create_tree()
    node = db.get_node(tree_id, root_id)
    assert node is not None
    assert node.node_id == root_id
    assert node.depth == 0
    assert node.parent_id is None
    db.close()
    print("PASS")


def test_get_children():
    """Test children retrieval"""
    print("Test 3: Get children... ", end="")
    db = TreeDB(":memory:")
    tree_id, root_id = db.create_tree()
    children = db.get_children(tree_id, root_id)
    assert len(children) == 0
    db.close()
    print("PASS")


def test_ingest_simple_tree():
    """Test simple tree ingestion"""
    print("Test 4: Ingest simple tree... ", end="")
    db = TreeDB(":memory:")

    tree = {
        "type": "object",
        "children": {
            "child1": {"type": "leaf"},
            "child2": {"type": "leaf"}
        }
    }

    tree_id = db.ingest_tree(tree)
    cursor = db.conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM nodes WHERE tree_id = ?", (tree_id,))
    count = cursor.fetchone()['count']
    assert count == 3  # root + 2 children
    db.close()
    print("PASS")


def test_ingest_with_entities():
    """Test tree ingestion with entities"""
    print("Test 5: Ingest tree with entities... ", end="")
    db = TreeDB(":memory:")

    tree = {
        "type": "object",
        "entity_type": "doc",
        "entity_id": "doc1",
        "children": {
            "content": {
                "type": "leaf",
                "entity_type": "text",
                "entity_id": "text1"
            }
        }
    }

    entities = {
        "doc1": {"type": "doc", "title": "Test"},
        "text1": {"type": "text", "content": "Hello"}
    }

    tree_id = db.ingest_tree(tree, entities=entities)

    cursor = db.conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM entities")
    entity_count = cursor.fetchone()['count']
    assert entity_count == 2

    db.close()
    print("PASS")


def test_get_subtree():
    """Test subtree retrieval"""
    print("Test 6: Get subtree... ", end="")
    db = TreeDB(":memory:")

    tree = {
        "type": "object",
        "children": {
            "level1": {
                "type": "object",
                "children": {
                    "level2": {
                        "type": "object",
                        "children": {
                            "level3": {"type": "leaf"}
                        }
                    }
                }
            }
        }
    }

    tree_id = db.ingest_tree(tree)

    cursor = db.conn.cursor()
    cursor.execute("SELECT root_node_id FROM trees WHERE tree_id = ?", (tree_id,))
    root_id = cursor.fetchone()['root_node_id']

    # Get depth 1
    subtree = db.get_subtree(tree_id, root_id, max_depth=1)
    assert len(subtree) == 2  # root + 1 child

    # Get depth 2
    subtree = db.get_subtree(tree_id, root_id, max_depth=2)
    assert len(subtree) == 3  # root + 2 descendants

    # Get full tree
    subtree = db.get_subtree(tree_id, root_id, max_depth=10)
    assert len(subtree) == 4  # all nodes

    db.close()
    print("PASS")


def test_get_entity():
    """Test entity retrieval"""
    print("Test 7: Get entity... ", end="")
    db = TreeDB(":memory:")

    tree = {
        "type": "leaf",
        "entity_type": "user",
        "entity_id": "user123"
    }

    entities = {
        "user123": {"type": "user", "name": "John"}
    }

    tree_id = db.ingest_tree(tree, entities=entities)

    cursor = db.conn.cursor()
    cursor.execute("SELECT root_node_id FROM trees WHERE tree_id = ?", (tree_id,))
    root_id = cursor.fetchone()['root_node_id']

    entity = db.get_entity(tree_id, root_id)
    assert entity is not None
    assert entity.entity_type == "user"

    db.close()
    print("PASS")


def test_path_structure():
    """Test path materialization"""
    print("Test 8: Path structure... ", end="")
    db = TreeDB(":memory:")

    tree = {
        "type": "object",
        "children": {
            "a": {
                "type": "object",
                "children": {
                    "b": {"type": "leaf"}
                }
            }
        }
    }

    tree_id = db.ingest_tree(tree)

    cursor = db.conn.cursor()
    cursor.execute("SELECT path, depth FROM nodes WHERE tree_id = ? ORDER BY depth", (tree_id,))
    rows = cursor.fetchall()

    # Root
    assert rows[0]['depth'] == 0
    assert rows[0]['path'].startswith('/r/')

    # Level 1
    assert rows[1]['depth'] == 1
    assert rows[0]['path'] in rows[1]['path']

    # Level 2
    assert rows[2]['depth'] == 2
    assert rows[1]['path'] in rows[2]['path']

    db.close()
    print("PASS")


def test_array_children():
    """Test array node type"""
    print("Test 9: Array children... ", end="")
    db = TreeDB(":memory:")

    tree = {
        "type": "array",
        "children": [
            {"type": "leaf", "attrs": {"value": "first"}},
            {"type": "leaf", "attrs": {"value": "second"}},
            {"type": "leaf", "attrs": {"value": "third"}}
        ]
    }

    tree_id = db.ingest_tree(tree)

    cursor = db.conn.cursor()
    cursor.execute("SELECT root_node_id FROM trees WHERE tree_id = ?", (tree_id,))
    root_id = cursor.fetchone()['root_node_id']

    children = db.get_children(tree_id, root_id)
    assert len(children) == 3
    assert children[0].slot == "0"
    assert children[1].slot == "1"
    assert children[2].slot == "2"

    db.close()
    print("PASS")


def test_context_manager():
    """Test context manager"""
    print("Test 10: Context manager... ", end="")
    with TreeDB(":memory:") as db:
        tree_id, root_id = db.create_tree()
        assert tree_id is not None
    print("PASS")


def run_all_tests():
    """Run all tests"""
    print("=" * 60)
    print("  TreeDB v0.1 - Test Suite")
    print("=" * 60)
    print()

    tests = [
        test_create_tree,
        test_get_node,
        test_get_children,
        test_ingest_simple_tree,
        test_ingest_with_entities,
        test_get_subtree,
        test_get_entity,
        test_path_structure,
        test_array_children,
        test_context_manager
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAIL - {e}")
            failed += 1

    print()
    print("=" * 60)
    print(f"  Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    import sys
    success = run_all_tests()
    sys.exit(0 if success else 1)
