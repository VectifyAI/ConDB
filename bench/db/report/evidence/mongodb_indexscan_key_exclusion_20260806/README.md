# Not materializing index keys when only a FETCH will read them

**Status: implemented and correct, but with no established performance win. Not proposed.**

Branch `agent/condb-ixscan-key-exclusion`, commit in `commit.txt`, on base `0561c098b99a`. Full diff
in `change.diff`. Nothing is pushed to a pull request, because the effect does not reproduce.

This covers **child expansion** and **subtree retrieval** from the report.

## The idea, and why it looked strong

`IndexScan` decodes every index key to BSON. On an `IXSCAN → FETCH` plan nobody reads it:
`WorkingSetCommon::fetch` clears `keyData` once its post-yield consistency check is done, so no
stage above the FETCH can see it, and that check is the only reader below. Upstream has a TODO
asking for exactly this opt-out (`working_set_common.cpp:146-147`), `CountScan` already passes
`KeyInclusion::kExclude`, and the check does not even consume BSON — it re-encodes the stored BSON
*back* into a `key_string::Value` to look up in a `KeyStringSet`, so the current path is
KeyString → BSON → KeyString.

The storage-layer ceiling is large. `sorted_data_interface_bm.cpp:249-252` already registers the
same cursor advance under both settings, so no new code was needed to measure it
(`sorted_data_interface_advance.json`, one pinned CPU, 5 repetitions, medians):

| Index | `kExclude` | `kInclude` | Ratio | Saved |
|---|---|---|---|---|
| non-unique | 5,215,663 ns | 9,953,005 ns | 0.5240 | 47.6% |
| unique | 5,093,290 ns | 10,812,723 ns | 0.4710 | 52.9% |

Not materializing the key roughly halves the cost of advancing the cursor.

## What was built

`FetchStage` tells a direct `IXSCAN` child that nothing above will read its keys — the same local
parent-tells-child pattern `CountStage` uses with `CountScan`, rather than a planner flag that could
be set wrongly. `IndexScan` applies it only when it also has no bounds checker, no filter and no key
metadata to produce, which are the only other readers of the key inside the stage, so the whole
condition is decided locally. It then uses `nextKeyString()` / `seekForKeyString()`, stores the
`key_string::Value` on `IndexKeyDatum`, and `WorkingSetCommon::fetch` compares it directly.

## Correctness: confirmed

**It fires exactly where intended and nowhere else.** A temporary diagnostic (removed before commit,
output retained in `diag_scan.log.gz` and `diag_cov.log.gz`) counted:

| Benchmark | Fired | Total | Why |
|---|---|---|---|
| `UniqueFieldRangeScan` (IXSCAN → FETCH) | 217 | 217 | parent is a FETCH |
| `UniqueFieldRangeScanCovered` | 0 | 127 | parent is `PROJECTION_COVERED`, which reads the key |

**It survives the path it rewrites.** 42 core jstest entries pass with
`internalQueryExecYieldIterations: 1`, which forces a yield on every iteration and so makes the
consistency check — the only consumer of the retained key — run constantly
(`resmoke_forced_yield.json`). `query_stage_ixscan` 6/6 and `query_stage_fetch` 2/2 also pass.

## Effect: not established, and probably not there

Two pairs of binaries, built from identical production source and differing only in which collection
sizes the benchmark registers, disagree:

| Measurement | Range scan (IXSCAN → FETCH) | Covered control |
|---|---|---|
| Pair 1 (`rs_*.json`) | 0.9799 — 2.01% saved | 1.0036 |
| Pair 1 re-run (`rs3_*.json`) | 0.9799 | 1.0033 |
| Pair 2 (`rs2_*.json`, 10k docs) | 1.0063 — 0.63% **worse** | 1.0041 |

Pair 1 reproduces exactly on re-run, so this is not run-to-run noise; the two *builds* genuinely
differ. Difference-in-differences against the covered control does not reconcile them either
(2.4% saved versus 0.2% worse).

The likely reason is mechanical, and it is the useful finding here. The storage layer's `kExclude`
wins by **producing nothing**. But the consistency check still needs per-key data to survive across
`work()` calls, so this change has to retain something — a `key_string::Value` from
`SortedDataKeyValueView::getValueCopy()` (`sorted_data_interface.h:644-647`), which allocates and
copies. It trades a BSON build for a KeyString copy rather than removing the work. The 47.6% storage
ceiling therefore does **not** transfer to a FETCH plan, and anyone reading that number as a
projection for this change would be misled.

## What would have to change to capture it

The win is only available to a consumer that needs nothing per key. That is `CountScan`'s situation,
not `FETCH`'s. Capturing it here would mean removing the need to retain the key at all — for
instance by having the FETCH re-derive consistency from the document rather than the stored key, or
by making the retained form a non-owning view whose lifetime is guaranteed by something other than
the WorkingSetMember. Both are larger changes than this one and neither was attempted.

The branch is kept because the correctness scaffolding — the local parent-tells-child condition, the
firing diagnostic, and the forced-yield test matrix — is what any future attempt would need anyway.
