#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from contextdb.core.storage import TreeDB
import json


def example_document_tree():
    print("\n=== Example 1: Document Tree ===\n")
    db = TreeDB("examples.sqlite")

    doc_tree = {
        "type": "object",
        "entity_type": "document",
        "entity_id": "doc_main",
        "children": {
            "title": {"type": "leaf", "attrs": {"text": "User Guide"}},
            "chapters": {
                "type": "array",
                "children": [
                    {
                        "type": "object",
                        "entity_type": "chapter",
                        "entity_id": "ch1",
                        "children": {
                            "title": {"type": "leaf", "attrs": {"text": "Getting Started"}},
                            "paragraphs": {
                                "type": "array",
                                "children": [
                                    {"type": "leaf", "entity_type": "paragraph", "entity_id": "p1"},
                                    {"type": "leaf", "entity_type": "paragraph", "entity_id": "p2"}
                                ]
                            }
                        }
                    }
                ]
            }
        }
    }

    entities = {
        "doc_main": {"type": "document", "author": "TreeDB Team", "version": "1.0"},
        "ch1": {"type": "chapter", "number": 1},
        "p1": {"type": "paragraph", "content": "TreeDB is a tree storage engine..."},
        "p2": {"type": "paragraph", "content": "This guide helps you get started..."}
    }

    tree_id = db.ingest_tree(doc_tree, entities=entities)
    print(f"Created document tree: {tree_id}")

    cursor = db.conn.cursor()
    cursor.execute("SELECT node_id FROM nodes WHERE tree_id=? AND entity_id=?", (tree_id, "ch1"))
    ch1_node_id = cursor.fetchone()['node_id']
    chapter_subtree = db.get_subtree(tree_id, ch1_node_id, max_depth=5, with_entities=True)
    print(f"Chapter 1 has {len(chapter_subtree)} nodes")
    db.close()


def example_config_tree():
    print("\n=== Example 2: Configuration Tree ===\n")
    db = TreeDB("examples.sqlite")

    config_tree = {
        "type": "object",
        "children": {
            "database": {
                "type": "object",
                "children": {
                    "host": {"type": "leaf", "attrs": {"value": "localhost"}},
                    "port": {"type": "leaf", "attrs": {"value": 5432}},
                    "name": {"type": "leaf", "attrs": {"value": "myapp"}}
                }
            },
            "cache": {
                "type": "object",
                "children": {
                    "enabled": {"type": "leaf", "attrs": {"value": True}},
                    "ttl": {"type": "leaf", "attrs": {"value": 3600}}
                }
            }
        }
    }

    tree_id = db.ingest_tree(config_tree, meta={"env": "production"})
    print(f"Created config tree: {tree_id}")

    cursor = db.conn.cursor()
    cursor.execute("""
        SELECT slot, attrs_json FROM nodes
        WHERE tree_id = ? AND node_type = 2 AND parent_id IN (
            SELECT node_id FROM nodes WHERE tree_id = ? AND slot IN ('database', 'cache')
        )
    """, (tree_id, tree_id))

    print("Config values:")
    for row in cursor.fetchall():
        attrs = json.loads(row['attrs_json']) if row['attrs_json'] else {}
        print(f"  {row['slot']}: {attrs.get('value', 'N/A')}")
    db.close()


def example_queries():
    print("\n=== Example 3: Advanced Queries ===\n")
    db = TreeDB("examples.sqlite")

    cursor = db.conn.cursor()
    cursor.execute("""
        SELECT node_type, COUNT(*) as count, AVG(depth) as avg_depth
        FROM nodes GROUP BY node_type
    """)

    print("Node statistics:")
    type_names = {0: "object", 1: "array", 2: "leaf"}
    for row in cursor.fetchall():
        print(f"  {type_names[row['node_type']]}: {row['count']} nodes (avg depth: {row['avg_depth']:.1f})")

    cursor.execute("SELECT tree_id, node_id, depth FROM nodes ORDER BY depth DESC LIMIT 3")
    print("\nDeepest nodes:")
    for row in cursor.fetchall():
        print(f"  Depth {row['depth']}: {row['node_id'][:12]}...")

    db.close()


if __name__ == "__main__":
    print("=" * 50)
    print("  TreeDB Examples")
    print("=" * 50)

    example_document_tree()
    example_config_tree()
    example_queries()

    print("\n" + "=" * 50)
    print("  Examples Complete!")
    print("=" * 50)
