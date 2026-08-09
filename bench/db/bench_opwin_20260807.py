#!/usr/bin/env python3
"""Where the get_node / get_children microsecond goes, and what can be taken back.

Measures three quantities per arm, never mixing them:

* ``wall_us``   -- client wall clock per operation (time.perf_counter)
* ``ccpu_us``   -- client process CPU per operation (time.process_time, user+sys)
* ``scpu_us``   -- mongod CPU on *this arm's own connection thread*, from
                   ``/proc/<pid>/task/<tid>/schedstat`` field 1.  The thread is
                   identified by the ``connectionId`` the server reports in its
                   ``hello`` reply, so a sibling agent's traffic on another
                   connection cannot leak into the number.

Design: every arm runs once per block, block order rotates, and the reported
effect is the median of the *per-block paired* delta against the baseline arm.
Output equality is checked element-wise against the baseline in a separate
verification pass before any timing happens.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import socket
import statistics
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import bson
from pymongo import MongoClient

OP_MSG = 2013
NODES = "layout2_view"
TEXT = "layout_shared_text"
NODE_PROJ = {
    "_id": 0, "node_id": 1, "parent_id": 1, "depth": 1,
    "title": 1, "summary": 1, "start_index": 1, "end_index": 1,
}
NODE_FIELDS = ("node_id", "parent_id", "depth", "title", "summary",
               "start_index", "end_index")
CHILD_PROJ = {"_id": 0, "node_id": 1, "title": 1, "summary": 1}
CHILD_FIELDS = ("node_id", "title", "summary")


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- raw OP_MSG

class RawMongo:
    """Minimal synchronous OP_MSG client: no sessions, no lsid, no topology.

    This is not a proposal to replace PyMongo.  It is a floor measurement: the
    cheapest a Python client can issue this exact command and decode this exact
    reply, using the same C BSON codec PyMongo uses.
    """

    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.create_connection((host, port))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._rid = 0
        self._hdr = bytearray(16)
        reply = self.command({
            "hello": 1,
            "client": {"driver": {"name": "condb-raw", "version": "0"},
                       "os": {"type": "Linux"}},
            "$db": "admin",
        })
        self.connection_id = reply.get("connectionId")

    def command(self, doc: dict) -> dict:
        body = bson.encode(doc)
        self._rid += 1
        self.sock.sendall(
            struct.pack("<iiiiIb", 21 + len(body), self._rid, 0, OP_MSG, 0, 0)
            + body
        )
        return self._recv()

    def _recv(self) -> dict:
        sock = self.sock
        hdr = self._read_exact(16)
        length = struct.unpack_from("<i", hdr, 0)[0]
        rest = self._read_exact(length - 16)
        # 4 byte flagBits, 1 byte section kind 0, then the body document.
        return bson.decode(rest[5:])

    def _read_exact(self, n: int) -> bytes:
        chunks = []
        got = 0
        while got < n:
            chunk = self.sock.recv(n - got)
            if not chunk:
                raise EOFError("server closed the connection")
            chunks.append(chunk)
            got += len(chunk)
        return chunks[0] if len(chunks) == 1 else b"".join(chunks)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------ server CPU tap

def thread_table(pid: int) -> dict[str, int]:
    """comm -> schedstat nanoseconds, for every thread of ``pid``."""
    out: dict[str, int] = {}
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return out
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/comm") as fh:
                comm = fh.read().strip()
            with open(f"{task_dir}/{tid}/schedstat") as fh:
                ns = int(fh.read().split()[0])
        except (OSError, ValueError, IndexError):
            continue
        out[comm] = out.get(comm, 0) + ns
    return out


def mongod_pid(container: str) -> int:
    out = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", container],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


# ------------------------------------------------------------------ canon

def canon_node(doc: Any) -> tuple:
    if doc is None:
        return ()
    return tuple("" if doc.get(f) is None else doc.get(f) for f in NODE_FIELDS)


def canon_children(rows: Any) -> tuple:
    return tuple(
        tuple("" if r.get(f) is None else r.get(f) for f in CHILD_FIELDS)
        for r in rows
    )


# ------------------------------------------------------------------- runner

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--published", default="localhost:57017")
    ap.add_argument("--container", default="172.17.0.3:27017")
    ap.add_argument("--db", default="bench")
    ap.add_argument("--blocks", type=int, default=12)
    ap.add_argument("--iters", type=int, default=400)
    ap.add_argument("--inputs", type=int, default=64)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    pub_host, pub_port = args.published.split(":")
    con_host, con_port = args.container.split(":")
    pub_port, con_port = int(pub_port), int(con_port)
    pid = mongod_pid("condb_mongo")

    # one pooled socket per client so the connection thread is unambiguous
    kw = dict(maxPoolSize=1, minPoolSize=1, directConnection=True)
    cli_pub = MongoClient(f"mongodb://{args.published}", **kw)
    cli_con = MongoClient(f"mongodb://{args.container}", **kw)
    db_pub, db_con = cli_pub[args.db], cli_con[args.db]
    conn_pub = db_pub.command("hello")["connectionId"]
    conn_con = db_con.command("hello")["connectionId"]
    raw_pub = RawMongo(pub_host, pub_port)
    raw_con = RawMongo(con_host, con_port)
    log(f"connection ids: pymongo pub={conn_pub} con={conn_con} "
        f"raw pub={raw_pub.connection_id} con={raw_con.connection_id}")

    n_pub, n_con = db_pub[NODES], db_con[NODES]

    # ---- inputs -----------------------------------------------------------
    node_ids = [r["_id"] for r in
                cli_pub[args.db][TEXT].find({}, {"_id": 1})
                .sort([("_id", 1)]).limit(args.inputs)]
    oids: list[Any] = []
    for nid in node_ids:
        d = n_pub.find_one({"tree_id": "base", "node_id": nid}, {"_id": 1})
        oids.append(d["_id"])
    samples = json.loads(
        Path("bench/db/runs/report_3eng_20260716/"
             "layout_2v3_postgres_10m_final.json").read_text())["samples"]
    parent_ids = [s["path"].rsplit("/", 1)[-1] for s in samples[:args.inputs]]

    # ---- arm definitions --------------------------------------------------
    def pm_node(coll):
        return lambda i: canon_node(coll.find_one(
            {"tree_id": "base", "node_id": node_ids[i]}, NODE_PROJ,
            hint="allops_tree_node"))

    def pm_node_nohint(coll):
        return lambda i: canon_node(coll.find_one(
            {"tree_id": "base", "node_id": node_ids[i]}, NODE_PROJ))

    def pm_node_idhack(coll):
        return lambda i: canon_node(coll.find_one({"_id": oids[i]}, NODE_PROJ))

    def raw_node(raw):
        def fn(i):
            r = raw.command({
                "find": NODES,
                "filter": {"tree_id": "base", "node_id": node_ids[i]},
                "hint": "allops_tree_node", "projection": NODE_PROJ,
                "limit": 1, "singleBatch": True, "$db": args.db})
            b = r["cursor"]["firstBatch"]
            return canon_node(b[0]) if b else ()
        return fn

    def raw_node_idhack(raw):
        def fn(i):
            r = raw.command({
                "find": NODES, "filter": {"_id": oids[i]},
                "projection": NODE_PROJ,
                "limit": 1, "singleBatch": True, "$db": args.db})
            b = r["cursor"]["firstBatch"]
            return canon_node(b[0]) if b else ()
        return fn

    def pm_children(coll):
        return lambda i: canon_children(
            coll.find({"tree_id": "base", "parent_id": parent_ids[i]}, CHILD_PROJ)
            .sort([("path", 1), ("node_id", 1)]).hint("allops_tree_parent_path"))

    def pm_children_nosort(coll):
        return lambda i: canon_children(
            coll.find({"tree_id": "base", "parent_id": parent_ids[i]}, CHILD_PROJ)
            .hint("allops_tree_parent_path"))

    def raw_children(raw, sort=True):
        def fn(i):
            cmd = {"find": NODES,
                   "filter": {"tree_id": "base", "parent_id": parent_ids[i]},
                   "hint": "allops_tree_parent_path", "projection": CHILD_PROJ,
                   "$db": args.db}
            if sort:
                cmd["sort"] = {"path": 1, "node_id": 1}
            r = raw.command(cmd)
            assert r["cursor"]["id"] == 0, "raw arm would need a getMore"
            return canon_children(r["cursor"]["firstBatch"])
        return fn

    ARMS: dict[str, dict[str, Any]] = {}

    def add(name, family, conn, fn, note):
        ARMS[name] = {"family": family, "conn": conn, "fn": fn, "note": note}

    # ---- get_node ---------------------------------------------------------
    add("node_base", "node", f"conn{conn_pub}", pm_node(n_pub),
        "BASELINE: pymongo find_one, hint, published docker port")
    add("node_ip", "node", f"conn{conn_con}", pm_node(n_con),
        "same, container IP direct (no docker-proxy)")
    add("node_nohint", "node", f"conn{conn_pub}", pm_node_nohint(n_pub),
        "hint dropped, published port")
    add("node_idhack", "node", f"conn{conn_pub}", pm_node_idhack(n_pub),
        "query by _id -> IDHACK, published port")
    add("node_idhack_ip", "node", f"conn{conn_con}", pm_node_idhack(n_con),
        "IDHACK + container IP")
    add("node_raw", "node", f"conn{raw_pub.connection_id}", raw_node(raw_pub),
        "raw OP_MSG, no lsid, published port")
    add("node_raw_ip", "node", f"conn{raw_con.connection_id}", raw_node(raw_con),
        "raw OP_MSG, container IP")
    add("node_raw_idhack_ip", "node", f"conn{raw_con.connection_id}",
        raw_node_idhack(raw_con), "raw OP_MSG + IDHACK + container IP (stacked)")

    # ---- get_children -----------------------------------------------------
    add("children_base", "children", f"conn{conn_pub}", pm_children(n_pub),
        "BASELINE: pymongo find+sort+hint, published docker port")
    add("children_ip", "children", f"conn{conn_con}", pm_children(n_con),
        "same, container IP direct")
    add("children_nosort", "children", f"conn{conn_pub}", pm_children_nosort(n_pub),
        "explicit sort dropped (index already supplies the order)")
    add("children_raw", "children", f"conn{raw_pub.connection_id}",
        raw_children(raw_pub), "raw OP_MSG, published port")
    add("children_raw_ip", "children", f"conn{raw_con.connection_id}",
        raw_children(raw_con), "raw OP_MSG, container IP")
    add("children_raw_nosort_ip", "children", f"conn{raw_con.connection_id}",
        raw_children(raw_con, sort=False), "raw OP_MSG, no sort, container IP")

    # ---- floors -----------------------------------------------------------
    adm_pub, adm_con = cli_pub["admin"], cli_con["admin"]
    add("ping_base", "ping", f"conn{conn_pub}",
        lambda i: bool(adm_pub.command("ping")), "pymongo ping, published port")
    add("ping_ip", "ping", f"conn{conn_con}",
        lambda i: bool(adm_con.command("ping")), "pymongo ping, container IP")
    add("ping_raw", "ping", f"conn{raw_pub.connection_id}",
        lambda i: bool(raw_pub.command({"ping": 1, "$db": "admin"})),
        "raw OP_MSG ping, published port")
    add("ping_raw_ip", "ping", f"conn{raw_con.connection_id}",
        lambda i: bool(raw_con.command({"ping": 1, "$db": "admin"})),
        "raw OP_MSG ping, container IP")

    if args.only:
        keep = set(args.only.split(","))
        ARMS = {k: v for k, v in ARMS.items() if k in keep}

    names = list(ARMS)

    # ---- verification pass (element-wise, outside the timing window) ------
    log("verifying output equality element-wise")
    equality: dict[str, Any] = {}
    for family, ref_name, count in (("node", "node_base", len(node_ids)),
                                    ("children", "children_base", len(parent_ids))):
        fam = [n for n in names if ARMS[n]["family"] == family]
        if not fam or ref_name not in ARMS:
            continue
        ref_fn = ARMS[ref_name]["fn"]
        total_elems = 0
        for i in range(count):
            expect = ref_fn(i)
            total_elems += sum(len(r) if isinstance(r, tuple) else 1
                               for r in (expect if family == "children" else [expect]))
            for name in fam:
                got = ARMS[name]["fn"](i)
                if got != expect:
                    raise SystemExit(
                        f"OUTPUT MISMATCH {name} input {i}: {got!r} != {expect!r}")
        equality[family] = {"arms": fam, "inputs": count,
                            "elements_compared": total_elems, "all_equal": True}
        log(f"  {family}: {len(fam)} arms x {count} inputs, "
            f"{total_elems} elements, all equal")

    # ---- warm ------------------------------------------------------------
    for name in names:
        fn = ARMS[name]["fn"]
        for i in range(200):
            fn(i % max(1, len(node_ids)))

    # ---- timed blocks ----------------------------------------------------
    results = {n: [] for n in names}
    n_in = len(node_ids)
    for block in range(args.blocks):
        order = names[block % len(names):] + names[:block % len(names)]
        for name in order:
            arm = ARMS[name]
            fn = arm["fn"]
            comm = arm["conn"]
            before = thread_table(pid)
            gc.disable()
            t0 = time.perf_counter()
            c0 = time.process_time()
            for k in range(args.iters):
                fn(k % n_in)
            c1 = time.process_time()
            t1 = time.perf_counter()
            gc.enable()
            after = thread_table(pid)
            scpu = (after.get(comm, 0) - before.get(comm, 0)) / 1e3 / args.iters
            other = sum(
                after.get(c, 0) - before.get(c, 0)
                for c in after if c.startswith("conn") and c != comm)
            results[name].append({
                "block": block,
                "wall_us": (t1 - t0) * 1e6 / args.iters,
                "ccpu_us": (c1 - c0) * 1e6 / args.iters,
                "scpu_us": scpu,
                "other_conn_us": other / 1e3 / args.iters,
            })
        log(f"block {block + 1}/{args.blocks} done")

    # ---- summarise -------------------------------------------------------
    def med(name, key):
        return statistics.median(r[key] for r in results[name])

    summary = {}
    for name in names:
        rows = results[name]
        summary[name] = {
            "family": ARMS[name]["family"],
            "note": ARMS[name]["note"],
            "wall_us": round(med(name, "wall_us"), 2),
            "wall_min": round(min(r["wall_us"] for r in rows), 2),
            "wall_max": round(max(r["wall_us"] for r in rows), 2),
            "ccpu_us": round(med(name, "ccpu_us"), 2),
            "scpu_us": round(med(name, "scpu_us"), 2),
            "other_conn_us": round(med(name, "other_conn_us"), 3),
        }

    paired = {}
    for family, base in (("node", "node_base"), ("children", "children_base"),
                         ("ping", "ping_base")):
        if base not in results:
            continue
        for name in names:
            if ARMS[name]["family"] != family or name == base:
                continue
            entry = {}
            for key in ("wall_us", "ccpu_us", "scpu_us"):
                deltas = [results[name][b][key] - results[base][b][key]
                          for b in range(args.blocks)]
                pcts = [
                    100.0 * d / results[base][b][key]
                    if results[base][b][key] else 0.0
                    for b, d in enumerate(deltas)]
                entry[key] = {
                    "median_delta_us": round(statistics.median(deltas), 2),
                    "median_pct": round(statistics.median(pcts), 2),
                    "min_pct": round(min(pcts), 2),
                    "max_pct": round(max(pcts), 2),
                    "blocks_favouring_arm": sum(1 for d in deltas if d < 0),
                }
            paired[name] = {"vs": base, **entry}

    out = {
        "run": {
            "generated_unix_s": time.time(),
            "blocks": args.blocks, "iters_per_block": args.iters,
            "inputs": len(node_ids),
            "mongod_pid": pid,
            "mongodb_version": cli_pub.server_info()["version"],
            "pymongo": __import__("pymongo").version,
            "published": args.published, "container": args.container,
            "profile_setting": cli_pub[args.db].command("profile", -1),
        },
        "contract": {
            "wall_us": "client wall clock per operation",
            "ccpu_us": "client python process CPU (user+sys) per operation",
            "scpu_us": "mongod CPU on this arm's own connection thread only",
            "other_conn_us": "CPU on every OTHER mongod connection thread during "
                             "the same window; a contamination gauge, not a cost",
        },
        "equality": equality,
        "summary": summary,
        "paired": paired,
        "raw": results,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2))

    log("")
    log(f"{'arm':<26}{'wall':>9}{'ccpu':>9}{'scpu':>9}{'other':>8}  note")
    for name in names:
        s = summary[name]
        log(f"{name:<26}{s['wall_us']:>9.1f}{s['ccpu_us']:>9.1f}"
            f"{s['scpu_us']:>9.1f}{s['other_conn_us']:>8.2f}  {s['note'][:44]}")
    log("")
    for name, p in paired.items():
        log(f"{name:<26} vs {p['vs']:<16} wall {p['wall_us']['median_pct']:>7.2f}% "
            f"[{p['wall_us']['min_pct']:.1f},{p['wall_us']['max_pct']:.1f}] "
            f"scpu {p['scpu_us']['median_pct']:>7.2f}% "
            f"ccpu {p['ccpu_us']['median_pct']:>7.2f}%")

    raw_pub.close(); raw_con.close(); cli_pub.close(); cli_con.close()


if __name__ == "__main__":
    main()
