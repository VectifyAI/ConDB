# ContextDB

SQLite storage for hierarchical context trees. Works with [PageIndex](https://github.com/VectifyAI/PageIndex).

## Install

```bash
pip install -r requirements.txt
pip install pageindex  # optional
```

## Usage

```python
from contextdb import ContextTree, LLMClient

# Create LLM client
llm = LLMClient("anthropic", api_key="sk-...", model="claude-sonnet-4-20250514")
# or
llm = LLMClient("openai", api_key="sk-...", model="gpt-4")

# Initialize
ct = ContextTree("context.sqlite", llm=llm)

# Index documents
tree_id = ct.index_pdf_file("doc.pdf")       # requires pageindex
tree_id = ct.index_markdown_file("doc.md")   # requires pageindex
tree_id = ct.index_pageindex(json_data)      # direct JSON

# View structure
print(ct.format_tree_view(tree_id, depth=2))

# Query with LLM
result = ct.query(tree_id, "What are the main topics?", use_llm=True, max_turns=5)
print(result.contents)

ct.close()
```

## Config

Create `.env`:
```
ANTHROPIC_API_KEY=sk-...
OPENAI_API_KEY=sk-...
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-20250514
```

Or use `Config`:
```python
from contextdb.config import Config
llm = Config.get_llm_client()
```

## Structure

```
contextdb/
├── core/
│   └── storage.py      # StorageProtocol, TreeDB
├── adapter/
│   └── base.py         # BaseAdapter, PageIndexAdapter, ChatIndexAdapter
├── retriever/
│   └── base.py         # RetrieverProtocol, LLMRetriever, ManualRetriever
├── llm.py              # LLMProtocol, LLMClient
└── api/
    └── context_tree.py # ContextTree
```

## Extending

**Custom Storage:**
```python
from contextdb import StorageProtocol

class MyStorage:
    def get_node(self, tree_id, node_id): ...
    def get_children(self, tree_id, node_id): ...
    # implement StorageProtocol methods

ct = ContextTree(storage=MyStorage())
```

**Custom LLM:**
```python
from contextdb import LLMProtocol

class MyLLM:
    def chat(self, messages, system="", tools=None):
        # return {"content": [...], "stop_reason": "..."}
        ...

ct = ContextTree("db.sqlite", llm=MyLLM())
```

## Test

```bash
./run_tests.sh all
```

## License

MIT
