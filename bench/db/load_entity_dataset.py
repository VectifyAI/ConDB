"""Load a deterministic copy of layout_shared_text into a server under test.

Two mongod versions are being compared on the get_entity shape, so both have to
serve the same bytes. Every document here is a pure function of its _id: the
text is drawn from a fixed vocabulary by a PRNG seeded on the id, 120
words per document, matching the real collection's shape (7-digit string _id,
120 words, ~1021 characters, ~1054 B of BSON). That makes the load reproducible
and order-independent, so it does not matter how many workers run or in what
order they finish.

It writes only to the database and collection named on the command line, which
must not be the shared benchmark dataset -- the script refuses port 57017.
"""

from __future__ import annotations

import argparse
import random
import time
from multiprocessing import Process

from pymongo import MongoClient

# The vocabulary the real collection draws from, in the order the sample gave.
VOCAB = (
    "traversal chapter query attention partition framework neural replica dense "
    "aggregation projection embedding sort distributed dataset annotation "
    "transformer precision index gradient system relationship evaluation page "
    "retrieval concurrent ranking storage throughput filter token scalability "
    "lookup reasoning serialization summary memory shard section metadata "
    "benchmark model node pipeline document cache graph semantic chunk recall "
    "vector cluster batch latency compression checkpoint tokenizer inference "
    "corpus schema encoder decoder attention_head snapshot replica_set journal "
    "collection namespace cursor planner executor optimizer statistics histogram "
    "selectivity cardinality"
).split()

WORDS_PER_DOC = 120


def make_text(doc_id: str) -> str:
    """120 words drawn by a PRNG seeded on the id, so the doc is a function of it.

    Seeding Mersenne Twister per document and letting random.choices do the
    drawing in C is several times faster than stretching a hash, and it keeps
    every document distinct -- a small pool of reused texts would compress far
    better than the real collection and change what the cache holds.
    """
    return " ".join(random.Random(int(doc_id)).choices(VOCAB, k=WORDS_PER_DOC))


def load_range(uri: str, db: str, coll: str, lo: int, hi: int, batch: int) -> None:
    client = MongoClient(uri)
    c = client[db][coll]
    docs = []
    for i in range(lo, hi):
        doc_id = str(i)
        docs.append({"_id": doc_id, "text": make_text(doc_id)})
        if len(docs) >= batch:
            c.insert_many(docs, ordered=False)
            docs = []
    if docs:
        c.insert_many(docs, ordered=False)
    client.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", required=True)
    ap.add_argument("--db", default="bench")
    ap.add_argument("--coll", default="layout_shared_text")
    ap.add_argument("--lo", type=int, default=1_000_000)
    ap.add_argument("--hi", type=int, default=10_000_000, help="exclusive")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--batch", type=int, default=2000)
    args = ap.parse_args()

    if ":57017" in args.uri:
        raise SystemExit(
            "refusing to write to port 57017: that is the shared benchmark dataset"
        )

    client = MongoClient(args.uri)
    existing = client[args.db][args.coll].estimated_document_count()
    want = args.hi - args.lo
    if existing:
        if existing >= want:
            print(f"{args.coll} already has {existing} documents; nothing to do")
            return
        raise SystemExit(
            f"{args.coll} has {existing} documents, which is neither empty nor the "
            f"{want} wanted; drop it first rather than loading on top"
        )

    span = want // args.workers
    started = time.time()
    procs = []
    for w in range(args.workers):
        lo = args.lo + w * span
        hi = args.lo + (w + 1) * span if w < args.workers - 1 else args.hi
        p = Process(target=load_range,
                    args=(args.uri, args.db, args.coll, lo, hi, args.batch))
        p.start()
        procs.append(p)
    for p in procs:
        p.join()
        if p.exitcode:
            raise SystemExit(f"a loader worker exited {p.exitcode}")

    n = client[args.db][args.coll].estimated_document_count()
    stats = client[args.db].command("collstats", args.coll)
    print(f"loaded {n} documents in {time.time() - started:.0f}s  "
          f"storage {stats['storageSize'] / 1e6:.0f} MB  "
          f"index {stats['totalIndexSize'] / 1e6:.0f} MB")
    if n != want:
        raise SystemExit(f"expected {want} documents, found {n}")


if __name__ == "__main__":
    main()
