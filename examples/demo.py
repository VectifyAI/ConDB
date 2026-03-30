#!/usr/bin/env python3
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from contextdb import ContextTree
from contextdb.config import Config


def get_sample_pdf():
    pdf_path = os.getenv("DEMO_PDF_PATH")
    if pdf_path and Path(pdf_path).exists():
        return pdf_path

    for name in ["sample.pdf", "test.pdf", "document.pdf"]:
        if Path(name).exists():
            return name

    raise FileNotFoundError("Set DEMO_PDF_PATH or place a PDF in current directory")


def main():
    print("=" * 60)
    print("  ContextDB Demo")
    print("=" * 60)

    doc_path = get_sample_pdf()
    print(f"\nDocument: {Path(doc_path).name}")

    llm = Config.get_llm_client()
    ct = ContextTree("/tmp/demo.sqlite", llm=llm)

    print("\nIndexing...")
    start = time.time()
    tree_id = ct.index_pdf_file(doc_path)
    print(f"Indexed in {time.time() - start:.2f}s")

    cursor = ct.storage.conn.cursor()
    cursor.execute("SELECT COUNT(*) as c FROM nodes WHERE tree_id = ?", (tree_id,))
    print(f"Nodes: {cursor.fetchone()['c']}")

    print("\nTree structure:")
    tree_view = ct.format_tree_view(tree_id, depth=2)
    for line in tree_view.split('\n')[:10]:
        print(f"  {line}")

    queries = [
        "What are the main topics?",
        "What financial metrics are discussed?",
    ]

    for query in queries:
        print(f"\nQuery: {query}")
        start = time.time()
        result = ct.query(tree_id, query, use_llm=True, max_turns=5)
        print(f"  Time: {time.time() - start:.2f}s, Turns: {result.turns}, Items: {len(result.contents)}")

    ct.close()
    print("\n" + "=" * 60)
    print("  Demo complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
