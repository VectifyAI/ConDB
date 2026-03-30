<div align="center">

<img src="assets/banner.png" alt="ConDB Banner" />

<br/>

# ConDB: The KV-Cache Native Context Database

<p align="center"><i>A new context database for reasoning-driven retrieval via tree search.<br/>
Fast, context-aware retrieval at scale with up to 70% less token cost.</i></p>

</div>

---

## 🌲 What is ConDB?

**ConDB** (Context Database) is a tree-structured context database that uses LLM-powered **reasoning-based retrieval** via tree search instead of vector similarity — no vector DB, no chunking. It accepts [PageIndex](https://github.com/VectifyAI/PageIndex)-compatible document trees, [ChatIndex](https://github.com/VectifyAI/ChatIndex) conversation trees, filesystem trees, and custom hierarchical JSON — with no runtime dependency on either. The LLM reasons over the tree, like a human expert using a table of contents, to locate relevant content.

### Why not vector search?

- **Similarity ≠ relevance** — vector search retrieves what looks similar, not what is truly relevant. Similar-looking chunks may differ in intent (low accuracy), while truly relevant information may be expressed in very different language and get missed entirely (low recall). True relevance requires reasoning
- **Chunking breaks semantic continuity** — documents must be split into fixed-size segments to fit embedding models, causing context fragmentation that destroys their natural structure and cross-section relationships
- **Retrieval is blind to context** — embedding models encode the query alone, ignoring conversational history, user intent, and other contextual signals

ConDB replaces this with **reasoning-based tree search**: the LLM performs node-level relevance classification over a hierarchical index, incorporating full context — making retrieval adaptive, explainable, and traceable.

### What makes ConDB different

- **Fast tree search at scale** — reasoning-driven tree search with block partitioning and parallel processing, supporting complex, context-aware retrieval over large hierarchical structures
- **KV-cache native** — the first database designed around LLM KV-cache reuse. By caching intermediate results during tree search, ConDB reduces token usage by up to 70% with no loss in accuracy. The same efficiency gains extend to memory systems for long-context reasoning at scale
- **Unified long-context infrastructure** — a single system for both static and dynamic long-context workloads

### Static long context
Structured, persistent knowledge — documents (via [PageIndex](https://github.com/VectifyAI/PageIndex)), file systems, and codebases. Scalable retrieval within large, organized hierarchies.

### Dynamic long context
Evolving, runtime context — agent memory, long conversations (via [ChatIndex](https://github.com/VectifyAI/ChatIndex)), and autoresearch. Systems can continuously update, retrieve, and reason over newly generated information.

### Key capabilities

- **Hierarchical storage** — document trees, chat trees, and custom hierarchical JSON in SQLite
- **Multiple retrieval strategies** — beam search for small trees, block retrieval for large documents
- **Multi-provider LLM support** — Anthropic (Claude) and OpenAI (GPT) out of the box
- **Extensible** — plug in custom storage backends, LLM providers, or retrieval strategies

---

## 🚀 Getting Started

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
db.set_llm(provider="anthropic", model="claude-sonnet-4-20250514")

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

### Configuration

Create a `.env` file:

```
ANTHROPIC_API_KEY=sk-...
OPENAI_API_KEY=sk-...
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-20250514
```

Or configure programmatically:

```python
from contextdb.config import Config
llm = Config.get_llm_client()
```

---

## 🔍 Retrieval Strategies

ConDB automatically selects the best retrieval strategy based on tree size:

| Strategy | Best for | How it works |
|----------|----------|--------------|
| **Beam** | Small trees (< 50 nodes) | LLM evaluates and selects promising branches at each depth level |
| **Block** | Large documents (50+ nodes) | Splits tree into token-bounded blocks, LLM reasons over each block. KV-cache native — caches intermediate block results to cut token usage by up to 70% |

You can also specify a strategy explicitly:

```python
result = db.query(tree_id, "question", strategy="block", beam_size=3)
```

---

## 📈 Benchmark Snapshot

Current filesystem benchmark summary lives in [bench/fs_block_beam_vertical.md](bench/fs_block_beam_vertical.md).

Run setup for the snapshot below: `beam_size=3`, `max_turns=10`, `5` filesystem queries on `context7` only.

### Block vs Beam vs Vertical

| Retriever | Avg Time (s) | Avg LLM Calls | Hit@1 | Hit@10 | Total Cost (USD) |
|---|---:|---:|---:|---:|---:|
| **Block** | 5.47 | 1.00 | 1.00 | 1.00 | 0.0762 |
| **Vertical** | 7.31 | 1.60 | 1.00 | 1.00 | 0.1486 |
| **Beam** | 20.18 | 4.60 | 0.60 | 0.80 | 0.1328 |

`Block` is the best default on this `context7` snapshot: same retrieval quality as `Vertical`, with lower latency and fewer model calls. `Beam` is still workable, but it trails clearly on retrieval accuracy.

These numbers are benchmark snapshots, not hard guarantees; exact cost and latency will vary with model choice, provider pricing, prompt-cache behavior, and corpus shape.

---

## 🧩 Learn More

### Architecture

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

### Extending

**Custom Storage Backend**

```python
from contextdb import StorageProtocol

class MyStorage:
    def get_node(self, tree_id, node_id): ...
    def get_children(self, tree_id, node_id): ...
    # implement StorageProtocol methods

ct = ContextTree(storage=MyStorage())
```

**Custom LLM Provider**

```python
from contextdb import LLMProtocol

class MyLLM:
    def chat(self, messages, system="", tools=None):
        return {"content": [...], "stop_reason": "..."}

ct = ContextTree("db.sqlite", llm=MyLLM())
```

### Testing

```bash
./run_tests.sh all
```

---

## 💬 Community

### Related Projects

- [**PageIndex**](https://github.com/VectifyAI/PageIndex) — vectorless, reasoning-based RAG that builds hierarchical tree indexes from long documents
- [**ChatIndex**](https://github.com/VectifyAI/ChatIndex) — tree indexing for long conversations, enabling reasoning-based retrieval over chat histories
- [**AgentFS**](https://github.com/anthropics/agentfs) — filesystem for AI agents

### Connect with Us

[![Twitter](https://img.shields.io/badge/Twitter-000000?style=for-the-badge&logo=x&logoColor=white)](https://x.com/PageIndexAI)&ensp;
[![LinkedIn](https://img.shields.io/badge/LinkedIn-0077B5?style=for-the-badge&logo=linkedin&logoColor=white)](https://www.linkedin.com/company/vectify-ai/)&ensp;
[![Discord](https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.com/invite/VuXuf29EUj)&ensp;
[![Contact Us](https://img.shields.io/badge/Contact_Us-3B82F6?style=for-the-badge&logo=envelope&logoColor=white)](https://ii2abc2jejf.typeform.com/to/tK3AXl8T)

---

[Apache-2.0](LICENSE) · © 2026 [Vectify AI](https://vectify.ai)
