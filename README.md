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

## Benchmark Snapshot

Current filesystem benchmark summary lives in [bench/fs_block_beam_vertical.md](bench/fs_block_beam_vertical.md).

Run setup: `fs_query_order=prefix`, `beam_size=3`, `max_turns=10`, `5` filesystem queries on `context7` only.

### Claude Opus 4.6

| Retriever | Avg Time (s) | Avg LLM Calls | Hit@1 | Hit@10 | Total Cost (USD) |
|---|---:|---:|---:|---:|---:|
| **Block** | 9.27 | 2.6 | 1.00 | 1.00 | 0.1416 |
| **Vertical** | 22.85 | 6.8 | 0.40 | 1.00 | 0.1682 |
| **Beam** | 18.37 | 5.0 | 0.60 | 1.00 | 0.1331 |

### Claude Sonnet 4.6

| Retriever | Avg Time (s) | Avg LLM Calls | Hit@1 | Hit@10 | Total Cost (USD) |
|---|---:|---:|---:|---:|---:|
| **Block** | 7.95 | 2.8 | 1.00 | 1.00 | 0.1670 |
| **Vertical** | 17.85 | 5.8 | 0.40 | 0.80 | 0.1438 |
| **Beam** | 17.41 | 4.8 | 0.60 | 1.00 | 0.1338 |

`Block` is the best default: perfect Hit@1 across both models. `Beam` and `Vertical` are sensitive to model version — `Block` is the most robust choice.

These numbers are benchmark snapshots, not hard guarantees; exact cost and latency will vary with model choice, provider pricing, prompt-cache behavior, and corpus shape.

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
