# Benchmarks

## SWEBench-FileTree

Path-only code retrieval over `AmuroEita/SWEBench-FileTree`.

```bash
PYTHONPATH=. python bench/run_swebench_filetree.py \
  --tier all \
  --strategy block \
  --ranker none \
  --output-dir bench/runs/block_none_all
```

`--max-results` (default 50) is a safety cap; the retriever stops naturally
when the LLM signals `done=true`. The bench compares the returned set
against the gold set — no fixed top-K cutoff is applied at scoring time.

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

Claude Sonnet 4.6, `tier=all`, `ranker=none`, 500 queries, 0 failures.

Metrics are set-based on the actual returned file set — no top-K cutoff:

- `precision`: |returned ∩ gold| / |returned|
- `recall`: |returned ∩ gold| / |gold|
- `f1`: harmonic mean of precision and recall
- `exact_match`: returned set equals gold set exactly
- `MRR`: reciprocal rank of the first gold hit in the returned order

#### Block (ConDB) vs Vertical (baseline)

Vertical is a per-beam-branch variant: each parent expands its children into
separate subtree blocks (`A→B`, `A→C`), one LLM call per branch. It removes
the cross-branch view Block keeps and serves as a direct baseline.

| variant | precision | recall | F1 | exact_match | MRR | avg returned | avg latency |
|---|---:|---:|---:|---:|---:|---:|---:|
| Vertical (baseline) | 0.262 | 0.560 | 0.319 | 0.130 | 0.466 | 3.00 | ~24 s |
| **Block (ConDB)** | **0.410** | **0.903** | **0.534** | 0.106 | **0.849** | 2.86 | ~8 s |

Block recall jumps from 0.56 to 0.90 at ~3× lower latency. Low
`exact_match` on both sides reflects the retriever's tendency to return
~3 candidates against `avg_gold ≈ 1.24` — i.e. it picks up the gold file
plus one or two plausible neighbours.

#### Block — per-gold-count breakdown

| gold files | queries | precision | recall | F1 | exact_match | avg returned |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 430 | 0.399 | 0.951 | 0.537 | 0.112 | 2.82 |
| 2 | 48  | 0.469 | 0.677 | 0.544 | 0.062 | 3.12 |
| 3 | 13  | 0.477 | 0.487 | 0.481 | 0.154 | 3.00 |
| 4 | 6   | 0.581 | 0.500 | 0.532 | 0.000 | 3.50 |
| 5 | 1   | 0.333 | 0.200 | 0.250 | 0.000 | 3.00 |
| 6+ | 2  | 0.500 | 0.190 | 0.264 | 0.000 | 3.00 |

#### Vertical — per-gold-count breakdown

| gold files | queries | precision | recall | F1 | exact_match | avg returned |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 430 | 0.275 | 0.612 | 0.340 | 0.147 | 3.07 |
| 2 | 48  | 0.211 | 0.312 | 0.232 | 0.042 | 2.67 |
| 3 | 13  | 0.131 | 0.103 | 0.102 | 0.000 | 2.54 |
| 4 | 6   | 0.083 | 0.042 | 0.056 | 0.000 | 2.50 |
| 5 | 1   | 0.400 | 0.400 | 0.400 | 0.000 | 5.00 |
| 6+ | 2  | 0.000 | 0.000 | 0.000 | 0.000 | 0.00 |
