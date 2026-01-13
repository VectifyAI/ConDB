# TreeDB v0.1 - SQLite MVP

A lightweight, SQLite-based tree storage engine with first-class tree and node primitives, native subtree operations, and on-demand entity dereferencing.

## Features

- **Trees are first-class objects** - Each tree is independently addressable with metadata
- **Nodes are first-class addressable records** - Every node has a unique ID and materialized path
- **Subtree retrieval is a native primitive** - Efficient depth-K subtree queries with single SQL operation
- **Entities are dereferenced on-demand** - Separate entity storage with batch fetching support

## Architecture

### Schema

**trees** - Tree registry
- `tree_id`: UUID primary key
- `root_node_id`: Reference to root node
- `meta_json`: Optional JSON metadata
- Timestamps: `created_at`, `updated_at`

**nodes** - Core tree storage
- Composite primary key: `(tree_id, node_id)`
- `parent_id`: Parent node reference (NULL for root)
- `slot`: Object key or array index
- `node_type`: 0=object, 1=array, 2=leaf
- `depth`: Node depth in tree
- `path`: Materialized path using node IDs (format: `/r/<root>/<child>/...`)
- Optional entity reference: `entity_type`, `entity_id`
- Optional `attrs_json`: Node-specific metadata
- Timestamps: `created_at`, `updated_at`

**entities** - Generic entity storage
- `entity_id`: UUID primary key
- `entity_type`: Entity type label
- `payload_json`: JSON payload
- `created_at`: Creation timestamp

### Indexes

Critical indexes for performance:
- `nodes_path_idx`: Enables fast subtree prefix scans
- `nodes_parent_idx`: Enables fast children traversal
- `nodes_child_unique`: Ensures one child per slot under parent
- `nodes_entity_idx`: Enables entity reverse lookup

### SQLite Configuration

Optimized for concurrency and performance:
```sql
PRAGMA journal_mode = WAL;      -- Write-Ahead Logging for concurrent access
PRAGMA synchronous = NORMAL;    -- Balanced durability/performance
PRAGMA temp_store = MEMORY;     -- In-memory temp tables
PRAGMA foreign_keys = ON;       -- Referential integrity
```

## Installation

No external dependencies required - uses Python standard library only.

```bash
# Just copy treedb.py to your project
cp treedb.py /your/project/
```

## Quick Start

```python
from treedb import TreeDB

# Initialize database
db = TreeDB("mydata.sqlite")

# Create a new tree
tree_id, root_node_id = db.create_tree(meta={"name": "My Tree"})

# Get a node
node = db.get_node(tree_id, root_node_id)

# Get node's children
children = db.get_children(tree_id, root_node_id)

# Get entire subtree (depth-limited)
subtree = db.get_subtree(tree_id, root_node_id, max_depth=5)

# Get subtree with entities
subtree_with_entities = db.get_subtree(
    tree_id,
    root_node_id,
    max_depth=10,
    with_entities=True
)

# Get entity for a node
entity = db.get_entity(tree_id, node_id)

db.close()
```

## Tree Ingestion

Ingest complex tree structures with entity references:

```python
# Define tree structure
tree_structure = {
    "type": "object",
    "entity_type": "document",
    "entity_id": "doc_001",
    "children": {
        "title": {
            "type": "leaf",
            "attrs": {"text": "Hello World"}
        },
        "sections": {
            "type": "array",
            "children": [
                {
                    "type": "object",
                    "entity_type": "section",
                    "entity_id": "section_001",
                    "children": {
                        "heading": {"type": "leaf", "attrs": {"text": "Introduction"}},
                        "content": {"type": "leaf", "entity_type": "text", "entity_id": "text_001"}
                    }
                }
            ]
        }
    }
}

# Define entities
entities = {
    "doc_001": {"type": "document", "title": "My Document"},
    "section_001": {"type": "section", "order": 1},
    "text_001": {"type": "text", "content": "This is the introduction..."}
}

# Ingest
tree_id = db.ingest_tree(
    tree_structure,
    entities=entities,
    meta={"source": "import", "version": "1.0"}
)
```

## API Reference

### TreeDB Class

#### `__init__(db_path: str = "treedb.sqlite")`
Initialize TreeDB with SQLite database at specified path.

#### `create_tree(meta: Optional[Dict] = None) -> Tuple[str, str]`
Create a new tree with root node. Returns `(tree_id, root_node_id)`.

#### `get_node(tree_id: str, node_id: str) -> Optional[Node]`
Fetch a single node by ID.

#### `get_children(tree_id: str, node_id: str) -> List[Node]`
Get all direct children of a node, ordered by slot.

#### `get_subtree(tree_id: str, node_id: str, max_depth: int = 100, with_entities: bool = False) -> List[Dict]`
Get entire subtree rooted at `node_id`, up to `max_depth` levels deep.
- `with_entities=True`: Include dereferenced entity data in results
- Returns list of node dictionaries (optionally with `entity` field)

#### `get_entity(tree_id: str, node_id: str) -> Optional[Entity]`
Get entity referenced by a node (if any).

#### `ingest_tree(tree_structure: Dict, entities: Optional[Dict] = None, meta: Optional[Dict] = None) -> str`
Ingest a tree structure with optional entities. Returns `tree_id`.

**Tree structure format:**
```python
{
    "type": "object" | "array" | "leaf",
    "children": {...} or [...],     # for object/array nodes
    "entity_type": str,              # optional
    "entity_id": str,                # optional
    "attrs": {...}                   # optional node metadata
}
```

## Data Model

### Node Types
- `OBJECT` (0): Container node with named children (slot = key name)
- `ARRAY` (1): Container node with ordered children (slot = index as string)
- `LEAF` (2): Terminal node (no children)

### Path Encoding
Paths use node IDs for stability (renaming doesn't break structure):
- Root: `/r/<root_node_id>`
- Child: `/r/<root_id>/<child_id>`
- Grandchild: `/r/<root_id>/<child_id>/<grandchild_id>`

Subtree queries use prefix matching: `WHERE path LIKE '/r/<root_id>/%'`

### Entity Dereferencing
Entities are fetched on-demand:
1. Node stores `entity_type` and `entity_id` (nullable)
2. Get entity: two-step fetch or JOIN
3. Subtree with entities: batch fetch all entity IDs in one query

## Performance Characteristics

### Hot Paths (Single-digit milliseconds)
- `get_node`: Primary key lookup
- `get_children`: Indexed parent scan
- `get_subtree`: Indexed path prefix scan + depth filter

### Batch Operations
- Ingestion: Wrapped in single transaction for speed
- Entity fetching: Batch SELECT with `IN (?, ?, ...)`
- Prepared statements for repeated queries

### Optimization Notes
- WAL mode enables concurrent readers during writes
- Keep `attrs_json` small (< 1KB recommended)
- For very large trees (millions of nodes), consider path length optimizations
- Subtree depth limiting prevents unbounded result sets

## Running the Demo

```bash
python demo.py
```

The demo showcases:
1. Basic tree and node operations
2. Complex tree ingestion
3. Subtree retrieval at different depths
4. Entity dereferencing
5. Database statistics

Demo creates `demo.sqlite` which you can inspect:
```bash
sqlite3 demo.sqlite
.tables
.schema nodes
SELECT COUNT(*) FROM nodes;
```

## Use Cases

- **Document structure storage**: Store nested document trees with text blocks as entities
- **AST storage**: Store abstract syntax trees with code snippets as entities
- **Configuration trees**: Hierarchical config with validation metadata
- **Navigation hierarchies**: Menus, sitemaps, org charts
- **Dependency graphs**: Build trees with shared entity references

## Limitations (MVP)

- Single SQLite file (no sharding)
- Node-ID based paths (longer than key-based paths)
- No built-in versioning or time-travel
- Entity storage is simple key-value (not relational)
- No built-in access control

## Future Enhancements

- Fractional indexing for array reordering
- Path hash optimization for very deep trees
- Incremental tree updates (node add/remove/move)
- Tree diff and merge operations
- Entity schema validation
- Compression for large attrs/payloads

## License

MIT
