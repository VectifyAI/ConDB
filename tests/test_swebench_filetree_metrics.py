from __future__ import annotations

from bench.run_swebench_filetree import aggregate, gold_cutoff_metrics, render_report


def test_gold_cutoff_metrics_use_gold_file_count_as_cutoff():
    metrics = gold_cutoff_metrics(["a.py", "x.py", "b.py"], {"a.py", "b.py"})

    assert metrics == {
        "found@gold": 1,
        "recall@gold": 0.5,
        "exact@gold": 0,
    }


def test_aggregate_recomputes_metrics_from_predictions():
    records = [
        {
            "gold": ["a.py"],
            "gold_count": 1,
            "top_k_preds": ["x.py", "a.py"],
            "num_preds": 2,
            "found@gold": 1,
            "recall@gold": 1.0,
            "exact@gold": 1,
            "rr": 1.0,
            "ndcg@10": 1.0,
            "error": None,
        }
    ]

    summary = aggregate(records)

    assert summary["found@gold"] == 0
    assert summary["recall@gold"] == 0
    assert summary["exact@gold"] == 0
    assert summary["mrr"] == 0.5
    assert summary["avg_gold"] == 1


def test_report_uses_gold_cutoff_metrics_without_fixed_hit_fields():
    records = [
        {
            "query_id": "q1",
            "repo": "repo",
            "commit": "abc",
            "snapshot_size": 10,
            "path_signal_level": 1,
            "gold": ["a.py", "b.py"],
            "gold_count": 2,
            "top_k_preds": ["a.py", "x.py"],
            "num_preds": 2,
            "error": None,
        }
    ]
    overall = aggregate(records)
    summary = {
        "config": {
            "tier": "all",
            "timestamp": "test",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "strategy": "block",
            "top_k": 10,
            "ranker": "none",
            "num_queries": 1,
            "num_snapshots": 1,
        },
        "overall": overall,
        "per_gold_count": {"2": overall},
        "per_repo": {"repo": overall},
        "per_path_signal_level": {1: overall},
        "per_snapshot_size_bucket": {"<=50": overall},
    }

    report = render_report(summary, records)

    assert "recall@gold" in report
    assert "exact@gold" in report
    assert "found@gold" in report
    assert "hit" + "@" not in report
    assert "recall" + "@10" not in report
