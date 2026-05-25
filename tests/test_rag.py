"""Test RAG with real regulation document."""
import json
from pathlib import Path

import pytest

from contextdb import ContextTree
from contextdb.config import Config
from contextdb.rag import RAG

DATA_FILE = Path(__file__).parent.parent / "examples/regulation_best_interest.json"
DB_PATH = Path(__file__).parent.parent / "context.sqlite"


def _format_trace_entry(trace_entry: dict) -> str:
    action = trace_entry.get("action") or trace_entry.get("type") or "turn"
    if trace_entry.get("node_id"):
        return f"{action} {trace_entry['node_id'][:8]}"
    if "candidates" in trace_entry and "kept" in trace_entry:
        return f"{action} candidates={trace_entry['candidates']} kept={trace_entry['kept']} done={trace_entry.get('done')}"
    return action


def test_rag():
    with open(DATA_FILE) as f:
        data = json.load(f)

    if not (Config.ANTHROPIC_API_KEY or Config.OPENAI_API_KEY):
        pytest.skip("Set ANTHROPIC_API_KEY or OPENAI_API_KEY to run live RAG test")

    llm = Config.get_llm_client()
    ct = ContextTree(str(DB_PATH), llm=llm)
    rag = RAG(llm)

    tree_id = ct.index_pageindex(data)
    print(f"\nIndexed: {tree_id[:8]}")
    print(ct.format_tree_view(tree_id, depth=3))

    query = "What is the solely incidental prong of the broker-dealer exclusion?"
    r = ct.query(tree_id, query, max_turns=10)

    print(f"\n{'='*60}")
    print(f"Q: {query}")
    print(f"\n[Trace] {r.turns} turns:")
    for t in r.trace:
        print(f"  {_format_trace_entry(t)}")

    print(f"\n[Retrieved] {len(r.nodes)} nodes:")
    for c in r.contents:
        title = c["content"].get("title", "untitled")
        print(f"  - {title}")

    print("\n[Answer]")
    answer = rag.answer(query, r)
    print(answer)
    print(f"{'='*60}\n")

    ct.close()


if __name__ == "__main__":
    test_rag()
