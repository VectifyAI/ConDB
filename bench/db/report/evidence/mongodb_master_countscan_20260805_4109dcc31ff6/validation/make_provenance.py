#!/usr/bin/env python3
"""Emit provenance.json for the count-minimal non-intrusion validation."""
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time

OUT = "/tmp/mongo-count-minimal-validation"
REPO = "/home/junyao/code/mongo"
HEAD = "ac20554faaf2e7ab6e1b2e2aad2a81308fae82cd"
BASE = "0561c098b99ac5e929005e70a2e37d7a97a82423"


def sh(cmd, cwd=None):
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return p.stdout.strip(), p.returncode


def sha256(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
    except OSError as e:
        return f"<unreadable: {e}>"
    return h.hexdigest()


def build_id(path):
    out, _ = sh(f"file -L {path!r}")
    m = re.search(r"BuildID\[sha1\]=([0-9a-f]+)", out)
    return m.group(1) if m else None


def binfo(label, path):
    real = os.path.realpath(path)
    return {
        "label": label,
        "requested_path": path,
        "real_path": real,
        "exists": os.path.exists(real),
        "size_bytes": os.path.getsize(real) if os.path.exists(real) else None,
        "sha256": sha256(real) if os.path.exists(real) else None,
        "gnu_build_id": build_id(real) if os.path.exists(real) else None,
    }


status, _ = sh("git status --short", cwd=REPO)
head_now, _ = sh("git rev-parse HEAD", cwd=REPO)
diff_text, _ = sh(f"git diff {BASE}..{HEAD}", cwd=REPO)
bazel_ver, _ = sh("bazel --version", cwd=REPO)
worktrees, _ = sh("git worktree list", cwd=REPO)

compiler = {}
tc = ("/home/junyao/.cache/bazel/_bazel_junyao/f9d2a126f5a2d8a662948099aef4ca45/external/"
      "mongo_toolchain_v5/stow/llvm-v5/bin/clang++")
if os.path.exists(tc):
    ver, _ = sh(f"{tc} --version")
    compiler = {
        "path": tc,
        "version": ver.splitlines()[0] if ver else None,
        "full_version_output": ver,
        "sha256": sha256(os.path.realpath(tc)),
    }

logs = {}
for root, _dirs, files in os.walk(OUT):
    # Do not hash the multi-GB fixture dbpaths.
    if any(seg in root for seg in ("-db", "base-worktree", "parity-db", "dbtests-opt-tmp")):
        continue
    for fn in files:
        p = os.path.join(root, fn)
        if os.path.getsize(p) > 200 * 1024 * 1024:
            continue
        logs[os.path.relpath(p, OUT)] = {
            "size_bytes": os.path.getsize(p),
            "sha256": sha256(p),
        }

BIN = "/home/junyao/code/mongo/bazel-out"
prov = {
    "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "hostname": platform.node(),
    "kernel": sh("uname -a")[0],
    "cpus_total": os.cpu_count(),
    "cpu_pinning": "taskset -c 1-47,49-95 (CPU 0 and CPU 48 deliberately avoided)",
    "repo": REPO,
    "head_expected": HEAD,
    "head_actual": head_now,
    "head_unchanged": head_now == HEAD,
    "git_status_short": status,
    "git_status_empty": status == "",
    "base_sha": BASE,
    "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
    "diff_bytes": len(diff_text.encode()),
    "git_worktree_list": worktrees,
    "bazel_version": bazel_ver,
    "compiler": compiler,
    "binaries": [
        binfo("head_mongod_opt", f"{BIN}/k8-opt/bin/src/mongo/db/mongod"),
        binfo("head_mongos_opt", f"{BIN}/k8-opt/bin/src/mongo/s/mongos"),
        binfo("head_mongo_shell_opt", f"{BIN}/k8-opt/bin/src/mongo/shell/mongo"),
        # The pure-opt dbtest used for task 2 was DISPLACED on disk by the
        # --dbg=True build, which reuses the same bazel-out/k8-opt tree. Its
        # identity is recorded here as captured before the overwrite.
        {
            "label": "head_dbtest_opt_PURE (task 2, no longer on disk)",
            "requested_path": f"{BIN}/k8-opt/bin/src/mongo/dbtests/dbtest_with_debug",
            "real_path": f"{BIN}/k8-opt/bin/src/mongo/dbtests/dbtest_with_debug",
            "exists": False,
            "overwritten_by": "bazel build --config=opt --dbg=True //src/mongo/dbtests:dbtest",
            "size_bytes": 674230168,
            "sha256":
                "2402ec61de39f507633e5763993d6b44883dc01f00c4edc635315904c3632509",
            "gnu_build_id": "4018f12d31fef5b43b89b4049b9efde3f3d15e86",
            "captured_from": "binaries-head.txt (recorded before the dbg build ran)",
        },
        binfo("head_dbtest_opt_dbg (task 2b, dassert live)",
              os.environ.get("DBG_DBTEST", "/nonexistent")),
        binfo("base_mongod_opt", os.environ.get("BASE_MONGOD", "/nonexistent")),
    ],
    "commands": json.load(open(f"{OUT}/commands.json"))
    if os.path.exists(f"{OUT}/commands.json") else [],
    "artifact_hashes": logs,
}

with open(f"{OUT}/provenance.json", "w") as f:
    json.dump(prov, f, indent=2, sort_keys=True)
print(json.dumps({k: prov[k] for k in
                  ["head_actual", "head_unchanged", "git_status_empty", "diff_sha256",
                   "bazel_version"]}, indent=2))
print(f"artifacts hashed: {len(logs)}")
