# Benchmarks

## SWEBench-FileTree

Path-only code retrieval over `AmuroEita/SWEBench-FileTree`.

```bash
PYTHONPATH=. python bench/run_swebench_filetree.py \
  --tier all \
  --strategy block \
  --ranker none \
  --top-k 10 \
  --output-dir bench/runs/block_none_all
```

Outputs:

- `summary.json`: aggregate metrics
- `per_query.jsonl`: per-query predictions and metrics
- `report.md`: markdown report
- `bench.sqlite`: temporary benchmark database

Strategies (`--strategy`):

- `auto`: pick `beam` for ≤50 nodes, `block` otherwise.
- `beam`: depth-first LLM beam expansion (small trees).
- `block`: token-bounded block partitioning with cross-block merge.
- `vertical`: baseline — per-beam-branch subtree blocks, no cross-branch view.

Ranker options (apply to `block` / `vertical`):

- `--ranker none`: preserve traversal and block-local LLM order.
- `--ranker bm25`: lexical path ordering for cross-block merge candidates.
- `--ranker vector`: embedding path ordering for cross-block merge candidates;
  configure with `--embedding-provider` and `--embedding-model`.

### Latest Full Run

Claude Sonnet 4.6, `tier=all`, `ranker=none`, `top_k=10`, 500 queries, 0 failures.

Metric notes:

- `recall@gold`: fraction of gold files recovered when the cutoff is the
  query's gold-file count.
- `exact@gold`: all gold files are recovered within that same cutoff.
- `found@gold`: average number of gold files recovered within that cutoff.

#### Block (ConDB) vs Vertical (baseline)

Vertical is a per-beam-branch variant: each parent expands its children into
separate subtree blocks (`A→B`, `A→C`), one LLM call per branch. It removes
the cross-branch view that Block keeps, so it serves as a direct baseline
for the merged-pool design used in ConDB.

| variant | recall@gold | exact@gold | MRR | nDCG@10 | avg returned | avg latency |
|---|---:|---:|---:|---:|---:|---:|
| Vertical (baseline) | 0.382 | 0.366 | 0.466 | 0.481 | 3.00 | ~24 s |
| **Block (ConDB)** | **0.711** | **0.672** | **0.805** | **0.813** | 7.20 | ~8 s |

Block is **+0.33 recall@gold** at ~3× lower latency.

#### Block — per-gold-count breakdown

| gold files | queries | cutoff | recall@gold | exact@gold | found@gold | avg returned |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 430 | 1 | 0.749 | 0.749 | 0.75 | 7.00 |
| 2 | 48 | 2 | 0.521 | 0.271 | 1.04 | 8.31 |
| 3 | 13 | 3 | 0.410 | 0.077 | 1.23 | 8.77 |
| 4 | 6 | 4 | 0.417 | 0.000 | 1.67 | 9.17 |
| 5 | 1 | 5 | 0.200 | 0.000 | 1.00 | 2.00 |
| 6+ | 2 | gold | 0.274 | 0.000 | 2.00 | 10.00 |

#### Vertical — per-gold-count breakdown

| gold files | queries | cutoff | recall@gold | exact@gold | found@gold | avg returned |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 430 | 1 | 0.412 | 0.412 | 0.41 | 3.07 |
| 2 | 48 | 2 | 0.250 | 0.125 | 0.50 | 2.67 |
| 3 | 13 | 3 | 0.103 | 0.000 | 0.31 | 2.54 |
| 4 | 6 | 4 | 0.042 | 0.000 | 0.17 | 2.50 |
| 5 | 1 | 5 | 0.400 | 0.000 | 2.00 | 5.00 |
| 6+ | 2 | gold | 0.000 | 0.000 | 0.00 | 0.00 |
