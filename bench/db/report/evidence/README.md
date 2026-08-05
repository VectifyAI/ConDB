# Report evidence bundles

## Current MongoDB source experiment

`mongodb_master_countscan_20260805_4109dcc31ff6/` is the canonical evidence for the report's
pinned-master direct CountScan experiment. It compares three arms built from one pinned base
commit with a byte-identical benchmark harness overlaid, so that only six production files differ
between them:

| Arm | Production source | Meaning |
|---|---|---|
| A | `0561c098b99ac5e929005e70a2e37d7a97a82423` | pinned upstream production files, common harness overlaid |
| B | `4109dcc31ff6df595c6b2e5caf3fbce077c488ba` | an earlier heavyweight implementation, rejected in review |
| C | `90814b83d3e55f099c1244266d86700b5f633972` | the minimal candidate |

Arm B is retained as a measured arm rather than discarded. It does everything arm C does and more —
a template on the base class of every classic stage, two `friend` declarations, a second entry point,
and a devirtualization of the two per-iteration calls between `CountStage` and `CountScan` that the
candidate deliberately does not take — so C/B answers what that additional machinery actually bought.
Because the extra work is not a superset of C's mechanism but a different one layered beside it, C/B
measures the two together and does not isolate either. Note the frozen `campaign.json` and
`analyze.py` describe arm B as "a strict mechanistic superset of the candidate". That holds of its
*effect* — B's saving is C's plus a flat per-iteration term — but not of its *mechanism*, and the
protocol is left unaltered. That comparison is registered as a
noninferiority test and reported as a complexity trade-off; it deliberately does not veto adoption,
which is decided by C/A, the controls, and CPU non-regression on C/A.

Five workloads run over 30 blocks, giving 450 fresh processes: three indexed-count endpoints, a
point-query negative control, and a count whose plan is `COUNT -> FETCH -> IXSCAN` so the
optimization cannot fire. The last is the only workload where a regression could hide, and its band
is two-sided because on a path neither arm touches an apparent improvement is as diagnostic of an
uncontrolled difference as a regression is.

Arm A is **not** an unmodified upstream binary: it carries the same benchmark harness as the other
arms, including an evidence-only point-query control that is not part of the MongoDB pull request.

The directory name ends in `4109dcc31ff6`, which was the candidate when the bundle was created but
is now arm B. The name is deliberately left alone: three pre-registrations are anchored to pushed
commits that reference this path, and renaming it would break the one external record that shows
the protocol was fixed before any measurement. Read the arm table above, not the directory name.

The bundle contains the pre-registered `campaign.json`, the analyzer and runner, the attested
builder, the protocol test suite, the three arm production patches and the common harness patch,
per-arm eleven-file source manifests pinned to commit-derived Git blob identities, a build
attestation covering the `C1 -> A -> B -> C2` build sequence, an append-only attempt ledger, the
raw benchmark JSON and logs for every process, and the analysis summary.

The attempt ledger records three attempts. Attempt 001 was superseded before execution and attempt
002 aborted on its first process, both without producing a single completed benchmark process or any
retained measurement; they are retained because deleting a failed attempt is exactly what the ledger exists
to prevent. Each preregistration is anchored to a pushed commit, since a locally held ledger cannot
prove its own completeness.

Two things the ledger's own notes say, which matter for reading the result. The protocol was pushed
40 seconds before the first benchmark process, but the attested build smokes had already run all five
workloads at campaign size on all three arms, so the per-arm instruction levels were visible when it
was frozen; the campaign establishes the intervals and the block-level consistency, not the
discovery. And attempt 003's anchor citation is wrong — see `anchor_correction.json`, which gives the
correct commit, the check that distinguishes them, and a record of a mistake made while filing the
correction.

`validation/` holds the correctness and non-intrusion evidence — dbtests, forced-classic resmoke, and
a field-by-field `explain` comparison against a separately built base binary. It is not part of the
pre-registered protocol, `analyze.py` does not read it, and it was run on a commit that differs from
arm C by one include-order line; its own README states all three.

`validation/review_fix_equivalence/` covers the gap between the commit the campaign measured
(`90814b83d3e5`, arm C) and the commit now under review (`90781b36b2`), which carries the fixes from
a later blank review. It holds the complete diff, a re-smoke of the reviewed binary at campaign size,
and the comparison showing every endpoint difference is smaller than arm C's own process-to-process
spread. The intervals stay pinned to the measured commit.

`analyze_controls_posthoc.py` explains why both controls fell outside their bands. It was written
after the results were known, is likewise not pre-registered, and cannot change the adoption gate; it
exists so the figures the report quotes for that explanation are reproducible rather than asserted.

Adoption gates read one interval family only: a stratified log-ratio Welch-t interval. The
percentile bootstrap in the same bundle is a sensitivity output that no gate or claim may read, and
its keys are suffixed `_not_for_citation` for that reason.

To re-check the bundle without re-running anything:

```bash
cd mongodb_master_countscan_20260805_4109dcc31ff6
python3 test_protocol.py     # protocol and statistics tests, no third-party package required
python3 analyze.py           # revalidates every artifact and reproduces summary.json
```

`analyze.py` exits non-zero if the adoption gate did not pass, so its exit status is meaningful.

## Superseded MongoDB source experiments

These two bundles are retained **unchanged** for auditability. Their internal files have not been
edited. They are historical and must not be used for the canonical report's source-experiment
numbers or validation claims.

- `mongodb_master_countscan_20260805_696f0d5d30f9/` — the two-arm activation ablation on an earlier
  candidate. Both of its arms were built from the same candidate commit and shared the
  implementation and harness, so it isolated activation rather than comparing the candidate with
  upstream. Superseded by the three-arm campaign above.
- `mongodb_master_countscan_20260805/` — the earlier ten-pair campaign, itself already superseded by
  the bundle above.

Any figure quoted from these two bundles describes a different candidate commit, a different base,
and a different comparison, and must not be presented as evidence for the current candidate.
