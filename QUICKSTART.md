# ConDB Quick Start

## Installation

No dependencies required. Just use the Python file:

```python
from condb import ConDB
```

## Core Operations

ConDB implements the Context Tree consumption model with four primary operations:

| Operation | What it does | Method |
|-----------|--------------|--------|
| **Select** | Choose a node by ID | `get_node()` |
| **Expand** | Get more detail (children or subtree) | `get_children()`, `get_subtree()` |
| **Collapse** | Use summary only | Read `attrs_json` |
| **Traverse** | Move up/down abstraction levels | Use `parent_id` or `path` |

## Basic Usage

```python
from condb import ConDB

# Initialize
db = ConDB("context.sqlite")

# Create a Context Tree
tree_id, root_id = db.create_tree(meta={"domain": "research"})

# Select: get a specific node
node = db.get_node(tree_id, root_id)

# Expand: get children (one level of refinement)
children = db.get_children(tree_id, root_id)

# Expand: get subtree (multiple refinement levels)
subtree = db.get_subtree(tree_id, root_id, max_depth=3)

# Close
db.close()
```

## Context Manager

```python
with ConDB("context.sqlite") as db:
    tree_id, root_id = db.create_tree()
    # ... operations ...
    # Automatically closed
```

## Ingest a Context Tree

Build a hierarchical context structure with summaries and content references:

```python
# Define the Context Tree structure
context_tree = {
    "type": "object",
    "attrs": {"summary": "Research paper on machine learning optimization"},
    "entity_type": "document",
    "entity_id": "paper_001",
    "children": {
        "abstract": {
            "type": "leaf",
            "attrs": {"summary": "Novel approach to gradient descent with 15% improvement"},
            "entity_type": "section",
            "entity_id": "abstract_001"
        },
        "methodology": {
            "type": "object",
            "attrs": {"summary": "Experimental setup and algorithms"},
            "children": {
                "algorithm": {
                    "type": "leaf",
                    "attrs": {"summary": "Modified Adam optimizer with momentum decay"},
                    "entity_type": "section",
                    "entity_id": "algo_001"
                },
                "dataset": {
                    "type": "leaf",
                    "attrs": {"summary": "ImageNet subset, 100k samples"},
                    "entity_type": "section",
                    "entity_id": "data_001"
                }
            }
        }
    }
}

# Define the underlying content
content = {
    "paper_001": {"title": "Adaptive Learning Rate Methods", "authors": ["A", "B"]},
    "abstract_001": {"text": "We present a novel optimization technique..."},
    "algo_001": {"text": "The algorithm modifies standard Adam by...", "code": "..."},
    "data_001": {"text": "Training was performed on ImageNet...", "samples": 100000}
}

# Ingest
tree_id = db.ingest_tree(context_tree, entities=content)
```

## Expand with Content

```python
# Get subtree with dereferenced content
result = db.get_subtree(
    tree_id,
    root_id,
    max_depth=5,
    with_entities=True  # Include underlying content
)

# Each node may have an 'entity' field with content
for node in result:
    print(f"Summary: {node.get('attrs', {}).get('summary', 'N/A')}")
    if 'entity' in node:
        print(f"Content: {node['entity']['payload']}")
```

## Traverse Abstraction Levels

```python
# Start at a leaf node, traverse up to higher abstractions
node = db.get_node(tree_id, some_leaf_id)

# Move up: get parent (higher abstraction)
parent = db.get_node(tree_id, node['parent_id'])

# Move down: get children (lower abstraction / more detail)
children = db.get_children(tree_id, parent['node_id'])
```

## Direct SQL Queries

```python
cursor = db.conn.cursor()

# Find all leaf nodes (factual grounding)
cursor.execute("""
    SELECT node_id, attrs_json, entity_id
    FROM nodes
    WHERE tree_id = ? AND node_type = 2
""", (tree_id,))

# Get nodes at a specific abstraction level
cursor.execute("""
    SELECT node_id, attrs_json
    FROM nodes
    WHERE tree_id = ? AND depth = 2
""", (tree_id,))

# Find nodes by content type
cursor.execute("""
    SELECT node_id, entity_id
    FROM nodes
    WHERE tree_id = ? AND entity_type = 'section'
""", (tree_id,))
```

## Node Types

- `ConDB.OBJECT` (0): Named refinements (semantic children)
- `ConDB.ARRAY` (1): Ordered refinements (sequential children)
- `ConDB.LEAF` (2): Factual grounding (no further refinement)

## Common Patterns

### Get all summaries at depth N

```python
cursor.execute("""
    SELECT node_id, json_extract(attrs_json, '$.summary') as summary
    FROM nodes
    WHERE tree_id = ? AND depth = ?
""", (tree_id, 1))
```

### Find nodes by summary keyword

```python
cursor.execute("""
    SELECT node_id, attrs_json
    FROM nodes
    WHERE tree_id = ? AND attrs_json LIKE '%optimization%'
""", (tree_id,))
```

### Get abstraction level distribution

```python
cursor.execute("""
    SELECT depth, COUNT(*) as count
    FROM nodes
    WHERE tree_id = ?
    GROUP BY depth
    ORDER BY depth
""", (tree_id,))
```

### Count children per node

```python
cursor.execute("""
    SELECT parent_id, COUNT(*) as refinements
    FROM nodes
    WHERE tree_id = ? AND parent_id IS NOT NULL
    GROUP BY parent_id
""", (tree_id,))
```

## Performance Tips

1. Use `max_depth` to control expansion scope
2. Use `with_entities=False` when only summaries are needed (Collapse pattern)
3. Store concise summaries in `attrs_json` for effective navigation
4. Keep content payloads in entities, not in node attributes
5. Use transactions for batch ingestion

## Demo

Run the demo to see Context Tree operations in action:

```bash
python demo.py
```

## Inspect Database

```bash
sqlite3 context.sqlite

# Show tables
.tables

# Show Context Node schema
.schema nodes

# Query nodes
SELECT COUNT(*) FROM nodes;
SELECT depth, COUNT(*) FROM nodes GROUP BY depth;
```
