"""Decompose PyMongo's fixed per-command cost on the get_entity shape.

The get_entity operation is an unhinted ``find_one`` on ``_id`` in
``layout_shared_text``, served by PROJECTION_SIMPLE -> IDHACK.  An earlier
ablation on the get_node shape (pymongo 4.12) put ``find_one`` at 79.1 us of
client CPU against 16.9 for a hand written OP_MSG exchange, and split the
difference into two 24-27 us steps.  That ladder had four rungs, so each rung
bundled several layers.  This one has eight, on this operation's own shape and
against the driver's current master, so the attribution names one layer at a
time:

  find_one      the public API: Cursor construction, iteration, teardown
  run_op        _Query + client._run_operation, no Cursor
  retry_read    client._retryable_read around a plain conn.command
  sel_checkout  server selection + checkout, no retryable-read wrapper
  checkout      server selected once, per-op pool checkout and checkin
  conn_sess     connection held, session and cluster time still applied
  conn_cmd      connection held, no session
  raw           struct.pack + bson encode + sendall + recv + bson decode

Steps between adjacent rungs are exclusive: each removes one named layer.
Two side arms are reported without steps, because neither is a rung:
db_command (Database.command, a different public entry point) and cursor_ctor
(Cursor construction and teardown with no query issued, a cross-check on the
find_one -> run_op step).

Discipline.  Client CPU comes from time.process_time(); os.times() reports in
10 ms clock ticks and quantises everything at this scale.  Arms rotate within
each block and deltas are taken per block, paired.  Every arm's result is
compared against the first arm's on every block, so an arm that is faster
because it returned something else cannot pass.  The connection the held arms
use is the pool's own connection, checked out around the timing loop rather
than for the whole run, so every arm talks over the same socket -- fresh
connections differ 14-26% in P50 on this box and would otherwise confound the
wall column.

Read-only.  No server settings are touched.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics as st
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pymongo
from bson import CodecOptions, _bson_to_dict, _dict_to_bson
from perfcount import open_counter
from pymongo import ReadPreference
from pymongo.message import _Query
from pymongo.read_concern import ReadConcern

# bench_all_ops_layouts.py:210 -- text.find_one({"_id": node_id}, {"_id": 1, "text": 1})
PROJ = {"_id": 1, "text": 1}
COLL = "layout_shared_text"
DB = "bench"
OPTS = CodecOptions()


def _cpu_us() -> float:
    return time.process_time() * 1e6


class RawClient:
    """Minimal OP_MSG round trip on an existing socket."""

    def __init__(self, sock, db):
        self.sock = sock
        self.db = db
        self.req = 0

    def command(self, spec):
        self.req += 1
        body = dict(spec)
        body["$db"] = self.db
        payload = _dict_to_bson(body, False, OPTS)
        msg = b"\x00\x00\x00\x00" + b"\x00" + payload
        header = struct.pack("<iiii", 16 + len(msg), self.req, 0, 2013)
        self.sock.sendall(header + msg)

        head = self._recv_exactly(16)
        length = struct.unpack("<i", head[:4])[0]
        rest = self._recv_exactly(length - 16)
        # flag bits (4) + section kind (1)
        return _bson_to_dict(bytes(rest[5:]), OPTS)

    def _recv_exactly(self, n):
        buf = bytearray(n)
        mv = memoryview(buf)
        got = 0
        while got < n:
            k = self.sock.recv_into(mv[got:])
            if k == 0:
                raise OSError("connection closed")
            got += k
        return buf


def _raw_socket(conn):
    """The OS socket under a pymongo Connection, across driver versions."""
    inner = conn.conn
    for attr in ("get_conn", "sock", "_sock"):
        sock = getattr(inner, attr, None)
        if sock is not None and hasattr(sock, "sendall"):
            return sock
    if hasattr(inner, "sendall"):
        return inner
    raise RuntimeError(f"cannot find the socket under {type(inner)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--entity-id", default=None, help="default: first _id found")
    ap.add_argument("--blocks", type=int, default=14)
    ap.add_argument("--iters", type=int, default=500)
    ap.add_argument("--warm", type=int, default=400)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    client = pymongo.MongoClient(args.mongo_uri)
    db = client[DB]
    coll = db[COLL]

    entity_id = args.entity_id
    if entity_id is None:
        entity_id = coll.find_one({}, {"_id": 1})["_id"]
    q = {"_id": entity_id}
    spec = {"find": COLL, "filter": q, "projection": PROJ,
            "limit": 1, "singleBatch": True}

    def first_batch(reply):
        b = reply["cursor"]["firstBatch"]
        return b[0] if b else None

    rp = ReadPreference.PRIMARY
    sess = client.start_session()
    counter = open_counter()

    # A Cursor built once, used only for its _run_with_conn / _unpack_response.
    # Neither mutates cursor state, so reusing it across iterations measures the
    # response handling without paying Cursor construction.
    proto_cursor = coll.find(q, PROJ)
    run_with_conn = proto_cursor._run_with_conn

    def make_query():
        return _Query(
            0, DB, COLL, 0, q, PROJ, coll.codec_options, rp, -1, 0,
            ReadConcern(), None, sess, client, None, False,
        )

    def arm_find_one():
        return coll.find_one(q, PROJ)

    def arm_run_op():
        resp = client._run_operation(make_query(), run_with_conn, address=None)
        return first_batch(resp.docs[0])

    def arm_retry_read():
        def inner(_session, _server, conn, read_preference):
            return conn.command(DB, dict(spec), read_preference=read_preference,
                                session=_session, client=client)
        return first_batch(client._retryable_read(inner, rp, sess, "find"))

    def arm_db_command():
        return first_batch(db.command(dict(spec)))

    def arm_cursor_ctor():
        # No I/O: Cursor construction and teardown alone, as a cross-check on
        # the find_one -> run_op step.  Excluded from the equality check
        # because it deliberately never runs the query.
        c = coll.find(q, PROJ)
        c.limit(-1)
        c.close()
        return expected_holder[0]

    # server selection hoisted out of the loop; per-op checkout and checkin stay
    selected_server = [None]

    def arm_sel_checkout():
        server = client._select_server(rp, sess, "find")
        with client._checkout(server, sess) as conn:
            return first_batch(conn.command(DB, dict(spec), read_preference=rp,
                                            session=sess, client=client))

    def arm_checkout():
        with client._checkout(selected_server[0], sess) as conn:
            return first_batch(conn.command(DB, dict(spec), read_preference=rp,
                                            session=sess, client=client))

    held = {"conn": None, "raw": None}

    def arm_conn_sess():
        return first_batch(held["conn"].command(
            DB, dict(spec), read_preference=rp, session=sess, client=client))

    def arm_conn_cmd():
        return first_batch(held["conn"].command(DB, dict(spec)))

    def arm_raw():
        return first_batch(held["raw"].command(spec))

    arms = {
        # the ladder proper: each rung removes exactly one named layer
        "find_one": arm_find_one,
        "run_op": arm_run_op,
        "retry_read": arm_retry_read,
        "sel_checkout": arm_sel_checkout,
        "checkout": arm_checkout,
        "conn_sess": arm_conn_sess,
        "conn_cmd": arm_conn_cmd,
        "raw": arm_raw,
        # side arms: reported, but not rungs, so no step is taken from them
        "db_command": arm_db_command,
        "cursor_ctor": arm_cursor_ctor,
    }
    HELD = {"conn_sess", "conn_cmd", "raw"}
    SIDE = {"db_command", "cursor_ctor"}
    LADDER = [n for n in arms if n not in SIDE]
    names = list(arms)
    expected_holder = [None]

    def _timed(fn, count):
        i0 = counter.read() if counter else 0
        c0, t0 = _cpu_us(), time.perf_counter()
        for _ in range(count):
            r = fn()
        wall = (time.perf_counter() - t0) / count * 1e6
        cpu = (_cpu_us() - c0) / count
        instr = ((counter.read() - i0) / count) if counter else 0.0
        return wall, cpu, instr, r

    def run_arm(name, count):
        """Call an arm `count` times; returns (wall_us, cpu_us, instr, result)."""
        fn = arms[name]
        if name not in HELD:
            return _timed(fn, count)
        ctx = selected_server[0].pool.checkout(handler=None)
        conn = ctx.__enter__()
        held["conn"] = conn
        held["raw"] = RawClient(_raw_socket(conn), DB)
        try:
            return _timed(fn, count)
        finally:
            ctx.__exit__(None, None, None)
            held["conn"] = held["raw"] = None

    try:
        selected_server[0] = client._select_server(rp, sess, "find")

        expected = arms["find_one"]()
        if expected is None:
            raise SystemExit(f"no document with _id={entity_id!r}")
        expected_holder[0] = expected
        for name in names:
            *_, got = run_arm(name, 1)
            if got != expected:
                raise SystemExit(f"arm {name} returned different data:\n"
                                 f"  got      {got}\n  expected {expected}")
        print(f"correctness ok: {len(arms) - len(SIDE)} ladder arms and "
              f"{len(SIDE)} side arms return identical documents "
              f"(cursor_ctor issues no query and echoes it)")
        print(f"entity_id={entity_id!r}  reply doc keys={sorted(expected)}")
        print("instrument: retired user instructions"
              if counter else "instrument: CPU time only (no perf counter)")

        for name in names:
            run_arm(name, args.warm)

        wall = {n: [] for n in names}
        cpu = {n: [] for n in names}
        instr = {n: [] for n in names}
        for b in range(args.blocks):
            rot = names[b % len(names):] + names[:b % len(names)]
            for name in rot:
                w, c, i, r = run_arm(name, args.iters)
                if r != expected:
                    raise SystemExit(f"arm {name} drifted from expected output")
                wall[name].append(w)
                cpu[name].append(c)
                instr[name].append(i)
            print(f"  block {b + 1:2d}: " + "  ".join(
                f"{n} {instr[n][-1]:6.0f}" for n in names))
    finally:
        proto_cursor.close()
        sess.end_session()
        if counter:
            counter.close()

    def paired(prev_s, cur_s):
        deltas = [prev_s[i] - cur_s[i] for i in range(len(cur_s))]
        return {
            "median": st.median(deltas), "min": min(deltas), "max": max(deltas),
            "n_positive": sum(1 for d in deltas if d > 0), "n_blocks": len(deltas),
        }

    base_w = st.median(wall["find_one"])
    base_c = st.median(cpu["find_one"])
    base_i = st.median(instr["find_one"])
    result = {
        "blocks": args.blocks, "iters_per_block": args.iters,
        "pymongo_version": pymongo.version, "entity_id": entity_id,
        "instrument": "retired user instructions + process_time CPU",
        "shape": {"collection": COLL, "filter": q, "projection": PROJ},
        "arms": {},
    }
    print(f"\n{'arm':<13}{'wall us':>9}{'cCPU us':>9}{'instr':>9}{'d instr%':>10}"
          f"{'instr step':>12}{'paired instr step':>28}")
    prev = None
    for n in LADDER + sorted(SIDE):
        w, c, i = st.median(wall[n]), st.median(cpu[n]), st.median(instr[n])
        # side arms are not rungs: no step is taken from or to them
        if prev is None or n in SIDE:
            step_txt = paired_txt = ""
            p_i = p_c = None
        else:
            p_i, p_c = paired(instr[prev], instr[n]), paired(cpu[prev], cpu[n])
            step_txt = f"{st.median(instr[prev]) - i:>12.0f}"
            paired_txt = (f"{p_i['median']:>10.0f}  "
                          f"[{p_i['min']:7.0f},{p_i['max']:7.0f}]  "
                          f"{p_i['n_positive']}/{p_i['n_blocks']}")
        result["arms"][n] = {
            "wall_p50_us": w, "client_cpu_p50_us": c, "instr_p50": i,
            "wall_vs_find_one_pct": (w / base_w - 1) * 100,
            "cpu_vs_find_one_pct": (c / base_c - 1) * 100,
            "instr_vs_find_one_pct": (i / base_i - 1) * 100,
            "paired_instr_step_vs_previous": p_i,
            "paired_cpu_step_vs_previous": p_c,
            "removes": _REMOVES.get(n),
            "wall_samples_us": wall[n], "cpu_samples_us": cpu[n],
            "instr_samples": instr[n],
        }
        print(f"{n:<13}{w:>9.1f}{c:>9.1f}{i:>9.0f}"
              f"{(i / base_i - 1) * 100:>9.1f}%{step_txt}{paired_txt}")
        if n not in SIDE:
            prev = n

    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    print(f"\nwrote {args.out}")


_REMOVES = {
    "run_op": "Cursor construction, iteration and teardown",
    "retry_read": "cursor command assembly, monitoring, unpack and Response",
    "sel_checkout": "the retryable-read wrapper",
    "checkout": "server selection",
    "conn_sess": "pool checkout, checkin and the error handler",
    "conn_cmd": "session application and cluster time gossip",
    "raw": "conn.command: message assembly, monitoring hooks, checks",
}

if __name__ == "__main__":
    main()
