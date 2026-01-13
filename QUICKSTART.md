# TreeDB Quick Start

## Installation

No dependencies required. Just use the Python file:

```python
from treedb import TreeDB
```

## Basic Usage

```python
# Initialize
db = TreeDB("mydata.sqlite")

# Create a tree
tree_id, root_node_id = db.create_tree(meta={"name": "My Tree"})

# Get a node
node = db.get_node(tree_id, root_node_id)

# Get children
children = db.get_children(tree_id, root_node_id)

# Get subtree
subtree = db.get_subtree(tree_id, root_node_id, max_depth=5)

# Close
db.close()
```

## Context Manager

```python
with TreeDB("mydata.sqlite") as db:
    tree_id, root_id = db.create_tree()
    # ... operations ...
    # Automatically closed
```

## Ingest a Tree

```python
tree_structure = {
    "type": "object",
    "entity_type": "document",
    "entity_id": "doc_001",
    "children": {
        "title": {
            "type": "leaf",
            "attrs": {"text": "My Document"}
        },
        "sections": {
            "type": "array",
            "children": [
                {
                    "type": "object",
                    "entity_type": "section",
                    "entity_id": "sect_001",
                    "children": {
                        "heading": {"type": "leaf", "attrs": {"text": "Introduction"}},
                        "content": {"type": "leaf", "entity_id": "text_001"}
                    }
                }
            ]
        }
    }
}

entities = {
    "doc_001": {"type": "document", "title": "TreeDB Guide"},
    "sect_001": {"type": "section", "number": 1},
    "text_001": {"type": "text", "content": "Welcome to TreeDB..."}
}

tree_id = db.ingest_tree(tree_structure, entities=entities)
```

## Get Subtree with Entities

```python
# Get entire subtree with entity data dereferenced
result = db.get_subtree(
    tree_id,
    root_node_id,
    max_depth=10,
    with_entities=True
)

# Each node in result may have an 'entity' field
for node in result:
    if 'entity' in node:
        print(f"Node {node['node_id']} has entity: {node['entity']['payload']}")
```

## Direct SQL Queries

```python
cursor = db.conn.cursor()

# Find all nodes with a specific entity type
cursor.execute("""
    SELECT node_id, depth, path
    FROM nodes
    WHERE tree_id = ? AND entity_type = ?
""", (tree_id, "section"))

for row in cursor.fetchall():
    print(f"Section at depth {row['depth']}: {row['path']}")
```

## Node Types

- `TreeDB.OBJECT` (0): Container with named children
- `TreeDB.ARRAY` (1): Container with ordered children
- `TreeDB.LEAF` (2): Terminal node

## Common Patterns

### Find all leaf nodes

```python
cursor.execute("SELECT * FROM nodes WHERE tree_id = ? AND node_type = 2", (tree_id,))
leaves = cursor.fetchall()
```

### Get depth distribution

```python
cursor.execute("""
    SELECT depth, COUNT(*) as count
    FROM nodes
    WHERE tree_id = ?
    GROUP BY depth
    ORDER BY depth
""", (tree_id,))
```

### Find nodes by entity type

```python
cursor.execute("""
    SELECT node_id, entity_id
    FROM nodes
    WHERE tree_id = ? AND entity_type = ?
""", (tree_id, "user"))
```

### Get children count

```python
cursor.execute("""
    SELECT parent_id, COUNT(*) as child_count
    FROM nodes
    WHERE tree_id = ?
    GROUP BY parent_id
""", (tree_id,))
```

## Performance Tips

1. Use transactions for batch operations
2. Limit subtree depth to avoid large result sets
3. Use `with_entities=False` when entity data isn't needed
4. Index on entity_type if doing frequent entity queries
5. Keep attrs_json small (< 1KB recommended)

## Examples

See `examples.py` for complete working examples:
- Document tree with chapters and paragraphs
- Configuration tree
- Abstract syntax tree
- Navigation menu hierarchy
- Advanced queries

Run with:
```bash
python examples.py
```

## Demo

Run the full demo:
```bash
python demo.py
```

## Inspect Database

```bash
sqlite3 mydata.sqlite

# Show tables
.tables

# Show schema
.schema nodes

# Query nodes
SELECT COUNT(*) FROM nodes;
SELECT * FROM nodes LIMIT 5;
```
