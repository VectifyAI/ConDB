#!/usr/bin/env python3
"""
TreeDB v0.1 - Demo Script

Demonstrates all core functionality:
- Creating trees
- Node operations
- Subtree retrieval
- Entity dereferencing
- Tree ingestion
"""

import json
from treedb import TreeDB


def print_section(title: str):
    """Print a formatted section header"""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print('=' * 60)


def demo_basic_operations():
    """Demonstrate basic tree and node operations"""
    print_section("1. Basic Tree & Node Operations")

    with TreeDB("demo.sqlite") as db:
        # Create a new tree
        tree_id, root_node_id = db.create_tree(meta={"name": "Demo Tree", "version": "1.0"})
        print(f"\nCreated tree: {tree_id}")
        print(f"Root node: {root_node_id}")

        # Get root node
        root = db.get_node(tree_id, root_node_id)
        print(f"\nRoot node details:")
        print(json.dumps(root.to_dict(), indent=2))

        # Get children (should be empty for new root)
        children = db.get_children(tree_id, root_node_id)
        print(f"\nRoot has {len(children)} children")

        return tree_id, root_node_id


def demo_tree_ingestion():
    """Demonstrate tree ingestion with complex structure"""
    print_section("2. Tree Ingestion")

    # Define a complex tree structure
    tree_structure = {
        "type": "object",
        "entity_type": "document",
        "entity_id": "doc_001",
        "attrs": {"title": "My Document"},
        "children": {
            "metadata": {
                "type": "object",
                "children": {
                    "author": {
                        "type": "leaf",
                        "entity_type": "user",
                        "entity_id": "user_123",
                        "attrs": {"name": "John Doe"}
                    },
                    "created": {
                        "type": "leaf",
                        "attrs": {"timestamp": "2024-01-13T10:00:00Z"}
                    }
                }
            },
            "sections": {
                "type": "array",
                "children": [
                    {
                        "type": "object",
                        "entity_type": "section",
                        "entity_id": "section_001",
                        "children": {
                            "title": {
                                "type": "leaf",
                                "attrs": {"text": "Introduction"}
                            },
                            "content": {
                                "type": "leaf",
                                "entity_type": "text_block",
                                "entity_id": "text_001"
                            }
                        }
                    },
                    {
                        "type": "object",
                        "entity_type": "section",
                        "entity_id": "section_002",
                        "children": {
                            "title": {
                                "type": "leaf",
                                "attrs": {"text": "Conclusion"}
                            },
                            "content": {
                                "type": "leaf",
                                "entity_type": "text_block",
                                "entity_id": "text_002"
                            }
                        }
                    }
                ]
            }
        }
    }

    # Define entities
    entities = {
        "doc_001": {
            "type": "document",
            "title": "TreeDB Documentation",
            "format": "markdown"
        },
        "user_123": {
            "type": "user",
            "name": "John Doe",
            "email": "john@example.com"
        },
        "section_001": {
            "type": "section",
            "order": 1
        },
        "section_002": {
            "type": "section",
            "order": 2
        },
        "text_001": {
            "type": "text_block",
            "content": "This is the introduction to TreeDB, a powerful tree storage engine."
        },
        "text_002": {
            "type": "text_block",
            "content": "In conclusion, TreeDB provides efficient tree operations with entity dereferencing."
        }
    }

    with TreeDB("demo.sqlite") as db:
        tree_id = db.ingest_tree(
            tree_structure,
            entities=entities,
            meta={"source": "demo", "imported_at": "2024-01-13"}
        )
        print(f"\nIngested tree: {tree_id}")

        return tree_id


def demo_subtree_retrieval(tree_id: str):
    """Demonstrate subtree retrieval at different depths"""
    print_section("3. Subtree Retrieval")

    with TreeDB("demo.sqlite") as db:
        # Get tree info
        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM trees WHERE tree_id = ?", (tree_id,))
        tree_row = cursor.fetchone()
        root_node_id = tree_row['root_node_id']

        # Get subtree depth 1 (no entities)
        print("\n--- Subtree (depth=1, no entities) ---")
        subtree = db.get_subtree(tree_id, root_node_id, max_depth=1, with_entities=False)
        print(f"Retrieved {len(subtree)} nodes")
        for node in subtree[:3]:  # Show first 3
            print(f"  - {node['node_id'][:8]}... depth={node['depth']} slot={node.get('slot', 'NULL')}")

        # Get subtree depth 2
        print("\n--- Subtree (depth=2, no entities) ---")
        subtree = db.get_subtree(tree_id, root_node_id, max_depth=2, with_entities=False)
        print(f"Retrieved {len(subtree)} nodes")

        # Get full subtree with entities
        print("\n--- Full Subtree (with entities) ---")
        subtree_with_entities = db.get_subtree(tree_id, root_node_id, max_depth=100, with_entities=True)
        print(f"Retrieved {len(subtree_with_entities)} nodes")

        # Show nodes with entities
        print("\nNodes with entities:")
        for node in subtree_with_entities:
            if 'entity' in node:
                print(f"  - Node {node['node_id'][:8]}...")
                print(f"    Entity type: {node['entity_type']}")
                print(f"    Entity data: {json.dumps(node['entity']['payload'], indent=6)}")


def demo_entity_retrieval(tree_id: str):
    """Demonstrate entity dereferencing"""
    print_section("4. Entity Dereferencing")

    with TreeDB("demo.sqlite") as db:
        # Find nodes with entities
        cursor = db.conn.cursor()
        cursor.execute("""
            SELECT node_id, entity_type, entity_id
            FROM nodes
            WHERE tree_id = ? AND entity_id IS NOT NULL
            LIMIT 3
        """, (tree_id,))

        print("\nFetching entities for nodes:")
        for row in cursor.fetchall():
            node_id = row['node_id']
            entity = db.get_entity(tree_id, node_id)
            if entity:
                print(f"\nNode {node_id[:8]}...")
                print(f"  Entity type: {entity.entity_type}")
                print(f"  Entity data: {json.dumps(json.loads(entity.payload_json), indent=4)}")


def demo_queries():
    """Demonstrate various queries"""
    print_section("5. Database Statistics")

    with TreeDB("demo.sqlite") as db:
        cursor = db.conn.cursor()

        # Count trees
        cursor.execute("SELECT COUNT(*) as count FROM trees")
        print(f"\nTotal trees: {cursor.fetchone()['count']}")

        # Count nodes
        cursor.execute("SELECT COUNT(*) as count FROM nodes")
        print(f"Total nodes: {cursor.fetchone()['count']}")

        # Count entities
        cursor.execute("SELECT COUNT(*) as count FROM entities")
        print(f"Total entities: {cursor.fetchone()['count']}")

        # Node type distribution
        cursor.execute("""
            SELECT node_type, COUNT(*) as count
            FROM nodes
            GROUP BY node_type
        """)
        print("\nNode type distribution:")
        type_names = {0: "object", 1: "array", 2: "leaf"}
        for row in cursor.fetchall():
            print(f"  {type_names.get(row['node_type'], 'unknown')}: {row['count']}")

        # Depth distribution
        cursor.execute("""
            SELECT depth, COUNT(*) as count
            FROM nodes
            GROUP BY depth
            ORDER BY depth
        """)
        print("\nDepth distribution:")
        for row in cursor.fetchall():
            print(f"  Depth {row['depth']}: {row['count']} nodes")


def main():
    """Run all demos"""
    print("\n" + "=" * 60)
    print("  TreeDB v0.1 - Demonstration")
    print("=" * 60)
    print("\nThis demo showcases:")
    print("  ✓ Trees are first-class objects")
    print("  ✓ Nodes are first-class addressable records")
    print("  ✓ Subtree retrieval is a native primitive (depth-K)")
    print("  ✓ Entities are dereferenced on-demand")

    # Run demos
    demo_basic_operations()
    tree_id = demo_tree_ingestion()
    demo_subtree_retrieval(tree_id)
    demo_entity_retrieval(tree_id)
    demo_queries()

    print("\n" + "=" * 60)
    print("  Demo Complete!")
    print("=" * 60)
    print("\nDatabase saved to: demo.sqlite")
    print("You can inspect it with: sqlite3 demo.sqlite")
    print()


if __name__ == "__main__":
    main()
