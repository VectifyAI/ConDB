# Not materializing index keys when the only consumer is a FETCH

**Status: specified and its ceiling measured, not implemented.** No production code was written and
nothing is pushed. This covers the **child expansion** and **subtree retrieval** operations from the
report.

Base commit in `base_commit.txt` (`0561c098b99a` lineage).

## The finding

`IndexScan::doWork` calls the index cursor with the default `KeyInclusion::kInclude`
(`index_scan.cpp:100,117,126,147,154`), so `wiredtiger_index.cpp:1009` runs `key_string::toBson` for
every key and `index_scan.cpp:266-277` stores the result on the `WorkingSetMember`.

On an `IXSCAN → FETCH` plan that BSON has no consumer. `WorkingSetCommon::fetch` clears `keyData`
unread at `working_set_common.cpp:192`, and the only thing that would have read it is the yield-time
index/document consistency check at `:154`, which is skipped entirely when no yield occurred:

```cpp
if (memberKey.snapshotId == currentSnapshotId) {
    continue;
}
```

Two facts make this a real candidate rather than a theory:

1. **`KeyInclusion::kExclude` already exists and is honored.** `sorted_data_interface.h:364-367`
   defines it, and `wiredtiger_index.cpp:1001-1013` skips the whole decode under it. **`CountScan`
   already passes it** (`count_scan.cpp:127,129`), so this is a per-stage decision with in-tree
   precedent, not a cross-cutting change.
2. **Upstream asks for exactly this.** `working_set_common.cpp:146-147`:
   `// TODO provide a way for the query planner to opt out of this checking if it is unneeded due to
   the structure of the plan.`

An earlier audit dismissed this on the grounds that the key BSON is load-bearing for the consistency
check. That reasoning is wrong. The check does not consume BSON — it re-encodes the stored BSON
*back* into a `key_string::Value` to look up in a `KeyStringSet` (`working_set_common.cpp:181-185`).
The current path is KeyString → BSON → KeyString, and the storage layer already had the KeyString.
SBE never materializes key BSON at all (`sbe/stages/ix_scan.cpp:258` uses `nextKeyValueView()`).

## Measured ceiling

The A/B needs no new code: `sorted_data_interface_bm.cpp:249-252` already registers the same cursor
advance under both settings. Built as
`//src/mongo/db/storage/wiredtiger:storage_wiredtiger_record_store_and_index_bm`, one pinned CPU,
5 repetitions, medians:

| Index | `kExclude` | `kInclude` | Ratio | Saved |
|---|---|---|---|---|
| non-unique | 5,215,663 ns | 9,953,005 ns | 0.5240 | **47.6%** |
| unique | 5,093,290 ns | 10,812,723 ns | 0.4710 | **52.9%** |

Not materializing the key roughly **halves the cost of advancing the cursor**.

Read this as a ceiling on the per-key component, not as a query-level projection. This benchmark
measures the storage cursor in isolation; a real `find` also does document fetch, filtering,
projection and reply assembly, none of which this touches. It also reports CPU time rather than
retired instructions — that harness does not use the PMU wrapper the query benchmarks do — so it is
subject to the same clock-versus-work caveat recorded for the CountScan campaign.

## What implementing it requires

The key cannot simply be dropped: if a yield does occur, the consistency check needs it. The design
is to keep the `key_string::Value` the storage layer already produced instead of a BSON round trip.

1. A flag on `IndexScanParams` / `IndexScanNode`, defaulting to false.
2. `classic_stage_builder.cpp` sets it only when the scan's sole consumer is a `FETCH` — which
   requires no covered projection, no sort-key generation, no shard filter, no `returnKey`, no
   `AND_HASH`/`AND_SORTED`, no `TEXT_OR`, and no index-only result path.
3. `IndexScan` uses `nextKeyString()` / `seekForKeyString()` and stores a `key_string::Value` on
   `IndexKeyDatum` (additive field; existing consumers only run when the flag is false).
4. `WorkingSetCommon::fetch` compares that value directly, dropping the `HeapBuilder` re-encode.

Reachability was checked for the two target patterns: single-interval exact bounds make `_checker`
null (`index_scan.cpp:105`), exact bounds elide the residual filter (`planner_access.cpp:1148`),
`addKeyMetadata` is false without `returnKey` (`planner_access.cpp:743`), and `shouldDedup` is false
for a non-multikey index (`query_solution.cpp:733`). It does **not** apply to a covered projection,
which reads `keyData` directly (`projection.cpp:268`).

## Why it was not implemented here

The correctness of the change rests entirely on step 2 — deciding when the flag is safe. There are
at least seven consumers of `keyData` (`projection.cpp:268`, `working_set.cpp:136`,
`orphan_chunk_skipper.cpp:188-198`, `return_key.cpp`, `and_common.h:60-70`, `text_or.cpp:277-278`,
`plan_executor_impl.cpp:759-765`), spread over 73 references in 23 files, and setting the flag where
any of them can run is a silent wrong-results bug rather than a crash. That deserves a careful
planner change and a broad resmoke matrix, not a rushed one.

The ceiling above is recorded so that work starts from a measured number rather than a hypothesis.
