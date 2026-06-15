"""Launch-quality figures from RunRecords / AggResults.

Every figure here speaks the brand language defined in `bench.style`: light
canvas, horizon-gradient fills, the embedding-vector motif, hairline frame.
They are intentionally NOT default-matplotlib.

The figure set:
  fig_cost_per_solved   per-arm $/solved-task bars, gradient-filled, hero arm lit
  fig_success_vs_cost   success-rate vs $/solved scatter (the efficiency frontier)
  fig_tokens_saved      tokens saved vs the baseline arm, per arm
  fig_cost_distribution violin/strip of per-run cost by arm (the tail story)
  fig_leaderboard       the hero leaderboard graphic (ranked cards)

All take a list of RunRecord (per-run) and/or AggResult (per-arm rollup) and a
results directory; each saves PNG (and SVG where cheap) under
<results>/figures/ and returns the saved Path(s).

Aggregation helper `aggregate()` turns raw RunRecords into AggResults using the
same cache-aware pricing the runner used, so figures can be regenerated from a
ledger alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import style
from .pricing import cache_frame_cost, flat_cost, rates_for
from .schema import AggResult, RunRecord


# ── aggregation (ledger -> per-arm rollups) ──────────────────────────────────
def aggregate(records: Iterable[RunRecord],
              model_id: str = "") -> list[AggResult]:
    """Roll per-run RunRecords up into one AggResult per arm.

    infra_failed rows are excluded. Dollar fields prefer the record's own
    cost_usd (already cache-priced by the runner); when absent (0) they are
    recomputed from the per-call `usage` series via cache_frame_cost so figures
    can be rebuilt from a bare ledger. cost_usd_flat is the list-price bound.
    """
    by: dict[str, list[RunRecord]] = {}
    for r in records:
        if getattr(r, "infra_failed", False):
            continue
        by.setdefault(r.arm, []).append(r)

    out: list[AggResult] = []
    for arm, rs in by.items():
        n = len(rs)
        n_succ = sum(1 for r in rs if r.success)
        in_tok = sum(r.input_tokens for r in rs)
        out_tok = sum(r.output_tokens for r in rs)
        cw = sum(r.cache_write_tok for r in rs)
        cr = sum(r.cache_read_tok for r in rs)

        cost = 0.0
        cost_flat = 0.0
        for r in rs:
            mid = r.model or model_id
            rates = rates_for(mid)
            if r.cost_usd:
                cost += r.cost_usd
            elif r.usage:
                cb = cache_frame_cost(
                    [u["prompt_tokens"] for u in r.usage],
                    [u["completion_tokens"] for u in r.usage],
                    rates,
                )
                cost += cb.total_usd
            else:
                cost += flat_cost(r.input_tokens, r.output_tokens, rates)
            cost_flat += flat_cost(r.input_tokens, r.output_tokens, rates)

        out.append(AggResult(
            arm=arm, n=n, n_success=n_succ,
            success_rate=n_succ / n if n else 0.0,
            input_tokens=in_tok, output_tokens=out_tok,
            cache_write_tok=cw, cache_read_tok=cr,
            cost_usd=cost, cost_usd_flat=cost_flat,
            cost_per_solved_usd=cost / max(n_succ, 1),
            mean_calls=sum(r.calls for r in rs) / n if n else 0.0,
            mean_wall_s=sum(r.wall_s for r in rs) / n if n else 0.0,
            total_wall_s=sum(r.wall_s for r in rs),
        ))
    # stable, friendly order: cheapest $/solved first
    out.sort(key=lambda a: a.cost_per_solved_usd)
    return out


# ── small drawing helpers ─────────────────────────────────────────────────────
def _figdir(results_dir: str | Path) -> Path:
    d = Path(results_dir) / "figures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(fig, figdir: Path, stem: str, *, svg: bool = True) -> list[Path]:
    paths = [figdir / f"{stem}.png"]
    fig.savefig(paths[0])
    if svg:
        p = figdir / f"{stem}.svg"
        try:
            fig.savefig(p)
            paths.append(p)
        except Exception:
            pass
    return paths


def _usd(v: float) -> str:
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:.2f}"
    return f"${v:.3f}"


def _is_hero(arm: str) -> bool:
    return arm.lower() == "dasein"


# ── fig 1: $/solved-task bars ────────────────────────────────────────────────
def fig_cost_per_solved(aggs: Sequence[AggResult], results_dir: str | Path,
                        *, stem: str = "cost_per_solved",
                        title: str = "Cost per solved task") -> list[Path]:
    """Per-arm $/solved-task, gradient-filled bars sorted cheapest-first. The
    hero arm is lit with the full horizon gradient; peers get a muted version of
    their own hue. The headline figure of the leaderboard."""
    import matplotlib.pyplot as plt

    style.apply_style()
    aggs = sorted(aggs, key=lambda a: a.cost_per_solved_usd)
    n = len(aggs)
    fig, ax = plt.subplots(figsize=(max(7.5, 1.25 * n + 3), 5.4))
    style.vector_motif(ax)

    vmax = max((a.cost_per_solved_usd for a in aggs), default=1.0)
    width = 0.62
    for i, a in enumerate(aggs):
        h = a.cost_per_solved_usd
        base = style.arm_color(a.arm, i)
        if _is_hero(a.arm):
            c_lo, c_hi = style.INDIGO, style.CYAN          # full horizon for the hero
        else:
            c_lo, c_hi = style.lighten(base, 0.45), base   # subtle lift
        style.gradient_fill_bar(ax, i, h, width, c_lo, c_hi,
                                radius=min(0.10, h * 0.02) if h else 0)
        # value label
        ax.text(i, h + vmax * 0.025, _usd(h), ha="center", va="bottom",
                fontsize=11, fontweight="bold",
                color=style.INK if not _is_hero(a.arm) else style.INDIGO)
        # solved/n subtext
        ax.text(i, -vmax * 0.045, f"{a.n_success}/{a.n}", ha="center", va="top",
                fontsize=9, color=style.MUTED)

    ax.set_xticks(range(n))
    labels = []
    for a in aggs:
        lab = a.arm
        labels.append(f"$\\bf{{{lab}}}$" if _is_hero(a.arm) else lab)
    ax.set_xticklabels(labels)
    ax.set_ylabel("$ per solved task (cache-priced)")
    ax.set_ylim(0, vmax * 1.18)
    ax.set_title(title)
    style.style_axes(ax, ygrid=True)
    fig.tight_layout()
    out = _save(fig, _figdir(results_dir), stem)
    plt.close(fig)
    return out


# ── fig 2: success vs cost scatter (efficiency frontier) ─────────────────────
def fig_success_vs_cost(aggs: Sequence[AggResult], results_dir: str | Path,
                        *, stem: str = "success_vs_cost",
                        title: str = "Success rate vs cost") -> list[Path]:
    """Each arm a bubble: x = $/solved (log), y = success rate, area ~ runs.
    The bottom-right-to-top-left frontier is what matters; the hero arm is
    ringed. A faint horizon band sits behind the plot."""
    import numpy as np
    import matplotlib.pyplot as plt

    style.apply_style()
    fig, ax = plt.subplots(figsize=(8.2, 5.6))

    # faint horizon wash behind the data
    grad = np.linspace(0, 1, 256).reshape(1, -1)
    ax.imshow(grad, extent=(0, 1, 0, 1), transform=ax.transAxes, aspect="auto",
              cmap=style.gradient_cmap(), alpha=0.05, zorder=0)

    xs = [max(a.cost_per_solved_usd, 1e-6) for a in aggs]
    ys = [100 * a.success_rate for a in aggs]
    for i, a in enumerate(aggs):
        c = style.arm_color(a.arm, i)
        size = 220 + 26 * a.n
        ax.scatter(xs[i], ys[i], s=size, color=c, alpha=0.85, zorder=3,
                   edgecolors=style.PANEL, linewidths=1.5)
        if _is_hero(a.arm):
            ax.scatter(xs[i], ys[i], s=size + 520, facecolors="none",
                       edgecolors=style.INDIGO, linewidths=2.2, zorder=4)
        ax.annotate(a.arm, (xs[i], ys[i]),
                    textcoords="offset points", xytext=(0, -size ** 0.5 / 2 - 14),
                    ha="center", fontsize=10,
                    fontweight="bold" if _is_hero(a.arm) else "normal",
                    color=style.INK)

    ax.set_xscale("log")
    ax.set_xlabel("$ per solved task (cache-priced, log)")
    ax.set_ylabel("success rate (%)")
    ax.set_ylim(0, max(ys + [1]) * 1.18)
    ax.set_title(title + "   ·   up-and-left is better")
    # arrow toward the good corner
    ax.annotate("", xy=(0.06, 0.94), xytext=(0.20, 0.80), xycoords="axes fraction",
                arrowprops=dict(arrowstyle="-|>", color=style.MUTED, lw=1.4))
    style.style_axes(ax, ygrid=True, xgrid=True)
    fig.tight_layout()
    out = _save(fig, _figdir(results_dir), stem)
    plt.close(fig)
    return out


# ── fig 3: tokens saved vs baseline ──────────────────────────────────────────
def fig_tokens_saved(aggs: Sequence[AggResult], results_dir: str | Path,
                     *, baseline_arm: str = "baseline", stem: str = "tokens_saved",
                     title: str = "Input tokens saved vs baseline") -> list[Path]:
    """Diverging bars of (input) tokens saved relative to the baseline arm.
    Savings to the right in horizon-warm; regressions to the left in rose."""
    import matplotlib.pyplot as plt

    style.apply_style()
    base = next((a for a in aggs if a.arm.lower() == baseline_arm.lower()), None)
    base_tok = base.input_tokens if base else max(
        (a.input_tokens for a in aggs), default=0)
    others = [a for a in aggs if not (base and a.arm == base.arm)]
    others.sort(key=lambda a: a.input_tokens)  # most saved (smallest) first -> top

    fig, ax = plt.subplots(figsize=(8.4, max(3.2, 0.7 * len(others) + 2)))
    ys = range(len(others))
    for i, a in enumerate(others):
        saved = base_tok - a.input_tokens
        pct = 100 * saved / base_tok if base_tok else 0.0
        c_hi = style.CORAL if saved >= 0 else style.ROSE
        ax.barh(i, saved, color=c_hi, height=0.6, zorder=3,
                edgecolor=style.PANEL, linewidth=0)
        lab_x = saved + (base_tok * 0.01 if saved >= 0 else -base_tok * 0.01)
        ax.text(lab_x, i, f"{saved/1e6:+.2f}M  ({pct:+.0f}%)",
                va="center", ha="left" if saved >= 0 else "right",
                fontsize=10, color=style.INK,
                fontweight="bold" if _is_hero(a.arm) else "normal")

    ax.axvline(0, color=style.HAIRLINE, lw=1.2, zorder=2)
    ax.set_yticks(list(ys))
    ax.set_yticklabels([a.arm for a in others])
    ax.set_xlabel(f"input tokens saved vs {base.arm if base else 'max'} "
                  f"(positive = cheaper)")
    ax.set_title(title)
    style.style_axes(ax, ygrid=False, xgrid=True)
    fig.tight_layout()
    out = _save(fig, _figdir(results_dir), stem)
    plt.close(fig)
    return out


# ── fig 4: per-run cost distribution (the tail) ──────────────────────────────
def fig_cost_distribution(records: Sequence[RunRecord], results_dir: str | Path,
                          *, stem: str = "cost_distribution",
                          title: str = "Per-run cost distribution") -> list[Path]:
    """Violin + jittered strip of per-run cost_usd by arm. Compression's real
    win lives in the right tail — a few runaway runs — so this shows the whole
    spread, not just the mean."""
    import numpy as np
    import matplotlib.pyplot as plt

    style.apply_style()
    by: dict[str, list[float]] = {}
    for r in records:
        if getattr(r, "infra_failed", False):
            continue
        by.setdefault(r.arm, []).append(max(r.cost_usd, 0.0))
    arms = sorted(by, key=lambda a: np.median(by[a]))
    if not arms:
        return []

    fig, ax = plt.subplots(figsize=(max(7.5, 1.2 * len(arms) + 3), 5.4))
    data = [by[a] for a in arms]
    parts = ax.violinplot(data, positions=range(len(arms)), widths=0.8,
                          showextrema=False, showmedians=False)
    rng = np.random.default_rng(3)
    for i, (a, vals) in enumerate(zip(arms, data)):
        c = style.arm_color(a, i)
        body = parts["bodies"][i]
        body.set_facecolor(style.lighten(c, 0.55))
        body.set_edgecolor(c)
        body.set_alpha(0.9)
        body.set_linewidth(1.2)
        # jittered points
        jx = i + rng.uniform(-0.12, 0.12, size=len(vals))
        ax.scatter(jx, vals, s=14, color=style.darken(c, 0.1), alpha=0.45, zorder=3)
        # median tick
        med = float(np.median(vals))
        ax.plot([i - 0.22, i + 0.22], [med, med], color=style.INK, lw=2, zorder=4)
        ax.text(i, med, f"  {_usd(med)}", va="center", ha="left",
                fontsize=9, color=style.INK)

    ax.set_xticks(range(len(arms)))
    ax.set_xticklabels([f"$\\bf{{{a}}}$" if _is_hero(a) else a for a in arms])
    ax.set_ylabel("$ per run (cache-priced)")
    ax.set_yscale("symlog", linthresh=0.01)
    ax.set_title(title + "   ·   median tick; the tail is the story")
    style.style_axes(ax, ygrid=True)
    fig.tight_layout()
    out = _save(fig, _figdir(results_dir), stem)
    plt.close(fig)
    return out


# ── fig 5: the hero leaderboard graphic ──────────────────────────────────────
def fig_leaderboard(aggs: Sequence[AggResult], results_dir: str | Path,
                    *, rank_key=None, rank_label: str = "$ / solved task",
                    stem: str = "leaderboard",
                    title: str = "Compression leaderboard") -> list[Path]:
    """A launch-quality ranked-card graphic: one row per arm, ranked by the given
    key (default $/solved-task), with a horizon accent bar whose length encodes
    the metric and a #1 medal on the leader. Built to drop straight into a README
    hero slot."""
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    style.apply_style()
    key = rank_key or (lambda a: a.cost_per_solved_usd)
    ranked = sorted(aggs, key=key)
    n = len(ranked)
    vmax = max((key(a) for a in ranked), default=1.0) or 1.0

    row_h = 1.0
    fig_h = max(2.8, 1.02 * n + 1.6)
    fig, ax = plt.subplots(figsize=(9.5, fig_h))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, n * row_h + 0.8)
    ax.axis("off")

    # title band with horizon underline
    ax.text(0.15, n * row_h + 0.45, title, fontsize=17, fontweight="bold",
            color=style.INK, va="center")
    import numpy as np
    grad = np.linspace(0, 1, 256).reshape(1, -1)
    ax.imshow(grad, extent=(0.15, 4.2, n * row_h + 0.18, n * row_h + 0.24),
              aspect="auto", cmap=style.gradient_cmap(), zorder=2)

    for idx, a in enumerate(ranked):
        y = (n - 1 - idx) * row_h + 0.2
        c = style.arm_color(a.arm, idx)
        is_hero = _is_hero(a.arm)
        leader = idx == 0

        # card
        card = FancyBboxPatch(
            (0.15, y + 0.06), 9.7, row_h - 0.18,
            boxstyle="round,pad=0,rounding_size=0.10",
            facecolor=style.PANEL if not leader else style.lighten(style.INDIGO, 0.92),
            edgecolor=style.INDIGO if (leader or is_hero) else style.HAIRLINE,
            linewidth=2.0 if (leader or is_hero) else 1.0, zorder=2)
        ax.add_patch(card)

        # three stacked y-bands inside the card: name (top), caption (mid), bar (base)
        y_name = y + row_h * 0.66
        y_cap = y + row_h * 0.40
        y_bar = y + 0.13

        # rank chip
        chip_c = style.INDIGO if leader else style.MUTED
        ax.text(0.55, y + row_h / 2, f"{idx+1}", fontsize=15 if leader else 12,
                fontweight="bold", color=chip_c, ha="center", va="center", zorder=3)

        # arm name (+ medal for leader)
        name = a.arm + ("  ●" if leader else "")
        ax.text(1.15, y_name, name, fontsize=13,
                fontweight="bold" if (is_hero or leader) else "medium",
                color=style.INK, va="center", zorder=3)

        # metric value + its label
        ax.text(9.55, y_name, _usd(key(a)), fontsize=13, fontweight="bold",
                color=style.INDIGO if leader else style.INK, ha="right",
                va="center", zorder=3)
        ax.text(9.55, y_cap, rank_label, fontsize=7.5, color=style.MUTED,
                ha="right", va="center", zorder=3)

        # success-rate caption (above the bar, never overlapping it)
        ax.text(1.15, y_cap, f"{a.n_success}/{a.n} solved · "
                f"{100*a.success_rate:.0f}%", fontsize=8, color=style.MUTED,
                ha="left", va="center", zorder=3)

        # accent bar encoding the metric (shorter = better, so invert length)
        frac = 1.0 - min(key(a) / vmax, 1.0)
        bar_w = 0.4 + 4.4 * frac
        c_lo, c_hi = ((style.INDIGO, style.CYAN) if (leader or is_hero)
                      else (style.lighten(c, 0.4), c))
        style.gradient_fill_bar(ax, 1.15 + bar_w / 2, 0.085, bar_w, c_lo, c_hi,
                                bottom=y_bar, radius=0.04, zorder=3)

    fig.tight_layout()
    out = _save(fig, _figdir(results_dir), stem)
    plt.close(fig)
    return out


# ── one-call orchestrator ─────────────────────────────────────────────────────
def render_all(records: Sequence[RunRecord], results_dir: str | Path,
               *, aggs: Optional[Sequence[AggResult]] = None,
               model_id: str = "") -> dict[str, list[Path]]:
    """Render the full figure set from a ledger (RunRecords). Returns a map of
    figure stem -> saved paths. `aggs` may be supplied to reuse the report's
    ranking; otherwise they're computed here."""
    A = list(aggs) if aggs is not None else aggregate(records, model_id=model_id)
    out: dict[str, list[Path]] = {}
    out["cost_per_solved"] = fig_cost_per_solved(A, results_dir)
    out["success_vs_cost"] = fig_success_vs_cost(A, results_dir)
    out["tokens_saved"] = fig_tokens_saved(A, results_dir)
    out["cost_distribution"] = fig_cost_distribution(records, results_dir)
    out["leaderboard"] = fig_leaderboard(A, results_dir)
    return out
