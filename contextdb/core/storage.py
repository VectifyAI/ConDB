import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from contextdb.logger import get_logger

log = get_logger(__name__)


@dataclass
class Node:
    tree_id: str
    node_id: str
    parent_id: Optional[str]
    slot: Optional[str]
    node_type: int
    depth: int
    path: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    attrs_json: Optional[str] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        result = {k: v for k, v in asdict(self).items() if v is not None}
        if "attrs_json" in result:
            if result["attrs_json"]:
                result["attrs"] = json.loads(result["attrs_json"])
            del result["attrs_json"]
        return result


@dataclass
class Entity:
    entity_id: str
    entity_type: str
    payload_json: str
    created_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "payload": json.loads(self.payload_json),
            "created_at": self.created_at,
        }


@dataclass
class Tree:
    tree_id: str
    root_node_id: str
    created_at: int
    updated_at: int
    meta_json: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "tree_id": self.tree_id,
            "root_node_id": self.root_node_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.meta_json:
            result["meta"] = json.loads(self.meta_json)
        return result


@runtime_checkable
class StorageProtocol(Protocol):
    def create_tree(self, meta: Optional[dict[str, Any]] = None) -> tuple[str, str]: ...
    def get_node(self, tree_id: str, node_id: str) -> Optional[Node]: ...
    def get_children(self, tree_id: str, node_id: str) -> list[Node]: ...
    def get_subtree(
        self, tree_id: str, node_id: str, max_depth: int = 100, with_entities: bool = False
    ) -> list[dict[str, Any]]: ...
    def get_entity(self, tree_id: str, node_id: str) -> Optional[Entity]: ...
    def get_root_id(self, tree_id: str) -> Optional[str]: ...
    def ingest_tree(
        self,
        tree_structure: dict[str, Any],
        entities: Optional[dict[str, dict[str, Any]]] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> str: ...
    def close(self) -> None: ...


class TreeDB:
    OBJECT = 0
    ARRAY = 1
    LEAF = 2

    def __init__(self, db_path: str = "treedb.sqlite"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        log.info(f"TreeDB opened: {db_path}")

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA temp_store = MEMORY")
        cursor.execute("PRAGMA foreign_keys = ON")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trees (
                tree_id       TEXT PRIMARY KEY,
                root_node_id  TEXT NOT NULL,
                created_at    INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL,
                meta_json     TEXT
            )
        """)

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

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                entity_id    TEXT PRIMARY KEY,
                entity_type  TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at   INTEGER NOT NULL
            )
        """)

        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS nodes_child_unique ON nodes(tree_id, parent_id, slot)")
        cursor.execute("CREATE INDEX IF NOT EXISTS nodes_path_idx ON nodes(tree_id, path)")
        cursor.execute("CREATE INDEX IF NOT EXISTS nodes_parent_idx ON nodes(tree_id, parent_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS nodes_entity_idx ON nodes(tree_id, entity_type, entity_id)")
        self.conn.commit()

    def _ts(self) -> int:
        return int(time.time() * 1000)

    def create_tree(self, meta: Optional[dict[str, Any]] = None) -> tuple[str, str]:
        tree_id = str(uuid.uuid4())
        root_id = str(uuid.uuid4())
        now = self._ts()

        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO trees (tree_id, root_node_id, created_at, updated_at, meta_json) VALUES (?, ?, ?, ?, ?)",
            (tree_id, root_id, now, now, json.dumps(meta) if meta else None),
        )
        cursor.execute(
            "INSERT INTO nodes (tree_id, node_id, parent_id, slot, node_type, depth, path, created_at, updated_at) VALUES (?, ?, NULL, NULL, ?, 0, ?, ?, ?)",
            (tree_id, root_id, self.OBJECT, f"/r/{root_id}", now, now),
        )
        self.conn.commit()
        log.debug(f"create_tree: {tree_id[:8]}")
        return tree_id, root_id

    def get_node(self, tree_id: str, node_id: str) -> Optional[Node]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM nodes WHERE tree_id = ? AND node_id = ?", (tree_id, node_id))
        row = cursor.fetchone()
        return Node(**dict(row)) if row else None

    def get_children(self, tree_id: str, node_id: str) -> list[Node]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM nodes WHERE tree_id = ? AND parent_id = ? ORDER BY slot", (tree_id, node_id))
        return [Node(**dict(row)) for row in cursor.fetchall()]

    def get_subtree(
        self, tree_id: str, node_id: str, max_depth: int = 100, with_entities: bool = False
    ) -> list[dict[str, Any]]:
        log.debug(f"get_subtree: {node_id[:8]} depth={max_depth}")
        cursor = self.conn.cursor()
        cursor.execute("SELECT path, depth FROM nodes WHERE tree_id = ? AND node_id = ?", (tree_id, node_id))
        root_row = cursor.fetchone()
        if not root_row:
            return []

        root_path, root_depth = root_row["path"], root_row["depth"]
        cursor.execute(
            """
            SELECT * FROM nodes WHERE tree_id = ? AND node_id = ?
            UNION ALL
            SELECT * FROM nodes WHERE tree_id = ? AND path LIKE ? || '/%%' AND depth <= ?
            ORDER BY path
        """,
            (tree_id, node_id, tree_id, root_path, root_depth + max_depth),
        )

        nodes = [Node(**dict(row)) for row in cursor.fetchall()]
        if not with_entities:
            return [n.to_dict() for n in nodes]

        entity_ids = [n.entity_id for n in nodes if n.entity_id]
        if not entity_ids:
            return [n.to_dict() for n in nodes]

        placeholders = ",".join("?" * len(entity_ids))
        cursor.execute(f"SELECT * FROM entities WHERE entity_id IN ({placeholders})", entity_ids)
        ent_map = {row["entity_id"]: Entity(**dict(row)) for row in cursor.fetchall()}

        result = []
        for n in nodes:
            d = n.to_dict()
            if n.entity_id and n.entity_id in ent_map:
                d["entity"] = ent_map[n.entity_id].to_dict()
            result.append(d)
        return result

    def get_entity(self, tree_id: str, node_id: str) -> Optional[Entity]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT entity_id FROM nodes WHERE tree_id = ? AND node_id = ?", (tree_id, node_id))
        row = cursor.fetchone()
        if not row or not row["entity_id"]:
            return None
        cursor.execute("SELECT * FROM entities WHERE entity_id = ?", (row["entity_id"],))
        ent_row = cursor.fetchone()
        return Entity(**dict(ent_row)) if ent_row else None

    def get_root_id(self, tree_id: str) -> Optional[str]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT root_node_id FROM trees WHERE tree_id = ?", (tree_id,))
        row = cursor.fetchone()
        return row["root_node_id"] if row else None

    def ingest_tree(
        self,
        tree_structure: dict[str, Any],
        entities: Optional[dict[str, dict[str, Any]]] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> str:
        tree_id = str(uuid.uuid4())
        root_id = str(uuid.uuid4())
        now = self._ts()
        cursor = self.conn.cursor()

        try:
            cursor.execute("BEGIN")
            cursor.execute(
                "INSERT INTO trees (tree_id, root_node_id, created_at, updated_at, meta_json) VALUES (?, ?, ?, ?, ?)",
                (tree_id, root_id, now, now, json.dumps(meta) if meta else None),
            )

            if entities:
                cursor.executemany(
                    "INSERT OR IGNORE INTO entities (entity_id, entity_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                    [(eid, p.get("type", "unknown"), json.dumps(p), now) for eid, p in entities.items()],
                )

            def insert(
                data: dict, nid: str, pid: Optional[str], ppath: Optional[str], pdepth: int, slot: Optional[str]
            ):
                depth = pdepth + 1 if pid else 0
                path = f"{ppath}/{nid}" if ppath else f"/r/{nid}"
                ntype_str = data.get("type", "object")
                ntype = self.OBJECT if ntype_str == "object" else (self.ARRAY if ntype_str == "array" else self.LEAF)
                attrs = data.get("attrs")

                cursor.execute(
                    """
                    INSERT INTO nodes (tree_id, node_id, parent_id, slot, node_type, depth, path, entity_type, entity_id, attrs_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        tree_id,
                        nid,
                        pid,
                        slot,
                        ntype,
                        depth,
                        path,
                        data.get("entity_type"),
                        data.get("entity_id"),
                        json.dumps(attrs) if attrs else None,
                        now,
                        now,
                    ),
                )

                children = data.get("children")
                if children:
                    if isinstance(children, dict):
                        for k, child in children.items():
                            insert(child, str(uuid.uuid4()), nid, path, depth, k)
                    elif isinstance(children, list):
                        for i, child in enumerate(children):
                            insert(child, str(uuid.uuid4()), nid, path, depth, str(i))

            insert(tree_structure, root_id, None, None, -1, None)
            self.conn.commit()
            log.info(f"ingest_tree: {tree_id[:8]}")
            return tree_id
        except Exception as e:
            log.error(f"ingest_tree failed: {e}")
            self.conn.rollback()
            raise e

    def delete_tree(self, tree_id: str):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM nodes WHERE tree_id = ?", (tree_id,))
        cursor.execute("DELETE FROM trees WHERE tree_id = ?", (tree_id,))
        self.conn.commit()
        log.info(f"delete_tree: {tree_id[:8]}")

    def close(self):
        self.conn.close()
        log.debug(f"TreeDB closed: {self.db_path}")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
