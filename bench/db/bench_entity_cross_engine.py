"""get_entity on MongoDB and PostgreSQL over the SAME transports, same box.

Every earlier cross-engine figure in this project compared a containerised
mongod behind a published docker port against psycopg on a container-local
socket -- two different transports. Here both engines are served on this host
over both transports, so each cell is one engine on one transport:

  mongo_tcp    mongod (host process) over 127.0.0.1 TCP
  mongo_unix   the same mongod over its unix domain socket
  pg_tcp_*     PostgreSQL 16 (host network) over 127.0.0.1 TCP
  pg_unix_*    the same server over its unix socket
               *_prep uses a prepared statement, *_unprep re-sends the text

Both serve the same 9,000,000 deterministic rows (load_entity_dataset.py /
load_entity_dataset_pg.py), checked value-for-value at startup.

Units, per this project's rule, never mixed or subtracted:
  wall         client-observed microseconds per operation (perf_counter)
  server CPU   nanoseconds on-CPU of the server-side worker serving the
               connection, from /proc/<pid>/task/<tid>/schedstat --
               mongod's conn* threads, PostgreSQL's backend processes --
               divided by operations

The client is the system python with stock pymongo and psycopg, one
connection per arm held for the whole run, a 40,000-id working set warmed on
every arm before anything is timed, arms rotated within each block.

Floors (`ping` / `SELECT 1`) are measured on the same connections, so the
above-floor cost is derivable within a column.

Read-only against both servers; refuses the shared instances.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics as st
import time
from pathlib import Path

import psycopg
from pymongo import MongoClient

PROJ = {"_id": 1, "text": 1}


def read_stat_threads(pid: int) -> dict[int, int]:
    """tid -> schedstat on-CPU ns for every thread of pid."""
    out = {}
    task = Path(f"/proc/{pid}/task")
    try:
        entries = list(task.iterdir())
    except OSError:
        return out
    for entry in entries:
        try:
            out[int(entry.name)] = int(
                entry.joinpath("schedstat").read_text().split()[0]
            )
        except (OSError, ValueError, IndexError):
            continue
    return out


def mongo_conn_tids(pid: int) -> set[int]:
    out = set()
    task = Path(f"/proc/{pid}/task")
    for entry in task.iterdir():
        try:
            if entry.joinpath("comm").read_text().strip().startswith("conn"):
                out.add(int(entry.name))
        except (OSError, ValueError):
            continue
    return out


def pg_backend_pids() -> set[int]:
    """Host pids of PostgreSQL client backends for the bench database."""
    out = set()
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = p.joinpath("cmdline").read_text().replace("\0", " ")
        except OSError:
            continue
        if cmd.startswith("postgres: bench bench"):
            out.add(int(p.name))
    return out


class MongoArm:
    def __init__(self, label: str, uri: str, pid: int):
        self.label = label
        self.pid = pid
        self.client = MongoClient(uri, maxPoolSize=1)
        self.coll = self.client.bench.layout_shared_text
        self.coll.find_one({"_id": "1000000"}, PROJ)  # open the connection

    def _cpu(self) -> int:
        stats = read_stat_threads(self.pid)
        return sum(stats.get(t, 0) for t in mongo_conn_tids(self.pid))

    def fetch(self, doc_id: str):
        d = self.coll.find_one({"_id": doc_id}, PROJ)
        return None if d is None else (d["_id"], d["text"])

    def floor(self):
        self.client.admin.command("ping")

    def run(self, ids, fn) -> tuple[float, float]:
        c0 = self._cpu()
        t0 = time.perf_counter()
        for doc_id in ids:
            fn(doc_id)
        wall = (time.perf_counter() - t0) / len(ids) * 1e6
        cpu = (self._cpu() - c0) / len(ids) / 1000.0
        return cpu, wall


class PgArm:
    def __init__(self, label: str, dsn: str, prepare: bool):
        self.label = label
        self.prepare = prepare
        self.conn = psycopg.connect(dsn, autocommit=True)
        # Server CPU is summed over ALL bench backends rather than pinned to
        # this connection's own: the harness holds the only bench connections,
        # one arm runs at a time, and an idle backend burns nothing -- the same
        # contract as summing every conn* thread on the mongod side.
        self.query = "SELECT id, text FROM layout_shared_text WHERE id = %s"

    def _cpu(self) -> int:
        total = 0
        for pid in pg_backend_pids():
            stats = read_stat_threads(pid)
            total += sum(stats.values())
        return total

    def fetch(self, doc_id: str):
        cur = self.conn.execute(self.query, (doc_id,), prepare=self.prepare)
        row = cur.fetchone()
        return None if row is None else (row[0], row[1])

    def floor(self):
        self.conn.execute("SELECT 1", prepare=self.prepare).fetchone()

    def run(self, ids, fn) -> tuple[float, float]:
        c0 = self._cpu()
        t0 = time.perf_counter()
        for doc_id in ids:
            fn(doc_id)
        wall = (time.perf_counter() - t0) / len(ids) * 1e6
        cpu = (self._cpu() - c0) / len(ids) / 1000.0
        return cpu, wall


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mongo-pid", type=int, required=True)
    ap.add_argument("--mongo-tcp", default="mongodb://127.0.0.1:57202")
    ap.add_argument("--mongo-unix",
                    default="mongodb://%2Ftmp%2Fentity-vercmp%2Fsock%2Fmongodb-57202.sock")
    ap.add_argument("--pg-tcp",
                    default="host=127.0.0.1 port=55433 user=bench password=bench dbname=bench")
    ap.add_argument("--pg-unix",
                    default="host=/tmp/entity-pg/sock port=55433 user=bench password=bench dbname=bench")
    ap.add_argument("--lo", type=int, default=1_000_000)
    ap.add_argument("--hi", type=int, default=10_000_000)
    ap.add_argument("--workset", type=int, default=40000)
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--iters", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260810)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    for guard, what in ((args.mongo_tcp, "57017"), (args.pg_tcp, "55432")):
        if what in guard:
            raise SystemExit(f"refusing shared instance on {what}")

    arms = [
        MongoArm("mongo_tcp", args.mongo_tcp, args.mongo_pid),
        MongoArm("mongo_unix", args.mongo_unix, args.mongo_pid),
        PgArm("pg_tcp_unprep", args.pg_tcp, prepare=False),
        PgArm("pg_tcp_prep", args.pg_tcp, prepare=True),
        PgArm("pg_unix_unprep", args.pg_unix, prepare=False),
        PgArm("pg_unix_prep", args.pg_unix, prepare=True),
    ]

    rng = random.Random(args.seed)
    workset = [str(rng.randrange(args.lo, args.hi)) for _ in range(args.workset)]

    # Cross-engine equality: same rows, value for value.
    for doc_id in rng.sample(workset, 50):
        vals = {a.label: a.fetch(doc_id) for a in arms}
        uniq = set(vals.values())
        if len(uniq) != 1 or None in uniq:
            raise SystemExit(f"engines disagree on id {doc_id}: {vals}")
    print("correctness ok: both engines return identical (id, text) rows")

    for _ in range(2):
        for a in arms:
            a.run(workset, a.fetch)

    labels = [a.label for a in arms]
    cpu = {n: [] for n in labels}
    wall = {n: [] for n in labels}
    for b in range(args.blocks):
        block_ids = [workset[rng.randrange(len(workset))] for _ in range(args.iters)]
        order = arms[b % len(arms):] + arms[:b % len(arms)]
        for a in order:
            c, w = a.run(block_ids, a.fetch)
            cpu[a.label].append(c)
            wall[a.label].append(w)
        print(f"  block {b + 1:2d}: " + "  ".join(
            f"{n}={cpu[n][-1]:5.1f}/{wall[n][-1]:5.1f}" for n in labels))

    # Floors on the same connections.
    fcpu = {n: [] for n in labels}
    fwall = {n: [] for n in labels}
    for b in range(4):
        for a in (arms if b % 2 == 0 else arms[::-1]):
            c, w = a.run(range(args.iters), lambda _i, a=a: a.floor())
            fcpu[a.label].append(c)
            fwall[a.label].append(w)

    result = {
        "blocks": args.blocks, "iters_per_block": args.iters,
        "workset": args.workset, "documents": args.hi - args.lo,
        "arms": {}, "floors": {},
    }
    print(f"\n{'arm':<16}{'server CPU us':>14}{'wall us':>10}"
          f"{'floor CPU':>11}{'floor wall':>12}{'above-floor CPU':>17}")
    for n in labels:
        c, w = st.median(cpu[n]), st.median(wall[n])
        fc, fw = st.median(fcpu[n]), st.median(fwall[n])
        result["arms"][n] = {"cpu_us": cpu[n], "wall_us": wall[n]}
        result["floors"][n] = {"cpu_us": fcpu[n], "wall_us": fwall[n]}
        print(f"{n:<16}{c:>14.2f}{w:>10.1f}{fc:>11.2f}{fw:>12.1f}{c - fc:>17.2f}")

    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
