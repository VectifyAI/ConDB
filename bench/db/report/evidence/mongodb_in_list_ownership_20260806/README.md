# Sharing the filter's BSON with `$in` lists

Base commit `0561c098b99ac5e929005e70a2e37d7a97a82423`. Candidate in `commit.txt`, branch
`agent/condb-in-list-ownership` on `carsontung666/mongo`, draft PR #2. Full diff in `change.diff`.

This is the **entity fetch** operation from the report: fetching a set of documents by a list of
known IDs, i.e. an `$in` over an indexed field.

## The problem

`IndexBoundsBuilder` wants owned BSON so its intervals can point directly at the `$in` array. When
`InListData::isBSONOwned()` is false it clones the whole `InMatchExpression` and calls
`makeBSONOwned()`, which copies the array into a fresh buffer and then remaps three vectors of
`BSONElement` onto the new addresses. `StageBuilderState::makeOwnedInList` does the same for SBE, so
there are **two** O(n) copies on the same condition — the measurement below cannot separate them.

That condition is not an edge case. `BSONElement::embeddedObject()` constructs
`BSONObj(value(), LargeSizeTrait{})` with no owned buffer, so `isOwned()` is false for the array of
**every** `$in` the parser produces from a command.

## The change

The bytes are already owned. `FindCommandRequest` holds the filter for the lifetime of the
`CanonicalQuery`, and the `$in` array lives inside it. `MatchExpressionParser::parse` now hands each
`$in` list shared ownership of the filter it was parsed from: a refcount instead of an O(n) copy,
with nothing to remap because the bytes do not move.

**Sharing trades a bounded copy for an unbounded pin**, and that needed a guard. An `InListData` can
outlive its query — SBE stores it in `PlanStageStaticData::inLists`, which reaches the plan cache,
where single-solution entries are *pinned* and the budget estimator does not account for `inLists`
at all. So a small list inside a large filter would pin the whole filter indefinitely. Sharing is
therefore declined when the filter is more than a kilobyte larger than the list itself. That keeps
the win where the list dominates the filter and leaves the pathological case on the copying path.

`shareBSONOwnershipWith()` also verifies the array really lies inside the object it is being tied
to, and declines on a list someone else already holds rather than cloning it away from them.

## Proof that the mechanism fires

Four unit tests in `expression_parser_leaf_test.cpp`:

- `InListSharesOwnershipOfAnOwnedFilter` — asserts `isBSONOwned()` becomes true **and** that the
  storage is the same bytes as the filter's array (`objdata()` pointer equality), so this is sharing
  and not a copy.
- `InListSurvivesTheFilterGoingOutOfScope` — parses inside a lambda so the caller's `BSONObj` is
  destroyed, then reads the elements back. This is the assertion that makes the change safe.
- `InListDoesNotShareOwnershipOfAnUnownedFilter` — unowned filter, no sharing.
- `InListDoesNotShareOwnershipOfAMuchLargerFilter` — exercises the size guard.

Against the unpatched base, with only the parser hunk reverted and the tests kept, the suite reports
**202 TOTAL / 200 PASS / 2 FAIL** (`on_base.log.gz`): the sharing and lifetime tests fail, the two
negative tests pass. That is the discrimination the set is for.

## Effect

`point_query_bm.cpp` gains `UniqueFieldInListQuery`, an `$in` over the unique field with the list
length as a third benchmark argument. Both binaries built from the same tree, one pinned CPU, 7
repetitions, **medians** of retired user-space instructions:

| `$in` length | Base | Patched | Ratio | Saved |
|---|---|---|---|---|
| 10 | 252,529 | 249,983 | 0.9899 | 1.01% (2,546) |
| 100 | 308,693 | 305,179 | 0.9886 | 1.14% (3,514) |
| 1000 | 835,181 | 820,170 | 0.9820 | 1.80% (15,011) |

The saving grows with list length, which is the shape an O(n) copy predicts. The two larger points
imply roughly 13 instructions per element; three points are too few to fit an intercept with
confidence, so no fixed/variable decomposition is claimed.

**A correction worth recording.** An earlier run of this benchmark reported 2.11% / 2.02% / 2.13%,
and those numbers were published in a first version of this commit and PR. They were measured while
the machine was under continuous build load. Re-running both binaries back to back under identical
conditions gives the table above, and a re-run of the *earlier* binary now reproduces the patched
figure to within one instruction (249,982 against 249,983, `inlist_patched_rerun.json`) — so the
difference was the machine, not the code. The lower numbers are the ones to use.

**Negative control.** `UniqueFieldPointQuery`, which has no `$in` and so cannot be affected, was run
on all three binaries (`ctrl_*.json`): 106,773 base, 106,493 without the size guard, 106,348 with
it. All within 0.4%, which bounds how much binary layout can contribute and confirms the 2,546 to
15,011 instruction differences above are not a layout artifact.

## Non-intrusion

| Suite | Result | Artifact |
|---|---|---|
| `expression_parser_test` | 202/202 | `expression_parser_test.log.gz` |
| `db_matcher_test` | 480/480 | `db_matcher_test.log.gz` |
| `index_bounds_builder_test` | 237/237 | `index_bounds_builder_test.log.gz` |
| resmoke core: `in.js`, `in2.js`, `in3.js`, `in4.js`, `index_bounds_number_edge_cases.js`, `explain_multi_plan.js` | 32/32 | `resmoke_core.json` |

## Limits

Retired instructions, not latency. One host, one pinned core, warm cache, in-process, single
threaded, and the benchmark registers only one thread where the rest of that file sweeps a thread
range. The benchmark collection holds 10 documents, so the ratios are specific to it and only the
absolute savings transfer; a 1000-element `$in` against a 10-key index also performs many seeks that
match nothing, which inflates the baseline. No sharded, 7.0.34 or production claim.

The saving cannot be attributed to `IndexBoundsBuilder` alone: `makeOwnedInList` removes an O(n)
copy on the same condition and no profile was taken to separate them.

The tree walk added to `parse` traverses every parsed filter to benefit only those containing `$in`.
It is recursive, like the parser it follows, and bounded by the same BSON depth limit.

No wider resmoke matrix was run, so the suites above are a lower bound on coverage.
