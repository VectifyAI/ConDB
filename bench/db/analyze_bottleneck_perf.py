#!/usr/bin/env python3
"""Symbolise a perf capture of the containerised mongod, which perf itself cannot.

perf leaves every user-space frame of the condb_mongo mongod as [unknown]:
that process runs as uid 999, this account is uid 1014, and /proc/<pid>/maps is
unreadable, so perf never learns where the binary is mapped.  Kernel frames
resolve; user frames do not.

Everything needed to do it by hand is obtainable without host root:

  * the load bias, from `docker exec --privileged -u root condb_mongo
    cat /proc/1/maps` -- the mapping of file offset 0 for /usr/bin/mongod
  * the symbol table, from the binary copied out with `docker cp`, which
    carries a full .symtab (137003 symbols; no DWARF, but -fno-omit-frame-pointer
    is in the build flags so frame-pointer unwinding gives real call chains)

Runtime address -> symbol is then st_value = ip - bias, because the first
PT_LOAD of this PIE has p_vaddr 0.

Two views are produced, and they answer different questions:

  self       which function the CPU was actually executing.  Sums to 100%.
  inclusive  which functions were anywhere on the stack.  Does NOT sum to 100%,
             because a caller and its callee are both counted.  This is the view
             that answers "how much of the operation is spent under
             canonicalisation", and it is the one to read for phase attribution
             -- but only ever by comparing a frame with its own ancestors, never
             by adding sibling percentages that may overlap.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_BINARY = "bench/db/runs/bottleneck_20260806/bin/mongod"


def log(m: str) -> None:
    print(m, file=sys.stderr, flush=True)


def container_load_bias(container: str = "condb_mongo") -> int:
    """Where file offset 0 of /usr/bin/mongod is mapped in the live process."""
    out = subprocess.run(
        ["docker", "exec", "--privileged", "-u", "root", container,
         "cat", "/proc/1/maps"],
        capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.rstrip().endswith("/usr/bin/mongod"):
            addr, _, offset = line.split()[0], line.split()[1], line.split()[2]
            if int(offset, 16) == 0:
                return int(addr.split("-")[0], 16)
    raise RuntimeError("no zero-offset mapping of /usr/bin/mongod found")


def load_symbols(binary: Path) -> tuple[list[int], list[int], list[str]]:
    """Sorted (start, size, name) from .symtab, demangled."""
    out = subprocess.run(
        ["nm", "-C", "--defined-only", "-S", "--numeric-sort", str(binary)],
        capture_output=True, text=True).stdout
    starts: list[int] = []
    sizes: list[int] = []
    names: list[str] = []
    for line in out.splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 3:
            continue
        try:
            addr = int(parts[0], 16)
        except ValueError:
            continue
        if len(parts) == 4:
            try:
                size = int(parts[1], 16)
            except ValueError:
                size = 0
            name = parts[3]
        else:
            size, name = 0, parts[2]
        starts.append(addr)
        sizes.append(size)
        names.append(name.strip())
    log(f"loaded {len(starts)} symbols from {binary}")
    return starts, sizes, names


class Symbolizer:
    def __init__(self, binary: Path, bias: int):
        self.bias = bias
        self.starts, self.sizes, self.names = load_symbols(binary)

    def lookup(self, ip: int) -> str | None:
        value = ip - self.bias
        if value < 0:
            return None
        i = bisect.bisect_right(self.starts, value) - 1
        if i < 0:
            return None
        start, size = self.starts[i], self.sizes[i]
        if size and value >= start + size:
            return None
        if not size and value - start > 1 << 20:
            return None
        return self.names[i]


def shorten(name: str) -> str:
    """Drop the trailing argument list so frames aggregate readably.

    Naive truncation at the first '(' destroys "mongo::(anonymous namespace)::f",
    which is a large fraction of MongoDB's internal symbols, so the argument list
    is found by scanning back from a trailing ')' and matching parentheses.
    Template arguments are left alone: collapsing them merges genuinely distinct
    instantiations.
    """
    name = name.strip()
    if not name.endswith(")"):
        return name
    depth = 0
    for i in range(len(name) - 1, -1, -1):
        if name[i] == ")":
            depth += 1
        elif name[i] == "(":
            depth -= 1
            if depth == 0:
                # "(anonymous namespace)" is part of the name, not an argument list
                if name[i:].startswith("(anonymous namespace)"):
                    return name
                return name[:i].strip() or name
    return name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("perf_data")
    parser.add_argument("--tid", type=int, required=True)
    parser.add_argument("--binary", default=DEFAULT_BINARY)
    parser.add_argument("--container", default="condb_mongo")
    parser.add_argument("--bias", default=None,
                        help="hex load bias; read from the live container if omitted")
    parser.add_argument("--out-prefix", required=True)
    parser.add_argument("--top", type=int, default=60)
    args = parser.parse_args()

    bias = int(args.bias, 16) if args.bias else container_load_bias(args.container)
    log(f"load bias 0x{bias:x}")
    sym = Symbolizer(Path(args.binary), bias)

    proc = subprocess.Popen(
        ["perf", "script", "-i", args.perf_data, "--tid", str(args.tid), "-F", "ip,dso"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1 << 20)

    self_counts: Counter[str] = Counter()
    incl_counts: Counter[str] = Counter()
    samples = 0
    chain: list[str] = []

    def finish(chain: list[str]) -> None:
        nonlocal samples
        if not chain:
            return
        samples += 1
        self_counts[chain[0]] += 1
        for name in set(chain):
            incl_counts[name] += 1

    for line in proc.stdout:
        line = line.strip()
        if not line:
            finish(chain)
            chain = []
            continue
        parts = line.split(None, 1)
        try:
            ip = int(parts[0], 16)
        except (ValueError, IndexError):
            continue
        dso = parts[1] if len(parts) > 1 else ""
        if "unknown" in dso:
            name = sym.lookup(ip)
            frame = shorten(name) if name else f"[unresolved 0x{ip:x}]"
        else:
            frame = f"[{dso.strip('()')}]"
        chain.append(frame)
    finish(chain)
    proc.wait()

    if not samples:
        raise SystemExit("no samples for that tid")
    log(f"{samples} samples on tid {args.tid}")

    resolved = sum(c for n, c in self_counts.items() if not n.startswith("[unresolved"))
    meta = {
        "perf_data": args.perf_data,
        "tid": args.tid,
        "samples": samples,
        "load_bias_hex": hex(bias),
        "binary": args.binary,
        "self_frames_resolved_pct": round(resolved / samples * 100, 2),
        "note": "self sums to 100%; inclusive does not, because a caller and its "
                "callee are both counted. Never add sibling inclusive percentages.",
    }

    out = Path(args.out_prefix)
    out.parent.mkdir(parents=True, exist_ok=True)
    def display(name: str, width: int = 150) -> str:
        """Collapse template arguments for display only; the JSON keeps them."""
        short = name
        for _ in range(12):
            new = re.sub(r"<[^<>]*>", "<>", short)
            if new == short:
                break
            short = new
        return short if len(short) <= width else short[:width - 3] + "..."

    with open(f"{args.out_prefix}.self.txt", "w") as fh:
        fh.write(f"# self (leaf) profile, {samples} samples, tid {args.tid}\n")
        fh.write(f"# {json.dumps(meta)}\n")
        for name, count in self_counts.most_common(args.top):
            fh.write("%7.3f%%  %8d  %s\n" % (count / samples * 100, count, display(name)))
    with open(f"{args.out_prefix}.inclusive.txt", "w") as fh:
        fh.write(f"# inclusive profile, {samples} samples, tid {args.tid}\n")
        fh.write(f"# {json.dumps(meta)}\n")
        for name, count in incl_counts.most_common(args.top * 4):
            fh.write("%7.3f%%  %8d  %s\n" % (count / samples * 100, count, display(name)))
    with open(f"{args.out_prefix}.meta.json", "w") as fh:
        json.dump({"meta": meta,
                   "self_top": [{"pct": round(c / samples * 100, 4), "n": c, "sym": n}
                                for n, c in self_counts.most_common(args.top)],
                   "inclusive_top": [{"pct": round(c / samples * 100, 4), "n": c, "sym": n}
                                     for n, c in incl_counts.most_common(args.top * 4)]},
                  fh, indent=2)
    log(f"wrote {args.out_prefix}.{{self,inclusive,meta.json}}")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
