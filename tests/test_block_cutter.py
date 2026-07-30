from contextdb.core.storage import TreeDB
from contextdb.retriever.algorithm.block_cutter import BlockCutter
from contextdb.utils.token_counter import TokenCounter


class DummyTokenCounter:
    def __init__(self, token_map: dict[str, int]):
        self.token_map = token_map

    def get_cached_count(self, node_id: str):
        return self.token_map.get(node_id)

    def count_node_tokens(self, node: dict):
        return self.token_map[node["node_id"]]


def _make_nodes(token_map: dict[str, int]) -> list[dict]:
    nodes = []
    for idx, node_id in enumerate(token_map.keys(), start=1):
        nodes.append(
            {
                "node_id": node_id,
                "parent_id": "p0",
                "slot": str(idx),
                "node_type": 2,
                "depth": 1,
                "path": f"/r/root/{node_id}",
                "attrs": {"title": node_id},
            }
        )
    return nodes


def test_horizontal_packing_keeps_single_block_when_within_budget():
    token_map = {"n1": 4, "n2": 4, "n3": 1}
    cutter = BlockCutter(
        storage=None,
        token_counter=DummyTokenCounter(token_map),
        max_tokens_per_block=10,
        min_tokens_per_block=4,
    )

    group = cutter._create_horizontal_blocks("tree_1", 1, 1, _make_nodes(token_map))

    assert len(group.blocks) == 1
    assert group.blocks[0].total_tokens == 9
    assert group.blocks[0].node_ids == ["n1", "n2", "n3"]


def test_horizontal_tail_pack_is_rebalanced_when_needed():
    token_map = {"n1": 6, "n2": 4, "n3": 1}
    cutter = BlockCutter(
        storage=None,
        token_counter=DummyTokenCounter(token_map),
        max_tokens_per_block=10,
        min_tokens_per_block=4,
    )

    group = cutter._create_horizontal_blocks("tree_2", 1, 1, _make_nodes(token_map))

    assert len(group.blocks) == 2
    assert group.blocks[0].node_ids == ["n1"]
    assert group.blocks[1].node_ids == ["n2", "n3"]
    assert group.blocks[0].total_tokens >= 4
    assert group.blocks[1].total_tokens >= 4


def test_horizontal_tail_pack_is_best_effort_when_rebalance_cannot_reach_minimum():
    token_map = {"n1": 10, "n2": 1, "n3": 1}
    cutter = BlockCutter(
        storage=None,
        token_counter=DummyTokenCounter(token_map),
        max_tokens_per_block=10,
        min_tokens_per_block=4,
    )

    group = cutter._create_horizontal_blocks("tree_3", 1, 1, _make_nodes(token_map))

    assert len(group.blocks) == 2
    assert group.blocks[0].node_ids == ["n1"]
    assert group.blocks[1].node_ids == ["n2", "n3"]
    assert group.blocks[0].total_tokens == 10
    assert group.blocks[1].total_tokens == 2


class CountingStorage:
    def __init__(self, storage):
        self.storage = storage
        self.get_subtree_calls = 0

    def __getattr__(self, name):
        return getattr(self.storage, name)

    def get_subtree(self, *args, **kwargs):
        self.get_subtree_calls += 1
        return self.storage.get_subtree(*args, **kwargs)


def _deep_tree(depth: int) -> dict:
    node = {"type": "leaf"}
    for _ in range(depth):
        node = {"type": "object", "children": {"child": node}}
    return node


def test_cut_tree_materializes_tokens_once_and_keeps_depth_past_100():
    with TreeDB(":memory:") as db:
        tree_id = db.ingest_tree(_deep_tree(120))
        storage = CountingStorage(db)
        token_counter = TokenCounter()
        root_id = db.get_root_id(tree_id)
        token_counter._cache[root_id] = 1_000_000
        cutter = BlockCutter(
            storage=storage,
            token_counter=token_counter,
            max_tokens_per_block=100_000,
        )

        plan = cutter.cut_tree(tree_id)

        assert storage.get_subtree_calls == 1
        assert plan.total_tokens < 1_000_000
        assert len(plan.blocks) == 1
        assert plan.blocks[0].depth_start == 0
        assert plan.blocks[0].depth_end == 120


def test_precompute_missing_tree_does_not_return_stale_cache():
    counter = TokenCounter()
    counter._cache["old-node"] = 123

    with TreeDB(":memory:") as db:
        assert counter.precompute_tree_tokens(db, "missing-tree") == {}
