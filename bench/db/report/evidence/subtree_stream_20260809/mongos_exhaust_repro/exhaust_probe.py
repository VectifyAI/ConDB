#!/usr/bin/env python3
"""Does an exhaust cursor actually work through mongos?

This project's report states that exhaust cursors are "refused behind mongos", and treats that as
excluding every sharded deployment from the ~40% overlap an exhaust cursor buys. That framing came
from the driver: PyMongo refuses to open an exhaust cursor when it is talking to a router
(`pymongo/synchronous/cursor.py`), so no experiment through PyMongo can distinguish "mongos cannot
do this" from "the driver will not ask".

Reading mongos says it can:
  * `s/commands/strategy.cpp:488`  -- opCtx->setExhaust(OpMsg::isFlagSet(m, kExhaustSupported))
  * `s/commands/query_cmd/cluster_getmore_cmd.h:111` -- if (opCtx->isExhaust() && cursorId != 0)
                                                          reply->setNextInvocation(boost::none);
  * `s/commands/strategy.cpp:1332` -- propagates shouldRunAgainForExhaust into the DbResponse

So this speaks OP_MSG directly, over a raw socket, and sets the exhaustAllowed flag itself. If
mongos streams, the server sends several replies for one getMore request, each with moreToCome set
on all but the last -- and that is checked here by decoding the flag bits, not inferred from timing.

Usage:
    exhaust_through_mongos.py --port 57022 --db bench --collection layout2_view --limit-batches 40
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from typing import Any

import bson

OP_MSG = 2013
FLAG_CHECKSUM_PRESENT = 1 << 0
FLAG_MORE_TO_COME = 1 << 1
FLAG_EXHAUST_ALLOWED = 1 << 16


def encode_op_msg(request_id: int, body: dict, exhaust_allowed: bool) -> bytes:
    flags = FLAG_EXHAUST_ALLOWED if exhaust_allowed else 0
    section = b"\x00" + bson.encode(body)
    payload = struct.pack("<I", flags) + section
    length = 16 + len(payload)
    header = struct.pack("<iiii", length, request_id, 0, OP_MSG)
    return header + payload


def recv_exactly(sock: socket.socket, n: int) -> bytes:
    chunks = []
    got = 0
    while got < n:
        b = sock.recv(n - got)
        if not b:
            raise ConnectionError(f"socket closed with {n - got} bytes outstanding")
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


def read_op_msg(sock: socket.socket) -> tuple[int, dict, int]:
    """Returns (flagBits, body, wire bytes consumed)."""
    header = recv_exactly(sock, 16)
    length, _req, _resp, opcode = struct.unpack("<iiii", header)
    if opcode != OP_MSG:
        raise RuntimeError(f"unexpected opcode {opcode}")
    payload = recv_exactly(sock, length - 16)
    flags = struct.unpack("<I", payload[:4])[0]
    off = 4
    body: dict = {}
    while off < len(payload):
        kind = payload[off]
        off += 1
        if kind == 0:
            size = struct.unpack("<i", payload[off:off + 4])[0]
            body = bson.decode(payload[off:off + size])
            off += size
        elif kind == 1:
            size = struct.unpack("<i", payload[off:off + 4])[0]
            off += size
        else:
            break
    if flags & FLAG_CHECKSUM_PRESENT:
        pass  # trailing crc32c already inside 'length'
    return flags, body, length


def drain(sock: socket.socket, db: str, coll: str, query: dict, batch_size: int,
          exhaust: bool, req_id: int) -> tuple[int, int, float]:
    """Drain one whole cursor. Returns (rows, replies, elapsed_us).

    With exhaust the server streams replies to a single getMore; without it the client sends one
    getMore per batch. Same socket, same wire format, same batch size -- the only difference is the
    exhaustAllowed flag, so the delta is what exhaust is worth through this endpoint.
    """
    t0 = time.perf_counter()
    find_cmd = {"find": coll, "filter": query,
                "projection": {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
                "batchSize": batch_size, "$db": db}
    sock.sendall(encode_op_msg(req_id, find_cmd, exhaust_allowed=exhaust))
    _flags, body, _n = read_op_msg(sock)
    cur = body["cursor"]
    cursor_id = cur["id"]
    rows = len(cur.get("firstBatch", []))
    replies = 1

    while cursor_id != 0:
        req_id += 1
        getmore = {"getMore": cursor_id, "collection": coll,
                   "batchSize": batch_size, "$db": db}
        sock.sendall(encode_op_msg(req_id, getmore, exhaust_allowed=exhaust))
        while True:
            flags, body, _n = read_op_msg(sock)
            replies += 1
            c = body.get("cursor", {})
            rows += len(c.get("nextBatch", []))
            cursor_id = c.get("id", 0)
            if not (flags & FLAG_MORE_TO_COME):
                break
    return rows, replies, (time.perf_counter() - t0) * 1e6


def measure_mode(host: str, port: int, db: str, coll: str, query: dict, batch_size: int,
                 blocks: int, reps: int) -> dict[str, Any]:
    """Paired exhaust vs sequential-getMore, alternating within blocks on fresh connections."""
    import statistics
    deltas, exh_us, seq_us = [], [], []
    ref_rows = None
    for b in range(blocks):
        order = [True, False] if b % 2 == 0 else [False, True]
        timings = {}
        for exhaust in order:
            sock = socket.create_connection((host, port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall(encode_op_msg(1, {"hello": 1, "$db": "admin"}, exhaust_allowed=False))
            read_op_msg(sock)
            drain(sock, db, coll, query, batch_size, exhaust, 100)  # settle
            best = None
            for r in range(reps):
                rows, replies, us = drain(sock, db, coll, query, batch_size, exhaust, 200 + r * 10)
                if ref_rows is None:
                    ref_rows = rows
                elif rows != ref_rows:
                    raise SystemExit(f"row count changed: {rows} vs {ref_rows}")
                best = us if best is None else min(best, us)
            timings[exhaust] = best
            sock.close()
        exh_us.append(timings[True])
        seq_us.append(timings[False])
        deltas.append((timings[True] / timings[False] - 1) * 100)
        print(f"  block {b}: exhaust {timings[True]/1000:.1f} ms vs sequential "
              f"{timings[False]/1000:.1f} ms  -> {deltas[-1]:+.1f}%", flush=True)
    return {"rows": ref_rows, "deltas_pct": deltas,
            "median_delta_pct": statistics.median(deltas),
            "min_delta_pct": min(deltas), "max_delta_pct": max(deltas),
            "improved": sum(1 for d in deltas if d < 0), "blocks": len(deltas),
            "median_exhaust_ms": statistics.median(exh_us) / 1000,
            "median_sequential_ms": statistics.median(seq_us) / 1000}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--db", default="bench")
    ap.add_argument("--collection", default="layout2_view")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--limit-batches", type=int, default=40)
    ap.add_argument("--filter-path", default=None,
                    help="restrict to a subtree prefix, e.g. /000006/000075/000773")
    ap.add_argument("--measure", action="store_true",
                    help="paired exhaust vs sequential getMore over the whole cursor")
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.measure:
        import json as _json
        q: dict[str, Any] = {}
        if args.filter_path:
            q = {"path": {"$gte": args.filter_path + "/", "$lt": args.filter_path + "0"}}
        probe = socket.create_connection((args.host, args.port))
        probe.sendall(encode_op_msg(1, {"hello": 1, "$db": "admin"}, exhaust_allowed=False))
        _f, h, _n = read_op_msg(probe)
        probe.close()
        endpoint = "mongos" if h.get("msg") == "isdbgrid" else "mongod"
        print(f"endpoint: {endpoint} at {args.host}:{args.port}; batchSize={args.batch_size}")
        res = measure_mode(args.host, args.port, args.db, args.collection, q,
                           args.batch_size, args.blocks, args.reps)
        res["endpoint"] = endpoint
        print(f"\nPAIRED exhaust vs sequential getMore through {endpoint}: "
              f"{res['median_delta_pct']:+.2f}% "
              f"[{res['min_delta_pct']:+.2f}, {res['max_delta_pct']:+.2f}] "
              f"{res['improved']}/{res['blocks']} blocks faster; "
              f"{res['median_exhaust_ms']:.1f} ms vs {res['median_sequential_ms']:.1f} ms "
              f"over {res['rows']} rows")
        if args.out:
            open(args.out, "w").write(_json.dumps(res, indent=2))
            print(f"wrote {args.out}")
        return 0

    sock = socket.create_connection((args.host, args.port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    # hello, so the server knows who we are and we can see what we are talking to.
    sock.sendall(encode_op_msg(1, {"hello": 1, "$db": "admin"}, exhaust_allowed=False))
    _, hello, _ = read_op_msg(sock)
    msg = hello.get("msg")
    print(f"connected to {args.host}:{args.port}: msg={msg!r} "
          f"({'mongos' if msg == 'isdbgrid' else 'mongod'})")

    query: dict[str, Any] = {}
    if args.filter_path:
        query = {"path": {"$gte": args.filter_path + "/", "$lt": args.filter_path + "0"}}

    find_cmd = {
        "find": args.collection,
        "filter": query,
        "projection": {"_id": 0, "node_id": 1, "title": 1, "summary": 1},
        "batchSize": args.batch_size,
        "$db": args.db,
    }
    t0 = time.perf_counter()
    sock.sendall(encode_op_msg(2, find_cmd, exhaust_allowed=True))
    flags, body, nbytes = read_op_msg(sock)
    if not body.get("ok"):
        print(f"find failed: {body}")
        return 1
    cursor = body["cursor"]
    cursor_id = cursor["id"]
    total = len(cursor.get("firstBatch", []))
    print(f"find: cursorId={cursor_id} firstBatch={total} "
          f"moreToCome={bool(flags & FLAG_MORE_TO_COME)}")

    if cursor_id == 0:
        print("cursor exhausted in one batch; use a larger range to exercise exhaust")
        return 1

    # One getMore WITH exhaustAllowed. If the server streams, it sends several replies for this
    # single request, each carrying moreToCome until the last.
    getmore = {
        "getMore": cursor_id,
        "collection": args.collection,
        "batchSize": args.batch_size,
        "$db": args.db,
    }
    sock.sendall(encode_op_msg(3, getmore, exhaust_allowed=True))

    replies = 0
    streamed = 0
    unsolicited = 0
    while replies < args.limit_batches:
        flags, body, nbytes = read_op_msg(sock)
        replies += 1
        more = bool(flags & FLAG_MORE_TO_COME)
        batch = body.get("cursor", {}).get("nextBatch", [])
        total += len(batch)
        if more:
            streamed += 1
        if replies <= 3 or not more:
            print(f"  reply {replies}: rows={len(batch)} bytes={nbytes} moreToCome={more} "
                  f"cursorId={body.get('cursor', {}).get('id')}")
        if not more:
            break
        # Anything after the first reply arrived without us asking for it.
        unsolicited += 1
    elapsed = (time.perf_counter() - t0) * 1e3

    print()
    print(f"replies to ONE getMore request: {replies}")
    print(f"  of which arrived unsolicited (server streamed them): {unsolicited}")
    print(f"  total rows received: {total}")
    print(f"  elapsed: {elapsed:.1f} ms")
    print()
    if unsolicited > 0:
        print("RESULT: exhaust WORKS through this endpoint -- the server sent multiple replies "
              "to a single getMore.")
    else:
        print("RESULT: no streaming -- the server answered one getMore with one reply.")
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
