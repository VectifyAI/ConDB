# Behavioural validation of the candidate

This directory holds the correctness and non-intrusion evidence for the candidate. It is **not**
part of the pre-registered performance protocol and nothing in it is read by `analyze.py`; it was
gathered on 2026-08-06 between 01:38 and 02:30, before the performance campaign, and is retained
here because the report cites it.

## Which commit this was run on

Everything here was run on `ac20554faaf2e7ab6e1b2e2aad2a81308fae82cd` against a separately built
base `0561c098b99ac5e929005e70a2e37d7a97a82423` — the same pinned base as the campaign's arm A.

`ac20554f` is **not** the campaign's arm C (`90814b83d3e55f099c1244266d86700b5f633972`). The two
differ by exactly one line, a `clang-format` include reorder:

```
$ git diff ac20554f 90814b83
 src/mongo/db/exec/classic/count.cpp | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)

-#include "mongo/db/exec/classic/count_scan.h"
 #include "mongo/base/checked_cast.h"
+#include "mongo/db/exec/classic/count_scan.h"
```

No statement, declaration or build setting differs. This gap is disclosed rather than closed: the
validation was not re-run after the reorder.

`change.diff` is the full `0561c098..ac20554f` diff as it was at capture time, and
`binaries-head.txt` records the binaries used.

## What was run

`commands.json` records every command with its working directory, exit code, elapsed time and
result line. `provenance.json` carries a SHA-256 and byte size for every artifact in the source
directory, including the ones not copied here.

**dbtests.** `query_stage_count_scan` 14/14 and `query_stage_count` 7/7, passing in both an
optimized build (`dbtest-opt.log.gz`, `dbtest-opt-persuite.log.gz`) and a runtime-`dassert` build
(`dbtest-dbg.log.gz`, built `--config=opt --dbg=True`). The `dassert` build matters because
`WorkingSet::get()` checks its argument only under `dassert`, so a leaked `INVALID_ID` would trip
there and not in release. No fault injection was done to prove that guard would actually fire; the
claim rests on the `kDebugBuild` marker showing it is compiled in.

**Forced-classic resmoke**, `resmoke/`. 60 result entries, all `pass`:

| Report | Files | Entries |
|---|---|---|
| `core.json` | 8 | 42 |
| `aggregation.json` | 2 | 12 |
| `no_passthrough.json` | 1 | 3 |
| `sharding.json` | 1 | 3 |

The eight core files are `profile/profile_count.js`, `index/index_count_scan.js`,
`index/wildcard/compound_wildcard_index_count.js`, `index/wildcard/wildcard_index_count.js`,
`query/count/count_scan_memory_limit.js`, `query/explain/explain_count.js`,
`query/explain/explain_multi_plan_count.js` and `query/explain/explain_multikey.js`.
`profile_count.js` is the one that checks indexed-count `keysExamined` and the `COUNT_SCAN` plan
summary directly. Full resmoke output is in the `.log.gz` files beside each report; the JSON
reports carry result names, statuses and timestamps but do not themselves embed the invocation or
the binary hash, which is what `commands.json` and `provenance.json` are for.

**Explain parity**, `explain_parity.js` + `diff_capture.py`. `explain("executionStats")` and
profiler output were captured from a base `mongod` and from the changed `mongod`, then compared
leaf field by leaf field:

- `diff-base-vs-head.txt` — 542 leaf fields on each side, **4 differing, 0 substantive**. All four
  are `queryPlanner.optimizationTimeMicros`.
- `diff-head-run1-vs-run2.txt` — the *same binary* run twice differs on 3 of those same 4 fields,
  which is what establishes them as timing noise rather than a behavioural change.
- `diff-base-vs-head-run2.txt` — the second head run against base, for completeness.

Two shapes in the capture carry specific weight. The count-like aggregation shape
(`hasCountStage: false`) runs a bare `CountScan` as the executor root with no `CountStage` above it,
so it dereferences the returned `WorkingSetID` unconditionally; it returns `nReturned: 200` on both
binaries, so the opt-in does not leak to it. The multikey shapes match exactly at 401 keys
examined, 200 advanced and 200 `needTime`, which covers the deduplication and memory accounting
that sit immediately above the changed branch.

## Reading the artifacts

```bash
zcat resmoke/core.log.gz | less
python3 -c "import json;d=json.load(open('resmoke/core.json'));print(len(d['results']))"
cat diff-base-vs-head.txt
```
