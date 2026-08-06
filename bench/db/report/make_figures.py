#!/usr/bin/env python3
"""Generate report figures from the benchmark result JSON files."""
import json
from pathlib import Path

import matplotlib as mpl
mpl.use("Agg")
import matplotlib.pyplot as plt

mpl.rcParams.update({
    "font.family": "serif",
    "font.size": 10,
    "axes.labelsize": 10,
    "axes.titlesize": 10,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.fontsize": 9,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.linewidth": 0.7,
    "axes.edgecolor": "#444444",
    "figure.dpi": 200,
})

RUNS = Path(__file__).resolve().parents[1] / "runs"
OUT = Path(__file__).resolve().parent / "figures"
OUT.mkdir(exist_ok=True)

ENGINES = ["mongo", "postgres", "duckdb", "sqlite"]
LABEL = {"mongo": "MongoDB", "postgres": "PostgreSQL", "duckdb": "DuckDB", "sqlite": "SQLite"}
COLOR = {"mongo": "#00684A", "postgres": "#336791", "duckdb": "#D9822B", "sqlite": "#1B2A4A"}
MARK = {"mongo": "o", "postgres": "s", "duckdb": "^", "sqlite": "D"}


def load(name):
    return json.load(open(RUNS / name))


def fig_concurrency():
    d = load("concurrency_medium.json")["engines"]
    levels = load("concurrency_medium.json")["concurrency_levels"]
    fig, ax = plt.subplots(figsize=(4.2, 2.55))
    for e in ENGINES:
        thr = [lv["throughput_ops_s"] / 1000.0 for lv in d[e]["levels"]]
        ax.plot(levels, thr, marker=MARK[e], label=LABEL[e], color=COLOR[e],
                markeredgecolor="white", markeredgewidth=0.6, markersize=5.5, linewidth=1.8)
    ax.set_xscale("log", base=2)
    ax.set_xticks(levels)
    ax.set_xticklabels(levels)
    ax.set_xlabel("Concurrent client processes")
    ax.set_ylabel("Throughput (k ops/s)")
    ax.grid(True, which="major", linestyle="-", linewidth=0.4, alpha=0.35)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, handlelength=1.6, borderaxespad=0.3)
    fig.tight_layout(pad=0.4)
    fig.savefig(OUT / "concurrency.pdf", bbox_inches="tight")
    plt.close(fig)


def fig_subtree():
    # Cross-engine subtree figures come only from the matched matrix.
    # Each configuration uses the same logical relation, result set, sampled
    # paths, and index capability on MongoDB and PostgreSQL.
    data = {
        "mongo": load("fair_sg2/fair_mid3m_mongo.json")["arms"],
        "postgres": load("fair_sg2/fair_mid3m_postgres.json")["arms"],
    }
    arms = ["naive", "covered", "deployed"]
    arm_labels = ["Naive", "Covered", "Narrow\nstructure"]
    x = list(range(len(arms)))
    w = 0.36
    fig, axes = plt.subplots(1, 2, figsize=(6.4, 2.65))

    for ax, percentile in zip(axes, ("p50_ms", "p95_ms")):
        mongo = [data["mongo"][arm][percentile] for arm in arms]
        postgres = [data["postgres"][arm][percentile] for arm in arms]
        bm = ax.bar([i - w / 2 for i in x], mongo, w, label="MongoDB",
                    color=COLOR["mongo"], edgecolor="white", linewidth=0.6)
        bp = ax.bar([i + w / 2 for i in x], postgres, w, label="PostgreSQL",
                    color=COLOR["postgres"], edgecolor="white", linewidth=0.6)
        ax.set_yscale("log")
        ax.set_ylim(0.18, max(mongo + postgres) * 1.9)
        ax.set_xticks(x)
        ax.set_xticklabels(arm_labels)
        ax.set_title(percentile[:3].upper())
        ax.grid(True, axis="y", which="major", linestyle="-", linewidth=0.4, alpha=0.35)
        ax.set_axisbelow(True)

        for bars, vals in ((bm, mongo), (bp, postgres)):
            for bar, value in zip(bars, vals):
                ax.annotate(f"{value:.2f}",
                            (bar.get_x() + bar.get_width() / 2, value),
                            textcoords="offset points", xytext=(0, 2),
                            ha="center", va="bottom", fontsize=7,
                            color="#333333")

    axes[0].set_ylabel("Latency (ms, log scale)")
    handles, labels = axes[1].get_legend_handles_labels()
    fig.legend(handles, labels, frameon=False, handlelength=1.2, ncol=2,
               loc="lower center", bbox_to_anchor=(0.5, 0.0))
    fig.tight_layout(rect=(0, 0.13, 1, 1), pad=0.55, w_pad=1.0)
    fig.savefig(OUT / "subtree.pdf", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    fig_concurrency()
    fig_subtree()
    print("wrote", OUT / "concurrency.pdf", "and", OUT / "subtree.pdf")
