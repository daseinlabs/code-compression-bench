# Code-Compression Bench — results, 2026-07-04

A like-for-like benchmark of context-compression layers for coding agents. One fixed scaffold (headless
Claude Code), one model (`claude-sonnet-4-6` on Vertex), 100 tasks from [SWE-bench Verified](https://www.swebench.com/),
and the official SWE-bench Docker grader are held identical for every arm; only the compression layer
changes, so any difference in cost or quality is attributable to it. Arms are ranked by **cost per solved
task** — cache-aware total cost divided by the tasks the grader passed.

> Run 2026-07-04 · 100 tasks from SWE-bench Verified · model `claude-sonnet-4-6` · cache-aware pricing ·
> official SWE-bench Docker grader.

## Leaderboard

| # | Arm | Solved | $ / solved | vs base | Total cost | vs base | Input | vs base | Wall-clock | vs base | Cache R:W |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | **Dasein** | **62 / 100** | **$1.45** | **−44%** | **$89.65** | **−39%** | **144.8M** | **−54%** | **10.8 h** | **−25%** | 22.6 |
| 2 | Woz | 55 / 100 | $2.33 | −10% | $128.28 | −13% | 203.1M | −35% | 17.8 h | +23% | 24.7 |
| 3 | Baseline (no compression) | 57 / 100 | $2.58 | — | $147.30 | — | 312.2M | — | 14.4 h | — | 41.6 |
| 4 | RTK | 54 / 100 | $3.07 | +19% | $165.77 | +13% | 360.7M | +16% | 16.2 h | +12% | 46.4 |
| 5 | Headroom | 58 / 100 | $3.66 | +42% | $212.14 | +44% | 329.6M | +6% | 16.3 h | +13% | 11.3 |

Dasein solves the most tasks of any arm (62 of 100) at the lowest total cost ($89.65) and the lowest cost
per solved task ($1.45). Woz is the one other arm below the baseline on cost per solved task; RTK and
Headroom cost more than running no compression at all. Compresr, Edgee, and bear-1.2 produced no graded
result on this set — the [vendor-claims breakdown](../../FACT-VS-FICTION.md) covers each.

<p align="center">
  <img src="figures/1_savings_vs_baseline.png" width="820" alt="Total cost relative to the no-compression baseline: Dasein −39%; Woz −13%; RTK +13% and Headroom +44% sit above the baseline.">
</p>

<p align="center">
  <img src="figures/1b_solves_vs_baseline.png" width="820" alt="Tasks solved relative to the no-compression baseline: Dasein +5, Headroom +1, Woz −2, RTK −3.">
</p>

## Every measured value

Best value in each row is in bold.

| KPI | Dasein | Woz | Baseline | RTK | Headroom |
|---|---:|---:|---:|---:|---:|
| Tasks solved (of 100) | **62** | 55 | 57 | 54 | 58 |
| Cost per solved task | **$1.45** | $2.33 | $2.58 | $3.07 | $3.66 |
| Cost per solved task vs baseline | **−44%** | −10% | — | +19% | +42% |
| Total cost | **$89.65** | $128.28 | $147.30 | $165.77 | $212.14 |
| List-price cost (no cache discount) | **$456** | $635 | $862 | $1,015 | $908 |
| Total cost vs baseline | **−39%** | −13% | — | +13% | +44% |
| Input tokens | **144.8M** | 203.1M | 312.2M | 360.7M | 329.6M |
| Input tokens vs baseline | **−54%** | −35% | — | +16% | +6% |
| Output tokens | **1.71M** | 2.95M | 3.00M | 3.22M | 2.95M |
| Agent steps | 4,856 | **3,322** | 5,325 | 6,131 | 5,850 |
| Wall-clock hours | **10.8** | 17.8 | 14.4 | 16.2 | 16.3 |
| Mean latency per call | **6.2 s** | 14.3 s | 8.8 s | 9.3 s | 8.0 s |
| Peak working context (mean) | **41.4K** | 83.2K | 79.8K | 81.6K | 76.7K |
| Cache hit rate | 93.9% | 95.0% | 97.0% | **97.3%** | 92.4% |
| Cache read:write ratio | 22.6 | 24.7 | 41.6 | **46.4** | 11.3 |
| Runs ended by context limit | 3 | **0** | 2 | 2 | **0** |
| API calls | 4,683 | 3,005 | 4,084 | 4,964 | 4,504 |

<p align="center">
  <img src="figures/2_cost_per_solved.png" width="49%" alt="Cost per solved task by arm: Dasein lowest at $1.45.">
  <img src="figures/3_total_cost.png" width="49%" alt="Total cost by arm: Dasein $90, the lowest.">
</p>

<p align="center">
  <img src="figures/7_input_tokens.png" width="49%" alt="Input tokens delivered to the model: Dasein 145M, the fewest.">
  <img src="figures/9_cache_health.png" width="49%" alt="Cache read-to-write ratio by arm; Headroom lowest at 11.3.">
</p>

## Method

- **One scaffold.** Headless Claude Code, driven through the Python Claude Agent SDK, with an identical
  system prompt, tools, and caps for every arm.
- **One model.** `claude-sonnet-4-6` for every arm.
- **One task set.** The 100 SWE-bench Verified instances listed in [`paired.csv`](paired.csv). Each task runs
  in a checkout at the SWE-bench base commit with its git history removed, so the reference patch is not
  reachable from the repository itself.
- **One grader.** The official SWE-bench Verified Docker harness. A task counts as solved only if its
  `fail_to_pass` tests pass and `pass_to_pass` stays intact — no partial credit, no model-as-judge.
- **One price table.** Cache-aware pricing (for Sonnet-4.6: uncached input $3.00, cache-write $3.75,
  cache-read $0.30, output $15.00 per 1M tokens), applied to each arm's real per-call usage at the rates of
  the model that served each call. Subagent calls the scaffold routes to Haiku are billed at Haiku rates.

Cost per solved task is the ranking metric because it cannot be gamed by either lever alone: a layer that
strips context aggressively can look cheap on tokens while failing more tasks, and a layer that solves a lot
can look strong while spending a fortune. Dividing real dollars by graded solves rewards the layer that
delivers correct patches for the least money. Holding the scaffold and model fixed is what makes the per-arm
delta clean; it also means the ordering is specific to headless Claude Code on `claude-sonnet-4-6`.

## Files

- [`paired.csv`](paired.csv) — per-task tokens, cache split, latency, submitted patch, and grader outcome.
- [`summary.json`](summary.json) · [`summary_rich.json`](summary_rich.json) — the per-arm rollups every
  figure and table is derived from.
- [`dashboard.html`](dashboard.html) — the interactive version of these results.
- [`../../FACT-VS-FICTION.md`](../../FACT-VS-FICTION.md) — each layer's advertised claim, its primary
  source, the benchmark it was measured on, and the mechanism behind the gap.
- [`../../README.md`](../../README.md) — benchmark overview.

---

_Benchmark sponsored and operated by [Dasein](https://daseinlabs.ai). All arms run under an identical
scaffold, model, task set, and grader; only the compression layer varies. This public repository contains
only thin client adapters to each product's public interface._
