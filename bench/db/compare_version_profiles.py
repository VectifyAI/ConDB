"""Compare two mongod profiles of the same operation, frame by frame.

Takes the chains files decomp_chains.py writes for two versions, plus the
server CPU per operation each was measured at, and reports where the difference
between them sits: by origin (MongoDB's own code, the kernel, this host's
endpoint protection), then by leaf frame, then as the largest movers.

The two captures must be taken the same way, or the comparison measures the
capture rather than the versions. The first pair taken here was not: master had
been running for half an hour and 7.0.34 had just finished loading 9,000,000
documents, and 7.0.34's profile carried 2.2 us of snappy decompression against
master's exactly zero. Exactly zero is the tell -- a version difference does not
produce that. So this prints the decompression term for each side up front: if
one side has it and the other does not, the captures are not comparable and the
rest of the output should not be read as a version difference.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from decomp_origin import origin  # noqa: E402

SEP = "\x1f"
MONGODB_ORIGINS = ("mongod", "wt", "alloc")


def load(path: str, us_per_op: float) -> tuple[Counter, Counter, float, int]:
    by_origin: Counter = Counter()
    by_leaf: Counter = Counter()
    total = 0
    decompress = 0
    for line in open(path):
        chain = line.rstrip("\n").split(SEP)
        if not chain:
            continue
        total += 1
        leaf = chain[0]
        if "snappy" in leaf or "zstd" in leaf or "Decompress" in leaf:
            decompress += 1
        by_origin[origin(leaf)] += 1
        by_leaf[leaf] += 1
    scale = us_per_op / total
    return (
        Counter({k: v * scale for k, v in by_origin.items()}),
        Counter({k: v * scale for k, v in by_leaf.items()}),
        decompress * scale,
        total,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-label", required=True)
    ap.add_argument("--a-chains", required=True)
    ap.add_argument("--a-us", type=float, required=True)
    ap.add_argument("--b-label", required=True)
    ap.add_argument("--b-chains", required=True)
    ap.add_argument("--b-us", type=float, required=True)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    a_org, a_leaf, a_dec, a_n = load(args.a_chains, args.a_us)
    b_org, b_leaf, b_dec, b_n = load(args.b_chains, args.b_us)
    la, lb = args.a_label, args.b_label

    print(f"# {la}: {a_n:,} samples at {args.a_us} us/op")
    print(f"# {lb}: {b_n:,} samples at {args.b_us} us/op")
    print(f"\ndecompression, which must be alike or the captures are not:")
    print(f"  {la:10} {a_dec:6.3f} us      {lb:10} {b_dec:6.3f} us")
    if (a_dec > 0.2) != (b_dec > 0.2):
        print("  ONE SIDE ONLY -- the caches were not in the same state; "
              "do not read what follows as a version difference")

    print(f"\n{'origin':<10}{la:>12}{lb:>12}{'delta':>10}")
    for key in sorted(set(a_org) | set(b_org), key=lambda k: -a_org.get(k, 0)):
        a, b = a_org.get(key, 0.0), b_org.get(key, 0.0)
        print(f"{key:<10}{a:>12.3f}{b:>12.3f}{b - a:>+10.3f}")
    a_mdb = sum(a_org.get(k, 0) for k in MONGODB_ORIGINS)
    b_mdb = sum(b_org.get(k, 0) for k in MONGODB_ORIGINS)
    print(f"{'MongoDB':<10}{a_mdb:>12.3f}{b_mdb:>12.3f}{b_mdb - a_mdb:>+10.3f}")

    print(f"\ntop {args.top} leaf frames of MongoDB's own code on {lb}")
    ranked = sorted(
        ((v, f) for f, v in b_leaf.items() if origin(f) in MONGODB_ORIGINS),
        reverse=True,
    )[: args.top]
    for value, frame in ranked:
        was = a_leaf.get(frame, 0.0)
        print(f"  {value:6.3f} us  ({was:5.2f} on {la}, {value - was:+5.2f})  "
              f"[{origin(frame):6}] {frame[:70]}")

    print(f"\nlargest movers, {la} -> {lb}")
    movers = sorted(
        (b_leaf.get(f, 0.0) - a_leaf.get(f, 0.0), f)
        for f in set(a_leaf) | set(b_leaf)
    )
    print(f"  down most on {lb}:")
    for delta, frame in movers[:8]:
        print(f"    {delta:+6.3f} us  {frame[:74]}")
    print(f"  up most on {lb}:")
    for delta, frame in movers[-8:][::-1]:
        print(f"    {delta:+6.3f} us  {frame[:74]}")


if __name__ == "__main__":
    main()
