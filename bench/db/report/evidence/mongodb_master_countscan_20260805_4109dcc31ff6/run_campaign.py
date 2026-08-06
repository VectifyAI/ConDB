#!/usr/bin/env python3

"""Execute the fixed 288-process three-arm CountScan benchmark campaign.

The runner refuses to start unless the frozen campaign, its protocol and source artifacts,
the build attestation, and the append-only attempt ledger all validate. Once started it runs
every pre-registered process in order without early stopping, and it appends exactly one
terminal outcome record to the ledger whether the attempt succeeds or fails.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import analyze

EVIDENCE_DIR = Path(__file__).resolve().parent
CAMPAIGN_PATH = EVIDENCE_DIR / "campaign.json"
RAW_DIR = EVIDENCE_DIR / "raw"
LOG_DIR = EVIDENCE_DIR / "logs"
RUN_RECORD_PATH = EVIDENCE_DIR / "campaign_run.json"
PARTIAL_JOURNAL_PATH = EVIDENCE_DIR / "campaign_partial.json"
LEDGER_PATH = EVIDENCE_DIR / "attempt_ledger.jsonl"
SUMMARY_PATH = EVIDENCE_DIR / "summary.json"
ATTEMPT_ARCHIVE_DIR = EVIDENCE_DIR / "attempts"

ENVIRONMENT_KEYS = (
    "PATH",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "LD_BIND_NOW",
    "MALLOC_ARENA_MAX",
    "TCMALLOC_",
    "LANG",
    "LC_ALL",
    "TZ",
    "OMP_NUM_THREADS",
)


def now() -> str:
    return datetime.now().astimezone().isoformat()


def fail(message: str) -> None:
    raise RuntimeError(message)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, payload: dict[str, Any], *, refuse_existing: bool = False) -> None:
    if refuse_existing and path.exists():
        fail(f"refusing to overwrite existing file: {path}")
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    if temporary.exists():
        fail(f"refusing to overwrite stale atomic-write temporary: {temporary}")
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        if refuse_existing and path.exists():
            fail(f"target appeared during atomic write: {path}")
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def append_ledger_record(record: dict[str, Any]) -> str:
    """Append one hash-chained ledger record and return the new tail digest."""
    existing = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    previous = analyze.ledger_line_digest(existing[-1]) if existing else analyze.GENESIS_LEDGER_DIGEST
    chained = {**record, "previous_record_sha256": previous}
    line = json.dumps(chained, sort_keys=True)
    with LEDGER_PATH.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(LEDGER_PATH.parent)
    return analyze.ledger_line_digest(line)


# ---------------------------------------------------------------------------
# Host state
# ---------------------------------------------------------------------------


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def governor(cpu: int) -> str:
    path = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        fail(f"cannot read the required CPU governor from {path}: {exc}")
    if not value:
        fail(f"CPU governor is empty: {path}")
    return value


def cpu_frequency_khz(cpu: int) -> int | None:
    value = read_text(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq")
    return int(value) if value.isdigit() else None


def cpu_stat(cpu: int) -> tuple[int, int]:
    """Return (total jiffies, idle jiffies) for one logical CPU from /proc/stat."""
    prefix = f"cpu{cpu} "
    for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            fields = [int(value) for value in line.split()[1:]]
            idle = fields[3] + fields[4]
            return sum(fields), idle
    fail(f"/proc/stat has no entry for cpu{cpu}")
    raise AssertionError("unreachable")


def busy_fraction(before: tuple[int, int], after: tuple[int, int]) -> float:
    total_delta = after[0] - before[0]
    idle_delta = after[1] - before[1]
    # A failed, empty, or counter-reset sample must not read as a perfectly idle CPU: for the SMT
    # sibling that would silently satisfy the contention upper bound. Report full contention.
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        return 1.0
    return min(max((total_delta - idle_delta) / total_delta, 0.0), 1.0)


def parse_cpu_list(value: str) -> list[int]:
    """Expand a Linux CPU list such as '0,3-5' into [0, 3, 4, 5]."""
    cpus: set[int] = set()
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            low, _, high = chunk.partition("-")
            if not low.isdigit() or not high.isdigit():
                fail(f"cannot parse CPU list entry {chunk!r}")
            cpus.update(range(int(low), int(high) + 1))
        elif chunk.isdigit():
            cpus.add(int(chunk))
        else:
            fail(f"cannot parse CPU list entry {chunk!r}")
    return sorted(cpus)


def loadavg() -> list[float]:
    return [float(value) for value in read_text("/proc/loadavg").split()[:3]]


def cpu_field(name: str) -> str:
    for line in read_text("/proc/cpuinfo").splitlines():
        if line.startswith(name):
            return line.split(":", 1)[1].strip()
    return ""


def numa_topology() -> str:
    nodes = read_text("/sys/devices/system/node/online")
    parts = [f"online={nodes}"]
    for entry in sorted(Path("/sys/devices/system/node").glob("node*")):
        cpulist = read_text(str(entry / "cpulist"))
        if cpulist:
            parts.append(f"{entry.name}={cpulist}")
    return " ".join(parts)


def turbo_state() -> str:
    no_turbo = read_text("/sys/devices/system/cpu/intel_pstate/no_turbo")
    boost = read_text("/sys/devices/system/cpu/cpufreq/boost")
    return f"intel_pstate.no_turbo={no_turbo or 'NA'} cpufreq.boost={boost or 'NA'}"


def shared_libraries(path: str) -> str:
    result = subprocess.run(["ldd", path], check=False, capture_output=True, text=True)
    return result.stdout.strip() or result.stderr.strip() or "static or unreadable"


def environment_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for key, value in sorted(os.environ.items()):
        if any(key == allowed or key.startswith(allowed) for allowed in ENVIRONMENT_KEYS):
            snapshot[key] = value
    return snapshot


def collect_runtime_provenance(campaign: dict[str, Any]) -> dict[str, Any]:
    attestation = analyze.load_json(analyze.BUILD_ATTESTATION_PATH)
    selected = campaign["execution"]["cpu_affinity"]
    sibling = campaign["execution"]["sibling_cpu"]
    siblings = read_text(f"/sys/devices/system/cpu/cpu{selected}/topology/thread_siblings_list")
    sibling_list = parse_cpu_list(siblings)
    if sibling_list != sorted({selected, sibling}):
        fail(f"CPU {selected} thread siblings are {sibling_list}, expected {sorted({selected, sibling})}")
    return {
        "cpu_model": cpu_field("model name") or "unknown",
        "microcode": cpu_field("microcode") or "unknown",
        "kernel": platform.release(),
        "libc": attestation["toolchain"]["libc"],
        "bazel_version": attestation["toolchain"]["bazel_version"],
        "compiler_version": attestation["toolchain"]["compiler_version"],
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "selected_cpu": selected,
        "smt_sibling_cpu": sibling,
        "selected_cpu_thread_siblings": sibling_list,
        "numa_topology": numa_topology(),
        "turbo_state": turbo_state(),
        "governors": {str(cpu): governor(cpu) for cpu in (selected, sibling)},
        "binary_shared_libraries": {
            arm: shared_libraries(campaign["arms"][arm]["binary_path"]) for arm in analyze.ARMS
        },
        "environment": environment_snapshot(),
        "scheduler_policy": f"SCHED policy {os.sched_getscheduler(0)}, nice {os.nice(0)}",
        "build_attestation_sha256": analyze.sha256_file(analyze.BUILD_ATTESTATION_PATH),
    }


def idle_preflight(campaign: dict[str, Any]) -> dict[str, Any]:
    gates = campaign["execution"]["runtime_gates"]
    selected = campaign["execution"]["cpu_affinity"]
    sibling = campaign["execution"]["sibling_cpu"]
    sample_seconds = gates["preflight_sample_seconds"]
    cooldown = gates["cooldown_seconds_before_campaign"]
    limit = gates["preflight_max_busy_fraction_selected_and_sibling"]

    print(f"cooling down for {cooldown} s before the idle preflight", flush=True)
    time.sleep(cooldown)
    before = {cpu: cpu_stat(cpu) for cpu in (selected, sibling)}
    load_before = loadavg()
    time.sleep(sample_seconds)
    after = {cpu: cpu_stat(cpu) for cpu in (selected, sibling)}
    selected_busy = busy_fraction(before[selected], after[selected])
    sibling_busy = busy_fraction(before[sibling], after[sibling])
    if selected_busy > limit:
        fail(f"selected CPU {selected} is {selected_busy:.4f} busy, above the pre-frozen idle gate {limit}")
    if sibling_busy > limit:
        fail(f"SMT sibling CPU {sibling} is {sibling_busy:.4f} busy, above the pre-frozen idle gate {limit}")
    return {
        "cooldown_seconds": cooldown,
        "sample_seconds": sample_seconds,
        "selected_cpu_busy_fraction": selected_busy,
        "sibling_cpu_busy_fraction": sibling_busy,
        "loadavg": load_before,
        "loadavg_after_sample": loadavg(),
        "measured_at": now(),
    }


# ---------------------------------------------------------------------------
# Binary and artifact verification
# ---------------------------------------------------------------------------


def read_build_id(path: Path) -> str:
    readelf = shutil.which("readelf")
    if readelf is None:
        fail("required command not found: readelf")
    result = subprocess.run([readelf, "-n", str(path)], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"readelf failed for {path}: {result.stderr.strip()}")
    match = re.search(r"Build ID:\s*([0-9a-fA-F]+)", result.stdout)
    if match is None:
        fail(f"no readable GNU Build ID in {path}")
    return match.group(1).lower()


def binary_stat(path: Path) -> dict[str, int]:
    try:
        value = path.stat()
    except OSError as exc:
        fail(f"cannot stat binary {path}: {exc}")
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def verify_binary(campaign: dict[str, Any], arm: str) -> tuple[dict[str, str], dict[str, int]]:
    config = campaign["arms"][arm]
    path = Path(config["binary_path"])
    if not path.is_absolute():
        fail(f"arm {arm} binary path is not absolute: {path}")
    if str(path) in analyze.FORBIDDEN_BINARY_PATHS:
        fail(f"arm {arm} points at a provisional reference snapshot: {path}")
    if not path.is_file() or not os.access(path, os.X_OK):
        fail(f"arm {arm} binary is missing or not executable: {path}")
    actual_sha256 = analyze.sha256_file(path)
    if actual_sha256 != config["sha256"]:
        fail(f"arm {arm} binary SHA-256 mismatch: expected {config['sha256']}, got {actual_sha256}")
    actual_build_id = read_build_id(path)
    if actual_build_id != config["build_id"]:
        fail(f"arm {arm} GNU Build ID mismatch: expected {config['build_id']}, got {actual_build_id}")
    return ({"path": str(path), "sha256": actual_sha256, "build_id": actual_build_id}, binary_stat(path))


def assert_unchanged_stat(path: Path, expected: dict[str, int], label: str) -> None:
    actual = binary_stat(path)
    if actual != expected:
        fail(f"{label} changed after preflight: expected stat {expected}, got {actual}")


def protocol_hashes(campaign: dict[str, Any]) -> dict[str, str]:
    hashes = {name: analyze.sha256_file(EVIDENCE_DIR / name) for name in campaign["protocol_artifacts"]}
    if hashes != campaign["protocol_artifacts"]:
        fail(f"protocol artifact identity mismatch: expected {campaign['protocol_artifacts']}, got {hashes}")
    return hashes


def source_artifact_hashes(campaign: dict[str, Any]) -> dict[str, str]:
    hashes = {name: analyze.sha256_file(EVIDENCE_DIR / name) for name in campaign["source_artifacts"]}
    if hashes != campaign["source_artifacts"]:
        fail(f"source artifact identity mismatch: expected {campaign['source_artifacts']}, got {hashes}")
    return hashes


def benchmark_command(campaign: dict[str, Any], item: dict[str, Any], raw_path: Path) -> list[str]:
    return analyze.expected_benchmark_command(campaign, item, raw_path)


def ensure_pristine_output_area() -> None:
    """Refuse to start on top of any previous attempt's output.

    Deliberately refuses rather than cleaning: the recovery path for an interrupted attempt must be
    to archive its partial output under attempts/<attempt_id>/ and pre-register a new attempt, so
    that an unfavourable or interrupted run is preserved instead of quietly deleted.
    """
    for path in (RUN_RECORD_PATH, PARTIAL_JOURNAL_PATH, SUMMARY_PATH):
        if path.exists():
            fail(
                f"partial reruns are forbidden; refusing existing campaign artifact: {path}. "
                f"Archive the previous attempt under {ATTEMPT_ARCHIVE_DIR} and pre-register a new attempt."
            )
    for directory in (RAW_DIR, LOG_DIR):
        if directory.exists():
            entries = list(directory.iterdir())
            if entries:
                fail(
                    f"partial reruns are forbidden; output directory is not empty: {directory} "
                    f"({len(entries)} entries). Archive the previous attempt under {ATTEMPT_ARCHIVE_DIR} "
                    f"and pre-register a new attempt."
                )
        else:
            directory.mkdir(mode=0o755)
            fsync_directory(directory.parent)


def verify_runtime_environment(campaign: dict[str, Any]) -> str:
    taskset = shutil.which("taskset")
    if taskset is None:
        fail("required command not found: taskset")
    cpu = campaign["execution"]["cpu_affinity"]
    availability = subprocess.run(
        [taskset, "-c", str(cpu), "true"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if availability.returncode != 0:
        fail(f"CPU {cpu} is not available to taskset: {availability.stderr.strip()}")
    expected_governor = campaign["execution"]["required_cpu_governor"]
    for candidate in (cpu, campaign["execution"]["sibling_cpu"]):
        actual = governor(candidate)
        if actual != expected_governor:
            fail(f"CPU {candidate} governor must be {expected_governor!r}, got {actual!r}")
    return expected_governor


def run_process(
    campaign: dict[str, Any],
    item: dict[str, Any],
    journal: dict[str, Any],
    preflight_stats: dict[str, dict[str, int]],
    expected_governor: str,
) -> dict[str, Any]:
    arm = item["arm"]
    selected = campaign["execution"]["cpu_affinity"]
    sibling = campaign["execution"]["sibling_cpu"]
    binary = Path(campaign["arms"][arm]["binary_path"])
    assert_unchanged_stat(binary, preflight_stats[arm], f"arm {arm} binary")

    # The governor is verified before every process, not merely once per block.
    process_governor = governor(selected)
    if process_governor != expected_governor:
        fail(
            f"CPU {selected} governor changed to {process_governor!r} before process "
            f"{item['process_index']}; expected {expected_governor!r}"
        )
    sibling_governor = governor(sibling)
    if sibling_governor != expected_governor:
        fail(
            f"SMT sibling CPU {sibling} governor changed to {sibling_governor!r} before process "
            f"{item['process_index']}; expected {expected_governor!r}"
        )

    final_raw = EVIDENCE_DIR / item["raw"]
    final_log = EVIDENCE_DIR / item["log"]
    temporary_raw = final_raw.with_name(f".{final_raw.name}.incomplete")
    temporary_log = final_log.with_name(f".{final_log.name}.incomplete")
    for path in (final_raw, final_log, temporary_raw, temporary_log):
        if path.exists():
            fail(f"refusing to overwrite process artifact: {path}")

    command = benchmark_command(campaign, item, temporary_raw)
    started_at = now()
    current = {**item, "status": "launching", "started_at": started_at, "command": command}
    journal["current_process"] = current
    atomic_write_json(PARTIAL_JOURNAL_PATH, journal)

    stat_before = {cpu: cpu_stat(cpu) for cpu in (selected, sibling)}
    load_before = loadavg()
    frequency_before = {str(cpu): cpu_frequency_khz(cpu) for cpu in (selected, sibling)}
    with temporary_log.open("xb") as log_stream:
        process = subprocess.Popen(command, stdout=log_stream, stderr=subprocess.STDOUT)
        current["status"] = "running"
        current["pid"] = process.pid
        atomic_write_json(PARTIAL_JOURNAL_PATH, journal)
        returncode = process.wait()
        log_stream.flush()
        os.fsync(log_stream.fileno())
    stat_after = {cpu: cpu_stat(cpu) for cpu in (selected, sibling)}
    load_after = loadavg()
    frequency_after = {str(cpu): cpu_frequency_khz(cpu) for cpu in (selected, sibling)}
    finished_command_at = now()

    if returncode != 0:
        fail(f"benchmark process {item['process_index']} exited {returncode}; partial log remains at {temporary_log}")
    if not temporary_raw.is_file() or temporary_raw.stat().st_size == 0:
        fail(f"benchmark process {item['process_index']} produced no JSON: {temporary_raw}")
    if temporary_log.stat().st_size == 0:
        fail(f"benchmark process {item['process_index']} produced an empty log: {temporary_log}")
    fsync_file(temporary_raw)
    analyze.validate_process_artifacts(campaign, item, temporary_raw, temporary_log)

    os.replace(temporary_raw, final_raw)
    fsync_directory(final_raw.parent)
    os.replace(temporary_log, final_log)
    fsync_directory(final_log.parent)
    completed_at = now()
    result = {
        **item,
        "pid": process.pid,
        "returncode": returncode,
        "governor": process_governor,
        "started_at": started_at,
        "process_exited_at": finished_command_at,
        "finished_at": completed_at,
        "command": command,
        "raw_sha256": analyze.sha256_file(final_raw),
        "log_sha256": analyze.sha256_file(final_log),
        "selected_cpu_busy_fraction": busy_fraction(stat_before[selected], stat_after[selected]),
        "sibling_cpu_busy_fraction": busy_fraction(stat_before[sibling], stat_after[sibling]),
        "loadavg_before": load_before,
        "loadavg_after": load_after,
        "cpu_khz_before": frequency_before,
        "cpu_khz_after": frequency_after,
    }
    journal["completed_processes"].append(result)
    journal["current_process"] = None
    journal["last_completed_process_index"] = item["process_index"]
    atomic_write_json(PARTIAL_JOURNAL_PATH, journal)
    print(
        f"completed {item['process_index']:03d}/{analyze.PROCESS_COUNT}: "
        f"block={item['block']:02d} workload={item['workload']} arm={item['arm']} "
        f"sibling_busy={result['sibling_cpu_busy_fraction']:.3f}",
        flush=True,
    )
    return result


def main() -> int:
    campaign = analyze.load_json(CAMPAIGN_PATH)
    # Strict validation deliberately rejects the draft until every binary identity is filled
    # and the status is changed to frozen_ready.
    analyze.validate_campaign(campaign, allow_placeholders=False)
    campaign_sha256 = analyze.sha256_file(CAMPAIGN_PATH)
    ledger = analyze.validate_attempt_ledger(campaign, campaign_sha256, expect_state="unstarted")
    attempt_id = ledger["attempt_id"]
    preflight_protocol = protocol_hashes(campaign)
    preflight_source_artifacts = source_artifact_hashes(campaign)
    expected_governor = verify_runtime_environment(campaign)

    preflight_identities: dict[str, dict[str, str]] = {}
    preflight_stats: dict[str, dict[str, int]] = {}
    for arm in analyze.ARMS:
        identity, stat = verify_binary(campaign, arm)
        preflight_identities[arm] = identity
        preflight_stats[arm] = stat

    provenance = collect_runtime_provenance(campaign)
    ensure_pristine_output_area()
    preflight = idle_preflight(campaign)

    started_at = now()
    # Record the start BEFORE the first process. Without this, a SIGKILL, OOM or power loss would
    # leave the attempt looking merely pre-registered, and the same preregistration could then be
    # reused for a fresh run with nothing in the ledger showing that a run had already happened.
    append_ledger_record(
        {
            "schema_version": 2,
            "attempt_id": attempt_id,
            "record_type": "started",
            "status": "started",
            "campaign_id": campaign["campaign_id"],
            "campaign_sha256": campaign_sha256,
            "pid": os.getpid(),
            "boot_id": read_text("/proc/sys/kernel/random/boot_id"),
            "started_at": started_at,
            "created_at": started_at,
        }
    )
    print(f"starting {attempt_id} at {started_at}", flush=True)
    host = {
        "name": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_governor": expected_governor,
    }
    journal: dict[str, Any] = {
        "schema_version": 2,
        "status": "running",
        "campaign_id": campaign["campaign_id"],
        "attempt_id": attempt_id,
        "campaign_sha256": campaign_sha256,
        "started_at": started_at,
        "expected_process_count": analyze.PROCESS_COUNT,
        "last_completed_process_index": 0,
        "current_process": None,
        "completed_processes": [],
    }
    atomic_write_json(PARTIAL_JOURNAL_PATH, journal, refuse_existing=True)

    # Exactly one terminal outcome may ever be written for an attempt. Anything that fails after
    # the outcome has been recorded -- the confirmatory re-analysis, a full disk -- must not append
    # a second, contradictory record, which would make the ledger permanently invalid.
    outcome_written = False

    try:
        sequence = analyze.expected_output_sequence()
        current_block = 0
        for item in sequence:
            if item["block"] != current_block:
                current_block = item["block"]
                if analyze.sha256_file(CAMPAIGN_PATH) != campaign_sha256:
                    fail(f"campaign.json changed before block {current_block}")
                if protocol_hashes(campaign) != preflight_protocol:
                    fail(f"protocol artifacts changed before block {current_block}")
                if source_artifact_hashes(campaign) != preflight_source_artifacts:
                    fail(f"source artifacts changed before block {current_block}")
            run_process(campaign, item, journal, preflight_stats, expected_governor)

        analyze.validate_exact_file_set(sequence)
        if len(journal["completed_processes"]) != analyze.PROCESS_COUNT:
            fail(f"expected {analyze.PROCESS_COUNT} completed processes, got {len(journal['completed_processes'])}")
        if analyze.sha256_file(CAMPAIGN_PATH) != campaign_sha256:
            fail("campaign.json changed during execution")
        postflight_protocol = protocol_hashes(campaign)
        if postflight_protocol != preflight_protocol:
            fail("protocol artifacts changed during execution")
        postflight_source_artifacts = source_artifact_hashes(campaign)
        if postflight_source_artifacts != preflight_source_artifacts:
            fail("source artifacts changed during execution")
        postflight_governor = governor(campaign["execution"]["cpu_affinity"])
        if postflight_governor != expected_governor:
            fail(f"CPU governor changed during execution: expected {expected_governor}, got {postflight_governor}")

        postflight_identities: dict[str, dict[str, str]] = {}
        postflight_stats: dict[str, dict[str, int]] = {}
        for arm in analyze.ARMS:
            identity, stat = verify_binary(campaign, arm)
            postflight_identities[arm] = identity
            postflight_stats[arm] = stat
            if stat != preflight_stats[arm]:
                fail(f"arm {arm} binary stat changed during execution")
        if postflight_identities != preflight_identities:
            fail("binary identities changed during execution")

        finished_at = now()
        run_record = {
            "schema_version": 3,
            "status": "complete",
            "campaign_id": campaign["campaign_id"],
            "attempt_id": attempt_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "host": host,
            "runtime_provenance": provenance,
            "idle_preflight": preflight,
            "cpu_affinity": campaign["execution"]["cpu_affinity"],
            "taskset_command": campaign["execution"]["taskset_command"],
            "process_count": analyze.PROCESS_COUNT,
            "repetitions_per_process": analyze.REPETITIONS,
            "campaign_sha256_preflight": campaign_sha256,
            "campaign_sha256_postflight": campaign_sha256,
            "protocol_sha256_preflight": preflight_protocol,
            "protocol_sha256_postflight": postflight_protocol,
            "source_artifact_sha256_preflight": preflight_source_artifacts,
            "source_artifact_sha256_postflight": postflight_source_artifacts,
            "binary_identity_preflight": preflight_identities,
            "binary_identity_postflight": postflight_identities,
            "binary_stat_preflight": preflight_stats,
            "binary_stat_postflight": postflight_stats,
            "completed_processes": journal["completed_processes"],
        }
        atomic_write_json(RUN_RECORD_PATH, run_record, refuse_existing=True)
        journal["status"] = "complete"
        journal["finished_at"] = finished_at
        journal["run_record_sha256"] = analyze.sha256_file(RUN_RECORD_PATH)
        journal["current_process"] = None
        atomic_write_json(PARTIAL_JOURNAL_PATH, journal)

        # Analyze BEFORE recording the outcome. The analysis enforces the pre-registered
        # validity gates (for example the SMT-sibling contention bound), so its result is part of
        # whether this attempt succeeded. Recording success first and analyzing afterwards would
        # let a validity failure append a second, contradictory outcome record.
        summary = analyze.analyze_campaign(emit=False, ledger_state="started")
        gates = summary["adoption_gates"]
        append_ledger_record(
            {
                "schema_version": 2,
                "attempt_id": attempt_id,
                "record_type": "outcome",
                "status": "succeeded",
                "campaign_id": campaign["campaign_id"],
                "campaign_sha256": campaign_sha256,
                "started_at": started_at,
                "finished_at": finished_at,
                "completed_process_count": len(journal["completed_processes"]),
                "run_record_sha256": journal["run_record_sha256"],
                "overall_adoption_gate_passed": gates["overall_adoption_gate_passed"],
                "created_at": now(),
            }
        )
        outcome_written = True
        # Regenerate the canonical summary now that the ledger is complete.
        summary = analyze.analyze_campaign(emit=False)
        gates = summary["adoption_gates"]
        passed = gates["overall_adoption_gate_passed"]
        print(
            f"campaign complete: overall_adoption_gate_passed={passed} summary={SUMMARY_PATH}",
            flush=True,
        )
        # A failed adoption gate is a real, recorded outcome -- but it must not look like success
        # to a shell or CI wrapper that keys on the exit status.
        return 0 if passed else 2
    except BaseException as exc:
        failed_at = now()
        journal["status"] = "failed"
        journal["failed_at"] = failed_at
        journal["failure_type"] = type(exc).__name__
        journal["failure"] = str(exc)[:4000]
        try:
            atomic_write_json(PARTIAL_JOURNAL_PATH, journal)
        except BaseException as journal_exc:
            print(f"failed to update partial journal: {journal_exc}", file=sys.stderr)
        if outcome_written:
            print(
                "the terminal outcome was already recorded; refusing to append a second, "
                "contradictory outcome record",
                file=sys.stderr,
            )
            raise
        try:
            append_ledger_record(
                {
                    "schema_version": 2,
                    "attempt_id": attempt_id,
                    "record_type": "outcome",
                    "status": "failed",
                    "campaign_id": campaign["campaign_id"],
                    "campaign_sha256": campaign_sha256,
                    "started_at": started_at,
                    "failed_at": failed_at,
                    "completed_process_count": len(journal["completed_processes"]),
                    "failure_type": type(exc).__name__,
                    "failure": str(exc)[:4000],
                    "created_at": now(),
                }
            )
        except BaseException as ledger_exc:
            print(f"failed to append the attempt outcome: {ledger_exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    sys.exit(main())
