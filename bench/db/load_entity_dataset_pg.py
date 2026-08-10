"""Load the same deterministic entity dataset into a PostgreSQL under test.

Rows are identical to the documents load_entity_dataset.py writes into a
mongod under test -- same 7-digit string ids, same 120-word text as a pure
function of the id -- so a MongoDB arm and a PostgreSQL arm serve the same
bytes and their outputs can be compared value by value.

The primary key is added after the COPY, so the index is built once rather
than maintained row by row; that changes load time, not query time.

Refuses the shared condb_pg instance on port 55432.
"""

from __future__ import annotations

import argparse
import sys
import time
from multiprocessing import Process
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_entity_dataset import make_text  # noqa: E402


def load_range(dsn: str, lo: int, hi: int) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            with cur.copy("COPY layout_shared_text (id, text) FROM STDIN") as copy:
                for i in range(lo, hi):
                    doc_id = str(i)
                    copy.write_row((doc_id, make_text(doc_id)))
        conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--lo", type=int, default=1_000_000)
    ap.add_argument("--hi", type=int, default=10_000_000, help="exclusive")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if "55432" in args.dsn:
        raise SystemExit("refusing port 55432: that is the shared condb_pg instance")

    want = args.hi - args.lo
    with psycopg.connect(args.dsn) as conn:
        existing = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'layout_shared_text'"
        ).fetchone()[0]
        if existing:
            n = conn.execute("SELECT count(*) FROM layout_shared_text").fetchone()[0]
            if n == want:
                print(f"layout_shared_text already has {n} rows; nothing to do")
                return
            raise SystemExit(f"layout_shared_text exists with {n} rows; drop it first")
        conn.execute("CREATE TABLE layout_shared_text (id text, text text)")
        conn.commit()

    started = time.time()
    span = want // args.workers
    procs = []
    for w in range(args.workers):
        lo = args.lo + w * span
        hi = args.lo + (w + 1) * span if w < args.workers - 1 else args.hi
        p = Process(target=load_range, args=(args.dsn, lo, hi))
        p.start()
        procs.append(p)
    for p in procs:
        p.join()
        if p.exitcode:
            raise SystemExit(f"a loader worker exited {p.exitcode}")

    with psycopg.connect(args.dsn) as conn:
        conn.execute("ALTER TABLE layout_shared_text ADD PRIMARY KEY (id)")
        conn.commit()
        conn.execute("VACUUM ANALYZE layout_shared_text")
        n = conn.execute("SELECT count(*) FROM layout_shared_text").fetchone()[0]
        size = conn.execute(
            "SELECT pg_table_size('layout_shared_text'), "
            "pg_indexes_size('layout_shared_text')"
        ).fetchone()
    print(f"loaded {n} rows in {time.time() - started:.0f}s  "
          f"table {size[0] / 1e6:.0f} MB  index {size[1] / 1e6:.0f} MB")
    if n != want:
        raise SystemExit(f"expected {want} rows, found {n}")


if __name__ == "__main__":
    main()
