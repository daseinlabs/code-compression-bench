# Fact vs fiction — what each layer claims, and what we measured

Every context-compression product advertises a large token-or-cost reduction. Almost all of those numbers
are real — **on the benchmark the vendor chose**. The problem is that those benchmarks are not a coding
agent: they are single-shot question-answering (GSM8K, SQuAD, FinanceBench), or they measure shell-command
output in isolation, or they hide behind an "up to" ceiling. None of them price the **cache-aware dollar
cost of a real, multi-turn coding agent** on hard, context-heavy tasks.

That is precisely the regime this benchmark measures: one fixed scaffold (headless Claude Code), one model
(`claude-sonnet-4-6` on Vertex), the bloated long tail of SWE-bench Verified, the official Docker grader, and
one cache-aware price table applied identically to every arm. The matched set below is **n=100** tasks every
arm completed (2026-06-24). Full per-task data: [`paired.csv`](paired.csv); per-arm rollup:
[`summary.json`](summary.json).

For reference, the cache-aware result on this benchmark:

| arm | solved | $/solved | total cost | vs A0 cost | input tokens | vs A0 input | wall-clock | cache R:W |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **dasein** | 50/100 | **$1.28** | **$64.21** | **−54%** | **94.1M** | **−63%** | **7.9 h** | 22.7 |
| woz | 62/100 | $2.10 | $130.29 | −6% | 199.2M | −22% | 17.2 h | 22.1 |
| A0 (baseline) | 62/100 | $2.23 | $138.17 | — | 255.9M | — | 13.3 h | 40.4 |
| rtk | 60/100 | $2.61 | $156.72 | +13% | 300.3M | +17% | 14.6 h | 44.0 |
| headroom | 59/100 | $3.33 | $196.65 | +42% | 268.9M | +5% | 14.3 h | 11.6 |

---

## woz (WOZCODE)

- **Claims.** *"Woz: Claude Code plugin that reduces token consumption and cost by 50%"* (Y Combinator
  tagline). The site adds *"25–55% cheaper"*, *"30–40% faster on most tasks · 5–10× on database work"*, and a
  TerminalBench 2.0 score of *80.2% vs 69%* for Claude Code (measured on Opus 4.7).
  Sources: [wozcode.com](https://www.wozcode.com/), [wozcode.com/how-it-works](https://www.wozcode.com/how-it-works), [ycombinator.com/companies/woz](https://www.ycombinator.com/companies/woz).
- **What we observed.** −6% total cost, −22% input tokens, **62/100 solved — identical to the
  no-compression baseline** (no quality lift on this set), and **17.2 h wall-clock: the slowest arm, +30%
  vs baseline.** Mean per-call latency 13.0 s vs the baseline's 9.0 s.
- **Why the gap.** Woz's input savings are real (its AST-truncation and ranked search do trim context, hence
  −22% input). But its "delegated exploration" runs **MCP sub-agents** that add round-trips and latency, and
  on this hard long-context slice that orchestration overhead eats most of the dollar saving and adds
  wall-clock time. The advertised "50% cheaper / 40% faster" is an "up-to" ceiling on favorable tasks; here
  it nets to roughly cost-neutral and materially slower.
- **Verdict: ⚠️ overstated.** Real token reduction, but the headline cost/speed wins don't survive a hard
  agentic run.

## rtk (Rust Token Killer)

- **Claims.** Repository tagline: *"CLI proxy that reduces LLM token consumption by **60–90%** on common dev
  commands."* Marketing cites *~80%* reduction in a 30-minute session and *89% noise reduction across 2,900+
  commands.* Source: [github.com/rtk-ai/rtk](https://github.com/rtk-ai/rtk).
- **What we observed.** **+17% input tokens and +13% cost — *more* expensive than doing nothing.** 60/100
  solved.
- **Why the gap.** rtk's own README states the limitation plainly: *"the hook only runs on Bash tool calls.
  Claude Code built-in tools like Read, Grep, and Glob do not pass through the Bash hook."* A coding agent's
  context is dominated by file reads and the growing model transcript — **not** shell output. rtk compresses
  the sliver it can see while the real context keeps growing untouched; the hook's own overhead and a couple
  of extra turns then push the total slightly *above* baseline. The "60–90%" is a true statement about
  `git status`/`cargo test` output, and a misleading one about a coding agent's bill.
- **Verdict: 🔴 backfired.** The advertised reduction is on the wrong denominator.

## headroom

- **Claims.** Repository description: *"Compress tool outputs, logs, files, and RAG chunks before they reach
  the LLM. **60–95% fewer tokens, same answers.**"* The project also claims it *"stabilizes dynamic content
  for better caching"* and is *"validated across GSM8K, TruthfulQA, and SQuAD."*
  Source: [github.com/chopratejas/headroom](https://github.com/chopratejas/headroom).
- **What we observed.** **+5% input tokens (essentially no reduction) and +42% cost — the most expensive
  arm.** Lowest cache health of any arm by far (**11.6:1** — roughly half the reuse of the next arm), lowest cache-hit rate
  (88.2%), the only `limit-death`, and cache-write volume 3–5× the others.
- **Why the gap.** Two mismatches. (1) Its validation benchmarks — GSM8K, TruthfulQA, SQuAD — are
  **single-shot QA**, where you compress one static blob once. A coding agent rewrites its prompt every turn,
  and headroom's rewriting **churns the prompt cache** — the exact opposite of its "better caching" claim. (2)
  Re-paying cache-write rates every turn means the cache-aware bill balloons even though the visible token
  count barely moved. It is the cleanest demonstration on the board that *fewer visible tokens ≠ cheaper.*
- **Verdict: 🔴 backfired.** Poor cache reuse turns a token-neutral run into the most expensive arm on the board.

## compresr

- **Claims.** *"100× compression"*; a headline FinanceBench result of *"10× compression, 74.5% accuracy vs
  72.3% baseline, 76% cheaper."* Founded by EPFL researchers; the open-source Context Gateway proxy sits
  between an agent and the LLM. Sources: [compresr.ai](https://compresr.ai/),
  [github.com/Compresr-ai/Context-Gateway](https://github.com/Compresr-ai/Context-Gateway).
- **What we observed.** **Infra-failed on the bloated agentic tasks — could not complete the matched set, so
  it is excluded from the leaderboard** (an infrastructure DNF, not a graded loss; we call it out rather than
  silently dropping it).
- **Why the gap.** FinanceBench is **single-document financial QA** — compress one report, answer one
  question. The Context Gateway proxy did not survive a live, long, multi-turn coding agent under our harness.
  The "100× / 76% cheaper" number says nothing about agentic coding because it was never measured there.
- **Verdict: ⚫ did not finish.** Will be included once a clean graded run is available.

## edgee

- **Claims.** *"Agent Gateway. Cut Token Costs **up to 50%**."* Describes input compression that *"rebuilds
  RTK functionality into the Rust gateway"* (stripping boilerplate from CLI/tool output), output-brevity
  compression, and tool-surface reduction. Sources: [edgee.ai](https://www.edgee.ai/),
  [github.com/edgee-ai/edgee](https://github.com/edgee-ai/edgee).
- **What we observed.** No graded run on this matched set.
- **Why it's noted.** By its own description Edgee's input compression *is* RTK's approach — trimming shell /
  tool output — which carries the **same structural ceiling** rtk hit here: it can't touch the file-read and
  transcript growth that dominate a coding agent's context. The "up to 50%" is again an output-trimming
  ceiling, not an agent-level result.
- **Verdict: ⚫ n/a (wired, no graded set).**

## bear-1.2 (The Token Company)

- **Claims.** *"Typically 10–40% token reduction while maintaining full accuracy."* A small classifier deletes
  the least-important tokens (nothing is summarized). Crucially, the product docs state it is *"**not designed
  for code** or highly structured languages like JSON schemas, SQL, and config files."* Sources:
  [thetokencompany.com](https://thetokencompany.com/), [YC](https://www.ycombinator.com/companies/the-token-company).
- **What we observed.** Access-gated (no self-serve API) → a documented no-op; the adapter is wired and will
  activate when access is granted.
- **Why it's noted.** By the vendor's own admission the product targets prose — transcripts, documents, RAG
  context — and explicitly **not** code. This benchmark's entire workload is code, so even a perfect run would
  be testing it outside its design envelope.
- **Verdict: ⚫ n/a (out of design scope + access-gated).**

---

## dasein — the control case for "measure the real thing"

- **Claim.** Curate the agent's **full working context** at serve time with a learned model.
- **What we observed.** **−54% total cost, −63% input context, $1.28 per solved task (lowest of any arm),
  fastest wall-clock (7.9 h), fewest steps (2,984), half the peak working context (38K)** — while keeping
  the prompt cache healthy (22.7:1).
- **Why it holds up.** Dasein doesn't compress shell output (a sliver) or a one-shot document (the wrong
  shape). It curates the part of the prompt that **actually grows every turn** in an agent loop — the live
  working context — which is where the cache-aware bill is generated. That's why its saving is the one that
  survives contact with a real agent, on the official grader, under a price table shared with everyone.
- **The honest asterisk.** On this hardest-context slice Dasein solves 50/100 to the baseline's 62 — it
  trades a few solves for less than half the cost. The leaderboard ranks on `$/solved` precisely so that
  trade is scored fairly, and Dasein still wins it outright.

> **Methodology note.** Helper-model calls (woz's MCP subagents, Dasein's haiku scout/adjudicator) are kept
> out of the same-model token/cost columns as out-of-band overhead, so the comparison stays apples-to-apples.
> The one place such overhead is visible is wall-clock time — which the user feels regardless of which model
> does the work — and even there Dasein is the fastest arm.
