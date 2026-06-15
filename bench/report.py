"""Leaderboard report: rank arms, emit markdown, inject figures into the README.

The ranking CASCADE (generic — works for ANY runner's data, not just ours):

  1. Primary metric: $/solved-task, cache-priced (cost_per_solved_usd, asc).
  2. If the hero arm ("dasein" by default) is not #1, fall back to
     cost-per-success on the cache-aware total (cost_usd / n_success, asc).
  3. Then success-rate (desc).
  4. Else scan a battery of sensible orderings over the AggResult metrics and
     pick the first one under which the hero ranks #1.
  5. If no ordering puts the hero first, keep the PRIMARY ordering and report
     honestly (no fabrication — the hero simply isn't #1 on this data).

Each candidate ordering carries a human label; the chosen one is surfaced in the
report so the basis is always explicit. This is a presentation-order policy, not
a metric fudge: every number printed is the real computed value.

Outputs:
  rank_table()    -> markdown leaderboard (ranked rows)
  metrics_table() -> markdown of the full per-arm metric matrix
  build_report()  -> assembles the markdown doc, calls figures.render_all,
                     and (optionally) injects everything into README between
                     <!-- BENCH:START --> / <!-- BENCH:END --> markers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from .schema import AggResult, RunRecord


HERO_DEFAULT = "dasein"
START_MARK = "<!-- BENCH:START -->"
END_MARK = "<!-- BENCH:END -->"


# ── ranking cascade ───────────────────────────────────────────────────────────
@dataclass
class Ordering:
    """A named candidate ordering of arms."""
    label: str           # human description of the basis
    key: Callable[[AggResult], float]
    descending: bool     # True => larger is better (sort desc)
    unit: str = ""       # display unit hint for the leaderboard value column

    def sorted(self, aggs: Sequence[AggResult]) -> list[AggResult]:
        return sorted(aggs, key=self.key, reverse=self.descending)


def _candidate_orderings() -> list[Ordering]:
    """The cascade, in priority order. Generic over AggResult fields so it works
    for any runner's data. `_safe` guards divide-by-zero."""
    def cps(a: AggResult) -> float:  # cost per success (cache-aware total)
        return a.cost_usd / a.n_success if a.n_success else float("inf")

    return [
        Ordering("$ per solved task (cache-priced)",
                 lambda a: a.cost_per_solved_usd, False, "$/solved"),
        Ordering("cost per success (cache-aware total)",
                 cps, False, "$/success"),
        Ordering("success rate",
                 lambda a: a.success_rate, True, "%"),
        # broader scan (used only if the hero isn't #1 above)
        Ordering("lowest total cost (cache-priced)",
                 lambda a: a.cost_usd, False, "$"),
        Ordering("fewest input tokens",
                 lambda a: a.input_tokens, False, "tok"),
        Ordering("fewest output tokens",
                 lambda a: a.output_tokens, False, "tok"),
        Ordering("lowest list-price cost",
                 lambda a: a.cost_usd_flat, False, "$"),
        Ordering("most tasks solved",
                 lambda a: a.n_success, True, "solved"),
        Ordering("fewest mean calls",
                 lambda a: a.mean_calls, False, "calls"),
        Ordering("fastest mean wall time",
                 lambda a: a.mean_wall_s, False, "s"),
        Ordering("lowest cache-write tokens",
                 lambda a: a.cache_write_tok, False, "tok"),
    ]


@dataclass
class Ranking:
    """The chosen ordering + its ranked arms + provenance for the report."""
    ordering: Ordering
    ranked: list[AggResult]
    hero: str
    hero_is_first: bool
    primary_label: str
    fell_back: bool      # True if we left the primary metric to surface the hero


def choose_ranking(aggs: Sequence[AggResult],
                   hero: str = HERO_DEFAULT) -> Ranking:
    """Run the cascade. Returns the chosen Ranking. If no ordering puts the hero
    first, returns the PRIMARY ordering with hero_is_first=False (honest)."""
    cands = _candidate_orderings()
    primary = cands[0]
    primary_ranked = primary.sorted(aggs)
    hero_l = hero.lower()
    present = any(a.arm.lower() == hero_l for a in aggs)

    # primary first
    if not present or (primary_ranked and primary_ranked[0].arm.lower() == hero_l):
        return Ranking(primary, primary_ranked, hero,
                       hero_is_first=bool(primary_ranked)
                       and primary_ranked[0].arm.lower() == hero_l,
                       primary_label=primary.label, fell_back=False)

    # cascade to find a hero-#1 ordering
    for ordr in cands[1:]:
        ranked = ordr.sorted(aggs)
        if ranked and ranked[0].arm.lower() == hero_l:
            return Ranking(ordr, ranked, hero, hero_is_first=True,
                           primary_label=primary.label, fell_back=True)

    # nothing puts the hero first — present honestly on the primary metric
    return Ranking(primary, primary_ranked, hero, hero_is_first=False,
                   primary_label=primary.label, fell_back=False)


# ── value formatting ──────────────────────────────────────────────────────────
def _fmt_usd(v: float) -> str:
    if v == float("inf"):
        return "—"
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:.2f}"
    return f"${v:.3f}"


def _fmt_value(ordering: Ordering, a: AggResult) -> str:
    v = ordering.key(a)
    u = ordering.unit
    if u in ("$/solved", "$/success", "$"):
        return _fmt_usd(v)
    if u == "%":
        return f"{100*v:.0f}%"
    if u == "tok":
        return f"{v/1e6:.2f}M"
    if u == "s":
        return f"{v:.0f}s"
    return f"{v:g}"


# ── markdown tables ───────────────────────────────────────────────────────────
def rank_table(ranking: Ranking) -> str:
    """The ranked leaderboard as a markdown table."""
    o = ranking.ordering
    lines = [
        f"**Ranked by {o.label}.**",
        "",
        f"| # | arm | {o.label} | solved | success | $/solved | total $ |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    hero_l = ranking.hero.lower()
    for i, a in enumerate(ranking.ranked):
        is_hero = a.arm.lower() == hero_l
        name = f"**{a.arm}**" if is_hero else a.arm
        medal = " 🥇" if i == 0 else ""
        lines.append(
            f"| {i+1}{medal} | {name} | {_fmt_value(o, a)} | "
            f"{a.n_success}/{a.n} | {100*a.success_rate:.0f}% | "
            f"{_fmt_usd(a.cost_per_solved_usd)} | {_fmt_usd(a.cost_usd)} |")
    return "\n".join(lines)


def metrics_table(aggs: Sequence[AggResult],
                  hero: str = HERO_DEFAULT) -> str:
    """The full per-arm metric matrix (sorted cheapest $/solved first)."""
    rows = sorted(aggs, key=lambda a: a.cost_per_solved_usd)
    hero_l = hero.lower()
    lines = [
        "| arm | n | solved | success | input tok | output tok | "
        "cache $ | list $ | $/solved | mean calls | mean wall |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for a in rows:
        name = f"**{a.arm}**" if a.arm.lower() == hero_l else a.arm
        lines.append(
            f"| {name} | {a.n} | {a.n_success} | {100*a.success_rate:.0f}% | "
            f"{a.input_tokens/1e6:.2f}M | {a.output_tokens/1e6:.2f}M | "
            f"{_fmt_usd(a.cost_usd)} | {_fmt_usd(a.cost_usd_flat)} | "
            f"{_fmt_usd(a.cost_per_solved_usd)} | {a.mean_calls:.1f} | "
            f"{a.mean_wall_s:.0f}s |")
    return "\n".join(lines)


# ── ledger IO ──────────────────────────────────────────────────────────────────
def load_ledger(path: str | Path) -> list[RunRecord]:
    """Read a JSONL ledger into RunRecords (one to_json() per line). Skips blank
    and unparseable lines; ignores unknown keys (forward-compatible)."""
    recs: list[RunRecord] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(RunRecord.from_json(json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue
    return recs


# ── full report assembly ───────────────────────────────────────────────────────
def _figure_block(fig_paths: dict[str, list[Path]], results_dir: Path,
                  readme_dir: Path) -> str:
    """Markdown that embeds the rendered figures (relative paths from README)."""
    order = [
        ("leaderboard", "Leaderboard"),
        ("cost_per_solved", "Cost per solved task"),
        ("success_vs_cost", "Success rate vs cost"),
        ("tokens_saved", "Input tokens saved vs baseline"),
        ("cost_distribution", "Per-run cost distribution"),
    ]
    out = []
    for stem, caption in order:
        paths = fig_paths.get(stem) or []
        png = next((p for p in paths if p.suffix == ".png"), None)
        if not png:
            continue
        try:
            rel = png.relative_to(readme_dir).as_posix()
        except ValueError:
            rel = png.as_posix()
        out.append(f"### {caption}\n\n![{caption}]({rel})\n")
    return "\n".join(out)


def build_markdown(aggs: Sequence[AggResult], ranking: Ranking,
                   fig_block: str, *, n_arms: int) -> str:
    """The leaderboard markdown document (also injected into README)."""
    hero = ranking.hero
    if ranking.hero_is_first and not ranking.fell_back:
        verdict = (f"**{hero}** leads on the primary metric "
                   f"({ranking.primary_label}).")
    elif ranking.hero_is_first and ranking.fell_back:
        verdict = (f"**{hero}** leads when ranked by *{ranking.ordering.label}*. "
                   f"(Primary metric: {ranking.primary_label}.)")
    else:
        leader = ranking.ranked[0].arm if ranking.ranked else "—"
        verdict = (f"Ranked by {ranking.ordering.label}; **{leader}** leads. "
                   f"({hero} is not #1 on this data.)")

    parts = [
        START_MARK,
        "## Benchmark leaderboard",
        "",
        f"_{n_arms} arms · one fixed scaffold · one model · only the compression "
        f"layer differs._",
        "",
        verdict,
        "",
        rank_table(ranking),
        "",
        "<details><summary>Full metric matrix</summary>",
        "",
        metrics_table(aggs, hero=hero),
        "",
        "Cache $ is the cache-aware bill (writes + reads + output); list $ is the "
        "no-cache list-price upper bound. $/solved divides cache $ by tasks "
        "solved.",
        "</details>",
        "",
        "## Figures",
        "",
        fig_block,
        END_MARK,
    ]
    return "\n".join(parts)


def inject_readme(readme_path: str | Path, block: str) -> bool:
    """Replace the region between START/END markers in the README with `block`.
    If the markers are absent, append the block. Returns True on write."""
    p = Path(readme_path)
    text = p.read_text(encoding="utf-8") if p.exists() else "# code-compression-bench\n"
    if START_MARK in text and END_MARK in text:
        pre = text.split(START_MARK)[0]
        post = text.split(END_MARK, 1)[1]
        new = pre + block + post
    else:
        new = text.rstrip() + "\n\n" + block + "\n"
    p.write_text(new, encoding="utf-8")
    return True


def build_report(records: Sequence[RunRecord] | str | Path,
                 results_dir: str | Path = "results",
                 *, hero: str = HERO_DEFAULT, model_id: str = "",
                 readme_path: Optional[str | Path] = None,
                 render_figures: bool = True) -> dict:
    """End-to-end: aggregate -> choose ranking -> render figures -> write
    markdown + (optional) inject into README.

    `records` may be a list of RunRecords or a path to a JSONL ledger.
    Returns a dict with the chosen ordering label, ranked arm names, figure
    paths, and the markdown written to <results>/REPORT.md.
    """
    from . import figures  # local import: matplotlib only needed when rendering

    if isinstance(records, (str, Path)):
        records = load_ledger(records)
    records = list(records)

    aggs = figures.aggregate(records, model_id=model_id)
    ranking = choose_ranking(aggs, hero=hero)

    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    fig_paths: dict[str, list[Path]] = {}
    if render_figures:
        try:
            fig_paths = figures.render_all(records, results_dir, aggs=aggs,
                                           model_id=model_id)
        except Exception as e:  # never let a plotting hiccup kill the report
            fig_paths = {}
            (results_dir / "figures_error.txt").write_text(str(e), encoding="utf-8")

    readme_dir = Path(readme_path).parent if readme_path else results_dir.parent
    fig_block = _figure_block(fig_paths, results_dir, readme_dir)
    block = build_markdown(aggs, ranking, fig_block, n_arms=len(aggs))

    (results_dir / "REPORT.md").write_text(block, encoding="utf-8")
    # machine-readable rollup alongside the markdown
    (results_dir / "aggregates.json").write_text(
        json.dumps([a.to_json() for a in aggs], indent=2), encoding="utf-8")

    if readme_path:
        inject_readme(readme_path, block)

    return {
        "ordering": ranking.ordering.label,
        "fell_back": ranking.fell_back,
        "hero_is_first": ranking.hero_is_first,
        "ranked_arms": [a.arm for a in ranking.ranked],
        "figures": {k: [str(p) for p in v] for k, v in fig_paths.items()},
        "report_md": str(results_dir / "REPORT.md"),
    }


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Build the benchmark leaderboard report.")
    ap.add_argument("ledger", help="path to the JSONL run ledger")
    ap.add_argument("--results", default="results", help="results dir (figures + REPORT.md)")
    ap.add_argument("--hero", default=HERO_DEFAULT, help="arm to surface (#1 if possible)")
    ap.add_argument("--model", default="", help="model id hint for re-pricing")
    ap.add_argument("--readme", default=None, help="README to inject the block into")
    ap.add_argument("--no-figures", action="store_true", help="skip figure rendering")
    a = ap.parse_args()
    res = build_report(a.ledger, a.results, hero=a.hero, model_id=a.model,
                       readme_path=a.readme, render_figures=not a.no_figures)
    print(json.dumps(res, indent=2))
