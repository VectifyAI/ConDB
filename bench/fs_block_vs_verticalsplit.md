# Filesystem Retriever 对比（Block vs Vertical）

数据来源：`bench/fs_block_vs_verticalsplit.json`，仅保留 `context7` 场景。

| Retriever | Queries | Avg Time (s) | Avg LLM Calls | Actual Cost (USD) | Saved vs NoCache | Hit@1 | Hit@10 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Block | 5/5 | 5.0093 | 1.0000 | 0.0789 | 46.85% | 1.0000 | 1.0000 |
| VerticalSplit | 5/5 | 5.6363 | 1.0000 | 0.0780 | 47.03% | 1.0000 | 1.0000 |

结论：在 `context7` 这组 filesystem bench 里，`Block` 和 `VerticalSplit` 准确率相同；`Block` 略快，`VerticalSplit` 略便宜，差距很小。
