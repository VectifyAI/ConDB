#!/usr/bin/env python3
"""
TreeDB Examples - Common use cases and patterns
"""

from treedb import TreeDB
import json


def example_document_tree():
    """Example: Store a document tree with sections and paragraphs"""
    print("\n=== Example 1: Document Tree ===\n")

    db = TreeDB("examples.sqlite")

    # Create document structure
    doc_tree = {
        "type": "object",
        "entity_type": "document",
        "entity_id": "doc_main",
        "children": {
            "title": {
                "type": "leaf",
                "attrs": {"text": "TreeDB User Guide"}
            },
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
        "p1": {"type": "paragraph", "content": "TreeDB is a powerful tree storage engine..."},
        "p2": {"type": "paragraph", "content": "This guide will help you get started..."}
    }

    tree_id = db.ingest_tree(doc_tree, entities=entities)
    print(f"Created document tree: {tree_id}")

    # Retrieve chapter 1 subtree
    cursor = db.conn.cursor()
    cursor.execute("SELECT node_id FROM nodes WHERE tree_id=? AND entity_id=?", (tree_id, "ch1"))
    ch1_node_id = cursor.fetchone()['node_id']

    chapter_subtree = db.get_subtree(tree_id, ch1_node_id, max_depth=5, with_entities=True)
    print(f"\nChapter 1 has {len(chapter_subtree)} nodes")

    db.close()


def example_config_tree():
    """Example: Store application configuration"""
    print("\n=== Example 2: Configuration Tree ===\n")

    db = TreeDB("examples.sqlite")

    config_tree = {
        "type": "object",
        "children": {
            "database": {
                "type": "object",
                "children": {
                    "host": {"type": "leaf", "attrs": {"value": "localhost", "env_var": "DB_HOST"}},
                    "port": {"type": "leaf", "attrs": {"value": 5432, "type": "int"}},
                    "name": {"type": "leaf", "attrs": {"value": "myapp", "required": True}}
                }
            },
            "cache": {
                "type": "object",
                "children": {
                    "enabled": {"type": "leaf", "attrs": {"value": True}},
                    "ttl": {"type": "leaf", "attrs": {"value": 3600, "unit": "seconds"}}
                }
            },
            "features": {
                "type": "array",
                "children": [
                    {"type": "leaf", "attrs": {"name": "feature_a", "enabled": True}},
                    {"type": "leaf", "attrs": {"name": "feature_b", "enabled": False}}
                ]
            }
        }
    }

    tree_id = db.ingest_tree(config_tree, meta={"env": "production", "version": "2.1"})
    print(f"Created config tree: {tree_id}")

    # Query all leaf nodes (actual config values)
    cursor = db.conn.cursor()
    cursor.execute("""
        SELECT slot, attrs_json
        FROM nodes
        WHERE tree_id = ? AND node_type = 2 AND parent_id IN (
            SELECT node_id FROM nodes WHERE tree_id = ? AND slot IN ('database', 'cache')
        )
    """, (tree_id, tree_id))

    print("\nConfig values:")
    for row in cursor.fetchall():
        attrs = json.loads(row['attrs_json']) if row['attrs_json'] else {}
        print(f"  {row['slot']}: {attrs.get('value', 'N/A')}")

    db.close()


def example_ast_tree():
    """Example: Store an abstract syntax tree"""
    print("\n=== Example 3: Abstract Syntax Tree ===\n")

    db = TreeDB("examples.sqlite")

    # Represent: x = a + b
    ast_tree = {
        "type": "object",
        "entity_type": "statement",
        "entity_id": "stmt_assign",
        "attrs": {"statement_type": "assignment"},
        "children": {
            "target": {
                "type": "leaf",
                "entity_type": "identifier",
                "entity_id": "id_x"
            },
            "value": {
                "type": "object",
                "entity_type": "binary_op",
                "entity_id": "op_add",
                "children": {
                    "left": {
                        "type": "leaf",
                        "entity_type": "identifier",
                        "entity_id": "id_a"
                    },
                    "operator": {
                        "type": "leaf",
                        "attrs": {"op": "+"}
                    },
                    "right": {
                        "type": "leaf",
                        "entity_type": "identifier",
                        "entity_id": "id_b"
                    }
                }
            }
        }
    }

    entities = {
        "stmt_assign": {"type": "statement", "kind": "assignment", "line": 10},
        "id_x": {"type": "identifier", "name": "x", "scope": "local"},
        "id_a": {"type": "identifier", "name": "a", "scope": "local"},
        "id_b": {"type": "identifier", "name": "b", "scope": "local"},
        "op_add": {"type": "binary_op", "operator": "+"}
    }

    tree_id = db.ingest_tree(ast_tree, entities=entities)
    print(f"Created AST tree: {tree_id}")

    # Get all identifiers
    cursor = db.conn.cursor()
    cursor.execute("""
        SELECT node_id FROM nodes
        WHERE tree_id = ? AND entity_type = 'identifier'
    """, (tree_id,))

    print("\nIdentifiers in AST:")
    for row in cursor.fetchall():
        entity = db.get_entity(tree_id, row['node_id'])
        if entity:
            payload = json.loads(entity.payload_json)
            print(f"  {payload['name']} (scope: {payload['scope']})")

    db.close()


def example_navigation_tree():
    """Example: Store navigation menu hierarchy"""
    print("\n=== Example 4: Navigation Menu ===\n")

    db = TreeDB("examples.sqlite")

    nav_tree = {
        "type": "object",
        "attrs": {"menu_name": "Main Navigation"},
        "children": {
            "home": {
                "type": "leaf",
                "entity_type": "menu_item",
                "entity_id": "menu_home",
                "attrs": {"label": "Home", "order": 1}
            },
            "products": {
                "type": "object",
                "entity_type": "menu_item",
                "entity_id": "menu_products",
                "attrs": {"label": "Products", "order": 2},
                "children": {
                    "electronics": {
                        "type": "leaf",
                        "entity_type": "menu_item",
                        "entity_id": "menu_electronics",
                        "attrs": {"label": "Electronics", "order": 1}
                    },
                    "clothing": {
                        "type": "leaf",
                        "entity_type": "menu_item",
                        "entity_id": "menu_clothing",
                        "attrs": {"label": "Clothing", "order": 2}
                    }
                }
            },
            "about": {
                "type": "leaf",
                "entity_type": "menu_item",
                "entity_id": "menu_about",
                "attrs": {"label": "About", "order": 3}
            }
        }
    }

    entities = {
        "menu_home": {"type": "menu_item", "url": "/", "icon": "home"},
        "menu_products": {"type": "menu_item", "url": "/products", "icon": "shopping-cart"},
        "menu_electronics": {"type": "menu_item", "url": "/products/electronics", "icon": "laptop"},
        "menu_clothing": {"type": "menu_item", "url": "/products/clothing", "icon": "shirt"},
        "menu_about": {"type": "menu_item", "url": "/about", "icon": "info"}
    }

    tree_id = db.ingest_tree(nav_tree, entities=entities, meta={"menu_type": "main"})
    print(f"Created navigation tree: {tree_id}")

    # Get full menu structure with URLs
    cursor = db.conn.cursor()
    cursor.execute("SELECT root_node_id FROM trees WHERE tree_id = ?", (tree_id,))
    root_id = cursor.fetchone()['root_node_id']

    menu_items = db.get_subtree(tree_id, root_id, max_depth=3, with_entities=True)

    print("\nMenu structure:")
    for item in menu_items:
        if item.get('entity'):
            indent = "  " * item['depth']
            label = json.loads(item['attrs_json'])['label'] if item.get('attrs_json') else "N/A"
            url = item['entity']['payload']['url']
            print(f"{indent}- {label} ({url})")

    db.close()


def example_queries():
    """Example: Advanced queries"""
    print("\n=== Example 5: Advanced Queries ===\n")

    db = TreeDB("examples.sqlite")

    # Count nodes by type
    cursor = db.conn.cursor()
    cursor.execute("""
        SELECT
            node_type,
            COUNT(*) as count,
            AVG(depth) as avg_depth
        FROM nodes
        GROUP BY node_type
    """)

    print("Node statistics:")
    type_names = {0: "object", 1: "array", 2: "leaf"}
    for row in cursor.fetchall():
        print(f"  {type_names[row['node_type']]}: {row['count']} nodes (avg depth: {row['avg_depth']:.1f})")

    # Find deepest nodes
    cursor.execute("""
        SELECT tree_id, node_id, depth, path
        FROM nodes
        ORDER BY depth DESC
        LIMIT 3
    """)

    print("\nDeepest nodes:")
    for row in cursor.fetchall():
        print(f"  Depth {row['depth']}: {row['node_id'][:12]}...")

    # Find nodes with entities grouped by type
    cursor.execute("""
        SELECT entity_type, COUNT(*) as count
        FROM nodes
        WHERE entity_type IS NOT NULL
        GROUP BY entity_type
        ORDER BY count DESC
    """)

    print("\nNodes by entity type:")
    for row in cursor.fetchall():
        print(f"  {row['entity_type']}: {row['count']}")

    db.close()


if __name__ == "__main__":
    print("=" * 60)
    print("  TreeDB Examples")
    print("=" * 60)

    example_document_tree()
    example_config_tree()
    example_ast_tree()
    example_navigation_tree()
    example_queries()

    print("\n" + "=" * 60)
    print("  Examples Complete!")
    print("=" * 60)
    print("\nDatabase saved to: examples.sqlite")
    print()
