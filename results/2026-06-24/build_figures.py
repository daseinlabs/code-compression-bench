#!/usr/bin/env python3
"""Clean, fully-controlled figure generator for the code-compression benchmark.

Reads summary_rich.json and renders a cohesive, presentation-grade figure set.
Every value label sits OUTSIDE its bar (no in-bar clipping); titles/subtitles/
legends are placed with explicit margins (no overlaps). Emerald-hero / slate
brand system. Data-driven: nothing hardcoded except arm order + brand colors.
"""
from __future__ import annotations
import json, os, sys, textwrap
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import numpy as np

RESULTS = os.environ.get("CC_RESULTS", "/home/nicks/cc_bench_results")
RICH = os.path.join(RESULTS, "summary_rich.json")
OUT = os.path.join(RESULTS, "figures2")
os.makedirs(OUT, exist_ok=True)

D = json.load(open(RICH))
A = D["arms"]
N = D["matched_n"]

# ── brand ──────────────────────────────────────────────────────────────────
HERO = "#10b981"; HERO_DARK = "#047857"; HERO_SOFT = "#d1fae5"
SLATE = "#64748b"; SLATE_DK = "#334155"
COL = {"dasein": HERO, "A0": SLATE, "woz": "#8b9bc4", "headroom": "#c9a26b", "rtk": "#b48ab8"}
EDGE = {"dasein": HERO_DARK, "A0": SLATE_DK, "woz": "#5f6f9c", "headroom": "#a07f47", "rtk": "#8c6690"}
INK = "#1e293b"; INK_SOFT = "#64748b"; GRID = "#e6ebf1"
BAD = "#d98a8a"; BAD_DK = "#b04a4a"
LABEL = {"dasein": "dasein", "A0": "A0  baseline", "woz": "woz", "headroom": "headroom", "rtk": "rtk"}

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Arial", "Liberation Sans", "sans-serif"],
    "font.size": 12, "axes.edgecolor": "#cbd5e1", "axes.linewidth": 1.0,
    "text.color": INK, "axes.labelcolor": INK, "xtick.color": INK_SOFT, "ytick.color": INK_SOFT,
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    "figure.dpi": 150, "savefig.dpi": 150,
})
FOOT = f"Code-Compression Bench   ·   matched set n={N}   ·   cache-aware Sonnet-4.6 pricing   ·   SWE-bench Verified (context-heavy long tail)"

def frame(ax):
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.tick_params(length=0)

def titleblock(fig, title, subtitle, wrap=94):
    fig.text(0.045, 0.95, title, fontsize=22, fontweight="bold", color=INK, ha="left", va="top")
    lines = textwrap.wrap(subtitle, width=wrap)
    for i, ln in enumerate(lines[:2]):
        fig.text(0.045, 0.878 - i * 0.046, ln, fontsize=12.5, color=INK_SOFT, ha="left", va="top")

def footer(fig):
    fig.text(0.045, 0.022, FOOT, fontsize=9, color="#94a3b8", ha="left", va="bottom")

def val(metric):
    return {a: A[a][metric] for a in A}

def hbar(fname, title, subtitle, values, fmt, *, better="low", xlabel="",
         scale=1.0, note=None, headline=None):
    """Horizontal bars, sorted best->worst by `better`, hero highlighted,
    value labels OUTSIDE each bar."""
    items = [(a, values[a]) for a in values]
    items.sort(key=lambda kv: kv[1], reverse=(better == "high"))
    arms = [a for a, _ in items]; vals = [v * scale for _, v in items]
    fig = plt.figure(figsize=(11.0, 6.6))
    ax = fig.add_axes([0.20, 0.165, 0.74, 0.60])
    y = np.arange(len(arms))[::-1]
    bars = ax.barh(y, vals, height=0.62,
                   color=[COL[a] for a in arms], edgecolor=[EDGE[a] for a in arms],
                   linewidth=[2.2 if a == "dasein" else 1.0 for a in arms], zorder=3)
    vmax = max(vals)
    ax.set_xlim(0, vmax * 1.20)
    for yi, a, v in zip(y, arms, vals):
        ax.text(v + vmax * 0.015, yi, fmt(v), va="center", ha="left",
                fontsize=13.5, fontweight="bold" if a == "dasein" else "normal",
                color=HERO_DARK if a == "dasein" else INK, zorder=4)
    ax.set_yticks(y)
    ax.set_yticklabels([LABEL[a] for a in arms], fontsize=13.5)
    for t, a in zip(ax.get_yticklabels(), arms):
        t.set_color(HERO_DARK if a == "dasein" else INK_SOFT)
        t.set_fontweight("bold" if a == "dasein" else "normal")
    ax.set_xlabel(xlabel, fontsize=12, color=INK_SOFT)
    ax.xaxis.grid(True, color=GRID, linewidth=1, zorder=0)
    ax.set_axisbelow(True)
    frame(ax)
    titleblock(fig, title, subtitle)
    footer(fig)
    if headline:
        # rounded callout pinned top-right of the plot area
        fig.text(0.945, 0.70, headline, fontsize=12.5, fontweight="bold", color=HERO_DARK,
                 ha="right", va="top",
                 bbox=dict(boxstyle="round,pad=0.5", fc=HERO_SOFT, ec=HERO, lw=1.4))
    fig.savefig(os.path.join(OUT, fname))
    plt.close(fig)

# ── 1. savings vs baseline (diverging hero) ─────────────────────────────────
def fig_savings():
    arms = ["dasein", "woz", "rtk", "headroom"]
    v = {a: A[a]["vs_a0_cost_pct"] for a in arms}
    items = sorted(v.items(), key=lambda kv: kv[1])  # most negative (best) first
    arms = [a for a, _ in items]; vals = [x for _, x in items]
    fig = plt.figure(figsize=(11.4, 6.6))
    ax = fig.add_axes([0.16, 0.17, 0.78, 0.58])
    y = np.arange(len(arms))[::-1]
    colors = [HERO if x < 0 else BAD for x in vals]
    edges = [HERO_DARK if x < 0 else BAD_DK for x in vals]
    lw = [2.4 if a == "dasein" else 1.0 for a in arms]
    ax.barh(y, vals, height=0.6, color=colors, edgecolor=edges, linewidth=lw, zorder=3)
    lo, hi = min(vals) - 14, max(vals) + 14
    ax.set_xlim(lo, hi)
    ax.axvline(0, color=SLATE_DK, lw=1.6, zorder=4)
    # shaded good/bad bands
    ax.axvspan(lo, 0, color=HERO, alpha=0.05, zorder=0)
    ax.axvspan(0, hi, color=BAD, alpha=0.05, zorder=0)
    for yi, a, x in zip(y, arms, vals):
        ha = "right" if x < 0 else "left"
        off = -1.6 if x < 0 else 1.6
        ax.text(x + off, yi, f"{x:+.0f}%", va="center", ha=ha,
                fontsize=15, fontweight="bold",
                color=HERO_DARK if x < 0 else BAD_DK, zorder=5)
    ax.set_yticks(y); ax.set_yticklabels([LABEL[a] for a in arms], fontsize=14)
    for t, a in zip(ax.get_yticklabels(), arms):
        t.set_color(HERO_DARK if a == "dasein" else INK_SOFT)
        t.set_fontweight("bold" if a == "dasein" else "normal")
    ax.set_xlabel("total cost vs the no-compression baseline   (negative = cheaper)", fontsize=12, color=INK_SOFT)
    ax.xaxis.grid(True, color=GRID, linewidth=1, zorder=0); ax.set_axisbelow(True)
    frame(ax)
    ax.text(0, 1.04, "A0 baseline", transform=ax.get_xaxis_transform(),
            ha="center", va="bottom", fontsize=11, fontweight="bold", color=SLATE_DK)
    ax.text(0.02, -0.16, "cheaper than no compression", transform=ax.transAxes,
            ha="left", color=HERO_DARK, fontsize=11)
    ax.text(0.98, -0.16, "costlier than no compression", transform=ax.transAxes,
            ha="right", color=BAD_DK, fontsize=11)
    titleblock(fig, "Total cost relative to the no-compression baseline",
               "Change in cache-aware total cost versus running the identical agent with no compression layer. Negative is cheaper.")
    footer(fig)
    fig.savefig(os.path.join(OUT, "1_savings_vs_baseline.png")); plt.close(fig)

# ── 2. cost per solved ──────────────────────────────────────────────────────
def fig_cost_per_solved():
    hbar("2_cost_per_solved.png",
         "Cost per solved task",
         "Cache-aware total cost divided by the number of tasks the official SWE-bench Docker grader passed. Lower is better.",
         val("cost_per_solved"), lambda v: f"${v:,.2f}",
         better="low", xlabel="cost per solved task  (USD)")

# ── 3. total cost ───────────────────────────────────────────────────────────
def fig_total_cost():
    hbar("3_total_cost.png",
         "Total cost over the matched set",
         f"Cache-aware Sonnet-4.6 cost to run all {N} matched tasks, summed across every API call.",
         val("cost_total"), lambda v: f"${v:,.0f}",
         better="low", xlabel="total cost  (USD)")

# ── 4. wall-clock speed ─────────────────────────────────────────────────────
def fig_speed():
    hbar("4_wall_time.png",
         "Wall-clock time over the matched set",
         "End-to-end hours to complete the run, including each layer's own orchestration overhead.",
         val("wall_h_total"), lambda v: f"{v:,.1f} h",
         better="low", xlabel="wall-clock hours")

# ── 5. steps ────────────────────────────────────────────────────────────────
def fig_steps():
    hbar("5_steps.png",
         "Agent steps over the matched set",
         "Total agent turns summed across the 100 tasks. Fewer turns indicates a tighter solve loop.",
         val("steps_total"), lambda v: f"{v:,.0f}",
         better="low", xlabel="agent steps")

# ── 6. peak working context ─────────────────────────────────────────────────
def fig_context():
    hbar("6_peak_context.png",
         "Peak working context",
         "Mean across tasks of the largest single prompt the model received during the solve.",
         val("max_prompt_mean"), lambda v: f"{v/1000:,.1f}K",
         better="low", xlabel="peak prompt tokens (mean per task)")

# ── 7. input tokens ─────────────────────────────────────────────────────────
def fig_input():
    hbar("7_input_tokens.png",
         "Input tokens delivered to the model",
         "Total input tokens billed across the run, summed over every API call (the post-compression context each layer actually sent).",
         {a: A[a]["input_tokens"] / 1e6 for a in A}, lambda v: f"{v:,.0f}M",
         better="low", xlabel="input tokens (millions)")

# ── 8. savings vs solve-rate scatter ────────────────────────────────────────
def fig_scatter():
    fig = plt.figure(figsize=(11.4, 7.0))
    ax = fig.add_axes([0.115, 0.135, 0.83, 0.60])
    # x = solve rate (%, right is better); y = cost savings vs baseline (%, up is better)
    pts = {a: (A[a]["solve_rate"], -A[a]["vs_a0_cost_pct"]) for a in A}
    xs = [p[0] for p in pts.values()]; ys = [p[1] for p in pts.values()]
    xlo, xhi = min(xs) - 3, max(xs) + 3.5
    ylo, yhi = min(ys) - 12, max(ys) + 12
    ax.set_xlim(xlo, xhi); ax.set_ylim(ylo, yhi)
    # baseline = zero savings; above it is cheaper than no compression, below is costlier
    ax.axhspan(0, yhi, color=HERO, alpha=0.05, zorder=0)
    ax.axhspan(ylo, 0, color=BAD, alpha=0.05, zorder=0)
    ax.axhline(0, color=SLATE_DK, ls=(0, (4, 4)), lw=1.3, zorder=1)
    ax.text(xlo + 0.3, 1.5, "baseline (no compression)", va="bottom", ha="left", color=SLATE_DK, fontsize=10)
    ax.text(0.015, 0.965, "cheaper than baseline", transform=ax.transAxes, color=HERO_DARK, fontsize=10.5, va="top")
    ax.text(0.015, 0.035, "costlier than baseline", transform=ax.transAxes, color=BAD_DK, fontsize=10.5, va="bottom")
    # white halo + uniform markers
    for a, (x, yv) in pts.items():
        ax.scatter([x], [yv], s=520, color="white", edgecolor="white", zorder=4.4)
    for a, (x, yv) in pts.items():
        big = a == "dasein"
        ax.scatter([x], [yv], s=300, color=COL[a], edgecolor=EDGE[a],
                   linewidth=2.2 if big else 1.3, zorder=5)
    place = {  # dx, dy, ha, va  (A0 y=0 and woz y≈+6 both at x=62 → split vertically)
        "dasein":   (0.7, 0.0, "left", "center"),
        "woz":      (0.7, 2.0, "left", "bottom"),
        "A0":       (0.7, -2.0, "left", "top"),
        "rtk":      (0.7, 0.0, "left", "center"),
        "headroom": (0.7, 0.0, "left", "center"),
    }
    for a, (dx, dy, ha, va) in place.items():
        x, yv = pts[a]; big = a == "dasein"
        slab = "0%" if abs(yv) < 0.5 else f"{yv:+.0f}%"
        ax.text(x + dx, yv + dy, f"{LABEL[a].split('  ')[0]}  {slab}",
                ha=ha, va=va, fontsize=13.5 if big else 12,
                fontweight="bold" if big else "normal",
                color=HERO_DARK if big else INK, zorder=6)
    ax.set_xlabel("solve rate  (% of matched tasks the grader passed)  → more is better", fontsize=12, color=INK_SOFT)
    ax.set_ylabel("cost savings vs baseline (%)  ↑ more savings is better", fontsize=12, color=INK_SOFT)
    from matplotlib.ticker import FuncFormatter
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+.0f}%"))
    ax.grid(True, color=GRID, linewidth=1, zorder=0); ax.set_axisbelow(True)
    frame(ax); ax.spines["left"].set_visible(True); ax.spines["left"].set_color("#cbd5e1")
    titleblock(fig, "Cost savings versus solve rate",
               "Each arm over the n=100 matched set. Up is more cost savings versus the no-compression baseline; right is more tasks solved. The top-right corner is best.")
    footer(fig)
    fig.savefig(os.path.join(OUT, "8_cost_vs_success.png")); plt.close(fig)

# ── 9. cache health ─────────────────────────────────────────────────────────
def fig_cache():
    arms = ["A0", "dasein", "woz", "headroom", "rtk"]
    v = {a: A[a]["cr_cw"] for a in arms}
    fig = plt.figure(figsize=(11.4, 6.6))
    ax = fig.add_axes([0.10, 0.16, 0.86, 0.585])
    x = np.arange(len(arms))
    vals = [v[a] for a in arms]
    colors = [COL[a] for a in arms]
    edges = [EDGE[a] for a in arms]
    ax.bar(x, vals, width=0.62, color=colors, edgecolor=edges,
           linewidth=[2.2 if a == "dasein" else 1.0 for a in arms], zorder=3)
    ax.set_xlim(-0.6, len(arms) - 0.4)
    ax.set_ylim(0, max(vals) * 1.18)
    for xi, a, vv in zip(x, arms, vals):
        ax.text(xi, vv + max(vals) * 0.02, f"{vv:.1f}:1", ha="center", va="bottom",
                fontsize=13, fontweight="bold" if a == "dasein" else "normal",
                color=HERO_DARK if a == "dasein" else INK)
    ax.set_xticks(x); ax.set_xticklabels([LABEL[a].replace("  ", "\n") for a in arms], fontsize=12.5)
    for t, a in zip(ax.get_xticklabels(), arms):
        t.set_color(HERO_DARK if a == "dasein" else INK_SOFT)
        t.set_fontweight("bold" if a == "dasein" else "normal")
    ax.set_ylabel("cache read : write ratio", fontsize=12, color=INK_SOFT)
    ax.yaxis.grid(True, color=GRID, linewidth=1, zorder=0); ax.set_axisbelow(True)
    frame(ax)
    titleblock(fig, "Cache reuse: read-to-write ratio",
               "Cached input is billed far below fresh input, so a higher read:write ratio is cheaper. A layer that rewrites the prefix each turn re-pays the cache-write rate.")
    footer(fig)
    fig.savefig(os.path.join(OUT, "9_cache_health.png")); plt.close(fig)

for f in (fig_savings, fig_cost_per_solved, fig_total_cost, fig_speed, fig_steps,
          fig_context, fig_input, fig_scatter, fig_cache):
    f(); print("ok", f.__name__)
print("DONE ->", OUT)
