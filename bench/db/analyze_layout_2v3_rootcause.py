#!/usr/bin/env python3
"""Assemble the layout 2-vs-3 causal experiments into one audit artifact."""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text())
    if value.get("status") != "complete":
        raise SystemExit(f"incomplete input: {path}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    root = "bench/db/runs/rootcause_20260718"
    parser.add_argument("--rootcause", default=f"{root}/rootcause_10m_3x.json")
    parser.add_argument("--batch-id", default=f"{root}/mongo_batch_idonly_10m_3x.json")
    parser.add_argument("--batch-covered", default=f"{root}/mongo_batch_covered_10m_3x.json")
    parser.add_argument("--profile", default=f"{root}/mongo_profile_10m_3x.json")
    parser.add_argument(
        "--count", default=f"{root}/count_command_forced_index_all200_10m_5x.json"
    )
    parser.add_argument(
        "--keyshape", default=f"{root}/keyshape_binary_collation_1m_5x.json"
    )
    parser.add_argument(
        "--perf", default=f"{root}/mongo_count_perf_summary.json"
    )
    parser.add_argument(
        "--pg-perf", default=f"{root}/postgres_count_perf_summary.json"
    )
    parser.add_argument(
        "--perf-stat", default=f"{root}/cross_engine_count_perf_stat_summary.json"
    )
    parser.add_argument("--out", default=f"{root}/rootcause_analysis.json")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    parser.add_argument(
        "--pg-dsn",
        default="host=localhost port=55432 dbname=bench user=postgres password=bench",
    )
    args = parser.parse_args()

    causal = load(args.rootcause)
    batch_id = load(args.batch_id)
    batch_covered = load(args.batch_covered)
    profile = load(args.profile)
    count = load(args.count)
    keyshape = load(args.keyshape)
    perf = load(args.perf)
    pg_perf = load(args.pg_perf)
    perf_stat = load(args.perf_stat)
    summary = causal["summary"]
    by = summary["by_engine_arm"]

    def mean(engine: str, arm: str, key: str) -> float:
        return float(by[engine][arm][key]["mean"])

    baseline_components = {
        "fetch_ms": {
            "mongo": mean("mongo", "baseline", "fetch_ms"),
            "postgres": mean("postgres", "baseline", "fetch_ms"),
        },
        "client_processing_ms": {
            "mongo": mean("mongo", "baseline", "normalize_ms")
            + mean("mongo", "baseline", "cleanup_ms"),
            "postgres": mean("postgres", "baseline", "normalize_ms")
            + mean("postgres", "baseline", "cleanup_ms"),
        },
        "total_ms": {
            "mongo": mean("mongo", "baseline", "total_ms"),
            "postgres": mean("postgres", "baseline", "total_ms"),
        },
    }
    for component in baseline_components.values():
        component["delta"] = round(component["mongo"] - component["postgres"], 6)
    total_delta = baseline_components["total_ms"]["delta"]
    for name in ("fetch_ms", "client_processing_ms"):
        baseline_components[name]["delta_share"] = round(
            baseline_components[name]["delta"] / total_delta, 6
        )

    repeat_deltas = []
    for repeat in range(causal["repeats"]):
        means = {}
        for engine in ("mongo", "postgres"):
            values = [
                sample["total_ms"]
                for sample in causal["samples"]
                if sample["engine"] == engine
                and sample["arm"] == "baseline"
                and sample["repeat"] == repeat
            ]
            means[engine] = statistics.mean(values)
        repeat_deltas.append(
            {
                "repeat": repeat,
                "mongo_ms": round(means["mongo"], 6),
                "postgres_ms": round(means["postgres"], 6),
                "delta_ms": round(means["mongo"] - means["postgres"], 6),
            }
        )

    server = summary["server_plan_delta"]
    mongo_server = summary["server_plan_summary"]["mongo"]
    server_rows = mongo_server["id_only"]["avg_rows"]
    server_means = {
        arm: {
            "mongo_ms": server[arm]["mongo_ms"],
            "postgres_ms": server[arm]["postgres_ms"],
            "delta_ms": server[arm]["mongo_minus_postgres_ms"],
            "ratio": round(server[arm]["mongo_ms"] / server[arm]["postgres_ms"], 6),
        }
        for arm in ("baseline", "covered", "id_only")
    }
    interventions = {
        "document_or_heap_fetch_penalty_ms": {
            "mongo": round(
                server_means["baseline"]["mongo_ms"]
                - server_means["covered"]["mongo_ms"],
                6,
            ),
            "postgres": round(
                server_means["baseline"]["postgres_ms"]
                - server_means["covered"]["postgres_ms"],
                6,
            ),
        },
        "title_summary_output_penalty_ms": {
            "mongo": round(
                server_means["covered"]["mongo_ms"]
                - server_means["id_only"]["mongo_ms"],
                6,
            ),
            "postgres": round(
                server_means["covered"]["postgres_ms"]
                - server_means["id_only"]["postgres_ms"],
                6,
            ),
        },
    }
    interventions["postgres_extra_fetch_penalty_vs_mongo_ms"] = round(
        interventions["document_or_heap_fetch_penalty_ms"]["postgres"]
        - interventions["document_or_heap_fetch_penalty_ms"]["mongo"],
        6,
    )

    def batch_effect(result: dict[str, Any]) -> dict[str, Any]:
        default = result["summary"]["default"]
        large = result["summary"]["1000000"]
        return {
            "paths": default["paths"],
            "avg_rows": default["avg_rows"],
            "default_total_ms": default["total_ms"]["mean"],
            "large_batch_total_ms": large["total_ms"]["mean"],
            "latency_removed_ms": round(
                default["total_ms"]["mean"] - large["total_ms"]["mean"], 6
            ),
            "default_commands": default["commands"]["mean"],
            "large_batch_commands": large["commands"]["mean"],
            "commands_removed": round(
                default["commands"]["mean"] - large["commands"]["mean"], 6
            ),
        }

    from pymongo import MongoClient
    import psycopg

    mongo = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
    mongo_stats = mongo["bench"].command("collStats", "layout2_view", scale=1)
    pg = psycopg.connect(args.pg_dsn, autocommit=True)
    pg_index_size = pg.execute(
        "SELECT pg_relation_size('layout2_pg_view_path_node_idx')"
    ).fetchone()[0]
    pg.close()
    mongo.close()

    output = {
        "status": "complete",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "rootcause": args.rootcause,
            "batch_id": args.batch_id,
            "batch_covered": args.batch_covered,
            "profile": args.profile,
            "count": args.count,
            "keyshape": args.keyshape,
            "perf": args.perf,
            "pg_perf": args.pg_perf,
            "perf_stat": args.perf_stat,
        },
        "baseline_200_paths": {
            "avg_rows": by["mongo"]["baseline"]["avg_rows"],
            "components": baseline_components,
            "repeat_deltas": repeat_deltas,
        },
        "server_interventions_40_stratified_paths": {
            "avg_rows": server_rows,
            "means": server_means,
            "interventions": interventions,
        },
        "mongo_batch_intervention_40_stratified_paths": {
            "id_only": batch_effect(batch_id),
            "covered": batch_effect(batch_covered),
        },
        "scalar_count_intervention_200_paths": count["summary"],
        "keyshape_intervention": keyshape["summary"],
        "mongo_count_cpu_profile": perf,
        "postgres_count_cpu_profile": pg_perf,
        "cross_engine_count_cpu_counters": perf_stat,
        "mongo_profiler": profile["summary"],
        "lean_index_bytes": {
            "mongo": mongo_stats["indexSizes"]["path_1_node_id_1"],
            "postgres": pg_index_size,
            "mongo_over_postgres": round(
                mongo_stats["indexSizes"]["path_1_node_id_1"] / pg_index_size, 6
            ),
        },
        "causal_findings": [
            "MongoDB document FETCH is not the cross-engine cause: covering widens, rather than closes, the server gap.",
            "The title/summary output is not the cause: its covered server penalty is nearly equal in both engines.",
            "The default extra getMore is not the cause: removing about one command does not reduce latency.",
            "The gap survives scalar output and the aggregation wrapper: MongoDB direct COUNT over COUNT_SCAN is slower than PostgreSQL index-only COUNT on all 200 paths.",
            "The MongoDB profiler reports zero storage bytes read and CPU time approximately equal to wall time, so this retained-data path is CPU-bound.",
            "Binary-collation key-shape controls show that MongoDB's per-key slope is nearly invariant across integer, short-string, and long-path keys; materialized-path length is not the cause.",
            "PostgreSQL also performs B-tree advance, index-tuple decoding, visibility checks, executor scan, and count aggregation; its CPU profile does not support claiming that PostgreSQL avoids these generic steps.",
            "Matched hardware counters show that MongoDB executes 2.17x as many instructions and consumes 2.30x as many cycles per counted key as PostgreSQL, closely reproducing the 2.29x latency ratio.",
            "Comparative CPU sampling attributes MongoDB's heavier per-key implementation path to WiredTiger cursor advance/key extraction plus classic COUNT_SCAN, WorkingSet, RecordId, timer, and yield bookkeeping; __wt_btcur_next is the largest individual MongoDB hotspot.",
            "Per-row BSON/Python work adds cost downstream but is not the origin of the server scan gap.",
        ],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
