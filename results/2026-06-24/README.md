# Code-Compression Benchmark — results, 2026-06-24

**A neutral, reproducible comparison of context-management layers for coding agents on the bloated long tail
of [SWE-bench Verified](https://www.swebench.com/).** One fixed scaffold (headless Claude Code), one model
(`claude-sonnet-4-6` on Vertex), the official SWE-bench Docker grader, and one cache-aware price table applied
to every arm. Only the compression layer changes, so any cost or quality difference is attributable to that
layer and its serving implementation.

> **▶ [Open the interactive dashboard](https://raw.githack.com/daseinlabs/code-compression-bench/master/results/2026-06-24/dashboard.html)** &nbsp;·&nbsp;
> [Fact vs fiction](fact-vs-fiction.md) &nbsp;·&nbsp; [paired.csv](paired.csv) &nbsp;·&nbsp; [summary.json](summary.json) &nbsp;·&nbsp; the full narrative lives in the [repo README](../../README.md).

_Run date: 2026-06-24 · matched set: **n=100** tasks every arm completed._

## Leaderboard

Ranked by **`$/solved`** — cache-aware total cost ÷ Docker-graded solves, the one metric that captures
efficiency and correctness together. Lower is better.

| Rank | Arm | Solved | Solve rate | $/solved | Total cost | vs A0 cost | Input | vs A0 input | Cache R:W |
|---:|:--|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | **dasein** | 50/100 | 50% | **$1.28** | $64.21 | **−54%** | 94.1M | **−63%** | 22.7 |
| 2 | woz | 62/100 | 62% | $2.10 | $130.29 | −6% | 199.2M | −22% | 22.1 |
| 3 | A0 *(baseline)* | 62/100 | 62% | $2.23 | $138.17 | — | 255.9M | — | 40.4 |
| 4 | rtk | 60/100 | 60% | $2.61 | $156.72 | +13% | 300.3M | +17% | 44.0 |
| 5 | headroom | 59/100 | 59% | $3.33 | $196.65 | +42% | 268.9M | +5% | 11.6 |

`A0` is the no-compression control. **Cache R:W** is the cache read:write ratio; higher is healthier.
Headroom's 11.6 is the weakest by far — roughly half the reuse of the next arm — so it re-pays the
cache-*write* rate more often, which is why it ends up the most expensive arm.
_`compresr` infra-failed on the bloated tasks and is excluded; `edgee` and `bear-1.2` did not produce a graded
set — see [fact-vs-fiction.md](fact-vs-fiction.md)._

![Savings vs the no-compression baseline](figures/1_savings_vs_baseline.png)

## The figures

| Cost | Speed &amp; steps | Mechanism |
|---|---|---|
| ![cost per solved](figures/2_cost_per_solved.png) | ![wall-clock time](figures/4_wall_time.png) | ![peak context](figures/6_peak_context.png) |
| ![total cost](figures/3_total_cost.png) | ![steps](figures/5_steps.png) | ![cache health](figures/9_cache_health.png) |
| ![input tokens](figures/7_input_tokens.png) | ![cost vs success](figures/8_cost_vs_success.png) | |

## Every KPI

| KPI | dasein | woz | A0 | rtk | headroom |
|---|---:|---:|---:|---:|---:|
| Solved / 100 | 50 | 62 | 62 | 60 | 59 |
| $ / solved | **$1.28** | $2.10 | $2.23 | $2.61 | $3.33 |
| Total cost | **$64.21** | $130.29 | $138.17 | $156.72 | $196.65 |
| List-price cost (no cache) | **$287** | $613 | $803 | $805 | $735 |
| vs A0 cost | **−54%** | −6% | — | +13% | +42% |
| Input tokens | **94.1M** | 199.2M | 255.9M | 300.3M | 268.9M |
| vs A0 input | **−63%** | −22% | — | +17% | +5% |
| Output tokens | **1.46M** | 2.69M | 2.55M | 2.80M | 2.70M |
| Agent steps | **2,984** | 3,147 | 3,677 | 3,915 | 3,684 |
| Wall-clock | **7.9 h** | 17.2 h | 13.3 h | 14.6 h | 14.3 h |
| Mean latency / call | **7.9 s** | 13.0 s | 9.0 s | 9.4 s | 10.4 s |
| Peak context (mean) | **38.1K** | 76.8K | 74.9K | 71.5K | 66.7K |
| Cache hit rate | 92.7% | 93.4% | 95.9% | 92.2% | 88.2% |
| Cache read:write | 22.7 | 22.1 | 40.4 | 44.0 | 11.6 |
| Limit-deaths | 0 | 0 | 0 | 0 | 1 |
| API calls | 3,085 | 3,247 | 3,686 | 4,454 | 4,128 |

## Method — and why `$/solved` is the fair metric

- **Cost is cache-aware.** A coding agent re-sends a long, growing prompt every turn, and the API bills a
  *cached* prefix far cheaper than fresh input. We price each call from the provider's real usage split —
  cache-write at the write rate, cache-read at the read rate, output at the output rate — with **identical
  Sonnet-4.6 pricing for every arm** (`uncached $3.00 · cache-write $3.75 · cache-read $0.30 · output $15.00`
  per 1M). A layer that shrinks the visible prompt but rewrites the cache every turn does not actually save
  money, and `$/solved` exposes that.
- **Solves are Docker-graded.** A task counts as solved only when the official SWE-bench Docker grader passes
  it (`fail_to_pass` resolved, `pass_to_pass` intact). No partial credit, no self-report, no LLM judge.
- **Scaffold, model, task set, and grader are held fixed;** each arm runs its product as shipped behind a
  common adapter contract.
- **Helper-model overhead is separate, not blended.** Every column counts only an arm's own
  `claude-sonnet-4-6` agent calls. Calls a layer makes on a *different* model (woz's MCP subagents, Dasein's
  haiku scout + adjudicator) are kept out of the token/step/cost columns so the comparison stays
  apples-to-apples; the one place that overhead surfaces is wall-clock time.

## Honest caveats

- **Hard, bloated long tail on purpose.** Solve rates run ~50–62% across *all* arms including the baseline;
  these characterize the regime where context management matters most, not whole-of-SWE-bench solve rates.
- **Matched, baseline-cost-ranked prefix (preliminary).** Numbers are over the n=100 tasks all arms
  completed — a prefix skewed toward the highest-baseline-cost tasks, gated by the slowest arm. Solve-count
  differences of a task or two are within noise, which is why the leaderboard ranks on `$/solved`.
- **Single scaffold and model.** The ordering is specific to headless Claude Code on `claude-sonnet-4-6`.

---

_Benchmark sponsored and operated by [Dasein](https://daseinlabs.ai); deliberately neutral — one fixed
scaffold and model, an official third-party grader, one shared price table. This public repository contains
no Dasein internals; the `dasein` arm is a thin over-the-wire client, wired in through the same adapter
contract every other arm uses. Figures and tables regenerate from `summary.json`._
