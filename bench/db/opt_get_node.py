"""Three stacking optimizations for the get_node point lookup.

The baseline is bench_all_ops_layouts.py:146-168: a hinted find_one on
(tree_id, node_id) with a seven-field projection.  On the decomposition arms it
costs 166.9 us -- 71.6 server CPU, 75.5 client CPU, 19.8 relay residual -- of
which reading the data is 1.99 us.  The rest is a fixed per-command path walked
once in mongod and once in PyMongo, and that is what these three attack:

  command   Database.command instead of find_one.  Same command, same field
            set, same 303-byte length -- the field *order* differs, so "byte
            identical" would be wrong -- and no Cursor object.  Client side,
            no server change, no patch.
  pinned    Server selection and pool checkout amortized across operations
            instead of run per operation.  Client side.
  idhack    The natural key promoted to a string _id, which makes the lookup
            eligible for the IDHACK plan and removes the redundant secondary
            index.  Server side, requires the re-keyed collection built by
            prepare_get_node_idhack.py.

Each reader returns the same seven fields in the same order, so a faster arm
that returns different data fails the harness's per-iteration fingerprint check
rather than scoring.

Deliberately not implemented here:

  * The slot-based execution engine.  Bounded near 12% of server CPU by the
    QueryPlanner::plan leaf bucket (8.84 of 71.7 us), it does not compose with
    idhack -- an IDHACK-eligible query is by construction not pushed to SBE
    (query_utils.cpp:61-70) -- and forceClassicEngine is the deliberate 7.0.34
    default, so it is a deployment decision rather than a code change.
  * $in coalescing.  Real and large, 2.6x at B=3, but it changes the call
    shape: it applies only where the caller already knows several ids, so it
    is not a like-for-like arm against a single-node lookup.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from pymongo import ReadPreference
# Private, and used deliberately: these are what Pool.checkout and
# Pool._perished use, and a fast path standing in for them has to use them too.
from pymongo.monitoring import ConnectionClosedReason
from pymongo.synchronous.mongo_client import _MongoClientErrorHandler
from pymongo.synchronous.pool import PoolState


# bench_all_ops_layouts.py:148-158.  Field order here is irrelevant to the wire
# but the tuple built by each reader must match the harness's column order.
PROJECTION = {
    "_id": 0,
    "node_id": 1,
    "parent_id": 1,
    "depth": 1,
    "title": 1,
    "summary": 1,
    "start_index": 1,
    "end_index": 1,
}

FIELDS = (
    "node_id",
    "parent_id",
    "depth",
    "title",
    "summary",
    "start_index",
    "end_index",
)

BASELINE_COLLECTION = "layout2_view"
BASELINE_INDEX = "allops_tree_node"
IDHACK_COLLECTION = "layout2_view_idhack"
CONTROL_COLLECTION = "layout2_view_control"


def idhack_key(tree_id: str, node_id: str) -> str:
    """The natural key as a string _id.

    A string, not a sub-document.  A sub-document _id matches by exact document
    equality including field order, so {_id: {tree_id, node_id}} returns one
    document and {_id: {node_id, tree_id}} returns none -- both taking IDHACK,
    with no error and no plan difference.  A dotted-path rewrite is not an
    escape either: it falls back to COLLSCAN.  The string form has identical
    IDHACK eligibility and no ordering hazard.

    The separator must not occur in either component or the encoding is
    ambiguous; prepare_get_node_idhack.py asserts that.
    """
    return f"{tree_id}|{node_id}"


class PinnedConnection:
    """Server selection and pool checkout, amortized instead of per operation.

    PyMongo runs server selection, pool checkout and pool checkin on every
    operation, including the common case where the topology has not changed and
    a healthy connection is already in the pool.  On get_node that is 27.3 us of
    client CPU per lookup, 44% of the driver's fixed cost, isolated by holding a
    connection while keeping the session and cluster-time gossip.

    This holds the selected server and one checked-out connection across
    operations, and revalidates before each use with the same conditions
    Pool._perished applies (pool.py:1373-1406), but without taking the pool
    lock:

      * TopologyDescription is immutable and replaced wholesale on any change
        (topology.py:501,568,708,725), so an identity check against the one
        selection ran under is sufficient to detect that re-selection is due;
      * the pool generation check is Pool.stale_generation, an integer compare;
      * the idle-time and socket-closed checks are the pool's own, skipped on
        the same condition the pool skips them -- idle below
        _check_interval_seconds, which is 1 second (pool.py:725).

    Any failed check, and any exception raised by the operation, releases the
    connection and sends the next call down a full checkout.

    Releasing is not on its own enough, and an earlier version of this class got
    it wrong.  ``Pool.checkout`` takes a ``_MongoClientErrorHandler`` and runs it
    from its own ``except BaseException`` branch (pool.py:1117-1127); exiting the
    checkout context with ``(None, None, None)``, as ``release`` does, skips that
    branch entirely.  Without it ``Topology.handle_error`` never runs, so after a
    network error or a NotPrimary reply the server is not marked Unknown, the
    pool is not cleared, its generation is not bumped, and the server session is
    not marked dirty -- and a dirty session going back into the pool is
    forbidden by the sessions spec.  The failure is self-concealing: the two
    checks ``_usable`` relies on to notice trouble, topology identity and pool
    generation, are exactly the state ``handle_error`` would have changed.

    ``_report_error`` below builds the handler lazily, in the except branch, so
    the happy path pays nothing for it.

    Not thread safe by construction: one pinned connection cannot serve
    concurrent operations.  A pool-side implementation of the same idea would
    keep the fast path per-thread; this class is the single-threaded case, which
    is what the benchmark drives and what bounds the win.

    Known limitation, found by deadlocking on it rather than by reasoning about
    it.  A pinned connection is held out of the pool for as long as the reader
    lives, so it reduces the pool's usable size by one.  Against maxPoolSize=1
    that is total starvation: any other user of the same MongoClient blocks
    forever on checkout.  Against the default maxPoolSize=100 it is a 1%
    reduction and unnoticeable, but the failure mode is a hang rather than an
    error, which is the wrong shape for a library default.

    A pool-side version has to bound the hold, and the cheap way is to release
    when the pool has waiters -- Pool already tracks them (``requests`` and the
    ``_max_connecting_cond`` waiters), so the fast path can check one integer
    and check the connection back in.  That check is not implemented here
    because the benchmark gives every arm its own client and never contends,
    so implementing it would add cost this measurement could not justify.

    Second known gap: ``Pool.checkout`` publishes ConnectionCheckOutStarted,
    ConnectionCheckedOut and ConnectionCheckedIn (pool.py:1082-1112) and this
    fast path emits none of them, so CMAP metrics and connection logging go
    dark for every operation it serves.  Irrelevant to the benchmark, a spec
    violation for anything shipped.

    The number this class produces should therefore be read as the ceiling for
    a pool-side fast path, not as the cost of a finished one -- and the run it
    was measured on never revalidated either: 10,463 fast-path uses against 1
    full checkout means no heartbeat changed the topology in the whole run, so
    the arm paid zero re-selection cost as well.
    """

    def __init__(
        self,
        client: Any,
        read_preference: Any = ReadPreference.PRIMARY,
        operation: str = "find",
    ) -> None:
        self._client = client
        self._topology = client._topology
        self._read_preference = read_preference
        self._operation = operation
        self._server: Optional[Any] = None
        self._context: Optional[Any] = None
        self._conn: Optional[Any] = None
        self._description: Optional[Any] = None
        self._handler: Optional[Any] = None
        self._last_used = 0.0
        self.fast_path = 0
        self.full_checkout = 0

    def _conn_healthy(self, now: float) -> bool:
        """Pool._perished's conditions AND its side effects.

        Copying only the conditions is worse than not revalidating at all, and
        an earlier version of this class did exactly that.  _perished
        (pool.py:1373-1406) calls conn.close_conn(...) on every failing branch;
        returning False without closing lets release() hand the connection to
        Pool.checkin, which sees conn.closed False, calls
        update_last_checkin_time and appends it to pool.conns.  The refreshed
        checkin time then puts idle_time_seconds below _check_interval_seconds,
        so the next checkout's own _perished skips its conn_closed() probe and
        re-issues the same dead socket -- turning a failure pymongo would have
        absorbed by reconnecting into a raised AutoReconnect.  The same path
        defeats maxIdleTimeMS outright: the connection is noticed to be
        over-idle, released, and marked freshly used, so the pool never retires
        it.
        """
        conn = self._conn
        if conn is None or conn.closed:
            return False
        pool = self._server.pool
        if pool.pid != os.getpid():
            # Forked since checkout.  _get_conn resets the pool here
            # (pool.py:1143-1149); leave that to the full checkout path.
            return False
        if pool.stale_generation(conn.generation, conn.service_id):
            conn.close_conn(ConnectionClosedReason.STALE)
            return False
        idle = now - self._last_used
        max_idle = pool.opts.max_idle_time_seconds
        if max_idle is not None and idle > max_idle:
            conn.close_conn(ConnectionClosedReason.IDLE)
            return False
        interval = pool._check_interval_seconds
        if interval is not None and (interval == 0 or idle > interval):
            if conn.conn_closed():
                conn.close_conn(ConnectionClosedReason.ERROR)
                return False
        return True

    def _usable(self, now: float) -> bool:
        """Health first, then whether the selection is still current.

        The order matters and is the second thing an earlier version got wrong.
        Checking the topology first short-circuits the health checks, so a
        connection that is both dead and superseded by a topology change was
        released without being closed -- and TopologyDescription identity churns
        on every heartbeat, roughly every 10 s at the default
        heartbeatFrequencyMS, so that is the common case rather than a corner.
        """
        if not self._conn_healthy(now):
            return False
        if self._topology.description is not self._description:
            # Healthy, but the selection it was made under is stale.  No close:
            # the connection goes back to the pool intact.
            return False
        if self._server.pool.state != PoolState.READY:
            return False
        return True

    def _acquire(self, session: Any = None) -> None:
        """Full checkout, with the error handler wrapped around the whole of it.

        MongoClient wraps checkout, not just the command
        (mongo_client.py:1728-1733), and Pool.connect uses the handler during
        the handshake -- contribute_socket(conn, completed_handshake=False) and
        receive_cluster_time (pool.py:1048-1049, 1059-1060).  Passing
        handler=None here, as an earlier version did, meant a handshake or auth
        failure during re-acquisition ran no Topology.handle_error at all: the
        server stayed Known, the pool was not cleared, its generation was not
        bumped, and the handshake cluster time was dropped.
        """
        self.release()
        # Read the description before selecting, not after: a change landing
        # between the two would otherwise be recorded as already-seen and the
        # stale server held until the next unrelated invalidation.
        description = self._topology.description
        server = self._topology.select_server(
            self._read_preference, self._operation
        )
        handler = _MongoClientErrorHandler(self._client, server, session)
        try:
            context = server.checkout(handler=handler)
            conn = context.__enter__()
        except BaseException as error:
            handler.handle(type(error), error)
            raise
        self._description = description
        self._server = server
        self._context = context
        self._handler = handler
        self._conn = conn

    def _report_error(self, error: BaseException, session: Any) -> None:
        """Run the SDAM error handling Pool.checkout would have run.

        Built here rather than held on the instance so the happy path never
        constructs it.  _MongoClientErrorHandler reads the pool generation at
        construction and contribute_socket then overwrites it with the
        connection's own, which is the value that matters, so constructing late
        is safe.
        """
        conn, server = self._conn, self._server
        if conn is None or server is None:
            return
        # Reuse the handler _acquire built where possible, so `handled` is
        # tracked across the checkout and the command exactly as
        # MongoClient's single `with` block tracks it.
        handler = self._handler
        if handler is None:
            handler = _MongoClientErrorHandler(self._client, server, session)
        handler.contribute_socket(conn)
        handler.handle(type(error), error)

    def command(
        self, dbname: str, spec: dict[str, Any], session: Any = None
    ) -> dict[str, Any]:
        now = time.monotonic()
        if self._usable(now):
            self.fast_path += 1
        else:
            self._acquire(session)
            self.full_checkout += 1
        try:
            reply = self._conn.command(
                dbname, spec, session=session, client=self._client
            )
        except BaseException as error:
            self._report_error(error, session)
            self.release()
            raise
        self._last_used = time.monotonic()
        return reply

    def release(self) -> None:
        context, self._context = self._context, None
        self._conn = None
        self._server = None
        self._description = None
        self._handler = None
        if context is not None:
            context.__exit__(None, None, None)

    def __enter__(self) -> "PinnedConnection":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def _row(document: Optional[dict[str, Any]]) -> list[tuple[Any, ...]]:
    if document is None:
        return []
    return [tuple(document.get(field) for field in FIELDS)]


def _require_closed_cursor(reply: dict[str, Any]) -> None:
    """The whole safety argument for the command arms rests on this.

    Not an assert: `python3 -O` erases asserts, and this one is what stops a
    caller silently leaking a server-side cursor for cursorTimeoutMillis.
    """
    cursor_id = reply["cursor"]["id"]
    if cursor_id != 0:
        raise RuntimeError(
            f"reply carries a live cursor ({cursor_id}); it will leak for "
            "cursorTimeoutMillis. limit=1 + singleBatch should prevent this."
        )


def _first(reply: dict[str, Any]) -> Optional[dict[str, Any]]:
    batch = reply["cursor"]["firstBatch"]
    return batch[0] if batch else None


class BaselineReader:
    """bench_all_ops_layouts.py:146-168, unchanged.  The reference arm."""

    name = "baseline"

    def __init__(self, database: Any) -> None:
        self._nodes = database[BASELINE_COLLECTION]

    def __call__(self, tree_id: str, node_id: str) -> list[tuple[Any, ...]]:
        return _row(
            self._nodes.find_one(
                {"tree_id": tree_id, "node_id": node_id},
                PROJECTION,
                hint=BASELINE_INDEX,
            )
        )

    def close(self) -> None:
        pass


class CommandReader:
    """Database.command in place of find_one.

    The two put the same command on the wire -- same field set, same 303-byte
    encoded length, differing only in the order of `hint` and `projection`,
    verified with a CommandListener -- so this is not a shortcut that does less
    work.  What it drops is the Cursor object find_one builds and immediately
    discards.  (An earlier version of this file, and report.tex, called the two
    "byte-identical".  They are not; the difference is field order only.)

    Prior: -23.46 us of client CPU, paired median, 16/16 blocks
    (report/evidence/review_20260807/v2_driver_paired.json).  Re-measured here
    at 19.5-23.2 us over four runs, so the 24.4 us prior overshoots by ~10%.

    Scope: a drop-in only where the reply's cursor id is zero, which limit 1
    plus singleBatch guarantees.  Without that the caller silently leaks a
    server-side cursor for cursorTimeoutMillis, ten minutes by default.  Read
    preference, read concern and retryable reads move to the caller, and are
    the defaults here because the baseline arm uses the defaults too.
    """

    name = "command"

    def __init__(self, database: Any) -> None:
        self._database = database
        self._collection = BASELINE_COLLECTION

    def _spec(self, tree_id: str, node_id: str) -> dict[str, Any]:
        return {
            "find": self._collection,
            "filter": {"tree_id": tree_id, "node_id": node_id},
            "projection": PROJECTION,
            "hint": BASELINE_INDEX,
            "limit": 1,
            "singleBatch": True,
        }

    def __call__(self, tree_id: str, node_id: str) -> list[tuple[Any, ...]]:
        reply = self._database.command(self._spec(tree_id, node_id))
        _require_closed_cursor(reply)
        return _row(_first(reply))

    def close(self) -> None:
        pass


class PinnedReader(CommandReader):
    """CommandReader over a pinned connection.  Stacks command and pinned."""

    name = "pinned"

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self._name = database.name
        self._client = database.client
        self._pinned = PinnedConnection(self._client)
        # An explicit session, so the fast path keeps session application and
        # cluster-time gossip rather than quietly dropping them: pool.py:402-404
        # skips session._apply_to and send_cluster_time when both session and
        # client are None, which would put different bytes on the wire (no lsid,
        # no $clusterTime) and skip client._process_response.  That is 2.9 us
        # this arm declines to claim.
        self._session = self._client.start_session()

    def __call__(self, tree_id: str, node_id: str) -> list[tuple[Any, ...]]:
        reply = self._pinned.command(
            self._name, self._spec(tree_id, node_id), session=self._session
        )
        _require_closed_cursor(reply)
        return _row(_first(reply))

    @property
    def checkout_stats(self) -> dict[str, int]:
        return {
            "fast_path": self._pinned.fast_path,
            "full_checkout": self._pinned.full_checkout,
        }

    def close(self) -> None:
        self._pinned.release()
        self._session.end_session()


class NoHintReader(BaselineReader):
    """The baseline query with the hint dropped, and nothing else changed.

    The idhack arm cannot carry a hint -- any hint disqualifies IDHACK
    (query_utils.cpp:52-59) -- so part of what it gains is hint removal rather
    than the IDHACK plan.  This arm prices that part under the harness's own
    conditions, so the IDHACK figure can be stated net.

    Unhinted, this query still plans PROJECTION_SIMPLE -> FETCH -> IXSCAN on
    allops_tree_node, but it also becomes plan-cache eligible, so what this arm
    measures is everything dropping the hint buys, not only the hint parse.
    """

    name = "nohint"

    def __call__(self, tree_id: str, node_id: str) -> list[tuple[Any, ...]]:
        return _row(
            self._nodes.find_one(
                {"tree_id": tree_id, "node_id": node_id}, PROJECTION
            )
        )


class UnpinnedReader(CommandReader):
    """Connection.command with a full per-operation checkout.

    This exists to split the `command` -> `pinned` step, which bundles three
    things: Database._command's wrapper, the per-operation implicit session, and
    server selection plus pool checkout/checkin.  Crediting all three to "pool
    checkout" overstates the pool-side change, and an earlier version of this
    work did exactly that.

    This arm is identical to PinnedReader in every respect -- Connection.command,
    explicit session, same client -- except that it runs server selection and
    checkout on every operation.  `unpinned` minus `pinned` is therefore the
    pool-side cost alone, and `command` minus `unpinned` is the wrapper plus the
    implicit session.

    Measurement arm only, never a proposal: it passes handler=None because it is
    not standing in for the pool, it is reproducing what the pool already does.
    """

    name = "unpinned"

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self._name = database.name
        self._client = database.client
        self._topology = self._client._topology
        self._session = self._client.start_session()

    def __call__(self, tree_id: str, node_id: str) -> list[tuple[Any, ...]]:
        server = self._topology.select_server(ReadPreference.PRIMARY, "find")
        with server.checkout(handler=None) as conn:
            reply = conn.command(
                self._name,
                self._spec(tree_id, node_id),
                session=self._session,
                client=self._client,
            )
        _require_closed_cursor(reply)
        return _row(_first(reply))

    def close(self) -> None:
        self._session.end_session()


class IdHackReader(PinnedReader):
    """PinnedReader against the re-keyed collection.  Stacks all three.

    The filter is an equality on _id with no hint, which is what IDHACK
    requires: query_utils.cpp:52-59 disqualifies any query carrying a hint, so
    the pinned index the baseline uses would suppress the very plan this arm
    exists to take.  prepare_get_node_idhack.py asserts the plan is IDHACK
    before the collection is accepted.
    """

    name = "idhack"

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self._collection = IDHACK_COLLECTION

    def _spec(self, tree_id: str, node_id: str) -> dict[str, Any]:
        return {
            "find": self._collection,
            "filter": {"_id": idhack_key(tree_id, node_id)},
            "projection": PROJECTION,
            "limit": 1,
            "singleBatch": True,
        }


class ControlReader(BaselineReader):
    """The baseline query, unchanged, against a same-age copy of the collection.

    layout2_view_idhack is written minutes before it is measured while
    layout2_view has been resident for weeks, so a cheaper read could be cache
    state rather than the IDHACK plan.  This arm is the baseline query, the
    baseline index and the baseline ObjectId _id, on a collection built at the
    same moment as the idhack one (prepare_get_node_control.py).  Any gap
    between this arm and `baseline` is freshness; there is nothing else left
    for it to be.
    """

    name = "control"

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self._nodes = database[CONTROL_COLLECTION]


READERS = {
    cls.name: cls
    for cls in (
        BaselineReader,
        ControlReader,
        CommandReader,
        NoHintReader,
        UnpinnedReader,
        PinnedReader,
        IdHackReader,
    )
}
