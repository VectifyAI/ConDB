#!/usr/bin/env python3
"""L1b ceiling probe: how much wall time is the server's *transmission* of a reply?

L1b is the only form of "transmit before the batch is full" that survives L1a --
writing the bytes of a single reply message to the socket as they are produced,
instead of building the whole message and handing it to `sinkMessage` at the end.
It is legal for every client because the number of messages does not change.

What it could possibly save is bounded by the time the server currently spends
pushing bytes after it has finished producing them.  It cannot save any of the
client's decode: the client still cannot parse until the one reply message is
complete, and the last byte is produced at the same instant either way.

So the ceiling is measured directly.  A byte-timestamping TCP relay sits between
PyMongo and mongod and records, per reply, the interval from the first byte the
server writes to the last.  With the current server that interval *is* the
transmission time, because production has already finished when the first byte
appears.

Ceiling(L1b) <= (first-byte -> last-byte) / (total operation wall).

The relay adds a hop.  That inflates the measured interval -- it makes the
ceiling look *larger* than it is -- so a small number here is a sound kill and a
large number would need re-measuring without the relay.
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import struct
import threading
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient

UPSTREAM = ("127.0.0.1", 57017)
DB = "bench"
NODES = "layout2_view"
COVER_INDEX = "layout2_rootcause_exact_cover"


class Relay:
    """TCP relay that timestamps the first and last byte of every reply message.

    OP_MSG replies are framed by a 4-byte little-endian total length, so message
    boundaries are recovered exactly rather than guessed from timing gaps.
    """

    def __init__(self, listen_port: int) -> None:
        self.listen_port = listen_port
        self.replies: list[dict[str, float]] = []
        self._lock = threading.Lock()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", listen_port))
        self._srv.listen(16)
        self._stop = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(client,), daemon=True).start()

    def _session(self, client: socket.socket) -> None:
        up = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            up.connect(UPSTREAM)
        except OSError:
            client.close()
            return
        threading.Thread(target=self._pump_c2s, args=(client, up), daemon=True).start()
        self._pump_s2c(up, client)

    def _pump_c2s(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                b = src.recv(65536)
                if not b:
                    break
                dst.sendall(b)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _pump_s2c(self, src: socket.socket, dst: socket.socket) -> None:
        """Forward server->client, recovering message frames from the length prefix."""
        pending = 0          # bytes still owed on the message being forwarded
        header = b""
        first_ts = 0.0
        total = 0
        try:
            while True:
                chunk = src.recv(262144)
                if not chunk:
                    break
                now = time.perf_counter()
                dst.sendall(chunk)
                off = 0
                while off < len(chunk):
                    if pending == 0:
                        need = 4 - len(header)
                        take = min(need, len(chunk) - off)
                        if not header:
                            first_ts = now
                            total = 0
                        header += chunk[off:off + take]
                        off += take
                        total += take
                        if len(header) < 4:
                            break
                        pending = struct.unpack("<i", header)[0] - 4
                        header = b""
                        if pending <= 0:
                            self._record(first_ts, now, total)
                            pending = 0
                        continue
                    take = min(pending, len(chunk) - off)
                    off += take
                    pending -= take
                    total += take
                    if pending == 0:
                        self._record(first_ts, now, total)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _record(self, first_ts: float, last_ts: float, nbytes: int) -> None:
        with self._lock:
            self.replies.append(
                {"first": first_ts, "last": last_ts,
                 "span_us": (last_ts - first_ts) * 1e6, "bytes": nbytes}
            )

    def take(self) -> list[dict[str, float]]:
        with self._lock:
            out = self.replies
            self.replies = []
        return out

    def close(self) -> None:
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass


def run_subtree(coll: Any, lower: str, upper: str) -> int:
    cursor = (
        coll.find({"path": {"$gte": lower, "$lt": upper}},
                  {"_id": 0, "node_id": 1, "title": 1, "summary": 1})
        .sort([("path", 1), ("node_id", 1)])
        .hint(COVER_INDEX)
    )
    n = 0
    for _ in cursor:
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", nargs="+", required=True,
                    help="subtree root paths, e.g. /000006/000075/000773")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--port", type=int, default=57099)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    relay = Relay(args.port)
    uri = f"mongodb://127.0.0.1:{args.port}/?directConnection=true"
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    coll = client[DB][NODES]
    results: dict[str, Any] = {"upstream": f"{UPSTREAM[0]}:{UPSTREAM[1]}", "inputs": {}}

    for path in args.paths:
        lower, upper = path + "/", path + "0"
        for _ in range(args.warmup):
            run_subtree(coll, lower, upper)
        relay.take()

        per_rep = []
        for _ in range(args.reps):
            t0 = time.perf_counter()
            rows = run_subtree(coll, lower, upper)
            wall = (time.perf_counter() - t0) * 1e6
            replies = relay.take()
            # Only replies that actually carry the cursor batches matter; the
            # small ones are the root find_one and any server chatter.
            big = [r for r in replies if r["bytes"] > 100_000]
            per_rep.append({
                "rows": rows,
                "wall_us": wall,
                "n_replies": len(replies),
                "n_big_replies": len(big),
                "reply_bytes": sum(r["bytes"] for r in replies),
                "transmit_us": sum(r["span_us"] for r in replies),
                "transmit_us_big": sum(r["span_us"] for r in big),
            })

        med = lambda k: statistics.median(r[k] for r in per_rep)  # noqa: E731
        share = med("transmit_us") / med("wall_us") if med("wall_us") else 0.0
        results["inputs"][path] = {
            "reps": per_rep,
            "median_rows": med("rows"),
            "median_wall_us": med("wall_us"),
            "median_transmit_us": med("transmit_us"),
            "median_reply_bytes": med("reply_bytes"),
            "ceiling_share_of_wall": share,
        }
        print(f"{path}: rows={med('rows'):.0f} wall={med('wall_us'):.0f}us "
              f"bytes={med('reply_bytes'):.0f} transmit={med('transmit_us'):.0f}us "
              f"=> L1b ceiling <= {share * 100:.2f}% of wall", flush=True)

    relay.close()
    client.close()
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
