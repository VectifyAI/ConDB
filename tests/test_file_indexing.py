from pathlib import Path

from contextdb import ContextTree


def _build_document_tree(path: str):
    text = Path(path).read_text(encoding="utf-8")
    return {
        "doc_name": Path(path).name,
        "doc_description": "built by external tree builder",
        "structure": [
            {
                "node_id": "root_section",
                "title": "ML Guide",
                "summary": "Overview of the markdown file",
                "text": text,
                "start_index": 1,
                "end_index": 1,
                "nodes": [],
            }
        ],
    }


def test_index_markdown_file_with_external_tree_builder(tmp_path):
    md_path = tmp_path / "guide.md"
    md_path.write_text(
        "# ML Guide\n\n## Introduction\nML is a subset of AI.\n",
        encoding="utf-8",
    )

    ct = ContextTree(":memory:")
    try:
        tree_id = ct.index_markdown_file(str(md_path), tree_builder=_build_document_tree)
        root_id = ct.storage.get_root_id(tree_id)
        root_content = ct.get_content(tree_id, root_id)
        children = ct.get_children(tree_id, root_id)

        assert root_content["title"] == "guide.md"
        assert len(children) == 1
        assert children[0]["attrs"]["title"] == "ML Guide"
    finally:
        ct.close()
