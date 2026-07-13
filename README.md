<h1 align="center">Code-Compression Bench</h1>

<p align="center">
  A reproducible, like-for-like benchmark of context-compression layers for coding agents.<br>
  One fixed agent, one model, one grader. Only the compression layer changes.
</p>

<p align="center">
  <a href="FACT-VS-FICTION.md">Vendor claims vs measured</a> &nbsp;·&nbsp;
  <a href="results/2026-07-04/">Full results &amp; method</a> &nbsp;·&nbsp;
  <a href="results/2026-07-04/paired.csv">Per-task data</a> &nbsp;·&nbsp;
  <a href="https://raw.githack.com/daseinlabs/code-compression-bench/master/results/2026-07-04/dashboard.html">Interactive dashboard</a>
</p>

---

## Overview

Every "we cut your tokens by N%" claim is measured on a different agent, a different task set, and a
different success bar, so none of them are comparable, and none answer the question that matters: does the
agent still solve the problem, and what did it cost end to end? This benchmark fixes everything except the
compression layer. One scaffold (headless Claude Code), one model (`claude-sonnet-4-6`), 100 tasks from
SWE-bench Verified, and the official SWE-bench Docker grader are held identical for every arm; only the
compression layer changes, so any difference in cost or quality is attributable to it. Arms are ranked by
**cost per solved task** — cache-aware total cost divided by the tasks the grader passed.

> Run 2026-07-04 · 100 tasks from SWE-bench Verified · model `claude-sonnet-4-6` · cache-aware pricing ·
> official SWE-bench Docker grader.

## Leaderboard

| # | Arm | Solved | $ / solved | vs base | Total cost | vs base | Input | vs base | Wall-clock | vs base | Cache R:W |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | **Parsec** | **62 / 100** | **$1.45** | **−44%** | **$89.65** | **−39%** | **144.8M** | **−54%** | **10.8 h** | **−25%** | 22.6 |
| 2 | Caveman | 58 / 100 | $2.05 | −21% | $118.99 | −19% | 253.8M | −19% | 12.0 h | −17% | 40.4 |
| 3 | Woz | 55 / 100 | $2.33 | −10% | $128.28 | −13% | 203.1M | −35% | 17.8 h | +23% | 24.7 |
| 4 | Baseline (no compression) | 57 / 100 | $2.58 | — | $147.30 | — | 312.2M | — | 14.4 h | — | 41.6 |
| 5 | RTK | 54 / 100 | $3.07 | +19% | $165.77 | +13% | 360.7M | +16% | 16.2 h | +12% | 46.4 |
| 6 | Headroom | 58 / 100 | $3.66 | +42% | $212.14 | +44% | 329.6M | +6% | 16.3 h | +13% | 11.3 |

Arms are ranked by cost per solved task. Three fall below the no-compression baseline — Parsec ($1.45),
Caveman ($2.05), and Woz ($2.33); RTK ($3.07) and Headroom ($3.66) fall above it. On total cost, Parsec
(−39%), Caveman (−19%), and Woz (−13%) are below the baseline and RTK (+13%) and Headroom (+44%) above.
Parsec and Caveman also solve more tasks than the baseline (62 and 58 of 100 versus 57).

The leaderboard uses cache-aware pricing. At undiscounted list price (no cache credit) the ranking is
unchanged; per-arm list-price totals are in the table below.

<p align="center">
  <img src="results/2026-07-04/figures/1_savings_vs_baseline.png" width="820" alt="Total cost relative to the no-compression baseline: Parsec −39%, Caveman −19%, Woz −13% below it; RTK +13% and Headroom +44% above it.">
</p>

<p align="center">
  <img src="results/2026-07-04/figures/1b_solves_vs_baseline.png" width="820" alt="Tasks solved relative to the no-compression baseline: Parsec +5, Caveman +1, Headroom +1, Woz −2, RTK −3.">
</p>

## Cost

<p align="center">
  <img src="results/2026-07-04/figures/2_cost_per_solved.png" width="49%" alt="Cost per solved task by arm.">
  <img src="results/2026-07-04/figures/3_total_cost.png" width="49%" alt="Total cost by arm.">

</p>

Shipping fewer visible tokens is necessary for a real saving but not sufficient. Under cache-aware pricing
the model re-reads a long, mostly-cached prompt every turn, and a cached prefix is billed far below fresh
input. A layer that trims the visible prompt but rewrites the cached prefix each turn re-pays the expensive
cache-write rate; the bill only falls when the layer removes the *right* tokens without churning the cache or
adding turns. That is why a layer can reduce its token counts and still end up more expensive
than the baseline: the ranking tracks dollars, not tokens.

## Time and steps

<p align="center">
  <img src="results/2026-07-04/figures/4_wall_time.png" width="49%" alt="Wall-clock time by arm.">
  <img src="results/2026-07-04/figures/5_steps.png" width="49%" alt="Agent steps by arm.">
</p>

Wall-clock time ranged from 10.8 hours (Parsec) to 17.8 hours (Woz), against 14.4 hours for the baseline.
Mean per-call latency ranged from 6.2 s (Parsec) to 14.3 s (Woz).

## Cost and time together

<p align="center">
  <img src="results/2026-07-04/figures/10_cost_vs_time.png" width="760" alt="Cost savings versus time savings against the baseline, per layer.">
</p>

Cost savings against time savings, both relative to the baseline. The upper-right region saves on both;
only Parsec falls in it (−39% cost, −25% time).

## Where the cost comes from

<p align="center">
  <img src="results/2026-07-04/figures/7_input_tokens.png" width="49%" alt="Input tokens delivered to the model, by arm.">
  <img src="results/2026-07-04/figures/6_peak_context.png" width="49%" alt="Peak working context (mean) by arm.">
</p>

Cost and time are outcomes; the drivers are how much context the model carries and how stable the prompt
cache stays underneath it. Input tokens delivered to the model range from 144.8M to 360.7M across arms, and
mean peak working context from 41K to 83K. Output tokens are billed at the highest rate ($15 per million),
so output volume weighs most heavily on the bill per token.

<p align="center">
  <img src="results/2026-07-04/figures/9_cache_health.png" width="58%" alt="Cache read-to-write ratio by arm; Headroom is lowest at 11.3.">
</p>

A cached prefix is billed far below fresh input, so a higher read:write ratio is cheaper per token of
context. A low ratio has two causes a single number cannot separate: rewriting the cached prefix often
(re-paying the cache-write rate), or carrying little cached context to begin with. Headroom's ratio (11.3,
the lowest) is the first case — it re-pays the cache-write rate repeatedly, which is why it is the most
expensive arm (+44%) despite input tokens that barely move (+6%). The ratio is read alongside total input,
not on its own.

## Savings versus solve rate

<p align="center">
  <img src="results/2026-07-04/figures/8_cost_vs_success.png" width="760" alt="Cost savings versus solve rate against the baseline, per layer.">
</p>

Each layer by cost savings (vertical) and solve rate (horizontal), both relative to the baseline. The
upper-right region is cheaper and solves more; only Parsec falls in it (62 solved, −39% cost).

## Every measured value

The complete per-arm rollup. Best value in each row is in bold.

| KPI | Parsec | Caveman | Woz | Baseline | RTK | Headroom |
|---|---:|---:|---:|---:|---:|---:|
| Tasks solved (of 100) | **62** | 58 | 55 | 57 | 54 | 58 |
| Cost per solved task | **$1.45** | $2.05 | $2.33 | $2.58 | $3.07 | $3.66 |
| Cost per solved task vs baseline | **−44%** | −21% | −10% | — | +19% | +42% |
| Total cost | **$89.65** | $118.99 | $128.28 | $147.30 | $165.77 | $212.14 |
| List-price cost (no cache discount) | **$456** | $733 | $635 | $862 | $1,015 | $908 |
| Total cost vs baseline | **−39%** | −19% | −13% | — | +13% | +44% |
| Input tokens | **144.8M** | 253.8M | 203.1M | 312.2M | 360.7M | 329.6M |
| Input tokens vs baseline | **−54%** | −19% | −35% | — | +16% | +6% |
| Output tokens | **1.71M** | 2.11M | 2.95M | 3.00M | 3.22M | 2.95M |
| Agent steps | 4,856 | 4,895 | **3,322** | 5,325 | 6,131 | 5,850 |
| Wall-clock hours | **10.8** | 12.0 | 17.8 | 14.4 | 16.2 | 16.3 |
| Mean latency per call | **6.2 s** | 6.9 s | 14.3 s | 8.8 s | 9.3 s | 8.0 s |
| Peak working context (mean) | **41.4K** | 70.1K | 83.2K | 79.8K | 81.6K | 76.7K |
| Cache hit rate | 93.9% | 96.6% | 95.0% | 97.0% | **97.3%** | 92.4% |
| Cache read:write ratio | 22.6 | 40.4 | 24.7 | 41.6 | **46.4** | 11.3 |
| Runs ended by context limit | 3 | 1 | **0** | 2 | 2 | **0** |
| API calls | 4,683 | 4,895 | 3,005 | 4,084 | 4,964 | 4,504 |

Cache read:write is the ratio of cached-prefix reads to cache writes; a lower ratio means the layer re-pays
the cache-write rate more often.

## Vendor claims versus measured

Each of the other layers advertises a large reduction in tokens, cost, or latency, but none of those headline
numbers is measured on what a coding team actually pays for — the cache-aware dollar cost of solving real
repository tasks with a multi-turn agent. They are measured on single-shot question-answering, single-document
QA, shell-command output in isolation, token counts with no task-success check, or "up to" ceilings.

| Layer | Headline claim | Measured on | Our result (n = 100) |
|---|---|---|---|
| Caveman | "Cuts 65% of output tokens (measured)" | Output-token count on general prose; input and tool output unchanged | −19% cost, 58 solved; output ~30% lower, not 65% |
| Woz | "Cut your Claude Code costs in half" | Live-session API usage, undisclosed task mix; quality on Opus 4.7 vs an Opus 4.6 baseline | −13% cost, 55 solved, 23% slower |
| RTK | "60–90% fewer tokens on common dev commands" | Shell-command output in isolation (its own README: native Read/Grep/Glob bypass the hook) | +16% input, +13% cost, 54 solved |
| Headroom | "60–95% fewer tokens, same answers" | Single-shot QA (GSM8K, SQuAD…); its docs: "code passes through" uncompressed | +6% input, +44% cost (most expensive), 58 solved |

This is a summary of the arms that ran. The detailed, fully-sourced breakdown of every layer — every quote,
every primary source, the exact benchmark each number was measured on, and the mechanism behind each gap — is
in **[FACT-VS-FICTION.md](FACT-VS-FICTION.md)**.

## Method

- **One scaffold.** A fixed agent: headless Claude Code, driven through the Python Claude Agent SDK,
  identical system prompt, tools, and caps for every arm.
- **One model.** `claude-sonnet-4-6` for every arm.
- **One task set.** 100 tasks from [SWE-bench Verified](https://www.swebench.com/); the exact instances are
  listed in [`paired.csv`](results/2026-07-04/paired.csv). Each task runs in a checkout at the SWE-bench base
  commit with its git history removed, so the reference patch is not reachable from the repository itself.
- **One grader.** The official SWE-bench Verified Docker harness. A task counts as solved only if its
  `fail_to_pass` tests pass and `pass_to_pass` stays intact. No partial credit, no model-as-judge.
- **One price table.** Cache-aware pricing — for Sonnet-4.6, uncached input $3.00, cache-write $3.75,
  cache-read $0.30, output $15.00 per 1M tokens — applied to each arm's real per-call usage at the rates of
  the model that served each call (subagent calls the scaffold routes to Haiku are billed at Haiku rates). Cost is cache-aware
  because a coding agent re-sends a long, growing prompt every turn and a cached prefix is billed far below
  fresh input; a naive list-price frame would flatter the shorter-prompt arms, so it is not used for the
  ranking.

Cost per solved task is the ranking metric because it cannot be gamed by either lever alone: a layer that
strips context aggressively can look cheap on tokens while failing more tasks, and a layer that solves a lot
can look strong while spending a fortune. Dividing real dollars by graded solves rewards the layer that
delivers correct patches for the least money.

Every layer runs as its shipped product through its own interface — proxy, API, plugin, or hook — and none
is reimplemented. Each is wired in through the same adapter the harness exposes to all arms, the sponsor's
included.

Holding the scaffold and model fixed is what makes the per-arm delta clean; it also means the ordering is
specific to headless Claude Code on `claude-sonnet-4-6`.

## Reproduce

```bash
pip install -e .
gcloud auth application-default login        # model auth: claude-sonnet on Vertex
cp .env.example .env                          # per-arm endpoints / keys
make smoke                                    # one task per ready arm, end to end
make bench                                    # the full task set across every ready arm
make report                                   # regenerate figures, tables, and the leaderboard
```

Arms whose keys or endpoints aren't configured are skipped automatically
(`python -m bench.cc_runner --list-arms` shows what's ready). Every figure and table regenerates from a
single `summary.json`, so anyone who runs this gets the same numbers.

## Layout

```
bench/      core: arm interface, runner, grader, pricing, figures, report
arms/       one adapter per compression layer (proxy / API / plugin / hook)
results/    per-run records, figures, the interactive dashboard, and the generated reports
```

## License

Apache-2.0. Compression products referenced here are the property of their respective owners; this repository
contains only thin client adapters to their public interfaces.

<p align="center"><sub>Sponsored and operated by Dasein. One fixed scaffold and model, an official third-party grader, one shared price table; every arm runs as its shipped product through its public interface. Anyone can re-run it and verify the numbers.</sub></p>
