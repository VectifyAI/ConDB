# Final candidate validation

Source identity:

- Candidate: `696f0d5d30f9bb6bcdb96ade8388e6bea36a92f9`
- Upstream base: `5d3b36cf3871846fe7894616e964cb520c11d473`
- Branch: `carsontung666/mongo:agent/condb-query-hotpath`

Builds completed successfully:

```text
bazel build --config=opt \
  //src/mongo/db:mongod \
  //src/mongo/s:mongos \
  //src/mongo/shell:mongo \
  //src/mongo/dbtests:dbtest \
  //src/mongo/db/query:count_query_bm
```

The five-target optimized build completed 7,650 actions. The independent
runtime-dassert build also completed successfully:

```text
bazel build --config=opt --dbg=True //src/mongo/dbtests:dbtest
```

Both `query_stage_count_scan` and `query_stage_count` passed under each build.
The four retained `dbtest_*.log` files are the complete command output; the
commands returned exit status zero.

The final-commit forced-classic resmoke matrix passed:

| Suite | Selected JS files | Passing report entries |
|---|---:|---:|
| `core` | 6 | 32 |
| `aggregation` | 2 | 12 |
| `no_passthrough` | 1 | 3 |
| `sharding` | 1 | 3 |

Report-entry counts include fixture and hook events; they are not counts of
independent JS tests. The retained JSON reports contain no non-pass entry.

The targeted unit coverage verifies the public `CountScan::work()` output
contract, the direct resultless path with skip/limit and save/restore/yield,
multikey materialization, and non-multikey compound-wildcard materialization.
The dedicated benchmark performs an untimed exact-count check and requires a
strict classic `COUNT -> COUNT_SCAN` winning plan before every timed process.

Repository checks also passed on the final patch: `quickmongolint`,
`clang-format --dry-run -Werror`, BUILD-file `buildifier` check, YAML parsing,
benchmark-suite selection, and `git diff --check`.

These are targeted checks, not a full Evergreen qualification matrix.
