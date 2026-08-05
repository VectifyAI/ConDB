#!/usr/bin/env python3

"""Reconstruct and build the three campaign arms under a single build attestation.

Every arm is rebuilt from the same pinned base commit in one clean, fixed-path worktree.
Each arm's eleven-file source set is verified against its frozen manifest and both patches
are path-whitelisted before any compiler runs. The builds are performed in the frozen order
C1 -> A -> B -> C2 so that the two independent builds of the final candidate bracket the
other arms; if C1 and C2 do not produce a byte-identical binary with an identical GNU build
ID, every output of the run is invalid.

Usage:
  build_arms.py --repo <mongo checkout> generate-patches --point-control-source <file>
  build_arms.py --repo <mongo checkout> build --worktree <fixed path>
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import analyze

EVIDENCE_DIR = Path(__file__).resolve().parent
BUILD_LOG_DIR = EVIDENCE_DIR / "build_logs"
ATTESTATION_PATH = EVIDENCE_DIR / "build_attestation.json"
BASE_COMMIT = analyze.ARM_COMMITS["A"]
# Deliberately a literal, NOT ARM_COMMITS["C"]. The benchmark harness was authored in
# 4109dcc31ff6 and is overlaid identically on every arm. Deriving it from the C arm would make
# the harness follow the candidate: re-pinning C to a commit that does not contain
# query_bm_fixture/BUILD.bazel/benchmarks_query.yml would resolve those to their base blobs and
# silently redefine the "common" harness.
HARNESS_SOURCE_COMMIT = analyze.HARNESS_SOURCE_COMMIT
POINT_CONTROL_PATH = "src/mongo/db/query/count_query_bm.cpp"
# The evidence-only point-query control is carried by common_harness.patch. Its Git blob is
# pinned here so that regenerating the patch from a working tree cannot silently drift.
# Single source of truth. Duplicating this literal here once let the two files disagree.
EXPECTED_POINT_CONTROL_BLOB = analyze.POINT_CONTROL_BLOB
BUILD_OUTPUT_RELATIVE = "bazel-bin/src/mongo/db/query/count_query_bm"
SMOKE_CPU = "2"


def fail(message: str) -> None:
    raise SystemExit(f"attested build failed: {message}")


def now() -> str:
    return datetime.now().astimezone().isoformat()


def run(command: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output through temporary FILES rather than pipes.

    `capture_output=True` would deadlock on any bazel invocation: bazel forks a long-lived server
    daemon that inherits the pipe's write end, so the pipe never reaches EOF even after the client
    exits, and the parent waits forever on a zombie child. Capturing to files has no such problem.
    stdin is /dev/null so nothing can block waiting for input either.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out_stream, tempfile.TemporaryFile(
        mode="w+", encoding="utf-8", errors="replace"
    ) as error_stream:
        completed = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=out_stream,
            stderr=error_stream,
            check=False,
        )
        out_stream.seek(0)
        error_stream.seek(0)
        stdout, stderr = out_stream.read(), error_stream.read()
    result = subprocess.CompletedProcess(command, completed.returncode, stdout, stderr)
    if check and result.returncode != 0:
        fail(f"command failed ({result.returncode}): {' '.join(command)}\n{stdout}\n{stderr}")
    return result


def git(repo: Path, *arguments: str, check: bool = True) -> str:
    return run(["git", "-C", str(repo), *arguments], check=check).stdout


def git_blob(repo: Path, path: Path) -> str:
    return git(repo, "hash-object", "--", str(path)).strip()


def commit_blob(repo: Path, commit: str, path: str) -> str:
    return git(repo, "rev-parse", f"{commit}:{path}").strip()


def git_reported_patch_paths(worktree: Path, patch: Path) -> set[str]:
    """Ask git itself which paths a patch touches; git is the authority, not a text scan."""
    result = run(["git", "-C", str(worktree), "apply", "--check", "--numstat", "-z", str(patch)])
    fields = [field for field in result.stdout.split("\0") if field]
    paths: set[str] = set()
    for field in fields:
        # `--numstat -z` emits "added\tdeleted\tpath" per record; the rename form leaves the path
        # empty and follows with separate source/destination fields, so skip empty paths rather
        # than recording one. Renames are rejected earlier in any case.
        parts = field.split("\t")
        candidate = parts[2] if len(parts) >= 3 else (field if len(parts) == 1 else "")
        if candidate:
            paths.add(candidate)
    return paths


# `.bazelrc.common_bes` is regenerated on every invocation and carries only Build Event Service
# telemetry keywords, one of which is a snapshot of the machine's available memory. It therefore
# changes between any two builds while having no effect on the produced binary. It is recorded and
# checked for build-affecting flags, but excluded from the byte-equality comparison; widening the
# comparison instead would have reopened the `.bazelrc.local` channel this check exists to close.
TELEMETRY_ONLY_BAZELRC = (".bazelrc.common_bes",)
BUILD_AFFECTING_FLAG_PATTERN = re.compile(
    r"--(copt|cxxopt|conlyopt|linkopt|host_copt|host_linkopt|define|features|config|"
    r"compilation_mode|per_file_copt|action_env|repo_env)\b"
)


def bazelrc_digests(worktree: Path) -> dict[str, str]:
    """Hash every bazelrc that can affect the produced binary.

    MongoDB's bazel wrapper can rewrite `.bazelrc.sync` from a remote flag service, and
    `.bazelrc.local` is try-imported while being git-ignored, so either could change the compiler
    flags between arms without appearing in `git status`.
    """
    digests: dict[str, str] = {}
    for pattern in (".bazelrc*", "*.bazelrc"):
        for entry in sorted(worktree.glob(pattern)):
            if entry.is_file():
                digests[entry.name] = analyze.sha256_file(entry)
    # `.bazelrc.local` is git-ignored yet try-imported by the tracked `.bazelrc`, so it is the one
    # file that can hand an arm different compiler flags while leaving `git status` clean. Equal
    # digests across builds would not catch it, because it would be equally present for all of
    # them; refuse it outright.
    for forbidden in (".bazelrc.local", ".bazelrc.evergreen"):
        if forbidden in digests:
            fail(f"{worktree / forbidden} exists and would silently alter the build flags; remove it and rebuild")
    for name in ("~/.bazelrc", "/etc/bazel.bazelrc"):
        candidate = Path(name).expanduser()
        if candidate.is_file():
            digests[f"external:{candidate}"] = analyze.sha256_file(candidate)
    # Telemetry-only files are removed from the comparison, but only after proving they carry
    # nothing that could change the binary.
    for name in TELEMETRY_ONLY_BAZELRC:
        entry = worktree / name
        if name in digests and entry.is_file():
            offending = BUILD_AFFECTING_FLAG_PATTERN.findall(entry.read_text(encoding="utf-8", errors="replace"))
            if offending:
                fail(
                    f"{entry} was treated as telemetry-only but contains build-affecting flags "
                    f"{sorted(set(offending))}; it must be pinned, not excluded"
                )
            digests.pop(name)
    return digests


def effective_command_description(worktree: Path) -> str:
    """Record what bazel actually runs, not merely what we typed.

    `bazel` here is bazelisk running MongoDB's tracked `tools/bazel` wrapper, which appends
    configuration flags of its own. The frozen command is therefore not the effective command, and
    the attestation should say what the effective one expands to.
    """
    # check=True deliberately: a failure here must abort the build rather than record a sentinel
    # string that would still satisfy a non-empty-string validator.
    canonical = run(["bazel", "canonicalize-flags", "--", "--config=opt"], cwd=worktree)
    expanded = " ".join(canonical.stdout.split())
    if not expanded:
        fail("bazel canonicalize-flags produced no output; the effective build flags were not captured")
    return f"frozen={' '.join(analyze.BUILD_COMMAND)} :: canonicalized(--config=opt)={expanded}"


# ---------------------------------------------------------------------------
# Patch generation
# ---------------------------------------------------------------------------


def generate_patches(repo: Path, point_control_source: Path) -> None:
    for commit in analyze.ARM_COMMITS.values():
        if git(repo, "cat-file", "-t", commit).strip() != "commit":
            fail(f"{commit} is not a commit in {repo}")

    if not point_control_source.is_file():
        fail(f"point-control source is missing: {point_control_source}")
    point_control_blob = git(repo, "hash-object", "-w", "--", str(point_control_source)).strip()
    if point_control_blob != EXPECTED_POINT_CONTROL_BLOB:
        fail(
            f"point-control source blob {point_control_blob} does not match the pinned "
            f"{EXPECTED_POINT_CONTROL_BLOB}; refusing to build a harness of unknown provenance"
        )

    harness_blobs = {path: commit_blob(repo, HARNESS_SOURCE_COMMIT, path) for path in analyze.COMMON_HARNESS_FILES}
    harness_blobs[POINT_CONTROL_PATH] = point_control_blob

    index_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"countscan-harness-index-{os.getpid()}"
    environment = dict(os.environ, GIT_INDEX_FILE=str(index_path))
    try:
        subprocess.run(
            ["git", "-C", str(repo), "read-tree", BASE_COMMIT],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        for path, blob in harness_blobs.items():
            subprocess.run(
                # --add is required: count_query_bm.cpp does not exist at the reconstruction base.
                ["git", "-C", str(repo), "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
        tree = subprocess.run(
            ["git", "-C", str(repo), "write-tree"],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    finally:
        index_path.unlink(missing_ok=True)

    harness_patch = git(
        repo,
        "diff",
        "--full-index",
        "--no-color",
        BASE_COMMIT,
        tree,
        "--",
        *analyze.COMMON_HARNESS_FILES,
    )
    (EVIDENCE_DIR / "common_harness.patch").write_text(harness_patch, encoding="utf-8")

    (EVIDENCE_DIR / "arm_A_production.patch").write_bytes(b"")
    for arm in ("B", "C"):
        patch = git(
            repo,
            "diff",
            "--full-index",
            "--no-color",
            BASE_COMMIT,
            analyze.ARM_COMMITS[arm],
            "--",
            *analyze.PRODUCTION_FILES,
        )
        (EVIDENCE_DIR / f"arm_{arm}_production.patch").write_text(patch, encoding="utf-8")

    analyze.validate_patch_whitelists()

    for arm in analyze.ARMS:
        write_source_manifest(repo, arm, harness_blobs)
    print("generated common_harness.patch, three production patches, and three source manifests")


def expected_identities(repo: Path, arm: str, harness_blobs: dict[str, str]) -> dict[str, dict[str, str]]:
    identities: dict[str, dict[str, str]] = {}
    pinned = analyze.expected_manifest_blobs(arm)
    for path in analyze.PRODUCTION_FILES:
        blob = commit_blob(repo, analyze.ARM_COMMITS[arm], path)
        identities[path] = {"sha256": blob_sha256(repo, blob), "git_blob": blob}
    for path in analyze.COMMON_HARNESS_FILES:
        blob = harness_blobs[path]
        identities[path] = {"sha256": blob_sha256(repo, blob), "git_blob": blob}
    # Cross-check the local repository against the identities pinned in analyze.py, so a doctored
    # or diverged checkout cannot silently define what an arm is.
    for path in analyze.MANIFEST_FILES:
        if identities[path]["git_blob"] != pinned[path]:
            fail(
                f"arm {arm}: local repository blob for {path} is {identities[path]['git_blob']}, but the pinned "
                f"commit-derived identity is {pinned[path]}"
            )
    return {path: identities[path] for path in analyze.MANIFEST_FILES}


def blob_sha256(repo: Path, blob: str) -> str:
    import hashlib

    content = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", blob],
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(content).hexdigest()


def write_source_manifest(repo: Path, arm: str, harness_blobs: dict[str, str]) -> None:
    patch_name = f"arm_{arm}_production.patch"
    manifest = {
        "schema_version": 1,
        "arm": arm,
        "reconstruction_base_commit": BASE_COMMIT,
        "production_source_commit": analyze.ARM_COMMITS[arm],
        "common_harness_patch": {
            "path": "common_harness.patch",
            "sha256": analyze.sha256_file(EVIDENCE_DIR / "common_harness.patch"),
        },
        "production_patch": {
            "path": patch_name,
            "sha256": analyze.sha256_file(EVIDENCE_DIR / patch_name),
        },
        "files": expected_identities(repo, arm, harness_blobs),
    }
    path = EVIDENCE_DIR / f"arm_{arm}_source_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Attested builds
# ---------------------------------------------------------------------------


def prepare_worktree(repo: Path, worktree: Path, recreate: bool) -> None:
    if worktree.exists():
        if not recreate:
            fail(f"worktree path already exists: {worktree} (pass --recreate to replace it)")
        run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], check=False)
        if worktree.exists():
            shutil.rmtree(worktree)
    run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), BASE_COMMIT])
    head = git(worktree, "rev-parse", "HEAD").strip()
    if head != BASE_COMMIT:
        fail(f"attested worktree is not at the reconstruction base: {head}")
    # A fresh worktree contains only TRACKED files, but MongoDB's bazel wrapper writes several
    # gitignored bazelrc files (.bazelrc.bazelisk, .bazelrc.common_bes, .bazelrc.wrapper_hook) on
    # its first invocation. Without materialising them here, the first build's before/after digest
    # maps would cover different file sets and the equality check would fail by construction, and
    # every later build's baseline comparison against the first would fail too.
    before = set(bazelrc_digests(worktree))
    run(["bazel", "canonicalize-flags", "--", "--config=opt"], cwd=worktree)
    after = set(bazelrc_digests(worktree))
    print(f"    materialised wrapper bazelrc files: {sorted(after - before) or 'none'}", flush=True)


BASE_PRESENT_MANIFEST_FILES = tuple(
    path for path in analyze.MANIFEST_FILES if path not in analyze.BASE_ABSENT_MANIFEST_FILES
)


def reset_manifest_files(worktree: Path) -> None:
    run(
        [
            "git",
            "-C",
            str(worktree),
            "restore",
            "--source",
            BASE_COMMIT,
            "--worktree",
            "--staged",
            "--",
            *BASE_PRESENT_MANIFEST_FILES,
        ]
    )
    # Files that do not exist at the reconstruction base cannot be restored; they must be removed.
    for path in analyze.BASE_ABSENT_MANIFEST_FILES:
        (worktree / path).unlink(missing_ok=True)
        run(["git", "-C", str(worktree), "rm", "--cached", "--quiet", "--ignore-unmatch", "--", path], check=False)
    status = git(worktree, "status", "--porcelain", "--", *analyze.MANIFEST_FILES).strip()
    if status:
        fail(f"manifest files are not pristine after reset:\n{status}")


def assert_only_manifest_files_modified(worktree: Path) -> None:
    tracked_changes = [
        line for line in git(worktree, "status", "--porcelain").splitlines() if line and not line.startswith("??")
    ]
    unexpected = [line for line in tracked_changes if line[3:].strip() not in set(analyze.MANIFEST_FILES)]
    if unexpected:
        joined = "\n".join(unexpected)
        fail(f"the attested worktree has tracked changes outside the eleven-file manifest:\n{joined}")
    if git(worktree, "diff", "--cached", "--name-only").strip():
        fail("the attested worktree index is not clean")
    # Untracked files are ignored in general because bazel creates many, but a stray untracked
    # source inside the two directories that feed this target could be picked up by a BUILD glob.
    guarded = ("src/mongo/db/query", "src/mongo/db/exec/classic")
    allowed_untracked = set(analyze.BASE_ABSENT_MANIFEST_FILES)
    stray = [
        line[3:].strip()
        for line in git(worktree, "status", "--porcelain", "--untracked-files=all", "--", *guarded).splitlines()
        if line.startswith("??") and line[3:].strip() not in allowed_untracked
    ]
    if stray:
        fail(f"the attested worktree has unexpected untracked files in guarded source directories: {sorted(stray)}")


def apply_patch(worktree: Path, patch: Path, allowed: set[str], allowed_new_files: frozenset[str]) -> None:
    if patch.stat().st_size == 0:
        return
    # Three independent checks: a strict hunk-aware parse that fails closed on anything it cannot
    # account for, git's own report of what the patch touches, and finally git enforcing the
    # whitelist itself via --include so that nothing outside it can be written even if both
    # enumerations were somehow wrong.
    parsed = analyze.parse_patch_paths(patch, allowed_new_files)
    reported = git_reported_patch_paths(worktree, patch)
    if parsed != reported:
        fail(f"{patch.name}: parsed path set {sorted(parsed)} disagrees with git's {sorted(reported)}")
    illegal = parsed - allowed
    if illegal:
        fail(f"{patch.name} touches paths outside its whitelist: {sorted(illegal)}")
    include_flags = [f"--include={path}" for path in sorted(allowed)]
    command = ["git", "-C", str(worktree), "apply", "--whitespace=nowarn", *include_flags, str(patch)]
    run([*command[:4], "--check", *command[4:]])
    run(command)


def verify_manifest(worktree: Path, arm: str) -> dict[str, dict[str, str]]:
    manifest = analyze.load_json(EVIDENCE_DIR / f"arm_{arm}_source_manifest.json")
    expected = manifest["files"]
    actual: dict[str, dict[str, str]] = {}
    for path in analyze.MANIFEST_FILES:
        absolute = worktree / path
        if not absolute.is_file():
            fail(f"arm {arm}: reconstructed file is missing: {path}")
        actual[path] = {
            "sha256": analyze.sha256_file(absolute),
            "git_blob": git_blob(worktree, absolute),
        }
        if actual[path] != expected[path]:
            fail(f"arm {arm}: reconstructed {path} does not match the frozen manifest: {actual[path]} != {expected[path]}")
    assert_only_manifest_files_modified(worktree)
    return actual


def read_build_id(path: Path) -> str:
    output = run(["readelf", "-n", str(path)]).stdout
    match = re.search(r"Build ID:\s*([0-9a-fA-F]+)", output)
    if match is None:
        fail(f"no readable GNU Build ID in {path}")
    return match.group(1).lower()


def read_comment_section(path: Path) -> str:
    output = run(["readelf", "-p", ".comment", str(path)], check=False).stdout
    versions = sorted({line.strip().split("]", 1)[-1].strip() for line in output.splitlines() if "]" in line})
    return "; ".join(value for value in versions if value)


def run_smoke(binary: Path, log_path: Path, json_dir: Path) -> dict[str, Any]:
    """Run every campaign workload at its campaign size, once, and check the frozen invariants.

    Deliberately the real S/M/W/P workloads rather than the small 10k variants. The campaign
    pre-commits count workloads to exactly one benchmark iteration per repetition, but
    google-benchmark decides that empirically: it stops at one iteration only when a single count
    consumes at least the configured minimum time. If a faster arm ever dropped under that
    threshold the analyzer would abort mid-campaign and destroy the whole pre-registered attempt.
    Checking it here, per arm, before any campaign runs, turns that into a cheap build-time failure.
    """
    commands: list[list[str]] = []
    observed: dict[str, int] = {}
    for workload in analyze.WORKLOADS:
        spec = analyze.WORKLOAD_SPECS[workload]
        raw_path = json_dir / f"{log_path.stem}_{workload}.json"
        command = [
            "taskset",
            "-c",
            SMOKE_CPU,
            str(binary),
            f"--benchmark_filter={spec['filter']}",
            f"--benchmark_min_time={spec['minimum_time_seconds']}",
            "--benchmark_repetitions=1",
            f"--benchmark_out={raw_path}",
            "--benchmark_out_format=json",
        ]
        commands.append(command)
        with log_path.open("ab") as stream:
            stream.write(f"\n=== smoke {workload}: {' '.join(command)}\n".encode())
            stream.flush()
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=False)
        if result.returncode != 0:
            fail(f"guarded smoke for {workload} failed on {binary}: returncode={result.returncode}; log={log_path}")
        payload = analyze.load_json(raw_path)
        rows = [row for row in payload.get("benchmarks", []) if row.get("run_type") == "iteration"]
        if len(rows) != 1 or rows[0].get("name") != spec["run_name"]:
            fail(f"guarded smoke for {workload} did not run {spec['run_name']} exactly once on {binary}")
        iterations = int(rows[0]["iterations"])
        observed[workload] = iterations
        if spec["role"] == "count_endpoint" and iterations != 1:
            fail(
                f"{binary} runs {iterations} benchmark iterations for workload {workload} at its campaign size, "
                f"but the frozen protocol requires exactly one. The campaign would abort mid-attempt."
            )
        if iterations < 1:
            fail(f"{binary} produced a non-positive iteration count for workload {workload}")
    return {
        "commands": commands,
        "returncode": 0,
        "workloads": sorted(analyze.WORKLOADS),
        "iterations_at_campaign_size": observed,
        "passed": True,
        "log": str(log_path.relative_to(EVIDENCE_DIR)),
        "log_sha256": analyze.sha256_file(log_path),
    }


def build_one(repo: Path, worktree: Path, build_key: str) -> dict[str, Any]:
    arm = analyze.BUILD_ARM_OF[build_key]
    print(f"=== attested build {build_key} (arm {arm}) ===", flush=True)
    reset_manifest_files(worktree)
    apply_patch(
        worktree,
        EVIDENCE_DIR / "common_harness.patch",
        set(analyze.COMMON_HARNESS_FILES),
        frozenset(analyze.BASE_ABSENT_MANIFEST_FILES),
    )
    apply_patch(
        worktree,
        EVIDENCE_DIR / f"arm_{arm}_production.patch",
        set(analyze.PRODUCTION_FILES),
        frozenset(),
    )
    verified = verify_manifest(worktree, arm)
    digests_before = bazelrc_digests(worktree)

    log_path = BUILD_LOG_DIR / f"build_{build_key}.log"
    started_at = now()
    build_started_monotonic = time.time()
    with log_path.open("xb") as stream:
        result = subprocess.run(
            list(analyze.BUILD_COMMAND),
            cwd=worktree,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=False,
        )
    finished_at = now()
    if result.returncode != 0:
        fail(f"bazel build for {build_key} exited {result.returncode}; log={log_path}")
    digests_after = bazelrc_digests(worktree)
    if digests_before != digests_after:
        fail(f"a bazelrc file changed while building {build_key}; the build inputs are not pinned")

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    process_summary = " | ".join(
        line.strip()
        for line in log_text.splitlines()
        if line.startswith("INFO: ") and (" process" in line or "Build completed" in line or "actions" in line)
    ) or "no bazel process summary line was emitted"

    produced = worktree / BUILD_OUTPUT_RELATIVE
    if not produced.is_file():
        fail(f"bazel produced no binary for {build_key}: {produced}")
    if produced.stat().st_mtime < build_started_monotonic - 86400:
        fail(f"the build output for {build_key} predates the build by more than a day: {produced}")
    output_path = Path(f"/tmp/mongo-count-query-bm-4109dcc-attested-{build_key}")
    if output_path.exists():
        fail(f"refusing to overwrite an existing attested output: {output_path}")
    shutil.copy2(produced, output_path)
    output_path.chmod(0o755)

    entry = {
        "build_key": build_key,
        "arm": arm,
        "worktree_path": str(worktree),
        "build_command": list(analyze.BUILD_COMMAND),
        "source_manifest": f"arm_{arm}_source_manifest.json",
        "source_manifest_sha256": analyze.sha256_file(EVIDENCE_DIR / f"arm_{arm}_source_manifest.json"),
        "verified_files": verified,
        "attested_output_path": str(output_path),
        "output_sha256": analyze.sha256_file(output_path),
        "build_id": read_build_id(output_path),
        "log": str(log_path.relative_to(EVIDENCE_DIR)),
        "log_sha256": analyze.sha256_file(log_path),
        "started_at": started_at,
        "finished_at": finished_at,
        "campaign_binary_path": None,
        "bazelrc_digests": {"before": digests_before, "after": digests_after},
        "effective_command": effective_command_description(worktree),
        "bazel_process_summary": process_summary,
        # Per build, not once from the last one: a toolchain swap confined to the A/B window and
        # reverted before C2 would otherwise leave C1 and C2 identical and go unrecorded.
        "compiler_version": read_comment_section(output_path),
        "build_environment": {
            name: os.environ.get(name, "") for name in ("CC", "CXX", "BAZEL_FLAGS", "BAZELISK_HOME")
        },
    }
    smoke_log = BUILD_LOG_DIR / f"smoke_{build_key}.log"
    if smoke_log.exists():
        fail(f"refusing to overwrite an existing smoke log: {smoke_log}")
    entry["smoke"] = run_smoke(output_path, smoke_log, BUILD_LOG_DIR)
    print(f"    output sha256 {entry['output_sha256']}  build-id {entry['build_id']}", flush=True)
    return entry


def build_all(repo: Path, worktree: Path, recreate: bool) -> None:
    if ATTESTATION_PATH.exists():
        fail(f"refusing to overwrite an existing build attestation: {ATTESTATION_PATH}")
    for arm in analyze.ARMS:
        manifest = EVIDENCE_DIR / f"arm_{arm}_source_manifest.json"
        if not manifest.is_file():
            fail(f"missing source manifest {manifest}; run generate-patches first")
    analyze.validate_patch_whitelists()
    BUILD_LOG_DIR.mkdir(exist_ok=True)
    if any(BUILD_LOG_DIR.iterdir()):
        fail(f"build log directory is not empty: {BUILD_LOG_DIR}")

    prepare_worktree(repo, worktree, recreate)
    builds = {build_key: build_one(repo, worktree, build_key) for build_key in analyze.BUILD_ORDER}

    # Every arm must have been built by the same toolchain with the same effective flags. The
    # C1/C2 bracket alone cannot see a change confined to the A/B window and reverted afterwards.
    for field in ("compiler_version", "effective_command", "build_environment"):
        reference = builds[analyze.BUILD_ORDER[0]][field]
        for build_key in analyze.BUILD_ORDER:
            if builds[build_key][field] != reference:
                fail(
                    f"build {build_key} has a different {field} than {analyze.BUILD_ORDER[0]}; the arms are not "
                    f"comparable.\n  {analyze.BUILD_ORDER[0]}: {reference!r}\n  {build_key}: {builds[build_key][field]!r}"
                )

    c1 = builds["C1"]
    c2 = builds["C2"]
    output_equal = c1["output_sha256"] == c2["output_sha256"]
    build_id_equal = c1["build_id"] == c2["build_id"]
    if not (output_equal and build_id_equal):
        fail(
            "C1 and C2 are not identical; every attested output of this run is invalid.\n"
            f"  C1 sha256={c1['output_sha256']} build-id={c1['build_id']}\n"
            f"  C2 sha256={c2['output_sha256']} build-id={c2['build_id']}"
        )

    for arm, build_key in analyze.CAMPAIGN_BUILD_OF_ARM.items():
        destination = Path(analyze.ARM_BINARY_PATHS[arm])
        if destination.exists():
            fail(f"refusing to overwrite an existing campaign binary: {destination}")
        shutil.copy2(builds[build_key]["attested_output_path"], destination)
        destination.chmod(0o755)
        if analyze.sha256_file(destination) != builds[build_key]["output_sha256"]:
            fail(f"campaign binary {destination} does not match its attested build output")
        if read_build_id(destination) != builds[build_key]["build_id"]:
            fail(f"campaign binary {destination} does not match its attested GNU build ID")
        builds[build_key]["campaign_binary_path"] = str(destination)

    attestation = {
        "schema_version": 1,
        # Read from the frozen campaign rather than duplicated, so the two cannot drift.
        "campaign_id": analyze.load_json(EVIDENCE_DIR / "campaign.json")["campaign_id"],
        "reconstruction_base_commit": BASE_COMMIT,
        "common_harness_source_commit": HARNESS_SOURCE_COMMIT,
        "worktree_path": str(worktree),
        "source_repository": str(repo),
        "build_command": list(analyze.BUILD_COMMAND),
        "build_order": list(analyze.BUILD_ORDER),
        "toolchain": {
            # cwd matters: bazelisk resolves the version from the workspace's .bazelversion, so
            # running this outside the worktree would record a version that built nothing.
            "bazel_version": run(["bazel", "--version"], cwd=worktree).stdout.strip(),
            "bazel_release": run(["bazel", "info", "release"], cwd=worktree, check=False).stdout.strip(),
            "bazel_wrapper_sha256": {
                name: analyze.sha256_file(worktree / name)
                for name in ("tools/bazel", "bazel/wrapper_hook/wrapper_hook.py", "bazel/workspace_status.py")
                if (worktree / name).is_file()
            },
            "compiler_version": read_comment_section(Path(c2["attested_output_path"])),
            "kernel": platform.release(),
            "hostname": platform.node(),
            "libc": run(["ldd", "--version"], check=False).stdout.splitlines()[0].strip(),
            "machine": platform.machine(),
        },
        "builds": builds,
        "reproducibility_check": {
            "c1_output_sha256": c1["output_sha256"],
            "c2_output_sha256": c2["output_sha256"],
            "output_sha256_equal": output_equal,
            "c1_build_id": c1["build_id"],
            "c2_build_id": c2["build_id"],
            "build_id_equal": build_id_equal,
            "scope": "same_worktree_and_output_base_state_stability_not_hermetic_rebuild",
            "interpretation": (
                "C1 and C2 share one worktree and one bazel output base, as the protocol requires, so C2 is "
                "substantially served from the bazel action cache. Their equality evidences that building arms A "
                "and B left the shared build state undisturbed. It is not a hermetic from-scratch rebuild and "
                "must not be cited as bit-for-bit build reproducibility."
            ),
            "c1_bazel_process_summary": c1["bazel_process_summary"],
            "c2_bazel_process_summary": c2["bazel_process_summary"],
        },
        "campaign_binaries": {arm: analyze.ARM_BINARY_PATHS[arm] for arm in analyze.ARMS},
        "created_at": now(),
    }
    ATTESTATION_PATH.write_text(json.dumps(attestation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {ATTESTATION_PATH}")
    print(f"C1 == C2: sha256 {c1['output_sha256']} build-id {c1['build_id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, type=Path, help="MongoDB checkout holding all three arm commits")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    generate = subparsers.add_parser("generate-patches")
    generate.add_argument("--point-control-source", required=True, type=Path)
    build = subparsers.add_parser("build")
    build.add_argument("--worktree", required=True, type=Path)
    build.add_argument("--recreate", action="store_true")
    arguments = parser.parse_args()

    repo = arguments.repo.resolve()
    if not (repo / ".git").exists():
        fail(f"not a Git checkout: {repo}")
    if arguments.mode == "generate-patches":
        generate_patches(repo, arguments.point_control_source.resolve())
    else:
        build_all(repo, arguments.worktree.resolve(), arguments.recreate)


if __name__ == "__main__":
    sys.exit(main())
