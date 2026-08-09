"""Dump one symbolised call chain per sample, so a capture can be re-asked.

The phase partition in runs/bottleneck_20260806/decomp_get_node was produced
from a chains file that was not retained, which meant every follow-up question
about the same capture needed another five-minute `perf script` pass. This
writes the chains out once, leaf-first, US separated, so the later passes are
seconds.

Frame naming is identical to the committed .sym.* artifacts because it reuses
analyze_bottleneck_perf.Symbolizer: perf cannot resolve this process's
user-space symbols itself (mongod runs as uid 999 in a container, this account
is 1014), so the symbol table comes out of a copy of the binary and the load
bias out of the live container.

Read-only. Touches nothing but the .perf.data already on disk.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_bottleneck_perf import Symbolizer, shorten  # noqa: E402

SEP = "\x1f"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--perf-data", required=True)
    ap.add_argument("--sym-meta", required=True,
                    help=".sym.meta.json holding tid and load_bias_hex")
    ap.add_argument("--binary", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    meta = json.loads(Path(args.sym_meta).read_text())
    # The .sym.meta.json nests it; the header comment in .sym.self.txt does not.
    meta = meta.get("meta", meta)
    tid = str(meta["tid"])
    bias = int(meta["load_bias_hex"], 16)
    expected = meta.get("samples")

    sym = Symbolizer(Path(args.binary), bias)
    proc = subprocess.Popen(
        ["perf", "script", "-i", args.perf_data, "--tid", tid, "-F", "ip,dso"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        bufsize=1 << 20,
    )

    samples = 0
    chain: list[str] = []
    with open(args.out, "w") as fh:
        def finish() -> None:
            nonlocal samples
            if not chain:
                return
            samples += 1
            fh.write(SEP.join(chain) + "\n")

        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                finish()
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
        finish()
        proc.wait()

    print(f"wrote {samples} chains to {args.out}")
    if expected is not None and samples != expected:
        print(f"WARNING: {samples} chains against {expected} samples in the "
              f".sym.meta.json; the two passes did not see the same capture",
              file=sys.stderr)
        raise SystemExit(1)
    print(f"sample count matches the retained .sym artifacts ({expected})")


if __name__ == "__main__":
    main()
