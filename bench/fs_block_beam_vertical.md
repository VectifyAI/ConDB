# Filesystem Retriever Comparison (Block vs Beam vs Vertical)

Data sources:
- `bench/fs_block_beam_vertical_context7.json`

Run setup: `fs_query_order=prefix`, `beam_size=3`, `max_turns=10`, total `5` queries on `context7` only.

## Overall (5 queries: context7)

| Retriever | Avg Time (s) | Avg LLM Calls | Total Cost (USD) | Hit@1 | Hit@10 |
|---|---:|---:|---:|---:|---:|
| Block | 5.4698 | 1.0000 | 0.0762 | 1.0000 | 1.0000 |
| Vertical | 7.3080 | 1.6000 | 0.1486 | 1.0000 | 1.0000 |
| Beam | 20.1798 | 4.6000 | 0.1328 | 0.6000 | 0.8000 |

Conclusion: on the current `context7` filesystem set, `Block` is still the best default. It keeps perfect `Hit@1` / `Hit@10`, while staying faster and cheaper than `Vertical`. `Beam` remains cheaper than `Vertical`, but it still loses clearly on accuracy.
