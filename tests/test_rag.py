"""Test RAG with real regulation document."""
import json
from pathlib import Path
from contextdb import ContextTree
from contextdb.config import Config
from contextdb.rag import RAG

DATA_FILE = Path(__file__).parent.parent / "examples/regulation_best_interest.json"
DB_PATH = Path(__file__).parent.parent / "context.sqlite"


def test_rag():
    with open(DATA_FILE) as f:
        data = json.load(f)

    llm = Config.get_llm_client()
    ct = ContextTree(str(DB_PATH), llm=llm)
    rag = RAG(llm)

    tree_id = ct.index_pageindex(data)
    print(f"\nIndexed: {tree_id[:8]}")
    print(ct.format_tree_view(tree_id, depth=3))

    query = "What is the solely incidental prong of the broker-dealer exclusion?"
    r = ct.query(tree_id, query, max_turns=10, view_depth=2)

    print(f"\n{'='*60}")
    print(f"Q: {query}")
    print(f"\n[Trace] {r.turns} turns:")
    for t in r.trace:
        print(f"  {t['action']} {t.get('node_id', '')[:8]}")

    print(f"\n[Retrieved] {len(r.nodes)} nodes:")
    for c in r.contents:
        title = c["content"].get("title", "untitled")
        print(f"  - {title}")

    print(f"\n[Answer]")
    answer = rag.answer(query, r)
    print(answer)
    print(f"{'='*60}\n")

    ct.close()


if __name__ == "__main__":
    test_rag()
