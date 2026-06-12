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
    d = load("read_large.json")["engines"]
    p50 = [d[e]["queries"]["q_subtree"]["p50_ms"] for e in ENGINES]
    p95 = [d[e]["queries"]["q_subtree"]["p95_ms"] for e in ENGINES]
    x = range(len(ENGINES))
    fig, ax = plt.subplots(figsize=(4.2, 2.55))
    w = 0.36
    b1 = ax.bar([i - w / 2 for i in x], p50, w, label="P50", color="#4C78A8", edgecolor="white", linewidth=0.6)
    b2 = ax.bar([i + w / 2 for i in x], p95, w, label="P95", color="#C44E52", edgecolor="white", linewidth=0.6)
    ax.set_yscale("log")
    ax.set_ylim(top=max(p95) * 3)
    ax.set_xticks(list(x))
    ax.set_xticklabels([LABEL[e] for e in ENGINES])
    ax.set_ylabel("Latency of get_subtree (ms, log scale)")
    ax.grid(True, axis="y", which="major", linestyle="-", linewidth=0.4, alpha=0.35)
    ax.set_axisbelow(True)

    def lab(bars, vals):
        for bar, v in zip(bars, vals):
            txt = f"{v:.0f}" if v >= 10 else f"{v:.1f}"
            ax.annotate(txt, (bar.get_x() + bar.get_width() / 2, v), textcoords="offset points",
                        xytext=(0, 2), ha="center", va="bottom", fontsize=7, color="#333333")

    lab(b1, p50)
    lab(b2, p95)
    ax.legend(frameon=False, handlelength=1.2, ncol=2, loc="upper center", borderaxespad=0.2)
    fig.tight_layout(pad=0.4)
    fig.savefig(OUT / "subtree.pdf", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    fig_concurrency()
    fig_subtree()
    print("wrote", OUT / "concurrency.pdf", "and", OUT / "subtree.pdf")
