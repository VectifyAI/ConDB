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

### Latest Full Run

Claude Sonnet 4.6, `tier=all`, `strategy=block`, `ranker=none`, `top_k=10`.

Metric notes:

- `recall@gold`: fraction of gold files recovered when the cutoff is the
  query's gold-file count.
- `exact@gold`: all gold files are recovered within that same cutoff.
- `found@gold`: average number of gold files recovered within that cutoff.

| gold files | queries | cutoff | recall@gold | exact@gold | found@gold | avg returned |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 430 | 1 | 0.749 | 0.749 | 0.75 | 7.00 |
| 2 | 48 | 2 | 0.521 | 0.271 | 1.04 | 8.31 |
| 3 | 13 | 3 | 0.410 | 0.077 | 1.23 | 8.77 |
| 4 | 6 | 4 | 0.417 | 0.000 | 1.67 | 9.17 |
| 5 | 1 | 5 | 0.200 | 0.000 | 1.00 | 2.00 |
| 6+ | 2 | gold | 0.274 | 0.000 | 2.00 | 10.00 |

Full-set aggregate: `n=500`, `recall@gold=0.711`, `exact@gold=0.672`,
`MRR=0.805`, `nDCG@10=0.813`, `avg gold=1.24`, `avg returned=7.20`.
