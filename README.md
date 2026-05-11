<div align="center">

<img src="https://docs.pageindex.ai/images/condb.png" alt="ConDB Banner" />

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

### Configuration

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

## 🔍 Retrieval Strategies

ConDB automatically selects the best retrieval strategy based on tree size:

| Strategy | Best for | How it works |
|----------|----------|--------------|
| **Beam** | Small trees <br/> (< 50 nodes) | LLM evaluates and selects promising branches at each depth level |
| **Block** | Large documents <br/> (50+ nodes) | Splits tree into token-bounded blocks, LLM reasons over each block. KV-cache native — caches intermediate block results to cut token usage by up to 70% |

You can also specify a strategy explicitly:

```python
result = db.query(tree_id, "question", strategy="block", beam_size=3)
```

---

## 📈 Benchmark Snapshot

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

Tiers (by retriever difficulty; lower difficulty = more path signal in query):

```
easy     107 queries   gold path appears in query text (sanity check)
medium   133 queries   gold filename appears in query    (main report)
hard     261 queries   gold module stem appears          (fuzzy matching)
all      500 queries   no filter, includes ~48% path-signal-less queries
```

Output goes to `bench/runs/<timestamp>__<tier>/`: `report.md`, `summary.json`,
`per_query.jsonl`.

#### Latest Run (Claude Sonnet 4.6, `--strategy block --ranker none`, top-k=10)

The cutoff for each query is its gold-file count: one-gold queries use top-1,
two-gold queries use top-2, and so on. The `6+` row contains one 6-gold query
and one 21-gold outlier.

| gold files | queries | cutoff | recall@gold | exact@gold | found@gold | avg returned |
|------------|--------:|-------:|------------:|-----------:|-----------:|-------------:|
| 1          | 430     | 1      | 0.749       | 0.749      | 0.75       | 7.00         |
| 2          | 48      | 2      | 0.521       | 0.271      | 1.04       | 8.31         |
| 3          | 13      | 3      | 0.410       | 0.077      | 1.23       | 8.77         |
| 4          | 6       | 4      | 0.417       | 0.000      | 1.67       | 9.17         |
| 5          | 1       | 5      | 0.200       | 0.000      | 1.00       | 2.00         |
| 6+         | 2       | gold   | 0.274       | 0.000      | 2.00       | 10.00        |

The full 500-query run aggregates to `recall@gold=0.711`, `exact@gold=0.672`,
`MRR=0.805`, `nDCG@10=0.813`, `avg gold=1.24`, and `avg returned=7.20`.

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

Licensed under [Apache 2.0](LICENSE).

© 2026 [Vectify AI](https://vectify.ai)
