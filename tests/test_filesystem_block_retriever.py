import json
import threading
import time

from contextdb.adapter.filesystem import FileSystemAdapter
from contextdb.core.storage import TreeDB
from contextdb.retriever.algorithm.beam_retriever import BeamRetriever
from contextdb.retriever.algorithm.block_retriever import BlockRetriever
from contextdb.retriever.algorithm.block_retriever_legacy import LegacyBlockRetriever


def _extract_current_block_paths(text):
    lines = text.splitlines()
    path_by_id = {}
    current_section = {}
    current_id = None
    path_prefix = ""
    in_block = False

    for line in lines:
        if line.startswith("=== BLOCK "):
            in_block = True
            current_section = {}
            current_id = None
            path_prefix = ""
            continue
        if in_block and line.startswith("Query:"):
            break
        if not in_block:
            continue
        if line.startswith("Path prefix: "):
            path_prefix = line.split(": ", 1)[1].strip()
            continue
        if line.startswith("- id: "):
            current_id = line.split(": ", 1)[1].strip()
            continue
        if current_id and line.startswith("  path: "):
            display_path = line.split(": ", 1)[1].strip()
            if path_prefix:
                if display_path in {"", "."}:
                    current_section[current_id] = path_prefix.rstrip("/")
                else:
                    current_section[current_id] = f"{path_prefix}{display_path}"
            else:
                current_section[current_id] = display_path
            path_by_id = dict(current_section)

    return path_by_id


def _flatten_cache_text(cache_content):
    if isinstance(cache_content, list):
        return "\n\n".join(part for part in cache_content if part)
    return cache_content or ""


class PathMatchLLM:
    def chat(self, messages, system="", tools=None, cache_key=None):
        text = messages[0]["content"] if messages else ""
        path_by_id = _extract_current_block_paths(text)

        ranked_ids = []
        needle_hits = [node_id for node_id, path in path_by_id.items() if "needle" in path]
        if needle_hits:
            ranked_ids = needle_hits[:1]
        else:
            src_hits = [node_id for node_id, path in path_by_id.items() if path == "src"]
            ranked_ids = src_hits[:1]

        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank_1",
                    "name": "rank",
                    "input": {"ranked_ids": ranked_ids, "done": False},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }


class DeepPathMatchLLM:
    def chat(self, messages, system="", tools=None, cache_key=None):
        text = messages[0]["content"] if messages else ""
        path_by_id = _extract_current_block_paths(text)

        ranked_ids = []
        for needle in ("needle.py", "target_pkg", "src"):
            hits = [node_id for node_id, path in path_by_id.items() if needle in path or path == needle]
            if hits:
                ranked_ids = hits[:1]
                break
        if not ranked_ids and path_by_id:
            ranked_ids = [next(iter(path_by_id.keys()))]

        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank_1",
                    "name": "rank",
                    "input": {"ranked_ids": ranked_ids, "done": False},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }


class CapturePromptLLM:
    def __init__(self):
        self.prompts = []

    def chat(self, messages, system="", tools=None, cache_key=None):
        text = messages[0]["content"] if messages else ""
        self.prompts.append(text)
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank_1",
                    "name": "rank",
                    "input": {"ranked_ids": [], "done": True},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }


class BranchRerankLLM:
    def chat(self, messages, system="", tools=None, cache_key=None):
        text = messages[0]["content"] if messages else ""
        path_by_id = _extract_current_block_paths(text)

        ranked_ids = []
        if any(path.endswith("needle.py") for path in path_by_id.values()):
            ranked_ids = [node_id for node_id, path in path_by_id.items() if path.endswith("needle.py")][:1]
        elif any(path.endswith("target_pkg_very_long_component_name") for path in path_by_id.values()) and any(
            path.endswith("aaa_direct_hit_very_long_component_name.py") for path in path_by_id.values()
        ):
            ranked_ids = [
                node_id
                for node_id, path in path_by_id.items()
                if path.endswith("target_pkg_very_long_component_name")
            ][:1]
        elif any(path.endswith("target_pkg_very_long_component_name") for path in path_by_id.values()):
            ranked_ids = [
                node_id
                for node_id, path in path_by_id.items()
                if path.endswith("target_pkg_very_long_component_name")
            ][:1]
        elif any(path.endswith("aaa_direct_hit_very_long_component_name.py") for path in path_by_id.values()):
            ranked_ids = [
                node_id
                for node_id, path in path_by_id.items()
                if path.endswith("aaa_direct_hit_very_long_component_name.py")
            ][:1]
        elif any(path == "src" for path in path_by_id.values()):
            ranked_ids = [node_id for node_id, path in path_by_id.items() if path == "src"][:1]

        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank_1",
                    "name": "rank",
                    "input": {"ranked_ids": ranked_ids, "done": False},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }


class CaptureBeamPromptLLM:
    def __init__(self):
        self.prompts = []

    def chat(self, messages, system="", tools=None, cache_key=None):
        text = messages[0]["content"] if messages else ""
        self.prompts.append(text)
        path_by_id = _extract_current_block_paths(text)
        ranked_ids = [node_id for node_id, path in path_by_id.items() if path == "src"][:1]

        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank_1",
                    "name": "rank",
                    "input": {"ranked_ids": ranked_ids, "done": not bool(ranked_ids)},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }


class ConcurrencyProbeLLM:
    def __init__(self, delay_s=0.05):
        self.delay_s = delay_s
        self.active_calls = 0
        self.max_active_calls = 0
        self._lock = threading.Lock()

    def chat(self, messages, system="", tools=None, cache_key=None):
        with self._lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)

        try:
            time.sleep(self.delay_s)
            text = messages[0]["content"] if messages else ""
            path_by_id = _extract_current_block_paths(text)

            ranked_ids = []
            if any(path == "src" for path in path_by_id.values()):
                ranked_ids = [node_id for node_id, path in path_by_id.items() if path == "src"][:1]
            elif path_by_id:
                ranked_ids = [next(iter(path_by_id.keys()))]

            return {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "rank_1",
                        "name": "rank",
                        "input": {"ranked_ids": ranked_ids, "done": False},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            }
        finally:
            with self._lock:
                self.active_calls -= 1


class CacheAwarePathLLM:
    provider = "anthropic"
    model = "claude-sonnet-4-20250514"

    def __init__(self):
        self.cache_calls = []

    def chat_with_cache(
        self,
        messages,
        system="",
        tools=None,
        cache_content=None,
        non_cached_content=None,
        cache_key=None,
    ):
        prompt_text = "\n\n".join(
            part
            for part in [
                _flatten_cache_text(cache_content),
                non_cached_content or "",
                messages[0]["content"] if messages else "",
            ]
            if part
        )
        self.cache_calls.append(
            {
                "messages": messages,
                "cache_content": cache_content,
                "non_cached_content": non_cached_content,
                "cache_key": cache_key,
            }
        )

        path_by_id = _extract_current_block_paths(prompt_text)
        ranked_ids = []
        done = False

        if any(path == "src" for path in path_by_id.values()):
            ranked_ids = [node_id for node_id, path in path_by_id.items() if path == "src"][:1]
        elif any(path.endswith("target_pkg") for path in path_by_id.values()):
            ranked_ids = [node_id for node_id, path in path_by_id.items() if path.endswith("target_pkg")][:1]
        elif any(path.endswith("needle.py") for path in path_by_id.values()):
            ranked_ids = [node_id for node_id, path in path_by_id.items() if path.endswith("needle.py")][:1]
            done = True

        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank_1",
                    "name": "rank",
                    "input": {"ranked_ids": ranked_ids, "done": done},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }


class CacheAwareBranchLLM:
    provider = "anthropic"
    model = "claude-sonnet-4-20250514"

    def __init__(self):
        self.cache_calls = []

    def chat_with_cache(
        self,
        messages,
        system="",
        tools=None,
        cache_content=None,
        non_cached_content=None,
        cache_key=None,
    ):
        prompt_text = "\n\n".join(
            part
            for part in [
                _flatten_cache_text(cache_content),
                non_cached_content or "",
                messages[0]["content"] if messages else "",
            ]
            if part
        )
        self.cache_calls.append(
            {
                "messages": messages,
                "cache_content": cache_content,
                "non_cached_content": non_cached_content,
                "cache_key": cache_key,
            }
        )

        path_by_id = _extract_current_block_paths(prompt_text)
        ranked_ids = []
        if any(path.endswith("needle.py") for path in path_by_id.values()):
            ranked_ids = [node_id for node_id, path in path_by_id.items() if path.endswith("needle.py")][:1]
        elif any(path.endswith("target_pkg_very_long_component_name") for path in path_by_id.values()) and any(
            path.endswith("aaa_direct_hit_very_long_component_name.py") for path in path_by_id.values()
        ):
            ranked_ids = [
                node_id
                for node_id, path in path_by_id.items()
                if path.endswith("target_pkg_very_long_component_name")
            ][:1]
        elif any(path.endswith("target_pkg_very_long_component_name") for path in path_by_id.values()):
            ranked_ids = [
                node_id
                for node_id, path in path_by_id.items()
                if path.endswith("target_pkg_very_long_component_name")
            ][:1]
        elif any(path.endswith("aaa_direct_hit_very_long_component_name.py") for path in path_by_id.values()):
            ranked_ids = [
                node_id
                for node_id, path in path_by_id.items()
                if path.endswith("aaa_direct_hit_very_long_component_name.py")
            ][:1]
        elif any(path == "src" for path in path_by_id.values()):
            ranked_ids = [node_id for node_id, path in path_by_id.items() if path == "src"][:1]

        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "rank_1",
                    "name": "rank",
                    "input": {"ranked_ids": ranked_ids, "done": False},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }


def _retrieved_paths(db, tree_id, node_ids):
    paths = []
    for node_id in node_ids:
        node = db.get_node(tree_id, node_id)
        if not node or not node.attrs_json:
            continue
        attrs = node.to_dict().get("attrs", {})
        rel_path = attrs.get("rel_path")
        if rel_path:
            paths.append(rel_path)
    return paths


def _replace_node_payload(db, tree_id, node_id, payload):
    cursor = db.conn.cursor()
    cursor.execute("SELECT entity_id FROM nodes WHERE tree_id = ? AND node_id = ?", (tree_id, node_id))
    entity_id = cursor.fetchone()["entity_id"]
    cursor.execute("UPDATE entities SET payload_json = ? WHERE entity_id = ?", (json.dumps(payload), entity_id))
    db.conn.commit()


def test_filesystem_retriever_splits_wide_directory_into_multiple_blocks(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    for idx in range(10):
        (src_dir / f"module_{idx:02d}_very_long_component_name.py").write_text("pass\n", encoding="utf-8")
    (src_dir / "zzzz_needle_target_module.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = BlockRetriever(
            db,
            PathMatchLLM(),
            max_tokens_per_block=150,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find src needle", beam_size=1, max_turns=10, select_k=1)

        retrieved_paths = _retrieved_paths(db, tree_id, result.nodes)

        assert "src/zzzz_needle_target_module.py" in retrieved_paths
        assert result.blocks_processed >= 3
        assert any(t.get("type") == "subtree_split" for t in result.trace)
        assert any(bt.get("type") == "subtree_horizontal" for bt in result.block_traces)
    finally:
        db.close()


def test_filesystem_retriever_splits_wide_root_before_descending(tmp_path):
    for idx in range(10):
        (tmp_path / f"pkg_{idx:02d}_very_long_component_name").mkdir()
    target_dir = tmp_path / "zzzz_target_pkg_very_long_component_name"
    target_dir.mkdir()
    (target_dir / "needle.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = BlockRetriever(
            db,
            DeepPathMatchLLM(),
            max_tokens_per_block=150,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find target package needle", beam_size=1, max_turns=10, select_k=1)
        retrieved_paths = _retrieved_paths(db, tree_id, result.nodes)

        assert "zzzz_target_pkg_very_long_component_name/needle.py" in retrieved_paths
        assert any(t.get("type") == "top_split" for t in result.trace)
        assert any(bt.get("type") == "top_horizontal" for bt in result.block_traces)
        assert not any(bt.get("type") == "top_global" for bt in result.block_traces)
    finally:
        db.close()


def test_filesystem_retriever_merges_split_blocks_without_global_llm(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    for idx in range(10):
        (src_dir / f"pkg_{idx:02d}_very_long_component_name").mkdir()
    target_dir = src_dir / "zzzz_target_pkg_very_long_component_name"
    target_dir.mkdir()
    (target_dir / "needle.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = BlockRetriever(
            db,
            DeepPathMatchLLM(),
            max_tokens_per_block=150,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find target package needle", beam_size=1, max_turns=10, select_k=1)

        assert any(t.get("type") == "subtree_split" for t in result.trace)
        assert all(bt.get("type") != "subtree_global" for bt in result.block_traces)
        assert any("merged_ids" in t for t in result.trace)
    finally:
        db.close()


def test_filesystem_retriever_respects_max_turns_as_round_limit(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    for idx in range(10):
        (src_dir / f"module_{idx:02d}_very_long_component_name.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = BlockRetriever(
            db,
            PathMatchLLM(),
            max_tokens_per_block=220,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find src needle", beam_size=1, max_turns=1, select_k=1)

        assert result.turns == 1
        assert result.total_llm_calls == 1
        assert result.blocks_processed == 1
    finally:
        db.close()


def test_filesystem_retriever_keeps_regular_single_child_directory_chain_on_llm_path(tmp_path):
    pkg_dir = tmp_path / "src" / "pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "needle.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = BlockRetriever(
            db,
            DeepPathMatchLLM(),
            max_tokens_per_block=220,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find needle", beam_size=1, max_turns=5, select_k=1)
        retrieved_paths = _retrieved_paths(db, tree_id, result.nodes)

        assert retrieved_paths == ["src/pkg/needle.py"]
        assert result.total_llm_calls == 3
        assert result.turns == 3
    finally:
        db.close()


def test_filesystem_retriever_auto_descends_virtual_json_entry_chain(tmp_path):
    tree_payload = {
        "docs": [
            {"path": "docs/api-guide.md", "content": "# API Guide"},
            {"path": "docs/reference.md", "content": "# Reference"},
        ]
    }
    (tmp_path / "tree.json").write_text(json.dumps(tree_payload), encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        llm = CapturePromptLLM()
        retriever = BlockRetriever(
            db,
            llm,
            max_tokens_per_block=220,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find api guide", beam_size=1, max_turns=1, select_k=1)

        assert result.turns == 1
        assert llm.prompts
        prompt = llm.prompts[0]
        assert "tree.json" not in prompt
        assert "api-guide.md" in prompt
        assert "reference.md" in prompt
    finally:
        db.close()


def test_filesystem_virtual_top_block_surfaces_nested_paths(tmp_path):
    tree_payload = {
        "docs": [
            {"path": "docs/agentic-tools/ai-sdk/tools/query-docs.md", "content": "# Query Docs"},
            {"path": "docs/agentic-tools/overview.md", "content": "# Overview"},
        ]
    }
    (tmp_path / "tree.json").write_text(json.dumps(tree_payload), encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        llm = CapturePromptLLM()
        retriever = BlockRetriever(
            db,
            llm,
            max_tokens_per_block=220,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find query docs tool", beam_size=1, max_turns=1, select_k=1)

        assert result.turns == 1
        assert llm.prompts
        prompt = llm.prompts[0]
        assert "tree.json" not in prompt
        assert "Path prefix: docs/agentic-tools/" in prompt
        assert "ai-sdk/tools/query-docs.md" in prompt
        assert "overview.md" in prompt
        assert "# Query Docs" not in prompt
    finally:
        db.close()


def test_filesystem_virtual_top_block_packs_with_virtual_render_options(tmp_path):
    tree_payload = {
        "docs": [
            {"path": f"docs/doc_{idx}.md", "content": "very long body " * 200}
            for idx in range(3)
        ]
    }
    (tmp_path / "tree.json").write_text(json.dumps(tree_payload), encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = BlockRetriever(
            db,
            PathMatchLLM(),
            max_tokens_per_block=120,
            cache_enabled=False,
            mode="filesystem",
        )

        root_beam = retriever._collapse_fs_beams(
            tree_id,
            [{"node_id": db.get_root_id(tree_id), "title": "root", "path": "root"}],
        )[0]
        top_specs = retriever._create_top_block_specs_fs(tree_id, root_beam)

        assert len(top_specs) == 1
        assert top_specs[0]["block"].cached_content.count("summary:") == 0
    finally:
        db.close()


def test_filesystem_top_prompt_only_lists_root_children(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    (src_dir / "module_alpha.py").write_text("pass\n", encoding="utf-8")
    (docs_dir / "readme.md").write_text("# docs\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        llm = CapturePromptLLM()
        retriever = BlockRetriever(
            db,
            llm,
            max_tokens_per_block=220,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find entrypoints", beam_size=1, max_turns=1, select_k=1)

        assert result.turns == 1
        assert llm.prompts
        prompt = llm.prompts[0]
        assert "  path: src" in prompt
        assert "  path: docs" in prompt
        assert "src/module_alpha.py" not in prompt
        assert "docs/readme.md" not in prompt
    finally:
        db.close()


def test_filesystem_block_content_compresses_shared_path_prefix(tmp_path):
    prefix_dir = tmp_path / "src" / "very" / "long" / "shared" / "prefix"
    prefix_dir.mkdir(parents=True)

    (prefix_dir / "module_alpha_very_long_name.py").write_text("pass\n", encoding="utf-8")
    (prefix_dir / "module_beta_very_long_name.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = BlockRetriever(
            db,
            PathMatchLLM(),
            max_tokens_per_block=220,
            cache_enabled=False,
            mode="filesystem",
        )

        root_id = db.get_root_id(tree_id)
        root_children = retriever._get_direct_children_nodes(tree_id, root_id)
        src_node = next(node for node in root_children if node.get("attrs", {}).get("rel_path") == "src")
        current = src_node
        for rel_path in ("src/very", "src/very/long", "src/very/long/shared", "src/very/long/shared/prefix"):
            children = retriever._get_direct_children_nodes(tree_id, current["node_id"])
            current = next(node for node in children if node.get("attrs", {}).get("rel_path") == rel_path)

        prefix_children = retriever._get_direct_children_nodes(tree_id, current["node_id"])
        block = retriever._build_fs_subtree_block(tree_id, current["node_id"], prefix_children, 0)

        assert "Path prefix: src/very/long/shared/prefix/" in block.cached_content
        assert "  path: module_alpha_very_long_name.py" in block.cached_content
        assert "  path: src/very/long/shared/prefix/module_alpha_very_long_name.py" not in block.cached_content
    finally:
        db.close()


def test_filesystem_block_packing_preserves_order_without_path_evidence(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    for idx in range(8):
        (src_dir / f"module_{idx:02d}_very_long_component_name.py").write_text("pass\n", encoding="utf-8")
    (src_dir / "zzzz_cache_invalidator_target.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = BlockRetriever(
            db,
            PathMatchLLM(),
            max_tokens_per_block=120,
            cache_enabled=False,
            mode="filesystem",
        )

        root_id = db.get_root_id(tree_id)
        root_children = retriever._get_direct_children_nodes(tree_id, root_id)
        src_node = next(node for node in root_children if node.get("attrs", {}).get("rel_path") == "src")
        specs = retriever._create_subtree_block_specs_fs(
            tree_id,
            [{"node_id": src_node["node_id"], "title": "src", "path": "src"}],
            query="cache invalidator target",
        )

        assert len(specs) > 1
        assert "zzzz_cache_invalidator_target.py" not in (specs[0]["block"].cached_content or "")
    finally:
        db.close()


def test_filesystem_leaf_block_omits_file_summary(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "target.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = BlockRetriever(
            db,
            PathMatchLLM(),
            max_tokens_per_block=512,
            cache_enabled=False,
            mode="filesystem",
        )

        root_id = db.get_root_id(tree_id)
        root_children = retriever._get_direct_children_nodes(tree_id, root_id)
        src_node = next(node for node in root_children if node.get("attrs", {}).get("rel_path") == "src")
        leaf = retriever._get_direct_children_nodes(tree_id, src_node["node_id"])[0]

        _replace_node_payload(
            db,
            tree_id,
            leaf["node_id"],
            {
                "type": "file",
                "title": "target.py",
                "summary": "stale file summary",
                "text": "pass\n",
            },
        )

        block = retriever._make_fs_block(tree_id, [retriever._get_nodes_by_ids(tree_id, [leaf["node_id"]])[0]], "leaf")

        assert "  path: src/target.py" in block.cached_content
        assert "stale file summary" not in block.cached_content
        assert "summary:" not in block.cached_content
        assert "tag:" not in block.cached_content
    finally:
        db.close()


def test_filesystem_adapter_keeps_virtual_file_summary_empty(tmp_path):
    tree_payload = {
        "docs": [
            {
                "path": "docs/api-guide.md",
                "title": "API Guide",
                "content": "This content should stay in text, not in summary.",
            }
        ]
    }
    (tmp_path / "tree.json").write_text(json.dumps(tree_payload), encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        cursor = db.conn.cursor()
        cursor.execute(
            """
            SELECT n.attrs_json, e.payload_json
            FROM nodes n JOIN entities e ON n.entity_id = e.entity_id
            WHERE n.tree_id = ? AND n.attrs_json LIKE ?
            """,
            (tree_id, '%"rel_path": "docs/api-guide.md"%'),
        )
        row = cursor.fetchone()
        attrs = json.loads(row["attrs_json"])
        payload = json.loads(row["payload_json"])

        assert "tag" not in attrs
        assert payload["type"] == "file"
        assert payload["summary"] == ""
        assert payload["text"] == "This content should stay in text, not in summary."
    finally:
        db.close()


def test_filesystem_beam_candidate_omits_leaf_summary_and_text(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "target.py").write_text("body text\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = BeamRetriever(db, PathMatchLLM(), mode="filesystem")
        root_id = db.get_root_id(tree_id)
        src_node = db.get_children(tree_id, root_id)[0]
        leaf = db.get_children(tree_id, src_node.node_id)[0]

        _replace_node_payload(
            db,
            tree_id,
            leaf.node_id,
            {
                "type": "file",
                "title": "target.py",
                "summary": "stale file summary",
                "text": "body text\n",
            },
        )

        candidate = retriever._candidate_from_child(tree_id, leaf, ["src"])

        assert candidate["rel_path"] == "src/target.py"
        assert candidate["summary"] == ""
        assert candidate["text"] == ""
        assert "tag" not in candidate
    finally:
        db.close()


def test_filesystem_legacy_block_renders_leaf_as_file_path_only(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "target.py").write_text("body text\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = LegacyBlockRetriever(db, PathMatchLLM(), max_tokens_per_block=512)
        root_id = db.get_root_id(tree_id)
        src_node = retriever._get_direct_children_nodes(tree_id, root_id)[0]
        leaf = retriever._get_direct_children_nodes(tree_id, src_node["node_id"])[0]

        _replace_node_payload(
            db,
            tree_id,
            leaf["node_id"],
            {
                "type": "file",
                "title": "target.py",
                "summary": "stale file summary",
                "text": "body text\n",
            },
        )
        leaf = retriever._get_direct_children_nodes(tree_id, src_node["node_id"])[0]

        content = retriever._generate_content_from_nodes([leaf])

        assert "  path: src/target.py" in content
        assert "  type: file" in content
        assert "tag:" not in content
        assert "stale file summary" not in content
        assert "body text" not in content
    finally:
        db.close()


def test_filesystem_prompt_uses_short_aliases_and_elides_redundant_allowed_ids(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "module_alpha.py").write_text("pass\n", encoding="utf-8")
    (src_dir / "module_beta.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        llm = CapturePromptLLM()
        retriever = BlockRetriever(
            db,
            llm,
            max_tokens_per_block=220,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find src module", beam_size=1, max_turns=1, select_k=1)

        assert result.turns == 1
        assert llm.prompts
        prompt = llm.prompts[0]
        top_block = retriever._create_top_block_specs_fs(
            tree_id,
            {"node_id": db.get_root_id(tree_id), "title": "root", "path": "root"},
        )[0]["block"]
        top_nodes = retriever._get_nodes_by_ids(tree_id, top_block.node_ids)

        assert "Allowed node ids" not in prompt
        assert "All ids in the CURRENT BLOCK are selectable." in prompt
        assert "- id: n1" in prompt
        for node in top_nodes:
            assert node["node_id"] not in prompt
    finally:
        db.close()


def test_filesystem_retriever_keeps_merge_order_without_global_rerank(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    (src_dir / "aaa_direct_hit_very_long_component_name.py").write_text("pass\n", encoding="utf-8")
    for idx in range(10):
        (src_dir / f"module_{idx:02d}_very_long_component_name.py").write_text("pass\n", encoding="utf-8")
    target_dir = src_dir / "zzz_target_pkg_very_long_component_name"
    target_dir.mkdir()
    (target_dir / "needle.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        retriever = BlockRetriever(
            db,
            BranchRerankLLM(),
            max_tokens_per_block=150,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find target package needle", beam_size=1, max_turns=10, select_k=1)
        retrieved_paths = _retrieved_paths(db, tree_id, result.nodes)

        assert "src/aaa_direct_hit_very_long_component_name.py" in retrieved_paths
        assert "src/zzz_target_pkg_very_long_component_name/needle.py" not in retrieved_paths
        assert any(bt.get("type") == "subtree_horizontal" for bt in result.block_traces)
        assert all(not bt.get("type", "").endswith("_global") for bt in result.block_traces)
    finally:
        db.close()


def test_filesystem_prompt_uses_repo_relative_beam_paths(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "module_alpha.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        llm = CaptureBeamPromptLLM()
        retriever = BlockRetriever(
            db,
            llm,
            max_tokens_per_block=220,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find src module", beam_size=1, max_turns=3, select_k=1)

        assert result.total_llm_calls == 2
        assert len(llm.prompts) == 2
        second_prompt = llm.prompts[1]

        assert "Current exploration path:" in second_prompt
        assert "\n- src\n" in second_prompt
        assert "/r/" not in second_prompt
    finally:
        db.close()


def test_filesystem_retriever_caps_horizontal_block_parallelism(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    for idx in range(12):
        (src_dir / f"module_{idx:02d}_very_long_component_name.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        llm = ConcurrencyProbeLLM()
        retriever = BlockRetriever(
            db,
            llm,
            max_tokens_per_block=80,
            max_parallel_blocks=2,
            cache_enabled=False,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find src module", beam_size=1, max_turns=3, select_k=1)

        subtree_blocks = [
            block_trace
            for block_trace in result.block_traces
            if block_trace.get("type") == "subtree_horizontal"
        ]
        assert len(subtree_blocks) >= 3
        assert llm.max_active_calls == 2
    finally:
        db.close()


def test_filesystem_cache_subtree_block_controls_future_cache_prefix(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    target_dir = src_dir / "target_pkg"
    target_dir.mkdir()
    (target_dir / "needle.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)

        for cache_subtree_block, expect_subtree_cached in ((False, False), (True, True)):
            llm = CacheAwarePathLLM()
            retriever = BlockRetriever(
                db,
                llm,
                max_tokens_per_block=512,
                cache_enabled=True,
                cache_current_block=True,
                cache_subtree_block=cache_subtree_block,
                mode="filesystem",
            )

            result = retriever.retrieve(tree_id, "find target package needle", beam_size=1, max_turns=5, select_k=1)

            assert result.total_llm_calls == 3
            assert len(llm.cache_calls) == 3

            third_call_cache = _flatten_cache_text(llm.cache_calls[2]["cache_content"])
            assert "=== BLOCK " in third_call_cache
            if expect_subtree_cached:
                assert "=== BLOCK fs_sub_" in third_call_cache
            else:
                assert "=== BLOCK fs_sub_" not in third_call_cache
    finally:
        db.close()


def test_filesystem_cache_keeps_only_winning_subtree_blocks(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    (src_dir / "aaa_direct_hit_very_long_component_name.py").write_text("pass\n", encoding="utf-8")
    for idx in range(10):
        (src_dir / f"module_{idx:02d}_very_long_component_name.py").write_text("pass\n", encoding="utf-8")
    target_dir = src_dir / "zzz_target_pkg_very_long_component_name"
    target_dir.mkdir()
    (target_dir / "needle.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        llm = CacheAwareBranchLLM()
        retriever = BlockRetriever(
            db,
            llm,
            max_tokens_per_block=80,
            cache_enabled=True,
            cache_current_block=True,
            cache_subtree_block=True,
            mode="filesystem",
        )

        result = retriever.retrieve(tree_id, "find target package needle", beam_size=1, max_turns=10, select_k=1)
        retrieved_paths = _retrieved_paths(db, tree_id, result.nodes)
        last_cache = _flatten_cache_text(llm.cache_calls[-1]["cache_content"])

        assert "src/zzz_target_pkg_very_long_component_name/needle.py" in retrieved_paths
        assert "zzz_target_pkg_very_long_component_name" in last_cache
        assert last_cache.count("=== BLOCK fs_sub_") == 2
    finally:
        db.close()


def test_filesystem_cache_window_pins_top_block(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    target_dir = src_dir / "target_pkg"
    target_dir.mkdir()
    (target_dir / "needle.py").write_text("pass\n", encoding="utf-8")

    adapter = FileSystemAdapter(str(tmp_path))
    db = TreeDB(":memory:")

    try:
        tree_id = adapter.ingest(db)
        llm = CacheAwarePathLLM()
        retriever = BlockRetriever(
            db,
            llm,
            max_tokens_per_block=512,
            cache_enabled=True,
            cache_current_block=True,
            cache_subtree_block=True,
            mode="filesystem",
        )

        top_block = retriever._create_top_block_specs_fs(
            tree_id,
            {"node_id": db.get_root_id(tree_id), "title": "root", "path": "root"},
        )[0]["block"]
        top_content = top_block.cached_content or ""
        top_entry = f"=== BLOCK {top_block.block_id} ===\n\n{top_content}"
        retriever.cache_window_tokens = retriever.token_counter.count_text_tokens(top_entry)

        result = retriever.retrieve(tree_id, "find target package needle", beam_size=1, max_turns=5, select_k=1)
        last_cache = _flatten_cache_text(llm.cache_calls[-1]["cache_content"])

        assert result.total_llm_calls == 3
        assert f"=== BLOCK {top_block.block_id} ===" in last_cache
        assert last_cache.count("=== BLOCK fs_sub_") == 1
    finally:
        db.close()
