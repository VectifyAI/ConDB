#!/usr/bin/env python3
"""Build every table in the bottleneck document straight out of the retained artifacts.

Nothing here computes a new measurement.  It reads the JSON files written by the
harnesses and prints the tables with the file each number came from, so the
document can be regenerated and each line checked against a file on disk.

    python3 bench/db/assemble_bottleneck_report.py > bench/db/runs/bottleneck_20260806/TABLES.txt
"""

from __future__ import annotations

import json
from pathlib import Path

RUN = Path("bench/db/runs/bottleneck_20260806")


def load(name: str):
    p = RUN / name
    if not p.exists():
        return None
    return json.loads(p.read_text())


def rule(title: str, source: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print(f"source: {source}")
    print("=" * 100)


def main() -> None:
    log_on = load("mongo_cpu_arms.json")
    log_off = load("mongo_cpu_arms_nolog.json")
    pgp = load("pg_psycopg_cpu.json")
    pgq = load("pg_cpu_arms.json")
    prof = load("server_profile.json")
    slow = load("slowlog_cost.json")

    def M(doc, arm, field="cpu_us_per_op_median"):
        if not doc:
            return None
        a = doc.get("arms", {}).get(arm)
        return a.get(field) if a else None

    def P(arm):
        if not pgp:
            return None
        a = pgp.get("arms", {}).get(arm)
        return a.get("cpu_us_per_op") if a else None

    # ---------------------------------------------------------------- 1
    rule("1. LIKE-FOR-LIKE SERVER CPU PER OPERATION (microseconds)",
         "mongo_cpu_arms.json, mongo_cpu_arms_nolog.json, pg_psycopg_cpu.json")
    print("MongoDB   = CPU on the mongod connection thread, /proc/<pid>/task/<tid>/schedstat")
    print("PostgreSQL= CPU (utime+stime) of the backend process serving the connection")
    print("Both exclude client cost. Both go through a published container port.")
    print("'log on' is slowms=0, the setting this server already had. PostgreSQL's")
    print("log_min_duration_statement is -1, so the 'log off' column is the matched one.")
    print()
    print("%-16s %12s %12s | %12s %12s | %10s %10s" % (
        "operation", "mongo_logon", "mongo_logoff", "pg_prepared", "pg_unprep",
        "M/PG_prep", "M/PG_unprep"))
    rows = [
        ("floor", "ping", "pg_select_1"),
        ("get_node", "get_node_hit", "pg_get_node"),
        ("get_children", "get_children_hit", "pg_get_children"),
        ("get_entity", "get_entity_hit", "pg_get_entity"),
        ("get_subtree", "get_subtree_full", "pg_get_subtree_full"),
    ]
    for label, marm, parm in rows:
        mon = M(log_on, marm)
        moff = M(log_off, marm)
        pp = P(f"{parm}__prepared")
        pu = P(f"{parm}__unprepared")
        r1 = round(moff / pp, 2) if moff and pp else None
        r2 = round(moff / pu, 2) if moff and pu else None
        print("%-16s %12s %12s | %12s %12s | %10s %10s"
              % (label, mon, moff, pp, pu, r1, r2))
    print()
    print("The two ratio columns use the logging-matched MongoDB number, because")
    print("PostgreSQL is not writing a log line per operation.")

    # ---------------------------------------------------------------- 2
    rule("2. WHAT PER-OPERATION SLOW-QUERY LOGGING COSTS (slowms=0)",
         "slowlog_cost.json")
    if slow:
        print("%-20s %12s %12s %12s %9s | %12s" % (
            "arm", "log_on", "log_off", "cost_us", "share", "clone_cost"))
        d = slow["derived"]["real"]
        c = slow["derived"].get("local_clone", {})
        for k, v in d.items():
            cc = c.get(k, {}).get("logging_cost_us")
            print("%-20s %12.1f %12.1f %12.1f %8.1f%% | %12s"
                  % (k, v["logging_on_us"], v["logging_off_us"], v["logging_cost_us"],
                     (v["logging_share_of_on"] or 0) * 100, cc))
        print()
        print("The clone column is the same measurement driven the opposite way")
        print("(logging turned ON on a server that had it off), as an independent check.")
        print("PostgreSQL logging settings on the same box:")
        for line in slow.get("postgresql_logging_settings", []):
            print(f"   {line}")

    # ---------------------------------------------------------------- 3
    rule("3. get_node ABLATION LADDER, logging off (CPU us/op, each line a difference of two measured arms)",
         "mongo_cpu_arms_nolog.json")
    if log_off:
        steps = [
            ("wire receive + dispatch + trivial command + reply", "ping", None),
            ("+ namespace, collection acquisition, empty-filter\n"
             "  canonicalisation, COLLSCAN plan, executor setup/teardown",
             "find_empty_collection", "ping"),
            ("+ 2-predicate filter canonicalisation, index bounds,\n"
             "  planning against a 5-index 10M collection, hinted IXSCAN setup",
             "get_node_miss_noproj", "find_empty_collection"),
            ("+ 7-field projection AST parse and analysis",
             "get_node_miss", "get_node_miss_noproj"),
            ("+ index seek hit, FETCH of the document,\n  projection transform, document in the reply",
             "get_node_hit", "get_node_miss"),
        ]
        total = 0.0
        for label, arm, base in steps:
            v = M(log_off, arm)
            b = M(log_off, base) if base else 0.0
            if v is None or b is None:
                continue
            total = v
            print("%8.2f  %s" % (v - b, label.replace("\n", "\n          ")))
        print("%8.2f  = get_node_hit total" % total)
        print()
        print("supporting arms:")
        for arm in ("get_node_hit_proj1", "get_node_hit_noproj", "get_node_hit_nohint"):
            print("   %-24s %8s" % (arm, M(log_off, arm)))
        miss_proj = (M(log_off, "get_node_miss") or 0) - (M(log_off, "get_node_miss_noproj") or 0)
        hit_proj = (M(log_off, "get_node_hit") or 0) - (M(log_off, "get_node_hit_noproj") or 0)
        print()
        print("   projection cost on a miss (parse+analysis only) : %6.2f us" % miss_proj)
        print("   projection cost on a hit  (parse+analysis+apply): %6.2f us" % hit_proj)
        print("   => applying the projection to one document      : %6.2f us" % (hit_proj - miss_proj))

    # ---------------------------------------------------------------- 4
    rule("4. INDEX-COUNT ABLATION: what fillOutIndexEntries actually costs",
         "mongo_cpu_arms_nolog.json (probe_idx{2,5,8}_hit)")
    if log_off:
        for arm in ("probe_idx2_hit", "probe_idx5_hit", "probe_idx8_hit"):
            print("   %-18s %8s us/op" % (arm, M(log_off, arm)))
        a, b = M(log_off, "probe_idx2_hit"), M(log_off, "probe_idx8_hit")
        if a and b:
            print("   6 extra indexes change the cost by %.2f us (%.2f us per index)"
                  % (b - a, (b - a) / 6))
        print("   Identical documents and identical hinted query; only the index count differs.")
        print("   get_executor.cpp:328-360 confirms fillOutIndexEntries walks every ready")
        print("   index with no hint-based early exit, so this is a real test of that cost.")

    # ---------------------------------------------------------------- 5
    rule("5. get_subtree: where the operation actually is",
         "server_profile.json (raw_profile_get_subtree.jsonl), mongo_cpu_arms*.json")
    if prof and "get_subtree" in prof["shapes"]:
        s = prof["shapes"]["get_subtree"]
        print("mongod's own cpuNanos, per component, %d client operations:" % s["iterations"])
        for comp, c in s["components"].items():
            print("   %-52s cpu %10.1f us  planning(wall) %5s us  rows %s"
                  % (comp, c["cpu_us_p50"], c["planning_wall_us_p50"], c["nreturned_p50"]))
        w = s["whole_operation_cpu_us_from_components"]
        gm = s["components"].get("getmore", {}).get("cpu_us_p50")
        print("   %-52s     %10.1f us" % ("whole operation", w))
        if gm:
            print("   getMore is %.1f%% of the operation's server CPU, and does no planning."
                  % (gm / w * 100))
    if log_off:
        scan = M(log_off, "get_subtree_scan")
        cnt = M(log_off, "get_subtree_scan_count")
        wall = M(log_off, "get_subtree_scan", "wall_us_per_op_median")
        cwall = M(log_off, "get_subtree_scan_count", "wall_us_per_op_median")
        print()
        print("   (logging off, mongo_cpu_arms_nolog.json)")
        print("   covered scan, fully drained : %9.1f us CPU   %9.1f us wall" % (scan, wall))
        print("   same index range, count only: %9.1f us CPU   %9.1f us wall" % (cnt, cwall))
        print("   => lower bound on everything past the raw walk: %.1f us." % (scan - cnt))
        print("      NOT a split: count_documents runs COUNT_SCAN plus a $group over every")
        print("      row (count_arm_plan_check.txt), so it skips the key decode AND adds")
        print("      per-row accumulation. Use the perf figures for the split.")
        print("   => %.1f us of the %.1f us wall (%.0f%%) is not server CPU at all"
              % (wall - scan, wall, (wall - scan) / wall * 100))
        print("      (client BSON decode + network). The count arm, whose response is one")
        print("      number, has CPU and wall within %.0f%% of each other, which is what"
              % (abs(cwall - cnt) / cwall * 100))
        print("      makes that attributable to moving 11,686 documents to the client.")

    # ---------------------------------------------------------------- 6
    rule("6. THE FLOOR: what survives even if planning were free",
         "mongo_cpu_arms*.json, pg_psycopg_cpu.json, method_validation.json")
    mv = load("method_validation.json")
    print("   MongoDB ping (no namespace, no plan, no collection):")
    print("      logging on  %6s us    logging off %6s us" % (M(log_on, "ping"), M(log_off, "ping")))
    print("   MongoDB get_entity (IDHACK, the cheapest real read):")
    print("      logging on  %6s us    logging off %6s us"
          % (M(log_on, "get_entity_hit"), M(log_off, "get_entity_hit")))
    if mv:
        for k, v in mv.get("profiler_reconciliation", {}).items():
            print("   %s: whole-thread %.1f us vs mongod's own cpuNanos %.1f us -> %.1f us "
                  "outside the command window (receive, dispatch, reply)"
                  % (k, v["profiler_off_thread_cpu_us_per_op"], v["profile_cpu_us_p50"],
                     v["gap_us_per_op"]))
    print("   PostgreSQL whole operation for get_entity: %s us prepared, %s us unprepared"
          % (P("pg_get_entity__prepared"), P("pg_get_entity__unprepared")))

    # ---------------------------------------------------------------- 7
    rule("7. PLAN CACHE", "plan_cache_per_shape_raw.txt, plan_cache_probe_raw.txt")
    print("   see the raw files: 100 identical executions of each of five shapes gave")
    print("   +100 misses and +0 hits on serverStatus metrics.query.planCache.classic,")
    print("   and $planCacheStats listed zero entries for every one of them.")

    print()
    print("=" * 100)
    print("Artifacts present in", RUN)
    for p in sorted(RUN.iterdir()):
        if p.is_file():
            print("   %-52s %10.1f KB" % (p.name, p.stat().st_size / 1024))
        else:
            n = sum(1 for _ in p.iterdir())
            size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            print("   %-52s %10.1f KB  (%d files)" % (p.name + "/", size / 1024, n))


if __name__ == "__main__":
    main()
