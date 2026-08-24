"""Check issues/003 against the gated express profile and L10 campaign JSON.

Drives the retained artifacts the note cites. Fails if the note's inclusive
shares do not match perf_express, or if it treats the L3 planned-path frames
as the remaining 60 µs breakdown.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTE = (
    ROOT
    / "bench/db/report/evidence/get_children_remaining_mongod_20260820/README.md"
)
INCLUSIVE = (
    ROOT
    / "bench/db/runs/getchildren_plancache_20260809/perf_express/children.inclusive.txt"
)
EXCLUSIVE = (
    ROOT
    / "bench/db/runs/getchildren_plancache_20260809/perf_express/children.exclusive.txt"
)
MONGOD_LOG = ROOT / "bench/db/runs/getchildren_plancache_20260809/perf_express/mongod.log"
CAMPAIGN = [
    ROOT / "bench/db/runs/getchildren_plancache_20260810" / f"dedup_rot{i}.json"
    for i in range(3)
]


def inclusive_pct(path: Path, needle: str) -> float:
    hits = []
    for line in path.read_text(errors="replace").splitlines():
        if needle not in line:
            continue
        m = re.match(r"\s*([0-9]+\.[0-9]+)%\s+([0-9]+\.[0-9]+)%", line)
        if m:
            hits.append(float(m.group(1)))
    if not hits:
        raise AssertionError(f"no inclusive row matching {needle!r} in {path}")
    return hits[0]


def note_table_pct(note: str, frame_substr: str) -> float:
    for line in note.splitlines():
        if not line.startswith("|") or frame_substr not in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[1].endswith("%"):
            return float(cells[1].rstrip("%"))
    raise AssertionError(f"no table row for {frame_substr!r} in the note")


def main() -> int:
    assert NOTE.is_file(), f"missing note {NOTE}"
    assert INCLUSIVE.is_file(), f"missing gated profile {INCLUSIVE}"
    note = NOTE.read_text()

    assert "no further mongod change" in note.lower() or (
        "No further mongod change" in note
    ), "note must state that no further mongod change clears the bar"
    assert "No experiment is warranted" in note
    assert "perf_express/children.inclusive.txt" in note
    assert "60–61" in note or "60-61" in note

    checks = [
        ("FindCmd::Invocation::run", "FindCmd::Invocation::run"),
        ("_doOneIteration", "_doOneIteration"),
        ("CollectionImpl::findDoc", "findDoc"),
        ("PlanExecutor::getNextBatch", "getNextBatch"),
    ]
    print("inclusive shares (artifact vs note)")
    for art_needle, note_needle in checks:
        art = inclusive_pct(INCLUSIVE, art_needle)
        documented = note_table_pct(note, note_needle)
        print(f"  {art_needle}: artifact {art:.2f}%  note {documented:.2f}%")
        assert abs(art - documented) < 0.05, (
            f"{art_needle}: artifact {art} vs note {documented}"
        )

    find_cmd = inclusive_pct(INCLUSIVE, "FindCmd::Invocation::run")
    iteration = inclusive_pct(INCLUSIVE, "_doOneIteration")
    find_doc = inclusive_pct(INCLUSIVE, "CollectionImpl::findDoc")
    next_batch = inclusive_pct(INCLUSIVE, "PlanExecutor::getNextBatch")
    denom = 60.5
    print(
        f"scaled to {denom} µs: FindCmd {find_cmd/100*denom:.1f}  "
        f"findDoc {find_doc/100*denom:.1f}  getNextBatch {next_batch/100*denom:.1f}"
    )
    print(
        f"FindCmd / _doOneIteration = {find_cmd:.2f}/{iteration:.2f} = "
        f"{find_cmd/iteration:.3f}; outside FindCmd {iteration-find_cmd:.2f} points"
    )

    gated = INCLUSIVE.read_text(errors="replace")
    assert "QueryPlanner::plan" not in gated
    assert "PlanExecutorImpl::getNextBatch" not in gated
    # The note may name those frames only to say they belong to L3 / are absent.
    l3_mentions = [
        line
        for line in note.splitlines()
        if "QueryPlanner::plan" in line or "PlanExecutorImpl" in line
    ]
    assert l3_mentions, "note should contrast L3 frames with the gated capture"
    for line in l3_mentions:
        assert any(
            w in line.lower()
            for w in ("absent", "not l3", "ungated", "l3")
        ), f"L3 frame cited without contrast: {line}"

    log = MONGOD_LOG.read_text(errors="replace")
    assert "internalQueryEnableExpressPrefixScan" in log
    assert '"true"' in log or "true" in log

    excl = EXCLUSIVE.read_text(errors="replace")
    assert "__wt_row_search" in excl

    probe_medians = []
    for path in CAMPAIGN:
        d = json.loads(path.read_text())
        med = statistics.median(b["probe"]["server_cpu_us"] for b in d["blocks"])
        probe_medians.append(med)
        print(f"  {path.name} probe median {med:.2f} µs")
    assert all(59.0 <= m <= 62.0 for m in probe_medians), probe_medians
    assert "PrefixScanViaUserIndex" in gated

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
