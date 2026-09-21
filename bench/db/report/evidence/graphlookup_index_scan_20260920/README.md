# `$graphLookup` covered tree-BFS A/B

Patched mongod on `:57027`, branch `graphlookup-index-scan`, gate
`internalQueryEnableGraphLookupIndexScan`. Harness
`bench/db/bench_graphlookup_index_scan.py`.

| file | SHA-256 | status |
|---|---|---|
| `graphlookup_from_root_d1to5.json` | `fc38c2b6a1f51fe83b093fdab831df1606b946717244ecd7f90f3e3081c653bb` | shipping: from tree root `n000000`, D=1..5 (tree height 5) |
| `graphlookup_depths_1to5.json` | `f08d45387103dfa202e66098b9eb566b9631628156a74af18430358359b051b1` | 16-root cohort D=1..5; D=4/D=5 median stalls at leaves |
| `graphlookup_covered_bfs_80k.json` | `24f91a686f0c469dcafe3d09181ecd812350badae4d46f0537a54a8a82f95917` | earlier D=1..3 covered BFS |
| `graphlookup_index_scan_cursor_80k.json` | `ab0d73953b7c7c07d0a2d0373b1cf26d8093e3e3cd95bb22521668b21bd3d999` | superseded: one cursor + FETCH |
| `graphlookup_index_scan_80k.json` | `fa9a34fd1ce1a3a3896939fd3adf3d960ce3048726981c95c522213ede288eb5` | superseded: per-`PlanExecutor` |

The 80k tree is height **5** (86% of nodes at depth 5). Shipping numbers for a
directory walk are `graphlookup_from_root_d1to5.json` (root `n000000`, D=1..5).
D=4 **−72.3%**, D=5 whole tree **−95.3%** vs stock `$graphLookup`, 0 mismatches.
The 16-root cohort file includes D=4/D=5 but its median stalls at leaves.

Write-up: `bench/db/report/ops/get_children_depth.md` §6.
