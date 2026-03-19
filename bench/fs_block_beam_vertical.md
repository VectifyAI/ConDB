# Filesystem Retriever Comparison (Block vs Beam vs Vertical)

Data sources:
- `bench/fs_block_beam_vertical.json`
- `bench/fs_block_beam_vertical_context7.json`
- `bench/fs_block_beam_vertical_arxiv.json`
- `bench/fs_block_beam_vertical_repo.json`

Run setup: `fs_query_order=prefix`, `beam_size=3`, `max_turns=10`, total `11` queries (`context7=5, arxiv=3, repo=3`).

## Overall (11 queries)

| Scenario | Retriever | Avg Time (s) | Avg LLM Calls | Cost (USD) | Hit@1 | 
|---|---|---:|---:|---:|---:|
| context7 | Block | 5.4698 | 1.0000 | 0.0762 | 1.0000 | 
| context7 | Beam | 20.1798 | 4.6000 | 0.1328 | 0.6000 | 
| context7 | Vertical | 7.3080 | 1.6000 | 0.1486 | 1.0000 |
