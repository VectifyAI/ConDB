# Report evidence bundles

## Current MongoDB source experiment

`mongodb_master_countscan_20260805_4109dcc31ff6/` is the canonical evidence for the report's
pinned-master direct CountScan experiment. It compares three arms built from one pinned base
commit with a byte-identical benchmark harness overlaid, so that only six production files differ
between them:

| Arm | Production source | Meaning |
|---|---|---|
| A | `0561c098b99ac5e929005e70a2e37d7a97a82423` | pinned upstream production files, common harness overlaid |
| B | `1b7362ef8c07e1870efafd0531e9ae0c5b7054e7` | previous implementation |
| C | `4109dcc31ff6df595c6b2e5caf3fbce077c488ba` | final candidate |

Arm A is **not** an unmodified upstream binary: it carries the same benchmark harness as the other
arms, including an evidence-only point-query control that is not part of the MongoDB pull request.

The bundle contains the pre-registered `campaign.json`, the analyzer and runner, the attested
builder, the protocol test suite, the three arm production patches and the common harness patch,
per-arm eleven-file source manifests pinned to commit-derived Git blob identities, a build
attestation covering the `C1 -> A -> B -> C2` build sequence, an append-only attempt ledger, the
raw benchmark JSON and logs for every process, and the analysis summary.

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
