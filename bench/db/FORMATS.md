# ConDB / PageIndex - JSON Format Inventory & Synthetic Generators

A survey of the **structurally-distinct JSON shapes** ConDB ingests/produces, and
synthetic generators for each. Different shapes stress different storage/retrieval
dimensions (the project plan's "categorize JSON types" step), so a fair DB
benchmark must cover more than the one canonical PageIndex tree.

Generators:
- `gen_pageindex.py` - the canonical document tree (format #1)
- `gen_formats.py`   - all the other shapes (`chatindex|filesystem|generic|embeddings|corpus|all`)

Every tree format is validated against the **real ContextDB adapter** that consumes
it (`DocumentTreeAdapter`, `ChatIndexAdapter`, `GenericAdapter`).

---

## The six shapes

### 1. PageIndex document tree  - `DocumentTreeAdapter` (`index_pageindex`)
Balanced recursive section tree. *Stresses:* nesting depth, summary/text payloads.

```json
{ "doc_name": "...", "doc_description": "...",
  "structure": [ { "node_id", "title", "summary",
                   "start_index", "end_index", "text",
                   "nodes": [ ...recursive... ] } ] }
```

### 2. ChatIndex conversation  - `ChatIndexAdapter` (`index_chatindex`)
Recursive **subtopics** plus per-topic **message arrays**.
*Stresses:* array cardinality (many messages per node), mixed nesting.

```json
{ "conversation_id": "...", "participants": ["user","assistant"],
  "topics": [ { "node_id", "title", "summary", "msg_start", "msg_end",
                "messages": [ {"role","content"}, ... ],
                "subtopics": [ ...recursive... ] } ] }
```

### 3. Filesystem directory tree  - `FileSystemAdapter` / `GenericAdapter`
`name` / `type` / `children` (list). High fan-out, mixed directory+file leaves.
*Stresses:* fan-out, heterogeneous leaf vs internal, `type` discriminator.

```json
{ "name": "root", "type": "directory",
  "children": [ { "name", "type": "directory", "children": [...] },
                { "name", "type": "file", "content": "..." } ] }
```

### 4. Generic deeply-nested JSON  - `GenericAdapter` (`index_generic`)
Arbitrary nesting; children may be a **dict OR a list**; heterogeneous attrs
(ints, floats, strings, arrays). *Stresses:* nesting depth, schema heterogeneity.

```json
{ "type":"object", "summary":"...", "author":"...", "score":0.42,
  "children": { "k0": { "type":"leaf", "content":"..." },
                "k1": { "type":"object", "children":[ ... ] } } }
```

### 5. Embeddings vectors  - internal (`embeddings.py` / `VectorPathRanker`)
`node_id -> float[dim]` (1536-dim, L2-normalized). No adapter; vector store data.
*Stresses:* wide numeric arrays, raw size (~14 KB/vector as JSON text).

```json
[ { "node_id": "000000", "vector": [0.013, -0.004, ...1536 floats...] }, ... ]
```

### 6. Flat corpus (JSONL)  - benchmark input (swebench-style)
One flat record per line. The **anti-tree** case: no nesting, huge row count.
*Stresses:* flat wide tables, ingest row throughput, metadata filtering.

```jsonl
{"_id":"repo:commit:path","repo":"acme/core","commit":"...","filepath":"...","title":"...","summary":"...","node_type":"file"}
```

(ConDB also has flat JSONL siblings: `queries.jsonl`, `qrels.jsonl`,
`instances.jsonl` - same flat-record shape, different fields.)

---

## Generated datasets

| format | file | scale | size | validation |
|---|---|---|---|---|
| pageindex (medium) | `data/medium.json`     | 70,843 nodes       | 85 MB   | DocumentTreeAdapter ok |
| pageindex (large)  | `data/large.json`      | 10,000,000 nodes   | 14.06 GB | DocumentTreeAdapter ok |
| chatindex          | `data/chatindex.json`  | 100k topics / 698k msgs | 207 MB | ChatIndexAdapter ok |
| filesystem         | `data/filesystem.json` | 43k nodes          | 20 MB   | GenericAdapter ok |
| generic            | `data/generic.json`    | 100k nodes         | 56 MB   | GenericAdapter ok |
| embeddings         | `data/embeddings.json` | 50k x 1536-dim     | ~720 MB | (vector store) |
| corpus (jsonl)     | `data/corpus.jsonl`    | 1,000,000 records  | 402 MB  | (flat benchmark) |

> Sizes are post-generation; embeddings number confirmed at run time. All
> content is random - only the *shape* matters. Reproduce any of them with
> `gen_formats.py <format> --scale <tiny|small|medium|large> --out <path>`
> (tree depth auto-scales to hit the target node count).

---

## Why this matters for the benchmark

Each shape exercises a different DB weakness:

- **chatindex** - large embedded arrays (`messages`) test how each engine stores
  and projects array fields (MongoDB native arrays vs PG JSONB arrays vs a join).
- **filesystem** - high fan-out tests wide parent->children fetches.
- **generic** - heterogeneous schema is MongoDB/JSONB's home turf and a pain for
  rigid columnar/relational layouts.
- **embeddings** - raw vector bulk; argues for a vector index / binary column
  rather than JSON (none of these four engines is a vector DB - that's the
  Pinecone/Qdrant/Weaviate column in the plan's competitor matrix).
- **corpus** - the flat, high-row-count case where columnar (DuckDB) and
  relational shine and document overhead hurts.

Next step (if wanted): extend `bench_databases.py` with per-shape flatteners so
the four engines are benchmarked on **all six** shapes, not just the doc tree.
