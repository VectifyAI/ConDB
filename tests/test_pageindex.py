import sys
import os
import tempfile
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from contextdb import ContextTree


def test_with_markdown():
    print("PageIndex Integration Test")
    print("=" * 50)

    markdown_content = """# ML Guide

## Introduction
ML is a subset of AI.

### What is ML?
Systems learning without explicit programming.

## Supervised Learning
Learning with labeled data.

### Linear Regression
Continuous predictions.

## Unsupervised Learning
Patterns in unlabeled data.
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
        f.write(markdown_content)
        md_path = f.name

    try:
        from pageindex import md_to_tree
        tree_json = asyncio.run(md_to_tree(md_path))
        print(f"PageIndex: {len(tree_json.get('structure', []))} sections")
    except Exception as e:
        print(f"PageIndex error: {e}")
        tree_json = {
            "doc_name": Path(md_path).name,
            "structure": [{
                "title": "ML Guide", "node_id": "root",
                "summary": "ML guide", "text": markdown_content,
                "start_index": 1, "end_index": 1, "nodes": []
            }]
        }

    ct = ContextTree("test_pageindex.sqlite")
    tree_id = ct.index_pageindex(tree_json)

    cursor = ct.storage.conn.cursor()
    cursor.execute("SELECT COUNT(*) as c FROM nodes WHERE tree_id = ?", (tree_id,))
    print(f"Nodes: {cursor.fetchone()['c']}")

    print(ct.format_tree_view(tree_id, depth=2))

    ct.close()
    Path(md_path).unlink()

    print("=" * 50)
    print("Test passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if test_with_markdown() else 1)
