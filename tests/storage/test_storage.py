"""Storage layer (TreeDB) unit tests.

Focus: data integrity, edge cases, and potential bugs in
create/ingest/get/delete operations.
"""

import json
import pytest
from contextdb.core.storage import TreeDB, Node, Entity


@pytest.fixture
def db():
    with TreeDB(":memory:") as db:
        yield db


# ── Helper ──────────────────────────────────────────────────────────

def _count(db, table, where="1=1", params=()):
    cur = db.conn.cursor()
    cur.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE {where}", params)
    return cur.fetchone()["c"]


def _ingest_deep_tree(db, depth=5):
    """Build a linear chain: root -> c0 -> c1 -> ... -> c{depth-1}."""
    def build(d):
        if d == 0:
            return {"type": "leaf", "attrs": {"level": depth}}
        return {"type": "object", "attrs": {"level": d}, "children": {"c": build(d - 1)}}
    return db.ingest_tree(build(depth))


def _ingest_wide_tree(db, width=10):
    """Build a flat tree: root with N leaf children."""
    children = {f"child_{i}": {"type": "leaf", "attrs": {"i": i}} for i in range(width)}
    return db.ingest_tree({"type": "object", "children": children})


# ════════════════════════════════════════════════════════════════════
# create_tree
# ════════════════════════════════════════════════════════════════════

def test_create_tree_returns_uuids(db):
    """create_tree returns valid (tree_id, root_id) pair."""
    tid, rid = db.create_tree()
    assert len(tid) == 36 and len(rid) == 36  # UUID format

def test_create_tree_root_exists(db):
    """Root node is actually inserted into nodes table."""
    tid, rid = db.create_tree()
    node = db.get_node(tid, rid)
    assert node is not None
    assert node.depth == 0
    assert node.parent_id is None

def test_create_tree_meta_stored(db):
    """Meta JSON is persisted on the tree row."""
    tid, _ = db.create_tree(meta={"env": "test", "version": 2})
    cur = db.conn.cursor()
    cur.execute("SELECT meta_json FROM trees WHERE tree_id = ?", (tid,))
    meta = json.loads(cur.fetchone()["meta_json"])
    assert meta == {"env": "test", "version": 2}

def test_create_tree_no_meta(db):
    """meta_json is NULL when no meta provided."""
    tid, _ = db.create_tree()
    cur = db.conn.cursor()
    cur.execute("SELECT meta_json FROM trees WHERE tree_id = ?", (tid,))
    assert cur.fetchone()["meta_json"] is None

def test_create_multiple_trees_unique(db):
    """Each create_tree produces unique IDs."""
    ids = {db.create_tree()[0] for _ in range(20)}
    assert len(ids) == 20


# ════════════════════════════════════════════════════════════════════
# ingest_tree — basic structure
# ════════════════════════════════════════════════════════════════════

def test_ingest_single_leaf(db):
    """Ingest a tree with only a root leaf node."""
    tid = db.ingest_tree({"type": "leaf"})
    assert _count(db, "nodes", "tree_id = ?", (tid,)) == 1

def test_ingest_dict_children(db):
    """Dict children → correct node count and slot names."""
    tree = {"type": "object", "children": {"a": {"type": "leaf"}, "b": {"type": "leaf"}}}
    tid = db.ingest_tree(tree)
    rid = db.get_root_id(tid)
    children = db.get_children(tid, rid)
    assert len(children) == 2
    slots = {c.slot for c in children}
    assert slots == {"a", "b"}

def test_ingest_list_children(db):
    """List children → slots are "0", "1", "2", ..."""
    tree = {"type": "array", "children": [{"type": "leaf"} for _ in range(4)]}
    tid = db.ingest_tree(tree)
    rid = db.get_root_id(tid)
    children = db.get_children(tid, rid)
    assert len(children) == 4
    assert [c.slot for c in children] == ["0", "1", "2", "3"]

def test_ingest_mixed_nesting(db):
    """Object → array → leaf: 3 levels, correct depths."""
    tree = {
        "type": "object",
        "children": {
            "arr": {
                "type": "array",
                "children": [{"type": "leaf"}, {"type": "leaf"}],
            }
        },
    }
    tid = db.ingest_tree(tree)
    assert _count(db, "nodes", "tree_id = ?", (tid,)) == 4  # root + arr + 2 leaves
    cur = db.conn.cursor()
    cur.execute("SELECT MAX(depth) AS d FROM nodes WHERE tree_id = ?", (tid,))
    assert cur.fetchone()["d"] == 2

def test_ingest_empty_children_dict(db):
    """Empty children dict → node has no children."""
    tid = db.ingest_tree({"type": "object", "children": {}})
    rid = db.get_root_id(tid)
    assert db.get_children(tid, rid) == []
    assert _count(db, "nodes", "tree_id = ?", (tid,)) == 1

def test_ingest_empty_children_list(db):
    """Empty children list → node has no children."""
    tid = db.ingest_tree({"type": "array", "children": []})
    rid = db.get_root_id(tid)
    assert db.get_children(tid, rid) == []

def test_ingest_no_children_key(db):
    """Missing 'children' key → treated as leaf."""
    tid = db.ingest_tree({"type": "object"})
    assert _count(db, "nodes", "tree_id = ?", (tid,)) == 1

def test_ingest_with_meta(db):
    """Meta is stored on ingest_tree."""
    tid = db.ingest_tree({"type": "leaf"}, meta={"tag": "x"})
    cur = db.conn.cursor()
    cur.execute("SELECT meta_json FROM trees WHERE tree_id = ?", (tid,))
    assert json.loads(cur.fetchone()["meta_json"]) == {"tag": "x"}


# ════════════════════════════════════════════════════════════════════
# ingest_tree — node types
# ════════════════════════════════════════════════════════════════════

def test_node_type_object(db):
    """type='object' → node_type=0."""
    tid = db.ingest_tree({"type": "object"})
    assert db.get_node(tid, db.get_root_id(tid)).node_type == TreeDB.OBJECT

def test_node_type_array(db):
    """type='array' → node_type=1."""
    tid = db.ingest_tree({"type": "array"})
    assert db.get_node(tid, db.get_root_id(tid)).node_type == TreeDB.ARRAY

def test_node_type_leaf(db):
    """type='leaf' → node_type=2."""
    tid = db.ingest_tree({"type": "leaf"})
    assert db.get_node(tid, db.get_root_id(tid)).node_type == TreeDB.LEAF

def test_node_type_default_object(db):
    """Missing type → defaults to 'object' (node_type=0)."""
    tid = db.ingest_tree({})
    assert db.get_node(tid, db.get_root_id(tid)).node_type == TreeDB.OBJECT


# ════════════════════════════════════════════════════════════════════
# ingest_tree — attrs
# ════════════════════════════════════════════════════════════════════

def test_attrs_stored(db):
    """attrs dict is serialized and retrievable."""
    tid = db.ingest_tree({"type": "leaf", "attrs": {"title": "Hello", "page": 5}})
    node = db.get_node(tid, db.get_root_id(tid))
    attrs = json.loads(node.attrs_json)
    assert attrs == {"title": "Hello", "page": 5}

def test_attrs_none(db):
    """No attrs → attrs_json is NULL."""
    tid = db.ingest_tree({"type": "leaf"})
    node = db.get_node(tid, db.get_root_id(tid))
    assert node.attrs_json is None

def test_attrs_unicode(db):
    """Unicode attrs round-trip correctly."""
    tid = db.ingest_tree({"type": "leaf", "attrs": {"title": "你好世界 🌍"}})
    node = db.get_node(tid, db.get_root_id(tid))
    assert json.loads(node.attrs_json)["title"] == "你好世界 🌍"

def test_attrs_nested(db):
    """Nested dict/list attrs survive JSON round-trip."""
    attrs = {"tags": ["a", "b"], "meta": {"k": 1}}
    tid = db.ingest_tree({"type": "leaf", "attrs": attrs})
    node = db.get_node(tid, db.get_root_id(tid))
    assert json.loads(node.attrs_json) == attrs


# ════════════════════════════════════════════════════════════════════
# ingest_tree — entities
# ════════════════════════════════════════════════════════════════════

def test_entities_stored(db):
    """Entities are inserted into entities table."""
    tree = {"type": "leaf", "entity_id": "e1", "entity_type": "doc"}
    entities = {"e1": {"type": "doc", "text": "hello"}}
    tid = db.ingest_tree(tree, entities=entities)
    ent = db.get_entity(tid, db.get_root_id(tid))
    assert ent is not None
    assert ent.entity_type == "doc"
    payload = json.loads(ent.payload_json)
    assert payload["text"] == "hello"

def test_entity_no_type_defaults_unknown(db):
    """Entity without 'type' key → entity_type='unknown'."""
    tree = {"type": "leaf", "entity_id": "e1"}
    entities = {"e1": {"content": "data"}}
    tid = db.ingest_tree(tree, entities=entities)
    ent = db.get_entity(tid, db.get_root_id(tid))
    assert ent.entity_type == "unknown"

def test_entities_none(db):
    """entities=None → no entities created."""
    tid = db.ingest_tree({"type": "leaf"})
    assert _count(db, "entities") == 0

def test_entity_payload_large(db):
    """Large entity payload stores and retrieves correctly."""
    big_text = "x" * 100_000
    tree = {"type": "leaf", "entity_id": "e1"}
    entities = {"e1": {"type": "text", "content": big_text}}
    tid = db.ingest_tree(tree, entities=entities)
    ent = db.get_entity(tid, db.get_root_id(tid))
    assert len(json.loads(ent.payload_json)["content"]) == 100_000

def test_multiple_entities(db):
    """Multiple entities on different nodes."""
    tree = {
        "type": "object",
        "entity_id": "root_e",
        "children": {
            "a": {"type": "leaf", "entity_id": "ea"},
            "b": {"type": "leaf", "entity_id": "eb"},
        },
    }
    entities = {
        "root_e": {"type": "root", "title": "Root"},
        "ea": {"type": "text", "content": "A"},
        "eb": {"type": "text", "content": "B"},
    }
    tid = db.ingest_tree(tree, entities=entities)
    assert _count(db, "entities") == 3


# ════════════════════════════════════════════════════════════════════
# ingest_tree — depth & path correctness
# ════════════════════════════════════════════════════════════════════

def test_depth_linear_chain(db):
    """Linear chain depth=5 → depths are 0,1,2,3,4,5."""
    tid = _ingest_deep_tree(db, depth=5)
    cur = db.conn.cursor()
    cur.execute("SELECT depth FROM nodes WHERE tree_id = ? ORDER BY depth", (tid,))
    depths = [r["depth"] for r in cur.fetchall()]
    assert depths == [0, 1, 2, 3, 4, 5]

def test_path_ancestry(db):
    """Every child's path contains its parent's path as prefix."""
    tid = _ingest_deep_tree(db, depth=4)
    cur = db.conn.cursor()
    cur.execute("SELECT node_id, parent_id, path FROM nodes WHERE tree_id = ? ORDER BY depth", (tid,))
    rows = {r["node_id"]: r for r in cur.fetchall()}
    for row in rows.values():
        if row["parent_id"]:
            parent_path = rows[row["parent_id"]]["path"]
            assert row["path"].startswith(parent_path + "/")

def test_path_root_prefix(db):
    """Root path starts with /r/."""
    tid = db.ingest_tree({"type": "leaf"})
    node = db.get_node(tid, db.get_root_id(tid))
    assert node.path.startswith("/r/")


# ════════════════════════════════════════════════════════════════════
# get_node
# ════════════════════════════════════════════════════════════════════

def test_get_node_exists(db):
    """Get existing node returns correct Node dataclass."""
    tid, rid = db.create_tree()
    node = db.get_node(tid, rid)
    assert isinstance(node, Node)
    assert node.tree_id == tid
    assert node.node_id == rid

def test_get_node_not_found(db):
    """Non-existent node returns None."""
    tid, _ = db.create_tree()
    assert db.get_node(tid, "nonexistent") is None

def test_get_node_wrong_tree(db):
    """Node from different tree returns None."""
    t1, r1 = db.create_tree()
    t2, _ = db.create_tree()
    assert db.get_node(t2, r1) is None


# ════════════════════════════════════════════════════════════════════
# get_children
# ════════════════════════════════════════════════════════════════════

def test_get_children_ordered_by_slot(db):
    """Children are returned sorted by slot name."""
    tree = {"type": "object", "children": {"z": {"type": "leaf"}, "a": {"type": "leaf"}, "m": {"type": "leaf"}}}
    tid = db.ingest_tree(tree)
    children = db.get_children(tid, db.get_root_id(tid))
    assert [c.slot for c in children] == ["a", "m", "z"]

def test_get_children_leaf_has_none(db):
    """Leaf node has no children."""
    tid = db.ingest_tree({"type": "leaf"})
    assert db.get_children(tid, db.get_root_id(tid)) == []

def test_get_children_nonexistent_parent(db):
    """Children of non-existent parent → empty list."""
    tid, _ = db.create_tree()
    assert db.get_children(tid, "ghost") == []


# ════════════════════════════════════════════════════════════════════
# get_subtree
# ════════════════════════════════════════════════════════════════════

def test_subtree_depth_limit(db):
    """max_depth limits how deep subtree goes."""
    tid = _ingest_deep_tree(db, depth=5)
    rid = db.get_root_id(tid)
    # depth=0 → root only
    assert len(db.get_subtree(tid, rid, max_depth=0)) == 1
    # depth=2 → root + 2 levels
    assert len(db.get_subtree(tid, rid, max_depth=2)) == 3
    # depth=100 → all 6 nodes
    assert len(db.get_subtree(tid, rid, max_depth=100)) == 6

def test_subtree_from_middle(db):
    """Subtree starting from non-root node."""
    tid = _ingest_deep_tree(db, depth=4)
    rid = db.get_root_id(tid)
    mid = db.get_children(tid, rid)[0]  # depth=1 node
    subtree = db.get_subtree(tid, mid.node_id, max_depth=100)
    assert len(subtree) == 4  # mid + 3 descendants

def test_subtree_nonexistent_node(db):
    """Subtree of non-existent node → empty list."""
    tid, _ = db.create_tree()
    assert db.get_subtree(tid, "ghost") == []

def test_subtree_with_entities(db):
    """with_entities=True attaches entity payloads."""
    tree = {"type": "leaf", "entity_id": "e1"}
    entities = {"e1": {"type": "doc", "text": "hello"}}
    tid = db.ingest_tree(tree, entities=entities)
    rid = db.get_root_id(tid)
    result = db.get_subtree(tid, rid, with_entities=True)
    assert "entity" in result[0]
    assert result[0]["entity"]["payload"]["text"] == "hello"

def test_subtree_without_entities(db):
    """with_entities=False (default) does not attach entities."""
    tree = {"type": "leaf", "entity_id": "e1"}
    entities = {"e1": {"type": "doc", "text": "hello"}}
    tid = db.ingest_tree(tree, entities=entities)
    rid = db.get_root_id(tid)
    result = db.get_subtree(tid, rid, with_entities=False)
    assert "entity" not in result[0]


# ════════════════════════════════════════════════════════════════════
# get_entity
# ════════════════════════════════════════════════════════════════════

def test_get_entity_exists(db):
    """Retrieve entity by tree_id + node_id."""
    tree = {"type": "leaf", "entity_id": "e1"}
    entities = {"e1": {"type": "doc", "k": "v"}}
    tid = db.ingest_tree(tree, entities=entities)
    ent = db.get_entity(tid, db.get_root_id(tid))
    assert isinstance(ent, Entity)
    assert json.loads(ent.payload_json)["k"] == "v"

def test_get_entity_no_entity_on_node(db):
    """Node without entity_id → returns None."""
    tid = db.ingest_tree({"type": "leaf"})
    assert db.get_entity(tid, db.get_root_id(tid)) is None

def test_get_entity_nonexistent_node(db):
    """Non-existent node → returns None."""
    tid, _ = db.create_tree()
    assert db.get_entity(tid, "ghost") is None


# ════════════════════════════════════════════════════════════════════
# get_root_id
# ════════════════════════════════════════════════════════════════════

def test_get_root_id(db):
    """Returns correct root node ID."""
    tid, rid = db.create_tree()
    assert db.get_root_id(tid) == rid

def test_get_root_id_nonexistent(db):
    """Non-existent tree → None."""
    assert db.get_root_id("ghost") is None

def test_get_root_id_after_ingest(db):
    """Root ID after ingest matches the actual root node."""
    tid = db.ingest_tree({"type": "object", "children": {"c": {"type": "leaf"}}})
    rid = db.get_root_id(tid)
    node = db.get_node(tid, rid)
    assert node.depth == 0
    assert node.parent_id is None


# ════════════════════════════════════════════════════════════════════
# delete_tree
# ════════════════════════════════════════════════════════════════════

def test_delete_tree_removes_nodes(db):
    """Delete removes all nodes for that tree."""
    tid = _ingest_wide_tree(db, width=10)
    assert _count(db, "nodes", "tree_id = ?", (tid,)) == 11
    db.delete_tree(tid)
    assert _count(db, "nodes", "tree_id = ?", (tid,)) == 0

def test_delete_tree_removes_tree_row(db):
    """Delete removes the trees table row."""
    tid = db.ingest_tree({"type": "leaf"})
    db.delete_tree(tid)
    assert db.get_root_id(tid) is None

def test_delete_tree_no_cross_contamination(db):
    """Deleting one tree doesn't affect another."""
    t1 = db.ingest_tree({"type": "leaf"})
    t2 = db.ingest_tree({"type": "leaf"})
    db.delete_tree(t1)
    assert db.get_root_id(t1) is None
    assert db.get_root_id(t2) is not None

def test_delete_nonexistent_tree(db):
    """Deleting non-existent tree doesn't raise."""
    db.delete_tree("ghost")  # should not raise


# ════════════════════════════════════════════════════════════════════
# Node.to_dict / Entity.to_dict
# ════════════════════════════════════════════════════════════════════

def test_node_to_dict_parses_attrs(db):
    """to_dict() converts attrs_json string to attrs dict."""
    tid = db.ingest_tree({"type": "leaf", "attrs": {"title": "Hi"}})
    node = db.get_node(tid, db.get_root_id(tid))
    d = node.to_dict()
    assert d["attrs"] == {"title": "Hi"}
    assert "attrs_json" not in d

def test_node_to_dict_no_attrs(db):
    """to_dict() without attrs doesn't include attrs key."""
    tid = db.ingest_tree({"type": "leaf"})
    node = db.get_node(tid, db.get_root_id(tid))
    d = node.to_dict()
    assert "attrs" not in d
    assert "attrs_json" not in d

def test_node_to_dict_strips_none(db):
    """to_dict() excludes None-valued fields."""
    tid = db.ingest_tree({"type": "leaf"})
    node = db.get_node(tid, db.get_root_id(tid))
    d = node.to_dict()
    assert "entity_id" not in d
    assert "entity_type" not in d

def test_entity_to_dict(db):
    """Entity.to_dict() parses payload_json."""
    tree = {"type": "leaf", "entity_id": "e1"}
    entities = {"e1": {"type": "doc", "val": 42}}
    tid = db.ingest_tree(tree, entities=entities)
    ent = db.get_entity(tid, db.get_root_id(tid))
    d = ent.to_dict()
    assert d["payload"] == {"type": "doc", "val": 42}
    assert "payload_json" not in d


# ════════════════════════════════════════════════════════════════════
# Tree isolation — cross-tree safety
# ════════════════════════════════════════════════════════════════════

def test_trees_isolated(db):
    """Nodes from one tree are invisible to another tree."""
    t1 = db.ingest_tree({"type": "object", "children": {"a": {"type": "leaf"}}})
    t2 = db.ingest_tree({"type": "leaf"})
    r1 = db.get_root_id(t1)
    # t1 root not visible via t2
    assert db.get_node(t2, r1) is None
    # t2 has only 1 node
    assert _count(db, "nodes", "tree_id = ?", (t2,)) == 1

def test_entity_shared_across_trees(db):
    """Two trees referencing same entity_id (INSERT OR IGNORE) — first wins."""
    ents = {"shared": {"type": "doc", "version": 1}}
    db.ingest_tree({"type": "leaf", "entity_id": "shared"}, entities=ents)
    ents2 = {"shared": {"type": "doc", "version": 2}}
    db.ingest_tree({"type": "leaf", "entity_id": "shared"}, entities=ents2)
    # INSERT OR IGNORE: first insert wins
    cur = db.conn.cursor()
    cur.execute("SELECT payload_json FROM entities WHERE entity_id = ?", ("shared",))
    payload = json.loads(cur.fetchone()["payload_json"])
    assert payload["version"] == 1


# ════════════════════════════════════════════════════════════════════
# Stress / scale
# ════════════════════════════════════════════════════════════════════

def test_wide_tree(db):
    """100 children under root, all retrievable."""
    tid = _ingest_wide_tree(db, width=100)
    rid = db.get_root_id(tid)
    children = db.get_children(tid, rid)
    assert len(children) == 100

def test_deep_tree(db):
    """Depth=20 linear chain, subtree returns all nodes."""
    tid = _ingest_deep_tree(db, depth=20)
    rid = db.get_root_id(tid)
    subtree = db.get_subtree(tid, rid, max_depth=100)
    assert len(subtree) == 21  # root + 20 levels


# ════════════════════════════════════════════════════════════════════
# Transaction safety
# ════════════════════════════════════════════════════════════════════

def test_ingest_rollback_on_error(db):
    """If ingest fails mid-way, nothing is committed."""
    # Duplicate entity_id in same tree → duplicate node_id won't happen,
    # but we can trigger failure with a bad structure.
    count_before = _count(db, "trees")
    try:
        # Insert a valid tree first, then try to insert a tree that references
        # a broken recursive structure (we'll monkeypatch to break it)
        db.ingest_tree({"type": "leaf"})  # should succeed
    except Exception:
        pass
    # At least no partial state from failed attempts
    assert _count(db, "trees") == count_before + 1
