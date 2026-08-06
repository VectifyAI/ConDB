import json
import re

from contextdb.core.storage import TreeDB
from contextdb.retriever.algorithm.beam_retriever import BeamRetriever


class TwoTurnLLM:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, system="", tools=None, cache_key=None):
        self.calls += 1
        candidate_ids = re.findall(r"- id: ([0-9a-f-]+)", messages[0]["content"])
        ranked_ids = candidate_ids if self.calls == 1 else candidate_ids[:1]
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank-call",
                    "name": "rank",
                    "input": {"ranked_ids": ranked_ids, "done": self.calls >= 2},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }


class ImmediateDoneLLM:
    def chat(self, messages, system="", tools=None, cache_key=None):
        candidate_ids = re.findall(r"- id: ([0-9a-f-]+)", messages[0]["content"])
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank-call",
                    "name": "rank",
                    "input": {"ranked_ids": candidate_ids[:1], "done": True},
                }
            ]
        }


class NeverDoneLLM:
    def chat(self, messages, system="", tools=None, cache_key=None):
        candidate_ids = re.findall(r"- id: ([0-9a-f-]+)", messages[0]["content"])
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank-call",
                    "name": "rank",
                    "input": {"ranked_ids": candidate_ids[:1], "done": False},
                }
            ]
        }


class CountingStorage:
    def __init__(self, storage):
        self.storage = storage
        self.get_children_calls = 0
        self.get_entity_calls = 0
        self.children_batch_sizes = []
        self.entity_batch_sizes = []

    def __getattr__(self, name):
        return getattr(self.storage, name)

    def get_children(self, tree_id, node_id):
        self.get_children_calls += 1
        return self.storage.get_children(tree_id, node_id)

    def get_children_many(self, tree_id, node_ids):
        self.children_batch_sizes.append(len(node_ids))
        return self.storage.get_children_many(tree_id, node_ids)

    def get_entity(self, tree_id, node_id):
        self.get_entity_calls += 1
        return self.storage.get_entity(tree_id, node_id)

    def get_entities(self, tree_id, node_ids):
        self.entity_batch_sizes.append(len(node_ids))
        return self.storage.get_entities(tree_id, node_ids)


class ScalarOnlyStorage(CountingStorage):
    def __getattribute__(self, name):
        if name in {"get_children_many", "get_entities"}:
            raise AttributeError(name)
        return super().__getattribute__(name)

    def __getattr__(self, name):
        if name in {"get_children_many", "get_entities"}:
            raise AttributeError(name)
        return super().__getattr__(name)


class SparseBatchStorage(CountingStorage):
    """Simulate an optional batch backend that returns only entity hits."""

    def get_entities(self, tree_id, node_ids):
        self.entity_batch_sizes.append(len(node_ids))
        return {
            node_id: entity
            for node_id in node_ids
            if (entity := self.storage.get_entity(tree_id, node_id)) is not None
        }


def _ingest_two_branch_tree(db):
    tree = {
        "type": "object",
        "children": {
            "branch-a": {
                "type": "object",
                "entity_id": "branch-a",
                "children": {
                    "a1": {"type": "leaf", "entity_id": "a1"},
                    "a2": {"type": "leaf", "entity_id": "a2"},
                },
            },
            "branch-b": {
                "type": "object",
                "entity_id": "branch-b",
                "children": {
                    "b1": {"type": "leaf", "entity_id": "b1"},
                    "b2": {"type": "leaf", "entity_id": "b2"},
                },
            },
        },
    }
    entities = {
        entity_id: {"type": "text", "title": entity_id, "text": f"content {entity_id}"}
        for entity_id in ("branch-a", "branch-b", "a1", "a2", "b1", "b2")
    }
    return db.ingest_tree(tree, entities=entities)


def test_beam_coalesces_frontier_children_and_candidate_entities():
    with TreeDB(":memory:") as db:
        tree_id = _ingest_two_branch_tree(db)
        storage = CountingStorage(db)

        result = BeamRetriever(storage, TwoTurnLLM()).retrieve(
            tree_id,
            "find a leaf",
            beam_size=2,
            max_turns=2,
        )

        assert len(result.nodes) == 1
        assert result.contents[0]["content"]["text"].startswith("content ")
        assert result.turns == 2
        assert result.trace[0]["top_candidate_ids"] == 0
        assert db.get_node(tree_id, result.nodes[0]).node_type == TreeDB.LEAF
        assert storage.children_batch_sizes == [2]
        assert storage.entity_batch_sizes == [2, 4]
        assert storage.get_children_calls == 1
        assert storage.get_entity_calls == 1


def test_beam_falls_back_for_scalar_only_storage():
    with TreeDB(":memory:") as db:
        tree_id = _ingest_two_branch_tree(db)
        storage = ScalarOnlyStorage(db)

        result = BeamRetriever(storage, TwoTurnLLM()).retrieve(
            tree_id,
            "find a leaf",
            beam_size=2,
            max_turns=2,
        )

        assert len(result.nodes) == 1
        assert any(
            json.loads(entity.payload_json)["text"].startswith("content ")
            for node_id in result.nodes
            if (entity := db.get_entity(tree_id, node_id)) is not None
        )
        assert storage.get_children_calls == 3
        assert storage.get_entity_calls == 7


def test_beam_final_contents_refresh_when_retriever_is_reused():
    with TreeDB(":memory:") as db:
        tree_id = _ingest_two_branch_tree(db)
        retriever = BeamRetriever(db, TwoTurnLLM())

        first = retriever.retrieve(
            tree_id,
            "find a leaf",
            beam_size=2,
            max_turns=2,
        )
        selected_id = next(item["node_id"] for item in first.contents)
        cursor = db.conn.cursor()
        cursor.execute(
            """
            UPDATE entities
            SET payload_json = ?
            WHERE entity_id = (
                SELECT entity_id FROM nodes
                WHERE tree_id = ? AND node_id = ?
            )
            """,
            (json.dumps({"type": "text", "title": "updated", "text": "content updated"}), tree_id, selected_id),
        )
        db.conn.commit()

        retriever.llm = TwoTurnLLM()
        second = retriever.retrieve(
            tree_id,
            "find a leaf",
            beam_size=2,
            max_turns=2,
        )

        assert next(item for item in second.contents if item["node_id"] == selected_id)["content"]["text"] == (
            "content updated"
        )


def test_beam_accepts_sparse_batch_entity_mapping():
    with TreeDB(":memory:") as db:
        tree_id = _ingest_two_branch_tree(db)
        storage = SparseBatchStorage(db)

        result = BeamRetriever(storage, TwoTurnLLM()).retrieve(
            tree_id,
            "find a leaf",
            beam_size=2,
            max_turns=2,
        )

        assert len(result.nodes) == 1
        assert len(result.contents) == 1
        assert result.contents[0]["content"]["text"].startswith("content ")


def test_done_true_can_return_a_specific_internal_node():
    with TreeDB(":memory:") as db:
        tree_id = _ingest_two_branch_tree(db)

        result = BeamRetriever(db, ImmediateDoneLLM()).retrieve(
            tree_id,
            "summarize one branch",
            beam_size=2,
            max_turns=2,
        )

        assert len(result.nodes) == 1
        assert db.get_node(tree_id, result.nodes[0]).node_type == TreeDB.OBJECT
        assert result.contents[0]["content"]["title"].startswith("branch-")
        assert result.turns == 1


def test_leaf_result_does_not_require_done_true():
    with TreeDB(":memory:") as db:
        tree_id = _ingest_two_branch_tree(db)

        result = BeamRetriever(db, NeverDoneLLM()).retrieve(
            tree_id,
            "find a leaf",
            beam_size=1,
            max_turns=2,
        )

        assert len(result.nodes) == 1
        assert db.get_node(tree_id, result.nodes[0]).node_type == TreeDB.LEAF
        assert len(result.contents) == 1
        assert result.turns == 2
