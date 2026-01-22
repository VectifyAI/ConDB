import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from contextdb.core.storage import TreeDB


def test_create_tree():
    print("Test 1: Create tree... ", end="")
    db = TreeDB(":memory:")
    tree_id, root_id = db.create_tree(meta={"test": "value"})
    assert tree_id is not None
    assert root_id is not None
    db.close()
    print("PASS")


def test_get_node():
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
    print("Test 3: Get children... ", end="")
    db = TreeDB(":memory:")
    tree_id, root_id = db.create_tree()
    children = db.get_children(tree_id, root_id)
    assert len(children) == 0
    db.close()
    print("PASS")


def test_ingest_simple_tree():
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
    assert count == 3
    db.close()
    print("PASS")


def test_ingest_with_entities():
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

    subtree = db.get_subtree(tree_id, root_id, max_depth=1)
    assert len(subtree) == 2

    subtree = db.get_subtree(tree_id, root_id, max_depth=2)
    assert len(subtree) == 3

    subtree = db.get_subtree(tree_id, root_id, max_depth=10)
    assert len(subtree) == 4

    db.close()
    print("PASS")


def test_get_entity():
    print("Test 7: Get entity... ", end="")
    db = TreeDB(":memory:")
    tree = {"type": "leaf", "entity_type": "user", "entity_id": "user123"}
    entities = {"user123": {"type": "user", "name": "John"}}
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
    print("Test 8: Path structure... ", end="")
    db = TreeDB(":memory:")
    tree = {
        "type": "object",
        "children": {
            "a": {
                "type": "object",
                "children": {"b": {"type": "leaf"}}
            }
        }
    }
    tree_id = db.ingest_tree(tree)
    cursor = db.conn.cursor()
    cursor.execute("SELECT path, depth FROM nodes WHERE tree_id = ? ORDER BY depth", (tree_id,))
    rows = cursor.fetchall()

    assert rows[0]['depth'] == 0
    assert rows[0]['path'].startswith('/r/')
    assert rows[1]['depth'] == 1
    assert rows[0]['path'] in rows[1]['path']
    assert rows[2]['depth'] == 2
    assert rows[1]['path'] in rows[2]['path']

    db.close()
    print("PASS")


def test_array_children():
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
    print("Test 10: Context manager... ", end="")
    with TreeDB(":memory:") as db:
        tree_id, root_id = db.create_tree()
        assert tree_id is not None
    print("PASS")


def run_all_tests():
    print("=" * 50)
    print("  TreeDB Test Suite")
    print("=" * 50)

    tests = [
        test_create_tree, test_get_node, test_get_children,
        test_ingest_simple_tree, test_ingest_with_entities,
        test_get_subtree, test_get_entity, test_path_structure,
        test_array_children, test_context_manager
    ]

    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAIL - {e}")
            failed += 1

    print("=" * 50)
    print(f"  Results: {passed} passed, {failed} failed")
    print("=" * 50)
    return failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all_tests() else 1)
