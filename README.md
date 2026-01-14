# ConDB v0.1 - Context Tree Database

A lightweight, SQLite-based database for storing and navigating **Context Trees** — hierarchical representations of contextual information organized from coarse-grained abstractions to fine-grained details.

## What is a Context Tree?

A **Context Tree** is a structured abstraction for context reasoning. Rather than treating context as flat text or isolated chunks, a Context Tree encodes hierarchy and abstraction directly into the representation, enabling:

- **Hierarchical Coherence**: All contextual units are explicitly situated within a global hierarchy
- **Traceability**: Any conclusion can be traced through the tree to its source content
- **Abstraction Control**: Explicit control over how much context is exposed at each reasoning step
- **Explainability**: Reasoning paths correspond directly to traversal paths in the tree

ConDB provides the storage layer for Context Trees, making them persistent, queryable, and navigable.

## Core Concepts

### Context Node

Each node in a Context Tree is defined as:

- **Summary** (`attrs_json`): A navigational abstraction sufficient for reasoning without expansion
- **Content Reference** (`entity_type`, `entity_id`): Reference to underlying content (text spans, documents, raw data)
- **Metadata**: Category labels, timestamps, provenance information

A node is **self-contained** when its summary is sufficient to support reasoning without requiring expansion into its children.

### Refinement Relation

For any parent-child relationship in the tree:
- The child is a **refinement** of the parent
- Child content is scoped within parent content
- Traversing downward increases specificity; upward increases abstraction

### Leaf Nodes

Leaf nodes contain raw or minimally processed content and serve as the **factual grounding** of the Context Tree.

## Context Consumption Model

ConDB supports the procedural context consumption model:

| Operation | Description | ConDB Method |
|-----------|-------------|--------------|
| **Select** | Choose a node based on relevance | `get_node()` |
| **Expand** | Include children for more detail | `get_children()`, `get_subtree()` |
| **Collapse** | Reason using only node summary | Access `attrs_json` only |
| **Traverse** | Move between abstraction levels | Navigate via `parent_id` or `path` |

## Architecture

### Schema

**trees** - Context Tree registry
- `tree_id`: UUID primary key
- `root_node_id`: Reference to root context node
- `meta_json`: Tree-level metadata (source, version, domain)
- Timestamps: `created_at`, `updated_at`

**nodes** - Context Node storage
- Composite key: `(tree_id, node_id)`
- `parent_id`: Parent node (refinement source)
- `slot`: Semantic key or position index
- `node_type`: 0=object, 1=array, 2=leaf
- `depth`: Abstraction level (higher = more detailed)
- `path`: Materialized path for efficient subtree queries
- `entity_type`, `entity_id`: Content reference (optional)
- `attrs_json`: Node summary and metadata

**entities** - Content storage
- `entity_id`: UUID primary key
- `entity_type`: Content type label
- `payload_json`: Underlying content payload

### Key Indexes

- `nodes_path_idx`: Fast subtree prefix scans (Expand operation)
- `nodes_parent_idx`: Fast children traversal
- `nodes_entity_idx`: Content reverse lookup

## Installation

No external dependencies — uses Python standard library only.

```bash
# Copy to your project
cp condb.py /your/project/
```

## Quick Start

```python
from condb import ConDB

# Initialize database
db = ConDB("context.sqlite")

# Create a new Context Tree
tree_id, root_id = db.create_tree(meta={"domain": "legal", "source": "contract_v2"})

# Select a node
node = db.get_node(tree_id, root_id)

# Expand: get children for more detail
children = db.get_children(tree_id, root_id)

# Expand: get full subtree with depth control
subtree = db.get_subtree(tree_id, root_id, max_depth=3)

# Expand with content dereferencing
subtree_with_content = db.get_subtree(
    tree_id, root_id,
    max_depth=5,
    with_entities=True  # Include underlying content
)

db.close()
```

## Ingesting Context Trees

Build hierarchical context structures with content references:

```python
# Define Context Tree structure
context_tree = {
    "type": "object",
    "attrs": {"summary": "Q3 2024 Financial Report - Overview of company performance"},
    "entity_type": "document",
    "entity_id": "report_001",
    "children": {
        "executive_summary": {
            "type": "leaf",
            "attrs": {"summary": "Key highlights: Revenue up 15%, new market expansion"},
            "entity_type": "section",
            "entity_id": "section_001"
        },
        "financial_details": {
            "type": "object",
            "attrs": {"summary": "Detailed financial breakdown by segment"},
            "children": {
                "revenue": {
                    "type": "leaf",
                    "attrs": {"summary": "Revenue analysis by product line"},
                    "entity_type": "section",
                    "entity_id": "section_002"
                },
                "expenses": {
                    "type": "leaf",
                    "attrs": {"summary": "Operating expenses and cost optimization"},
                    "entity_type": "section",
                    "entity_id": "section_003"
                }
            }
        }
    }
}

# Define underlying content
content = {
    "report_001": {"title": "Q3 2024 Report", "pages": 45},
    "section_001": {"text": "This quarter showed strong performance..."},
    "section_002": {"text": "Product line A generated $2.3M...", "figures": [...]},
    "section_003": {"text": "Operating expenses decreased by 8%..."}
}

# Ingest the Context Tree
tree_id = db.ingest_tree(context_tree, entities=content)
```

## API Reference

### ConDB Class

#### `create_tree(meta: Optional[Dict] = None) -> Tuple[str, str]`
Create a new Context Tree. Returns `(tree_id, root_node_id)`.

#### `get_node(tree_id: str, node_id: str) -> Optional[Node]`
**Select** operation — fetch a single context node.

#### `get_children(tree_id: str, node_id: str) -> List[Node]`
**Expand** operation — get direct refinements of a node.

#### `get_subtree(tree_id: str, node_id: str, max_depth: int = 100, with_entities: bool = False) -> List[Dict]`
**Expand** operation — get entire subtree with abstraction level control.
- `max_depth`: Control how many refinement levels to include
- `with_entities`: Include dereferenced content in results

#### `get_entity(tree_id: str, node_id: str) -> Optional[Entity]`
Get underlying content referenced by a node.

#### `ingest_tree(tree_structure: Dict, entities: Optional[Dict] = None, meta: Optional[Dict] = None) -> str`
Ingest a complete Context Tree with content. Returns `tree_id`.

## Use Cases

ConDB is designed for systems that implement the Context Tree abstraction:

- **PageIndex**: Document context — hierarchical representation of documents, papers, reports
- **ChatIndex**: Conversational context — structured conversation history with topic hierarchy
- **AgentFS**: Agent context — filesystem-like context navigation for AI agents
- **Legal/Compliance**: Navigable policy and regulatory document structures
- **Research**: Hierarchical literature review with traceable citations

## Data Model

### Node Types
- `OBJECT` (0): Named children (semantic slots like "introduction", "methodology")
- `ARRAY` (1): Ordered children (sequential refinements)
- `LEAF` (2): Terminal node with content reference (factual grounding)

### Abstraction Levels
The `depth` field represents abstraction level:
- `depth=0`: Root — highest abstraction (e.g., "entire document")
- `depth=1`: First refinement (e.g., "major sections")
- `depth=N`: N-th refinement level (increasing specificity)

### Path Encoding
Paths enable efficient subtree queries:
```
/r/<root_id>                      # Root node
/r/<root_id>/<child_id>           # First refinement
/r/<root_id>/<child_id>/<...>     # Deeper refinements
```

## Performance

### Optimized Operations
- **Select**: O(1) primary key lookup
- **Expand (children)**: Indexed parent scan
- **Expand (subtree)**: Indexed path prefix scan with depth filter

### SQLite Configuration
```sql
PRAGMA journal_mode = WAL;      -- Concurrent access
PRAGMA synchronous = NORMAL;    -- Balanced durability
PRAGMA foreign_keys = ON;       -- Referential integrity
```

## Limitations (MVP)

- Single SQLite file (no distributed storage)
- No built-in versioning or temporal queries
- Simple key-value content storage
- No access control layer

## Future Directions

- Tree diff and merge for context evolution
- Incremental updates (node add/remove/move)
- Content schema validation
- Compression for large content payloads
- Multi-tree linking for cross-reference

## License

MIT
