# Benchmarks

## SWEBench-FileTree

Path-only code retrieval over `AmuroEita/SWEBench-FileTree`.

```bash
PYTHONPATH=. python bench/run_swebench_filetree.py --tier all --strategy block --ranker none --top-k 10 --output-dir bench/runs/block_none_all
```

Outputs:

- `summary.json`: aggregate metrics
- `per_query.jsonl`: per-query predictions and metrics
- `report.md`: markdown report
- `bench.sqlite`: temporary benchmark database

### Latest Full Run

Claude Sonnet 4.6, `tier=all`, `strategy=block`, `ranker=none`, `top_k=10`.
Results are from `bench/runs/swe_all_20260504_135806/block_none_all_repaired`.

| tier | n | hit@1 | hit@3 | hit@5 | hit@10 | MRR | nDCG@10 | avg LLM calls | avg turns | avg latency |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| easy | 107 | 0.794 | 0.850 | 0.916 | 0.981 | 0.846 | 0.837 | 2.49 | 1.69 | 6.79s |
| medium | 133 | 0.812 | 0.872 | 0.925 | 0.985 | 0.861 | 0.851 | 2.38 | 1.64 | 6.71s |
| hard | 261 | 0.808 | 0.862 | 0.908 | 0.977 | 0.853 | 0.849 | 2.41 | 1.65 | 6.70s |

Full-set aggregate: `n=500`, `hit@1=0.746`, `hit@10=0.962`, `MRR=0.805`,
`nDCG@10=0.813`.

BM25 was not a win in the prior full-run comparison. It improved 27 queries and
worsened 32 by RR or hit@10. The main positive cases were path namespace
disambiguation; the main negative cases were already solved by the existing
block ordering.
