import json

import pytest

from contextdb.core.storage import Entity, TreeDB


@pytest.fixture
def populated_db():
    tree = {
        "type": "object",
        "children": {
            "right": {
                "type": "object",
                "children": {
                    "z": {"type": "leaf", "entity_id": "right-z"},
                    "a": {"type": "leaf"},
                },
            },
            "left": {
                "type": "object",
                "children": {
                    "z": {"type": "leaf", "entity_id": "left-z"},
                    "a": {"type": "leaf", "entity_id": "left-a"},
                },
            },
        },
    }
    entities = {
        "right-z": {"type": "text", "text": "right z"},
        "left-z": {"type": "text", "text": "left z"},
        "left-a": {"type": "text", "text": "left a"},
    }
    with TreeDB(":memory:") as db:
        tree_id = db.ingest_tree(tree, entities=entities)
        root_id = db.get_root_id(tree_id)
        parents = {node.slot: node for node in db.get_children(tree_id, root_id)}
        yield db, tree_id, root_id, parents


def test_get_children_many_preserves_parent_keys_and_slot_order(populated_db):
    db, tree_id, _, parents = populated_db
    right_id = parents["right"].node_id
    left_id = parents["left"].node_id

    result = db.get_children_many(tree_id, [right_id, "missing", left_id, right_id])

    assert list(result) == [right_id, "missing", left_id]
    assert [node.slot for node in result[right_id]] == ["a", "z"]
    assert result["missing"] == []
    assert [node.slot for node in result[left_id]] == ["a", "z"]
    assert db.get_children_many(tree_id, []) == {}


def test_get_children_many_chunks_large_request(populated_db):
    db, tree_id, _, parents = populated_db
    right_id = parents["right"].node_id
    requested = [right_id, *[f"missing-{i}" for i in range(500)]]

    result = db.get_children_many(tree_id, requested)

    assert len(result) == 501
    assert [node.slot for node in result[right_id]] == ["a", "z"]
    assert all(result[node_id] == [] for node_id in requested[1:])


def test_get_children_many_is_tree_scoped(populated_db):
    db, tree_id, _, parents = populated_db
    parent_id = parents["right"].node_id
    other_tree_id, _ = db.create_tree()

    assert db.get_children_many(other_tree_id, [parent_id])[parent_id] == []


def test_get_entities_returns_values_by_node_not_entity_id(populated_db):
    db, tree_id, _, parents = populated_db
    left_children = {node.slot: node for node in db.get_children(tree_id, parents["left"].node_id)}
    right_children = {node.slot: node for node in db.get_children(tree_id, parents["right"].node_id)}
    requested = [
        left_children["z"].node_id,
        left_children["a"].node_id,
        right_children["a"].node_id,
        "missing",
        left_children["z"].node_id,
    ]

    result = db.get_entities(tree_id, requested)

    assert list(result) == requested[:-1]
    assert isinstance(result[left_children["z"].node_id], Entity)
    assert json.loads(result[left_children["z"].node_id].payload_json)["text"] == "left z"
    assert json.loads(result[left_children["a"].node_id].payload_json)["text"] == "left a"
    assert result[right_children["a"].node_id] is None
    assert result["missing"] is None
    assert db.get_entities(tree_id, []) == {}


def test_get_entities_chunks_large_request(populated_db):
    db, tree_id, _, parents = populated_db
    node_id = db.get_children(tree_id, parents["left"].node_id)[0].node_id
    requested = [node_id, *[f"missing-{i}" for i in range(500)]]

    result = db.get_entities(tree_id, requested)

    assert len(result) == 501
    assert result[node_id] is not None
    assert all(result[missing_id] is None for missing_id in requested[1:])


def test_get_entities_is_tree_scoped(populated_db):
    db, tree_id, _, parents = populated_db
    node_id = db.get_children(tree_id, parents["left"].node_id)[0].node_id
    other_tree_id, _ = db.create_tree()

    assert db.get_entities(other_tree_id, [node_id])[node_id] is None
