"""Split a phase of a mongod capture by where its leaf samples actually land.

get_entity_phases.txt says transport is 39.6% of this operation and command
dispatch 20.6%. Neither number is actionable on its own: a sample inside
"send reply" may be mongod's own code, the kernel's socket path, or one of the
EDR and container-networking modules this box loads into every connection
thread. Only the first is something MongoDB can change.

For each phase root, this classifies the LEAF frame of every sample whose chain
passes through that root, so the parts sum to the phase and the phase sums to
the operation. It is the same exclusive discipline as phases.py, applied one
level down.

Origins:
  mongod    resolved mongod symbols -- MongoDB's own code
  wt        WiredTiger internals, still MongoDB's code but a separate component
  alloc     the allocator (tcmalloc, operator new/delete)
  kernel    kernel.kallsyms and kernel modules that ship with Linux
  edr       this box's endpoint-protection and container-networking modules
  unres     addresses that did not resolve

Read-only; consumes the chains file decomp_chains.py writes.
"""

from __future__ import annotations

import argparse
from collections import Counter

SEP = "\x1f"

# Endpoint protection and container networking loaded on this host. Not
# MongoDB's code and not present on a normal deployment, so it is separated
# rather than counted against the server.
EDR = (
    "dsa_filter", "bmhook", "tmhook", "nf_tables", "nf_conntrack", "nf_nat",
    "bridge.ko", "nft_compat", "xt_", "iptable", "veth", "overlay",
)
ALLOC = ("operator new", "operator delete", "tc_malloc", "tcmalloc", "TCMalloc",
         "__libc_malloc", "cfree", "operator delete[]", "operator new[]")
WT = ("__wt_", "__config_", "__curfile", "__cursor", "__session_", "__txn_",
      "__block_", "__page_", "__rec_", "__evict", "__schema")


def origin(frame: str) -> str:
    if frame.startswith("[") and any(e in frame for e in EDR):
        return "edr"
    if frame.startswith("[[kernel") or (frame.startswith("[") and ".ko" in frame):
        return "kernel"
    if frame.startswith("[unresolved"):
        return "unres"
    if any(a in frame for a in ALLOC):
        return "alloc"
    if any(w in frame for w in WT):
        return "wt"
    if frame.startswith("["):
        return "kernel"
    return "mongod"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", required=True)
    ap.add_argument("--us-per-op", type=float, required=True)
    ap.add_argument("--roots", required=True,
                    help="pipe-separated frame substrings, one phase each")
    ap.add_argument("--top", type=int, default=12,
                    help="leaf frames to name per phase")
    args = ap.parse_args()

    roots = args.roots.split("|")
    total = 0
    per_root_origin: dict[str, Counter] = {r: Counter() for r in roots}
    per_root_leaf: dict[str, Counter] = {r: Counter() for r in roots}
    matched = Counter()

    for line in open(args.chains):
        chain = line.rstrip("\n").split(SEP)
        if not chain:
            continue
        total += 1
        # Nearest-to-leaf root wins, so nested phases do not double count.
        hit = None
        for frame in chain:
            for r in roots:
                if r in frame:
                    hit = r
                    break
            if hit:
                break
        if hit is None:
            continue
        matched[hit] += 1
        leaf = chain[0]
        per_root_origin[hit][origin(leaf)] += 1
        per_root_leaf[hit][leaf] += 1

    us = args.us_per_op

    def fmt(n: int) -> str:
        return f"{n / total * 100:6.3f}% {n / total * us:7.3f} us"

    print(f"# {total} samples, {us} us/op")
    for r in roots:
        n = matched[r]
        if not n:
            print(f"\n=== {r}: no samples ===")
            continue
        print(f"\n=== {r}: {fmt(n)} ===")
        print("  by origin")
        for o, k in per_root_origin[r].most_common():
            share = k / n * 100
            print(f"    {o:7} {fmt(k)}   ({share:5.1f}% of this phase)")
        print(f"  top {args.top} leaf frames")
        for f, k in per_root_leaf[r].most_common(args.top):
            print(f"    {fmt(k)}  [{origin(f):6}] {f[:88]}")

    covered = sum(matched.values())
    print(f"\n# roots cover {fmt(covered)} of the operation")


if __name__ == "__main__":
    main()
