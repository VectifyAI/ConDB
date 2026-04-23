<div align="center">

# ConDB

<p align="center"><b>Context Database for Hierarchical Document Trees</b></p>

<p align="center">
  Store, navigate, and query hierarchical document structures with LLM-powered reasoning retrieval.
</p>

</div>

---

## What is ConDB?

**ConDB** stores hierarchical document trees in a SQLite database and provides LLM-powered **reasoning-based retrieval** to query them — no vector DB, no chunking. It accepts pageindex-compatible trees, chat trees, and custom hierarchical JSON without taking a runtime code dependency on PageIndex itself.

**Key capabilities:**

- **Hierarchical storage** — store document trees, chat trees, and custom hierarchical JSON in SQLite
- **Reasoning-based retrieval** — LLM navigates the tree to find relevant content, like a human expert
- **Multiple retrieval strategies** — beam search for small trees, block retrieval for large documents
- **Multi-provider LLM support** — works with Anthropic (Claude) and OpenAI (GPT) out of the box
- **Extensible** — plug in custom storage backends, LLM providers, or retrieval strategies

---

## Quick Start

### Install

```bash
pip install -r requirements.txt
```

### Basic Usage

```python
import contextdb

# Open database
db = contextdb.open("my_docs.sqlite")

# Configure LLM
db.set_llm(provider="anthropic", model="claude-sonnet-4-6")

# Store a document tree
tree_id = db.store(document_tree_json, format="document")

# Query with LLM reasoning
result = db.query(tree_id, "What are the key findings?")
print(result.contents)
```

### Index from files with an external tree builder

```python
from contextdb import ContextTree

def build_markdown_tree(path: str) -> dict:
    ...

ct = ContextTree("context.sqlite")

tree_id = ct.index_markdown_file("doc.md", tree_builder=build_markdown_tree)

# You can also generate a tree out of process and call:
# tree_id = ct.index_document_tree(document_tree_json)

ct.close()
```

---

## Configuration

Create a `.env` file with your API keys:

```
ANTHROPIC_API_KEY=sk-...
OPENAI_API_KEY=sk-...
```

Model and provider settings live in `contextdb/config/config.yaml`:

```yaml
llm:
  provider: anthropic          # anthropic or openai
  model: claude-sonnet-4-6     # any model the provider supports
  context_limit: 100000
  max_concurrent: 10

retriever:
  beam_size: 3
  max_turns: 5
```

Override at runtime with environment variables:

```bash
LLM_MODEL=claude-opus-4-6 python your_script.py
```

---

## Retrieval Strategies

ConDB automatically selects the best retrieval strategy based on tree size:

| Strategy | Best for | How it works |
|----------|----------|--------------|
| **Beam** | Small trees (< 50 nodes) | LLM evaluates and selects promising branches at each depth level |
| **Block** | Large documents (50+ nodes) | Splits tree into token-bounded blocks, LLM reasons over each block |

You can also specify a strategy explicitly:

```python
result = db.query(tree_id, "question", strategy="block", beam_size=3)
```

---

## Benchmark

Two benchmarks live under `bench/`.

### Filesystem mode — SWEBench-FileTree

Runs on [`AmuroEita/SWEBench-FileTree`](https://huggingface.co/datasets/AmuroEita/SWEBench-FileTree),
a path-only version of SWE-bench code retrieval:

- 500 GitHub issues as queries
- 475 `(repo, commit)` repository snapshots as independent retrieval universes
- 58,058 file paths; no source code, no file summaries

Given an issue and one snapshot's file tree, return the file(s) the fix
touches. Specification: `notes/condb_swebench_filetree_bench.md`.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python bench/run_swebench_filetree.py --tier medium
```

Tiers:

```
strict   107 queries   sanity check (gold path appears in query text)
medium   133 queries   main report
loose    261 queries   fuzzy matching
full     500 queries   includes ~48% path-signal-less queries
```

Output goes to `bench/runs/<timestamp>__<tier>/`: `report.md`, `summary.json`,
`per_query.jsonl`.

### Document mode — single long document

Compares retriever algorithms (Block / Beam / Vertical / ...) on one
hierarchical document. Reports time, LLM calls, token usage with prompt
caching, and USD cost.

```bash
python bench/run_document_bench.py \
  --doc examples/large_doc.json \
  --config bench/queries.json
```

Queries live in the config JSON as `{"queries": ["...", "..."]}`. Swap in
any `--doc` and any `--config` to benchmark a different document.

---

## Architecture

```
contextdb/
├── api/
│   ├── condb.py          # ConDB — main entry point
│   └── context_tree.py   # ContextTree — tree indexing + query API
├── core/
│   └── storage.py        # TreeDB (SQLite), StorageProtocol
├── adapter/
│   └── base.py           # DocumentTree, ChatIndex, Generic adapters
├── retriever/
│   ├── base.py           # Retriever protocols
│   └── algorithm/        # Beam, Block retrieval strategies
├── llm.py                # LLMClient (Anthropic, OpenAI)
├── config/               # YAML configs for retrievers
└── prompts/              # Jinja2 prompt templates
```

---

## Extending

<details>
<summary><b>Custom Storage Backend</b></summary>

```python
from contextdb import StorageProtocol

class MyStorage:
    def get_node(self, tree_id, node_id): ...
    def get_children(self, tree_id, node_id): ...
    # implement StorageProtocol methods

ct = ContextTree(storage=MyStorage())
```
</details>

<details>
<summary><b>Custom LLM Provider</b></summary>

```python
from contextdb import LLMProtocol

class MyLLM:
    def chat(self, messages, system="", tools=None):
        return {"content": [...], "stop_reason": "..."}

ct = ContextTree("db.sqlite", llm=MyLLM())
```
</details>

---

## Testing

```bash
./run_tests.sh all
```

---

## Related Projects

- [**PageIndex**](https://github.com/VectifyAI/PageIndex) — one possible external producer of pageindex-compatible document trees
- [**AgentFS**](https://github.com/anthropics/agentfs) — filesystem for AI agents

---

## License

Apache-2.0
