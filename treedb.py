"""
TreeDB v0.1 - SQLite-based tree storage with entity references
"""

import sqlite3
import uuid
import json
import time
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class Node:
    """Represents a node in the tree"""
    tree_id: str
    node_id: str
    parent_id: Optional[str]
    slot: Optional[str]
    node_type: int  # 0=object, 1=array, 2=leaf
    depth: int
    path: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    attrs_json: Optional[str] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Entity:
    """Represents an entity"""
    entity_id: str
    entity_type: str
    payload_json: str
    created_at: int

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary with parsed payload"""
        return {
            'entity_id': self.entity_id,
            'entity_type': self.entity_type,
            'payload': json.loads(self.payload_json),
            'created_at': self.created_at
        }


@dataclass
class Tree:
    """Represents a tree"""
    tree_id: str
    root_node_id: str
    created_at: int
    updated_at: int
    meta_json: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary with parsed meta"""
        result = {
            'tree_id': self.tree_id,
            'root_node_id': self.root_node_id,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }
        if self.meta_json:
            result['meta'] = json.loads(self.meta_json)
        return result


class TreeDB:
    """
    TreeDB v0.1 - SQLite-based tree storage

    Features:
    - Trees are first-class objects
    - Nodes are first-class addressable records
    - Subtree retrieval is a native primitive (depth-K)
    - Entities are dereferenced on-demand
    """

    # Node types
    OBJECT = 0
    ARRAY = 1
    LEAF = 2

    def __init__(self, db_path: str = "treedb.sqlite"):
        """Initialize TreeDB with SQLite database"""
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._initialize_db()

    def _initialize_db(self):
        """Initialize database schema, indexes, and pragmas"""
        cursor = self.conn.cursor()

        # Enable WAL mode for concurrency
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA temp_store = MEMORY")
        cursor.execute("PRAGMA foreign_keys = ON")

        # Create trees table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trees (
                tree_id       TEXT PRIMARY KEY,
                root_node_id  TEXT NOT NULL,
                created_at    INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL,
                meta_json     TEXT
            )
        """)

        # Create nodes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                tree_id      TEXT NOT NULL,
                node_id      TEXT NOT NULL,
                parent_id    TEXT,
                slot         TEXT,
                node_type    INTEGER NOT NULL,
                depth        INTEGER NOT NULL,
                path         TEXT NOT NULL,
                entity_type  TEXT,
                entity_id    TEXT,
                attrs_json   TEXT,
                created_at   INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL,
                PRIMARY KEY (tree_id, node_id)
            )
        """)

        # Create entities table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                entity_id    TEXT PRIMARY KEY,
                entity_type  TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at   INTEGER NOT NULL
            )
        """)

        # Create indexes
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS nodes_child_unique
            ON nodes(tree_id, parent_id, slot)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS nodes_path_idx
            ON nodes(tree_id, path)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS nodes_parent_idx
            ON nodes(tree_id, parent_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS nodes_entity_idx
            ON nodes(tree_id, entity_type, entity_id)
        """)

        self.conn.commit()

    def _timestamp(self) -> int:
        """Get current timestamp in milliseconds"""
        return int(time.time() * 1000)

    def create_tree(self, meta: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        """
        Create a new tree with a root node

        Args:
            meta: Optional metadata dictionary

        Returns:
            Tuple of (tree_id, root_node_id)
        """
        tree_id = str(uuid.uuid4())
        root_node_id = str(uuid.uuid4())
        now = self._timestamp()

        cursor = self.conn.cursor()

        # Insert tree
        cursor.execute("""
            INSERT INTO trees (tree_id, root_node_id, created_at, updated_at, meta_json)
            VALUES (?, ?, ?, ?, ?)
        """, (tree_id, root_node_id, now, now, json.dumps(meta) if meta else None))

        # Insert root node
        root_path = f"/r/{root_node_id}"
        cursor.execute("""
            INSERT INTO nodes (tree_id, node_id, parent_id, slot, node_type, depth, path, created_at, updated_at)
            VALUES (?, ?, NULL, NULL, ?, 0, ?, ?, ?)
        """, (tree_id, root_node_id, self.OBJECT, root_path, now, now))

        self.conn.commit()

        return tree_id, root_node_id

    def get_node(self, tree_id: str, node_id: str) -> Optional[Node]:
        """
        Get a single node by ID

        Args:
            tree_id: Tree identifier
            node_id: Node identifier

        Returns:
            Node object or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT *
            FROM nodes
            WHERE tree_id = ? AND node_id = ?
        """, (tree_id, node_id))

        row = cursor.fetchone()
        if row:
            return Node(**dict(row))
        return None

    def get_children(self, tree_id: str, node_id: str) -> List[Node]:
        """
        Get all direct children of a node

        Args:
            tree_id: Tree identifier
            node_id: Parent node identifier

        Returns:
            List of child Node objects, ordered by slot
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT *
            FROM nodes
            WHERE tree_id = ? AND parent_id = ?
            ORDER BY slot
        """, (tree_id, node_id))

        return [Node(**dict(row)) for row in cursor.fetchall()]

    def get_subtree(
        self,
        tree_id: str,
        node_id: str,
        max_depth: int = 100,
        with_entities: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Get entire subtree rooted at node_id, up to max_depth levels deep

        Args:
            tree_id: Tree identifier
            node_id: Root node of subtree
            max_depth: Maximum depth to traverse (relative to root)
            with_entities: If True, include entity data for nodes that reference entities

        Returns:
            List of node dictionaries (optionally with entity data)
        """
        cursor = self.conn.cursor()

        # Get root node's path and depth
        cursor.execute("""
            SELECT path, depth
            FROM nodes
            WHERE tree_id = ? AND node_id = ?
        """, (tree_id, node_id))

        root_row = cursor.fetchone()
        if not root_row:
            return []

        root_path = root_row['path']
        root_depth = root_row['depth']
        depth_limit = root_depth + max_depth

        # Get root + all descendants within depth limit
        cursor.execute("""
            SELECT * FROM nodes WHERE tree_id = ? AND node_id = ?
            UNION ALL
            SELECT *
            FROM nodes
            WHERE tree_id = ?
              AND path LIKE ? || '/%'
              AND depth <= ?
            ORDER BY path
        """, (tree_id, node_id, tree_id, root_path, depth_limit))

        nodes = [Node(**dict(row)) for row in cursor.fetchall()]

        if not with_entities:
            return [node.to_dict() for node in nodes]

        # Collect entity IDs to fetch
        entity_ids = [node.entity_id for node in nodes if node.entity_id]

        if not entity_ids:
            return [node.to_dict() for node in nodes]

        # Batch fetch entities
        placeholders = ','.join('?' * len(entity_ids))
        cursor.execute(f"""
            SELECT *
            FROM entities
            WHERE entity_id IN ({placeholders})
        """, entity_ids)

        entities_map = {row['entity_id']: Entity(**dict(row)) for row in cursor.fetchall()}

        # Combine nodes with entities
        result = []
        for node in nodes:
            node_dict = node.to_dict()
            if node.entity_id and node.entity_id in entities_map:
                node_dict['entity'] = entities_map[node.entity_id].to_dict()
            result.append(node_dict)

        return result

    def get_entity(self, tree_id: str, node_id: str) -> Optional[Entity]:
        """
        Get entity referenced by a node

        Args:
            tree_id: Tree identifier
            node_id: Node identifier

        Returns:
            Entity object or None if node doesn't reference an entity
        """
        cursor = self.conn.cursor()

        # Get entity ID from node
        cursor.execute("""
            SELECT entity_type, entity_id
            FROM nodes
            WHERE tree_id = ? AND node_id = ?
        """, (tree_id, node_id))

        row = cursor.fetchone()
        if not row or not row['entity_id']:
            return None

        # Fetch entity
        cursor.execute("""
            SELECT *
            FROM entities
            WHERE entity_id = ?
        """, (row['entity_id'],))

        entity_row = cursor.fetchone()
        if entity_row:
            return Entity(**dict(entity_row))
        return None

    def ingest_tree(
        self,
        tree_structure: Dict[str, Any],
        entities: Optional[Dict[str, Dict[str, Any]]] = None,
        meta: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Ingest a tree structure with optional entity references

        Args:
            tree_structure: Nested dictionary representing the tree
                Format: {
                    "type": "object" | "array" | "leaf",
                    "children": {...} or [...],  # for object/array
                    "entity_type": str,  # optional
                    "entity_id": str,    # optional
                    "attrs": {...}       # optional metadata
                }
            entities: Optional mapping of entity_id -> entity_payload
            meta: Optional tree metadata

        Returns:
            tree_id of the ingested tree
        """
        tree_id = str(uuid.uuid4())
        root_node_id = str(uuid.uuid4())
        now = self._timestamp()

        cursor = self.conn.cursor()

        try:
            # Begin transaction
            cursor.execute("BEGIN")

            # Insert tree
            cursor.execute("""
                INSERT INTO trees (tree_id, root_node_id, created_at, updated_at, meta_json)
                VALUES (?, ?, ?, ?, ?)
            """, (tree_id, root_node_id, now, now, json.dumps(meta) if meta else None))

            # Insert entities first
            if entities:
                for entity_id, payload in entities.items():
                    cursor.execute("""
                        INSERT OR IGNORE INTO entities (entity_id, entity_type, payload_json, created_at)
                        VALUES (?, ?, ?, ?)
                    """, (entity_id, payload.get('type', 'unknown'), json.dumps(payload), now))

            # Process tree structure
            def insert_node(
                node_data: Dict[str, Any],
                node_id: str,
                parent_id: Optional[str],
                parent_path: Optional[str],
                parent_depth: int,
                slot: Optional[str]
            ):
                """Recursively insert nodes"""
                depth = parent_depth + 1 if parent_id else 0
                path = f"{parent_path}/{node_id}" if parent_path else f"/r/{node_id}"

                # Determine node type
                node_type_str = node_data.get('type', 'object')
                if node_type_str == 'object':
                    node_type = self.OBJECT
                elif node_type_str == 'array':
                    node_type = self.ARRAY
                else:
                    node_type = self.LEAF

                # Extract optional fields
                entity_type = node_data.get('entity_type')
                entity_id = node_data.get('entity_id')
                attrs = node_data.get('attrs')
                attrs_json = json.dumps(attrs) if attrs else None

                # Insert node
                cursor.execute("""
                    INSERT INTO nodes (
                        tree_id, node_id, parent_id, slot, node_type, depth, path,
                        entity_type, entity_id, attrs_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    tree_id, node_id, parent_id, slot, node_type, depth, path,
                    entity_type, entity_id, attrs_json, now, now
                ))

                # Process children
                children = node_data.get('children')
                if children:
                    if isinstance(children, dict):
                        # Object children
                        for key, child_data in children.items():
                            child_id = str(uuid.uuid4())
                            insert_node(child_data, child_id, node_id, path, depth, key)
                    elif isinstance(children, list):
                        # Array children
                        for idx, child_data in enumerate(children):
                            child_id = str(uuid.uuid4())
                            insert_node(child_data, child_id, node_id, path, depth, str(idx))

            # Insert root and all descendants
            insert_node(tree_structure, root_node_id, None, None, -1, None)

            # Commit transaction
            self.conn.commit()

            return tree_id

        except Exception as e:
            self.conn.rollback()
            raise e

    def close(self):
        """Close database connection"""
        self.conn.close()

    def __enter__(self):
        """Context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()
