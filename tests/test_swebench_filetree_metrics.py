from __future__ import annotations

from bench.run_swebench_filetree import aggregate, render_report, set_metrics


def test_set_metrics_uses_the_full_returned_set():
    metrics = set_metrics(["a.py", "x.py", "b.py"], {"a.py", "b.py"})

    assert metrics == {
        "precision": 2 / 3,
        "recall": 1.0,
        "f1": 0.8,
        "exact_match": 0,
        "hit": 2,
    }


def test_aggregate_recomputes_metrics_from_predictions():
    records = [
        {
            "gold": ["a.py"],
            "gold_count": 1,
            "preds": ["x.py", "a.py"],
            "num_preds": 2,
            "precision": 1.0,
            "recall": 0.0,
            "f1": 0.0,
            "exact_match": 1,
            "error": None,
        }
    ]

    summary = aggregate(records)

    assert summary["precision"] == 0.5
    assert summary["recall"] == 1.0
    assert summary["f1"] == 2 / 3
    assert summary["exact_match"] == 0
    assert summary["mrr"] == 0.5
    assert summary["avg_gold"] == 1


def test_report_uses_set_metrics_without_fixed_cutoff_fields():
    records = [
        {
            "query_id": "q1",
            "repo": "repo",
            "commit": "abc",
            "snapshot_size": 10,
            "path_signal_level": 1,
            "gold": ["a.py", "b.py"],
            "gold_count": 2,
            "preds": ["a.py", "x.py"],
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

    assert "precision" in report
    assert "exact_match" in report
    assert "| recall |" in report
    assert "hit" + "@" not in report
    assert "recall" + "@gold" not in report
