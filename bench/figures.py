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
from .pricing import cache_frame_cost, flat_cost, price_run, rates_for
from .schema import AggResult, RunRecord


def _median(xs: list[float]) -> float:
    """Plain median (stdlib statistics not used to keep the import surface tiny)."""
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def _uncached_input_tok(r: RunRecord) -> int:
    """Per-run uncached input = input billed at the full input rate (neither
    written nor read). Prefer the record's own field; else reconstruct from the
    token identity input − write − read (floored at 0)."""
    u = getattr(r, "uncached_input_tokens", 0) or 0
    if u:
        return int(u)
    return max(0, int(r.input_tokens) - int(r.cache_write_tok) - int(r.cache_read_tok))


# ── aggregation (ledger -> per-arm rollups) ──────────────────────────────────
def aggregate(records: Iterable[RunRecord],
              model_id: str = "", *, baseline_arm: str = "baseline") -> list[AggResult]:
    """Roll per-run RunRecords up into one AggResult per arm.

    infra_failed rows are excluded. Computes the FULL KPI set:

      - cost (both frames): cache-aware total (prefers the record's own
        runner-priced cost_usd; else re-prices the usage series via price_run,
        which itself prefers the REAL cache fields and falls back to prompt-growth
        inference) AND the list-price upper bound (list_frame_cost via flat_cost).
      - cache: token-weighted cache_hit_rate = Σread / max(1, Σread+Σwrite+Σuncached),
        plus the per-rate dollar buckets (write/read/output $).
      - outcome/effort: limit_death_rate, mean_steps, mean time-to-submit (over
        runs that submitted), mean peak single-call prompt.
      - cost summaries: median per-run cost, cost_per_success (alias of
        cost_per_solved_usd).

    vs-baseline savings %s are NOT filled here (the aggregator doesn't pick a
    baseline policy); `fill_savings()` / the report fills them once the baseline
    arm is chosen. `baseline_arm` is accepted for signature symmetry but only
    used by the report layer.
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
        unc = sum(_uncached_input_tok(r) for r in rs)

        cost = 0.0
        cost_flat = 0.0
        write_usd = read_usd = output_usd = 0.0
        per_run_cost: list[float] = []
        for r in rs:
            mid = r.model or model_id
            rates = rates_for(mid)
            # cache-aware total: trust the runner's own price when present,
            # otherwise re-price the usage series (real fields preferred).
            if r.cost_usd:
                rc = float(r.cost_usd)
            elif r.usage:
                rc = price_run(r.usage, rates).total_usd
            else:
                rc = flat_cost(r.input_tokens, r.output_tokens, rates)
            cost += rc
            per_run_cost.append(rc)
            cost_flat += flat_cost(r.input_tokens, r.output_tokens, rates)
            # per-rate dollar buckets: prefer the record's own split; else derive
            # the cache buckets at table rates and back out output as the residual.
            wr = getattr(r, "cache_write_usd", 0.0) or 0.0
            rr = getattr(r, "cache_read_usd", 0.0) or 0.0
            orr = getattr(r, "output_usd", 0.0) or 0.0
            if not (wr or rr or orr):
                wr = r.cache_write_tok / 1e6 * rates["cache_write"]
                rr = r.cache_read_tok / 1e6 * rates["cache_read"]
                orr = r.output_tokens / 1e6 * rates["output"]
            write_usd += wr
            read_usd += rr
            output_usd += orr

        # token-weighted hit rate across the whole arm
        denom = cr + cw + unc
        hit_rate = cr / denom if denom else 0.0

        # outcome / effort
        limit_deaths = sum(1 for r in rs if getattr(r, "limit_death", False))
        steps = [getattr(r, "steps", 0) or r.calls for r in rs]
        submitted = [getattr(r, "time_to_submit_s", 0.0) for r in rs
                     if getattr(r, "time_to_submit_s", 0.0)]
        peaks = [getattr(r, "max_prompt_tokens", 0) for r in rs]

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
            # outcome / effort
            limit_death_rate=limit_deaths / n if n else 0.0,
            mean_steps=sum(steps) / n if n else 0.0,
            mean_time_to_submit_s=sum(submitted) / len(submitted) if submitted else 0.0,
            mean_max_prompt_tokens=sum(peaks) / n if n else 0.0,
            # cache (token-weighted)
            cache_hit_rate=hit_rate,
            cache_write_usd=write_usd,
            cache_read_usd=read_usd,
            output_usd=output_usd,
            # cost summaries
            median_cost_usd=_median(per_run_cost),
            cost_per_success=cost / max(n_succ, 1),
        ))
    # stable, friendly order: cheapest $/solved first
    out.sort(key=lambda a: a.cost_per_solved_usd)
    return out


def fill_savings(aggs: Sequence[AggResult], baseline_arm: str = "baseline") -> None:
    """Fill the vs-baseline savings %s in place against the baseline arm.

    Positive = this arm SAVES vs the baseline. If no arm matches `baseline_arm`,
    the most expensive arm by list-frame cost is used as the reference so the
    deltas are still meaningful. The baseline itself gets 0.0 across the board.
    """
    if not aggs:
        return
    base = next((a for a in aggs if a.arm.lower() == baseline_arm.lower()), None)
    if base is None:
        base = max(aggs, key=lambda a: a.cost_usd_flat)
    bi, bo, bc = base.input_tokens, base.output_tokens, base.cost_usd
    for a in aggs:
        a.input_saving_pct = 100 * (1 - a.input_tokens / bi) if bi else 0.0
        a.output_saving_pct = 100 * (1 - a.output_tokens / bo) if bo else 0.0
        a.cost_saving_pct = 100 * (1 - a.cost_usd / bc) if bc else 0.0


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


# ── fig 6: cache hit rate comparison (the headline competitor claim) ─────────
def fig_cache_hit_rate(aggs: Sequence[AggResult], results_dir: str | Path,
                       *, stem: str = "cache_hit_rate",
                       title: str = "Cache hit rate by arm") -> list[Path]:
    """Per-arm token-weighted cache hit rate, gradient-filled bars sorted
    highest-first. "Cache hit rate" is the headline number a competitor leads
    with, so this figure is built to SING: each bar is the read share of total
    input work, the hero arm lit with the full horizon gradient, a dotted
    reference line at the best peer, and the read/write/uncached token split
    annotated underneath. Measured uniformly from the API usage series across
    every arm — vendor-internal evictions aren't observable, but everything the
    API reports is."""
    import numpy as np
    import matplotlib.pyplot as plt

    style.apply_style()
    aggs = sorted(aggs, key=lambda a: a.cache_hit_rate, reverse=True)
    n = len(aggs)
    fig, ax = plt.subplots(figsize=(max(7.5, 1.3 * n + 3), 5.6))
    style.vector_motif(ax)

    width = 0.62
    best_peer = max((a.cache_hit_rate for a in aggs if not _is_hero(a.arm)),
                    default=0.0)
    for i, a in enumerate(aggs):
        h = 100 * a.cache_hit_rate
        base = style.arm_color(a.arm, i)
        if _is_hero(a.arm):
            c_lo, c_hi = style.INDIGO, style.CYAN
        else:
            c_lo, c_hi = style.lighten(base, 0.45), base
        if h > 0:
            style.gradient_fill_bar(ax, i, h, width, c_lo, c_hi, radius=0.4)
        ax.text(i, h + 1.6, f"{h:.0f}%", ha="center", va="bottom",
                fontsize=12, fontweight="bold",
                color=style.INDIGO if _is_hero(a.arm) else style.INK)
        # read/write/uncached split underneath
        tot = a.cache_read_tok + a.cache_write_tok + max(
            0, a.input_tokens - a.cache_read_tok - a.cache_write_tok)
        unc = max(0, a.input_tokens - a.cache_read_tok - a.cache_write_tok)
        if tot:
            ax.text(i, -6.5,
                    f"R {a.cache_read_tok/1e6:.1f}M · W {a.cache_write_tok/1e6:.1f}M"
                    f"\nU {unc/1e6:.1f}M",
                    ha="center", va="top", fontsize=7.5, color=style.MUTED)

    # dotted reference at the best peer (the bar the hero must clear)
    if best_peer > 0:
        ax.axhline(100 * best_peer, color=style.HAIRLINE, lw=1.2, ls=(0, (3, 3)),
                   zorder=1)
        ax.text(n - 0.5, 100 * best_peer + 1.0, "best peer",
                ha="right", va="bottom", fontsize=8, color=style.MUTED)

    ax.set_xticks(range(n))
    ax.set_xticklabels([f"$\\bf{{{a.arm}}}$" if _is_hero(a.arm) else a.arm
                        for a in aggs])
    ax.set_ylabel("cache hit rate (read share of input, %)")
    ax.set_ylim(-12, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_title(title + "   ·   higher = more input served from cache")
    style.style_axes(ax, ygrid=True)
    fig.tight_layout()
    out = _save(fig, _figdir(results_dir), stem)
    plt.close(fig)
    return out


# ── fig 7: reliability — latency + limit-death ───────────────────────────────
def fig_reliability(aggs: Sequence[AggResult], results_dir: str | Path,
                    *, stem: str = "reliability",
                    title: str = "Reliability: latency & limit-death") -> list[Path]:
    """Two-panel reliability story per arm. LEFT: mean wall-clock seconds per run
    (gradient bars, lower is better). RIGHT: limit-death rate — the share of runs
    that hit the call/wall cap WITHOUT ever submitting, a productive-death failure
    mode distinct from a graded wrong answer. A compression layer that inflates
    context or stalls shows up here even when its $/solved looks fine, so this
    figure guards the headline against a hollow win."""
    import matplotlib.pyplot as plt

    style.apply_style()
    arms = sorted(aggs, key=lambda a: a.mean_wall_s)
    n = len(arms)
    # explicit margins (not tight_layout): the gradient bars are aspect="auto"
    # imshow artists, which tight_layout flags as incompatible.
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(max(10.0, 1.5 * n + 5), 5.4))

    # LEFT — mean wall seconds
    width = 0.62
    wmax = max((a.mean_wall_s for a in arms), default=1.0) or 1.0
    for i, a in enumerate(arms):
        h = a.mean_wall_s
        base = style.arm_color(a.arm, i)
        c_lo, c_hi = ((style.INDIGO, style.CYAN) if _is_hero(a.arm)
                      else (style.lighten(base, 0.45), base))
        if h > 0:
            style.gradient_fill_bar(axL, i, h, width, c_lo, c_hi,
                                    radius=min(0.1, h * 0.02))
        axL.text(i, h + wmax * 0.025, f"{h:.0f}s", ha="center", va="bottom",
                 fontsize=10, fontweight="bold",
                 color=style.INDIGO if _is_hero(a.arm) else style.INK)
        if a.mean_time_to_submit_s:
            axL.text(i, -wmax * 0.05, f"submit {a.mean_time_to_submit_s:.0f}s",
                     ha="center", va="top", fontsize=7.5, color=style.MUTED)
    axL.set_xticks(range(n))
    axL.set_xticklabels([f"$\\bf{{{a.arm}}}$" if _is_hero(a.arm) else a.arm
                         for a in arms], fontsize=9)
    axL.set_ylabel("mean wall time per run (s)")
    axL.set_ylim(-wmax * 0.12, wmax * 1.18)
    axL.set_title("Latency  ·  lower is better")
    style.style_axes(axL, ygrid=True)

    # RIGHT — limit-death rate (sorted lowest first; lower is better)
    armsR = sorted(aggs, key=lambda a: a.limit_death_rate)
    dmax = max((a.limit_death_rate for a in armsR), default=0.0)
    ymax = max(0.05, dmax * 1.25)
    for i, a in enumerate(armsR):
        h = a.limit_death_rate
        base = style.arm_color(a.arm, i)
        deaths = round(h * a.n)
        if h <= 0:
            # a perfect record: no bar to draw (a zero-height gradient fill is a
            # degenerate, singular imshow extent). Mark it as a deliberate
            # "clean" tick + label instead, so the win reads, not a gap.
            axR.plot([i - width / 2, i + width / 2], [0, 0],
                     color=style.MINT, lw=3, solid_capstyle="round", zorder=3)
            axR.text(i, ymax * 0.03, "clean", ha="center", va="bottom",
                     fontsize=10, fontweight="bold", color=style.MINT)
        else:
            # rose when any deaths — reliability reads at a glance
            if _is_hero(a.arm):
                c_lo, c_hi = style.lighten(style.ROSE, 0.3), style.ROSE
            else:
                c_lo, c_hi = style.lighten(base, 0.45), base
            style.gradient_fill_bar(axR, i, h, width, c_lo, c_hi,
                                    radius=min(0.004, h * 0.05))
            axR.text(i, h + ymax * 0.03, f"{100*h:.0f}%", ha="center", va="bottom",
                     fontsize=10, fontweight="bold",
                     color=style.INDIGO if _is_hero(a.arm) else style.INK)
        axR.text(i, -ymax * 0.05, f"{deaths}/{a.n}", ha="center", va="top",
                 fontsize=7.5, color=style.MUTED)
    axR.set_xticks(range(n))
    axR.set_xticklabels([f"$\\bf{{{a.arm}}}$" if _is_hero(a.arm) else a.arm
                         for a in armsR], fontsize=9)
    axR.set_ylabel("limit-death rate (capped without submitting)")
    axR.set_ylim(-ymax * 0.12, ymax)
    axR.set_title("Limit-death  ·  lower is better")
    style.style_axes(axR, ygrid=True)

    fig.suptitle(title, fontsize=14, fontweight="bold", y=0.99)
    fig.subplots_adjust(left=0.08, right=0.97, top=0.86, bottom=0.12, wspace=0.30)
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
    # new KPI figures: the headline cache-hit-rate claim + reliability guardrail
    out["cache_hit_rate"] = fig_cache_hit_rate(A, results_dir)
    out["reliability"] = fig_reliability(A, results_dir)
    return out
