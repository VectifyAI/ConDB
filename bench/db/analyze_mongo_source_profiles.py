#!/usr/bin/env python3
"""Reduce MongoDB source-profile artifacts to a reproducible JSON summary."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any


ARMS = (
    "node_miss",
    "node_hit",
    "entity_miss",
    "entity_hit",
    "children_empty",
    "children_covered128",
    "children_noncovered128",
)

FUNCTIONS = {
    "find_command": "FindCmd::Invocation::run",
    "get_executor": "getExecutorFind",
    "query_planner": "QueryPlanner::plan(",
    "find_parse": "parsed_find_command::parse",
    "collection_acquisition": (
        "AutoGetCollectionForReadCommandBase<"
        "mongo::AutoGetCollectionForReadLockFree>::"
        "AutoGetCollectionForReadCommandBase"
    ),
    "canonicalize": "CanonicalQuery::canonicalize",
    "canonical_query_init": "CanonicalQuery::init(",
    "stage_builder": "ClassicStageBuilder::build",
    "executor_next": "PlanExecutorImpl::getNext",
    "idhack": "IDHackStage::doWork",
    "id_index_lookup": "SortedDataIndexAccessMethod::findSingle",
    "index_scan": "IndexScan::doWork",
    "covered_projection": "ProjectionStageCovered::transform",
    "keystring_decode": "KeyString::toBsonSafe",
    "fetch_stage": "FetchStage::doWork",
    "document_fetch": "WorkingSetCommon::fetch",
    "record_seek": "WiredTigerRecordStoreCursorBase::seekExact",
    "simple_projection": "ProjectionStageSimple::transform",
    "wiredtiger_index_next": "__wt_btcur_next",
    "wiredtiger_row_search": "__wt_row_search",
    "response_append": "CursorResponseBuilder::append",
}

SOURCE_REFERENCES = {
    "find_command": "src/mongo/db/commands/find_cmd.cpp:400-410,603-667",
    "hinted_cache_policy": "src/mongo/db/query/classic_plan_cache.cpp:124-143",
    "single_solution_planning": "src/mongo/db/query/get_executor.cpp:778-870",
    "idhack_construction": "src/mongo/db/query/get_executor.cpp:997-1069,1076-1087",
    "idhack_execution": "src/mongo/db/exec/idhack.cpp:82-151",
    "id_index_lookup": "src/mongo/db/index/index_access_method.cpp:493-507",
    "index_scan": "src/mongo/db/exec/index_scan.cpp:153-242",
    "fetch": (
        "src/mongo/db/exec/fetch.cpp:80-141;"
        "src/mongo/db/exec/working_set_common.cpp:74-124"
    ),
    "record_seek": (
        "src/mongo/db/storage/wiredtiger/"
        "wiredtiger_record_store.cpp:1994-2031"
    ),
    "index_next": (
        "src/mongo/db/storage/wiredtiger/wiredtiger_index.cpp:976-1025;"
        "src/third_party/wiredtiger/src/btree/bt_curnext.c:762-829"
    ),
    "projection": "src/mongo/db/exec/projection.cpp:266-326",
    "response_append": "src/mongo/db/query/cursor_response.h:83-101",
}

QUERY_COMM_RE = re.compile(
    r"^\s*([0-9.]+)%\s+[0-9.]+%\s+(conn[0-9]+)\s*$"
)
PERIOD_RE = re.compile(r"^\s*(\S+)\s+([0-9]+)\s*$")
EVENT_COUNT_RE = re.compile(r"^# Event count \(approx\.\): ([0-9]+)$")
LOST_RE = re.compile(r"^# Total Lost Samples: ([0-9]+)$")
INCLUSIVE_RE = re.compile(
    r"^\s*([0-9.]+)%\s+([0-9.]+)%\s+\S+\s+\[.\]\s+(.*)$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "run_dir",
        nargs="?",
        default=(
            Path(__file__).resolve().parent
            / "runs"
            / "source_breakdown_20260724_r3"
        ),
        type=Path,
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("/home/junyao/code/mongo-r7.0.34"),
    )
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def ensure_comm_report(run_dir: Path, arm: str) -> Path:
    output = run_dir / f"{arm}.perf-comm.txt"
    if output.exists():
        return output
    result = subprocess.run(
        [
            "perf",
            "report",
            "-i",
            str(run_dir / f"{arm}.perf.data"),
            "--stdio",
            "-g",
            "none",
            "--sort",
            "comm",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output.write_text(result.stdout)
    return output


def query_comm(path: Path) -> tuple[str, float]:
    for line in path.read_text(errors="replace").splitlines():
        match = QUERY_COMM_RE.match(line)
        if match:
            return match.group(2), float(match.group(1))
    raise ValueError(f"no query connection in {path}")


def sample_periods(path: Path) -> tuple[str, int, int, int]:
    process = subprocess.Popen(
        [
            "perf",
            "script",
            "-i",
            str(path),
            "-F",
            "comm,period",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    periods: dict[str, int] = {}
    samples: dict[str, int] = {}
    total_period = 0
    for line in process.stdout:
        match = PERIOD_RE.match(line)
        if not match:
            continue
        comm = match.group(1)
        period = int(match.group(2))
        periods[comm] = periods.get(comm, 0) + period
        samples[comm] = samples.get(comm, 0) + 1
        total_period += period
    stderr = process.stderr.read() if process.stderr is not None else ""
    return_code = process.wait()
    if return_code:
        raise RuntimeError(f"perf script failed for {path}: {stderr}")
    query_workers = {
        comm: period for comm, period in periods.items() if comm.startswith("conn")
    }
    if not query_workers:
        raise ValueError(f"no query connection in {path}")
    query_worker = max(query_workers, key=query_workers.get)
    return (
        query_worker,
        periods[query_worker],
        total_period,
        samples[query_worker],
    )


def event_metadata(path: Path) -> tuple[int, int]:
    event_count = None
    lost_samples = None
    for line in path.read_text(errors="replace").splitlines():
        if match := EVENT_COUNT_RE.match(line):
            event_count = int(match.group(1))
        if match := LOST_RE.match(line):
            lost_samples = int(match.group(1))
    if event_count is None or lost_samples is None:
        raise ValueError(f"incomplete perf metadata in {path}")
    return event_count, lost_samples


def inclusive_functions(path: Path) -> dict[str, dict[str, float]]:
    rows: list[tuple[float, float, str]] = []
    for line in path.read_text(errors="replace").splitlines():
        if match := INCLUSIVE_RE.match(line):
            rows.append(
                (
                    float(match.group(1)),
                    float(match.group(2)),
                    match.group(3),
                )
            )

    result: dict[str, dict[str, float]] = {}
    for label, needle in FUNCTIONS.items():
        matches = [row for row in rows if needle in row[2]]
        if matches:
            inclusive, self_percent, _ = max(matches)
            result[label] = {
                "inclusive_percent": inclusive,
                "self_percent": self_percent,
            }
    return result


def git_output(source_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=source_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def runtime_version(path: Path) -> tuple[str, str]:
    text = path.read_text()
    version_match = re.search(r"^db version v([0-9.]+)$", text, re.MULTILINE)
    git_match = re.search(r'"gitVersion": "([0-9a-f]{40})"', text)
    if not version_match or not git_match:
        raise ValueError(f"cannot parse runtime version from {path}")
    return version_match.group(1), git_match.group(1)


def main() -> None:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    source_dir = args.source_dir.resolve()
    manifest = read_json(run_dir / "manifest.json")
    if manifest.get("status") != "complete":
        raise ValueError("profile manifest is not complete")
    if tuple(sorted(manifest["arms"])) != tuple(sorted(ARMS)):
        raise ValueError("profile manifest does not contain all expected arms")

    arms: dict[str, Any] = {}
    for arm in ARMS:
        hotloop = read_json(run_dir / f"{arm}.hotloop.json")
        report_comm, _ = query_comm(ensure_comm_report(run_dir, arm))
        comm, query_period, total_period, query_samples = sample_periods(
            run_dir / f"{arm}.perf.data"
        )
        if report_comm != comm:
            raise ValueError(
                f"query worker mismatch for {arm}: {report_comm} != {comm}"
            )
        query_percent = query_period * 100.0 / total_period
        event_count, lost_samples = event_metadata(
            run_dir / f"{arm}.perf-query.txt"
        )
        arms[arm] = {
            "iterations": hotloop["iterations"],
            "rows": hotloop["rows"],
            "plan": hotloop["plan"],
            "query_thread": comm,
            "query_thread_percent": round(query_percent, 5),
            "query_thread_sample_count": query_samples,
            "query_thread_sample_period": query_period,
            "total_sample_period": total_period,
            "approximate_event_count_from_report": event_count,
            "lost_samples": lost_samples,
            "query_cycles_per_command": round(
                query_period / hotloop["iterations"], 3
            ),
            "inclusive_functions": inclusive_functions(
                run_dir / f"{arm}.perf-query-inclusive.txt"
            ),
        }

    cycles = {
        arm: data["query_cycles_per_command"] for arm, data in arms.items()
    }
    deltas = {
        "node_hit_minus_miss_cycles_per_command": (
            round(cycles["node_hit"] - cycles["node_miss"], 3)
        ),
        "entity_hit_minus_miss_cycles_per_command": (
            round(cycles["entity_hit"] - cycles["entity_miss"], 3)
        ),
        "children_covered_minus_empty_cycles_per_child": round(
            (
                cycles["children_covered128"]
                - cycles["children_empty"]
            )
            / 128,
            3,
        ),
        "children_noncovered_minus_covered_cycles_per_child": round(
            (
                cycles["children_noncovered128"]
                - cycles["children_covered128"]
            )
            / 128,
            3,
        ),
        "children_noncovered_minus_empty_cycles_per_child": round(
            (
                cycles["children_noncovered128"]
                - cycles["children_empty"]
            )
            / 128,
            3,
        ),
    }

    public_commit = git_output(source_dir, "rev-parse", "HEAD")
    source_message = git_output(
        source_dir, "show", "-s", "--format=%B", "HEAD"
    )
    origin_match = re.search(
        r"^GitOrigin-RevId: ([0-9a-f]{40})$",
        source_message,
        re.MULTILINE,
    )
    if not origin_match:
        raise ValueError("source tag lacks GitOrigin-RevId")
    binary_version, binary_git_version = runtime_version(
        run_dir / "runtime-version.txt"
    )
    if origin_match.group(1) != binary_git_version:
        raise ValueError("public source tag does not match running binary")

    result = {
        "run": {
            **manifest,
            "profiling_before": read_json(
                run_dir / "profiling-before.json"
            ),
            "profiling_during": read_json(
                run_dir / "profiling-during.json"
            ),
            "profiling_after": read_json(
                run_dir / "profiling-after.json"
            ),
        },
        "source": {
            "tag": git_output(source_dir, "describe", "--tags", "--exact-match"),
            "public_commit": public_commit,
            "git_origin_revision": origin_match.group(1),
            "runtime_version": binary_version,
            "runtime_git_version": binary_git_version,
            "mapping": (
                "The official public tag records the running binary's "
                "gitVersion as GitOrigin-RevId."
            ),
            "references": SOURCE_REFERENCES,
        },
        "measurement_notes": {
            "cycles_per_command": (
                "Sum of perf sample period for the hot query thread, divided "
                "by completed commands"
            ),
            "inclusive_percentages": (
                "Call-graph inclusive percentages overlap and must not be summed."
            ),
            "scope": (
                "The query-thread period includes user-space, kernel, network, "
                "and host-hook work sampled on that thread."
            ),
            "replication": (
                "This is one warmed source-localization run, not a replicated "
                "latency estimate."
            ),
            "children_confound": (
                "The empty, covered, and noncovered arms differ in filter, "
                "projection, and index shape. Their cycle differences are "
                "descriptive contrasts, not pure per-child or FETCH effects."
            ),
        },
        "arms": arms,
        "paired_deltas": deltas,
        "root_causes": {
            "get_node": (
                "The compound hinted point lookup rebuilds the classic find "
                "plan and stage tree per command. The hit adds little work "
                "over the miss in this query shape, so hit-dependent fetch and "
                "output are secondary to its fixed command and planning path. "
                "This MongoDB-only profile does not assign the full "
                "cross-engine latency gap to planning."
            ),
            "get_entity": (
                "IDHACK skips general QueryPlanner::plan but constructs a "
                "fresh IDHackStage and projection inside the full find "
                "command path. Hit and miss are indistinguishable in this run, "
                "so no fetch premium is measurable at this payload."
            ),
            "get_children": (
                "The empty scan pays the same fixed classic command and "
                "planning path. Each returned child then adds index-cursor "
                "advance, KeyString decode, projection, and response append; "
                "only the noncovered arm contains FetchStage and record-store "
                "seek work. Because the arms also differ in index and filter "
                "shape, the paired difference localizes the noncovered "
                "execution bundle rather than a pure FETCH cost."
            ),
        },
    }
    output = run_dir / "source_breakdown.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(output)


if __name__ == "__main__":
    main()
