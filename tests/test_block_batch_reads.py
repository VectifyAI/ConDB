import time
from types import SimpleNamespace

from contextdb.core.storage import TreeDB
from contextdb.retriever.algorithm.block_retriever import BlockRetriever
from contextdb.retriever.algorithm.block_types import BlockResult


class DummyLLM:
    pass


class CountingStorage:
    def __init__(self, storage):
        self.storage = storage
        self.children_batch_sizes = []
        self.entity_batch_sizes = []
        self.get_children_calls = 0
        self.get_entity_calls = 0

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


class ScalarOnlyStorage:
    """Storage adapter exposing only the original scalar read methods."""

    def __init__(self, storage):
        self.storage = storage
        self.get_children_calls = 0
        self.get_entity_calls = 0

    def get_root_id(self, tree_id):
        return self.storage.get_root_id(tree_id)

    def get_children(self, tree_id, node_id):
        self.get_children_calls += 1
        return self.storage.get_children(tree_id, node_id)

    def get_entity(self, tree_id, node_id):
        self.get_entity_calls += 1
        return self.storage.get_entity(tree_id, node_id)


def _ingest_tree(db):
    tree = {
        "type": "object",
        "children": {
            "left": {
                "type": "object",
                "children": {
                    "a": {"type": "leaf", "entity_id": "a"},
                },
            },
            "right": {
                "type": "object",
                "children": {
                    "b": {"type": "leaf", "entity_id": "b"},
                },
            },
        },
    }
    entities = {
        "a": {"type": "text", "text": "content a"},
        "b": {"type": "text", "text": "content b"},
    }
    tree_id = db.ingest_tree(tree, entities=entities)
    root_id = db.get_root_id(tree_id)
    parents = {
        node.slot: node
        for node in db.get_children(tree_id, root_id)
    }
    leaves = {
        "a": db.get_children(tree_id, parents["left"].node_id)[0],
        "b": db.get_children(tree_id, parents["right"].node_id)[0],
    }
    return tree_id, parents, leaves


def test_block_batches_final_content_reads_in_result_order():
    with TreeDB(":memory:") as db:
        tree_id, _, leaves = _ingest_tree(db)
        storage = CountingStorage(db)
        retriever = BlockRetriever(storage, DummyLLM())
        requested = [leaves["b"].node_id, leaves["a"].node_id]

        contents = retriever._gather_contents(tree_id, requested)

        assert [item["node_id"] for item in contents] == requested
        assert [
            item["content"]["text"] for item in contents
        ] == ["content b", "content a"]
        assert storage.entity_batch_sizes == [2]
        assert storage.get_entity_calls == 0


def test_block_batches_document_frontier_child_checks():
    with TreeDB(":memory:") as db:
        tree_id, parents, _ = _ingest_tree(db)
        storage = CountingStorage(db)
        retriever = BlockRetriever(storage, DummyLLM(), mode="document")
        frontier = [
            {"node_id": parents["left"].node_id},
            {"node_id": parents["right"].node_id},
        ]

        assert retriever._frontier_has_children(tree_id, frontier) is True
        assert storage.children_batch_sizes == [2]
        assert storage.get_children_calls == 0


def test_block_read_helpers_preserve_scalar_storage_compatibility():
    with TreeDB(":memory:") as db:
        tree_id, parents, leaves = _ingest_tree(db)
        storage = ScalarOnlyStorage(db)
        retriever = BlockRetriever(storage, DummyLLM(), mode="document")
        requested = [leaves["b"].node_id, leaves["a"].node_id]
        frontier = [
            {"node_id": parents["left"].node_id},
            {"node_id": parents["right"].node_id},
        ]

        contents = retriever._gather_contents(tree_id, requested)

        assert [item["node_id"] for item in contents] == requested
        assert retriever._frontier_has_children(tree_id, frontier) is True
        assert storage.get_entity_calls == 2
        assert storage.get_children_calls == 1


def test_parallel_horizontal_results_follow_block_order():
    retriever = BlockRetriever(storage=None, llm=DummyLLM(), max_parallel_blocks=3)
    blocks = [
        SimpleNamespace(block_id="slow", cached_content=""),
        SimpleNamespace(block_id="fast", cached_content=""),
        SimpleNamespace(block_id="medium", cached_content=""),
    ]
    delays = {"slow": 0.03, "fast": 0.0, "medium": 0.01}

    retriever._collect_allowed_node_ids = (
        lambda tree_id, block, beams: [block.block_id]
    )

    def process_block(block, *args, **kwargs):
        time.sleep(delays[block.block_id])
        result = BlockResult(
            block_id=block.block_id,
            ordered_node_ids=[block.block_id],
            top_candidate_node_ids=[block.block_id],
            done=False,
        )
        return result, 1, {}

    retriever._process_block = process_block

    rows = retriever._process_horizontal_group(
        blocks,
        tree_id="tree",
        query="query",
        beams=[],
        previous_top_candidate_ids=[],
        pick_limit=1,
    )

    assert [block.block_id for _, _, _, block in rows] == [
        "slow",
        "fast",
        "medium",
    ]
