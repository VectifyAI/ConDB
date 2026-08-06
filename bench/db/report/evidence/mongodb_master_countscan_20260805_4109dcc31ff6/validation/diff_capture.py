#!/usr/bin/env python3
"""Field-by-field diff of two explain/profiling capture JSON blobs.

Flattens both documents to leaf paths and reports every path where the values
differ, or where a path exists on only one side. Exit 0 iff no differences.
"""
import json
import sys


def flatten(obj, prefix="", out=None):
    if out is None:
        out = {}
    if isinstance(obj, dict):
        if not obj:
            out[prefix] = "{}"
        for k in sorted(obj):
            flatten(obj[k], f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(obj, list):
        if not obj:
            out[prefix] = "[]"
        for i, v in enumerate(obj):
            flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj
    return out


def load(path):
    with open(path) as f:
        text = f.read().strip()
    if not text:
        raise SystemExit(f"EMPTY CAPTURE: {path}")
    return json.loads(text)


def main():
    a_path, b_path, a_label, b_label = sys.argv[1:5]
    a = flatten(load(a_path))
    b = flatten(load(b_path))

    keys = sorted(set(a) | set(b))
    diffs = []
    for k in keys:
        if k not in a:
            diffs.append((k, "<ABSENT>", b[k]))
        elif k not in b:
            diffs.append((k, a[k], "<ABSENT>"))
        elif a[k] != b[k]:
            diffs.append((k, a[k], b[k]))

    print(f"# Field-by-field explain/profiling diff")
    print(f"#   A = {a_label}  ({a_path})")
    print(f"#   B = {b_label}  ({b_path})")
    print(f"# leaf fields compared: {len(keys)}  (A={len(a)}, B={len(b)})")
    print()
    if not diffs:
        print("RESULT: IDENTICAL - 0 differing fields across all", len(keys), "leaf paths.")
        return 0

    # Fields that vary run-to-run purely as a function of wall-clock timing, or
    # that encode build identity rather than behaviour. Established empirically
    # by diffing two runs of the SAME binary (see diff-head-run1-vs-run2.txt).
    TIMING = ("optimizationTimeMicros", "TimeMicros", "TimeMillis", "TimeNanos", "gitVersion")
    timing = [d for d in diffs if any(t in d[0] for t in TIMING)]
    substantive = [d for d in diffs if d not in timing]

    print(f"RESULT: {len(diffs)} DIFFERING FIELD(S)"
          f"  -> {len(substantive)} SUBSTANTIVE, {len(timing)} timing/build-identity")
    print()
    if substantive:
        print("## SUBSTANTIVE DIFFERENCES")
        for k, av, bv in substantive:
            print(f"  path : {k}")
            print(f"    A  : {av!r}")
            print(f"    B  : {bv!r}")
        print()
    if timing:
        print("## TIMING / BUILD-IDENTITY ONLY (not behavioural)")
        for k, av, bv in timing:
            print(f"  path : {k}\n    A  : {av!r}\n    B  : {bv!r}")
    return 1 if substantive else 0


if __name__ == "__main__":
    sys.exit(main())
