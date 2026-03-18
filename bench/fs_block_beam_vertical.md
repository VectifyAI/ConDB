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
| arxiv | Block | 4.4765 | 1.0000 | 0.0386 | 1.0000 | 
| arxiv | Beam | 15.0650 | 3.0000 | 0.0567 | 0.3333 | 
| arxiv | Vertical | 5.2354 | 1.0000 | 0.0384 | 1.0000 | 
| repo | Block | 9.0874 | 1.6667 | 1.0502 | 1.0000 |
| repo | Beam | 33.5101 | 4.3333 | 0.0948 | 1.0000 |
| repo | Vertical | 25.5759 | 2.6667 | 1.7665 | 1.0000 | 
