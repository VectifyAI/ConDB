import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextdb import ContextTree
from contextdb.core.storage import TreeDB


def _make_pageindex(doc_name: str, title: str, node_id: str, text: str):
    return {
        "doc_name": doc_name,
        "doc_description": "",
        "structure": [
            {
                "title": title,
                "node_id": node_id,
                "summary": "",
                "text": text,
                "start_index": 1,
                "end_index": 1,
                "nodes": []
            }
        ]
    }


def _entity_payload_by_slot(storage: TreeDB, tree_id: str, slot: str):
    cursor = storage.conn.cursor()
    cursor.execute(
        "SELECT node_id, entity_id FROM nodes WHERE tree_id = ? AND slot = ?",
        (tree_id, slot),
    )
    row = cursor.fetchone()
    assert row is not None
    entity = storage.get_entity(tree_id, row["node_id"])
    assert entity is not None
    return row["entity_id"], json.loads(entity.payload_json)


def test_pageindex_cross_document_entity_isolation():
    storage = TreeDB(":memory:")
    ct = ContextTree(storage=storage)
    try:
        doc1 = _make_pageindex("Doc A", "Section A", "0000", "alpha")
        doc2 = _make_pageindex("Doc B", "Section B", "0000", "beta")

        tree1 = ct.index_pageindex(doc1)
        tree2 = ct.index_pageindex(doc2)

        eid1, payload1 = _entity_payload_by_slot(storage, tree1, "0000")
        eid2, payload2 = _entity_payload_by_slot(storage, tree2, "0000")

        assert eid1 != eid2
        assert payload1.get("text") == "alpha"
        assert payload2.get("text") == "beta"

        root1 = storage.get_root_id(tree1)
        root2 = storage.get_root_id(tree2)
        ent1 = storage.get_entity(tree1, root1)
        ent2 = storage.get_entity(tree2, root2)
        assert ent1 is not None and ent2 is not None
        assert ent1.entity_id != ent2.entity_id
        assert json.loads(ent1.payload_json).get("title") == "Doc A"
        assert json.loads(ent2.payload_json).get("title") == "Doc B"
    finally:
        ct.close()


def test_generic_cross_document_entity_isolation():
    storage = TreeDB(":memory:")
    ct = ContextTree(storage=storage)
    try:
        tree1 = ct.index_generic({"content": "alpha"})
        tree2 = ct.index_generic({"content": "beta"})

        root1 = storage.get_root_id(tree1)
        root2 = storage.get_root_id(tree2)
        ent1 = storage.get_entity(tree1, root1)
        ent2 = storage.get_entity(tree2, root2)

        assert ent1 is not None and ent2 is not None
        assert ent1.entity_id != ent2.entity_id
        assert json.loads(ent1.payload_json).get("content") == "alpha"
        assert json.loads(ent2.payload_json).get("content") == "beta"
    finally:
        ct.close()
