#!/usr/bin/env python3
"""At what link speed does wire compression start paying for this workload?

Wire compression was ruled out for `get_subtree` on loopback: snappy +57.7%, zstd +84.1%, zlib
+1254.9%. That is a true result about loopback and a misleading one about networks. Loopback moves
bytes at roughly 2 GB/s, so a compressor has almost no transfer time to buy back, and the CPU it
spends is pure loss. On a slower link the trade inverts. The useful question is not "does compression
help" but **"below what bandwidth does it help"**, and that has an answer that does not depend on
the size of the reply.

For a payload of S bytes compressing to S·r, with compressor throughput C and decompressor
throughput D (bytes/s), sent over a link of bandwidth B:

    uncompressed   t_u = S/B
    compressed     t_c = S/C  +  S·r/B  +  S/D

Setting t_u = t_c and cancelling S:

    **B_breakeven = (1 - r) / (1/C + 1/D)**

Below that bandwidth compression wins; above it, it loses. The reply size cancels out, so one
measurement of r, C and D answers it for every subtree size.

The bytes measured are the real ones: this drains an actual cursor over the raw wire protocol and
compresses the reply payloads mongod actually produced, not a synthetic stand-in. Compressor
throughput is measured on those same bytes.

Caveats worth keeping attached to the number: C and D here are the Python bindings' throughput,
which wrap the same C libraries mongod and the drivers use but add a little call overhead, so the
break-even is if anything slightly pessimistic. The model is single-stream and ignores the CPU being
taken from something else on a busy server, and it ignores that compression also shrinks the kernel
socket work measured at ~7.3% of server CPU, which would push the break-even higher.

Usage:
    bench_subtree_wire_breakeven.py --port 57018 --filter-path /000006/000075/000773
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import struct
import time
from pathlib import Path
from typing import Any

import bson

OP_MSG = 2013
FLAG_MORE_TO_COME = 1 << 1
FLAG_EXHAUST_ALLOWED = 1 << 16


def encode_op_msg(request_id: int, body: dict, exhaust: bool) -> bytes:
    flags = FLAG_EXHAUST_ALLOWED if exhaust else 0
    payload = struct.pack("<I", flags) + b"\x00" + bson.encode(body)
    return struct.pack("<iiii", 16 + len(payload), request_id, 0, OP_MSG) + payload


def recv_exactly(sock: socket.socket, n: int) -> bytes:
    out = []
    got = 0
    while got < n:
        b = sock.recv(n - got)
        if not b:
            raise ConnectionError("closed")
        out.append(b)
        got += len(b)
    return b"".join(out)


def read_reply(sock: socket.socket) -> tuple[int, dict, bytes]:
    header = recv_exactly(sock, 16)
    length = struct.unpack("<i", header[:4])[0]
    payload = recv_exactly(sock, length - 16)
    flags = struct.unpack("<I", payload[:4])[0]
    body = {}
    if len(payload) > 5 and payload[4] == 0:
        size = struct.unpack("<i", payload[5:9])[0]
        body = bson.decode(payload[5:5 + size])
    # The compressible part is everything after the OP_MSG flag word: what wire compression
    # actually operates on is the message body, not the 16-byte header.
    return flags, body, payload[4:]


def capture(host: str, port: int, db: str, coll: str, path: str,
            batch_size: int) -> tuple[bytes, int]:
    sock = socket.create_connection((host, port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(encode_op_msg(1, {"hello": 1, "$db": "admin"}, False))
    read_reply(sock)

    q = {"path": {"$gte": path + "/", "$lt": path + "0"}}
    sock.sendall(encode_op_msg(2, {
        "find": coll, "filter": q,
        "projection": {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
        "batchSize": batch_size, "$db": db}, True))
    flags, body, chunk = read_reply(sock)
    blobs = [chunk]
    rows = len(body["cursor"]["firstBatch"])
    cursor_id = body["cursor"]["id"]

    while cursor_id != 0:
        sock.sendall(encode_op_msg(3, {
            "getMore": cursor_id, "collection": coll,
            "batchSize": batch_size, "$db": db}, True))
        while True:
            flags, body, chunk = read_reply(sock)
            blobs.append(chunk)
            c = body.get("cursor", {})
            rows += len(c.get("nextBatch", []))
            cursor_id = c.get("id", 0)
            if not (flags & FLAG_MORE_TO_COME):
                break
    sock.close()
    return b"".join(blobs), rows


def bench(name: str, comp: Any, decomp: Any, data: bytes, reps: int) -> dict[str, Any]:
    ct = []
    for _ in range(reps):
        t0 = time.perf_counter()
        packed = comp(data)
        ct.append(time.perf_counter() - t0)
    dt = []
    for _ in range(reps):
        t0 = time.perf_counter()
        decomp(packed)
        dt.append(time.perf_counter() - t0)
    n = len(data)
    c_rate = n / statistics.median(ct)
    d_rate = n / statistics.median(dt)
    r = len(packed) / n
    breakeven = (1 - r) / (1 / c_rate + 1 / d_rate)
    return {"compressor": name, "ratio": r, "compressed_bytes": len(packed),
            "compress_MBps": c_rate / 1e6, "decompress_MBps": d_rate / 1e6,
            "breakeven_bandwidth_Mbps": breakeven * 8 / 1e6}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=57018)
    ap.add_argument("--db", default="bench")
    ap.add_argument("--collection", default="layout2_view")
    ap.add_argument("--filter-path", default="/000006/000075/000773")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    data, rows = capture(args.host, args.port, args.db, args.collection,
                         args.filter_path, args.batch_size)
    print(f"captured {len(data):,} bytes of real reply payload over {rows} rows "
          f"({len(data)/rows:.0f} B/row)")

    import zlib
    import snappy
    import zstandard as zstd
    zc, zd = zstd.ZstdCompressor(level=3), zstd.ZstdDecompressor()

    results = [
        bench("snappy", snappy.compress, snappy.decompress, data, args.reps),
        bench("zstd-3", zc.compress, lambda b: zd.decompress(b, max_output_size=len(data) * 2),
              data, args.reps),
        bench("zlib-6", lambda b: zlib.compress(b, 6), zlib.decompress, data, args.reps),
    ]

    print()
    print(f"{'compressor':<10} {'ratio':>7} {'comp MB/s':>10} {'decomp MB/s':>12} "
          f"{'break-even link':>17}")
    for r in results:
        be = r["breakeven_bandwidth_Mbps"]
        be_s = f"{be/1000:.2f} Gbps" if be >= 1000 else f"{be:.0f} Mbps"
        print(f"{r['compressor']:<10} {r['ratio']:>7.3f} {r['compress_MBps']:>10.0f} "
              f"{r['decompress_MBps']:>12.0f} {be_s:>17}")

    print()
    print("Compression pays on links SLOWER than the break-even, and costs on faster ones.")
    print("Reference points: loopback here ~16 Gbps, 10 GbE 10 Gbps, 1 GbE 1 Gbps,")
    print("typical cloud cross-AZ 1-5 Gbps, cross-region often well under 1 Gbps.")

    # Field names are a second, compressor-independent way to shrink the same bytes.
    names = sum(len(k) + 2 for k in ("node_id", "title", "summary"))
    print()
    print(f"Field-name overhead: {names} B/document x {rows} = {names*rows:,} B "
          f"= {names*rows/len(data)*100:.1f}% of the reply, before compression.")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"bytes": len(data), "rows": rows, "results": results,
             "field_name_bytes": names * rows}, indent=2))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
