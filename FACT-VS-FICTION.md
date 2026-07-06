# Fact vs fiction: compression vendor claims, measured

Every product in this benchmark advertises a large reduction in tokens, cost, or latency. Most of those
numbers are real — **on the benchmark the vendor chose**. This document shows, for each layer: (1) exactly
what they claim, with verbatim quotes and primary sources; (2) exactly what that number was measured on;
(3) what we observed when the same product ran behind a real coding agent under a controlled, cache-aware,
outcome-graded benchmark; and (4) the mechanism that explains the gap.

The controlled benchmark is described in the [README](README.md). In one line: one fixed scaffold (headless
Claude Code), one model (`claude-sonnet-4-6` on Vertex), 100 tasks from SWE-bench Verified, the official
SWE-bench Docker grader, and one cache-aware price table applied to every arm's real per-call usage at the
rates of the model that served each call. The matched set is the 100 SWE-bench Verified tasks every measured
arm completed (run 2026-07-04). Per-task data: [`results/2026-07-04/paired.csv`](results/2026-07-04/paired.csv).

This is not an accusation of dishonesty. Several of these vendors disclose their methods and limits plainly,
and we quote those disclosures because they are the most important part of the story. The thesis is narrow
and entirely evidence-based:

> **No layer's headline number is measured on what a coding team actually pays for** — the cache-aware
> dollar cost of solving real repository tasks with a real multi-turn agent. The numbers are measured on
> single-shot question-answering, single-document QA, shell-command output in isolation, token counts with no
> task-success check, or favorable-case "up to" ceilings. When you measure the real thing, the headlines do
> not reproduce, and in two cases (RTK, Headroom) the bill goes *up*.

---

## What we observed (the matched set, n = 100)

Arms are ranked by cost per solved task — cache-aware total cost divided by the tasks the official grader
passed.

| Arm | Solved | $ / solved | Total cost | vs baseline | Input tokens | vs baseline | Wall-clock | Cache R:W |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Dasein** | **62/100** | **$1.45** | **$89.65** | **−39%** | **144.8M** | **−54%** | **10.8 h** | 22.6 |
| Woz | 55/100 | $2.33 | $128.28 | −13% | 203.1M | −35% | 17.8 h | 24.7 |
| Baseline (no compression) | 57/100 | $2.58 | $147.30 | — | 312.2M | — | 14.4 h | 41.6 |
| RTK | 54/100 | $3.07 | $165.77 | +13% | 360.7M | +16% | 16.2 h | 46.4 |
| Headroom | 58/100 | $3.66 | $212.14 | +44% | 329.6M | +6% | 16.3 h | 11.3 |

Compresr, Edgee, and bear-1.2 produced no graded result; the reasons are specific to each and covered below.
The remainder of this document takes the layers one at a time, strongest first.

---

## Woz (WOZCODE)

**What it is.** A paid Claude Code plugin that replaces Claude Code's built-in file tools with AST-aware
tools and routes read-only codebase exploration to a Haiku sub-agent. Integration: local plugin (not a
proxy). Homepage: [wozcode.com](https://wozcode.com). YC W25.

Woz is the strongest of the other layers and deserves to be treated as such. Its token reduction is
real, and unlike the others its flagship *quality* benchmark is a genuine multi-turn agentic harness, not
single-shot QA. The gap between its marketing and our result is therefore not "they measured the wrong kind
of thing" — it is narrower and more specific: the headline cost and speed numbers are favorable-case
ceilings (one a self-described estimate), and the quality comparison is run on a stronger model than its
stated baseline.

### Claims (verbatim, sourced)

| Claim | Source | What it was measured on |
|---|---|---|
| "Cut your Claude Code costs in half" / "25–55% cost reduction vs Claude Code" | [wozcode.com](https://wozcode.com), [how-it-works](https://www.wozcode.com/how-it-works) | Derived from live Anthropic API usage fields × calls saved × posted rates. The task mix that produces the 25–55% range is undisclosed; the homepage markets it as "up to 50%." |
| "Faster: 30–40% faster on most tasks · 5–10× on database work" | [how-it-works](https://www.wozcode.com/how-it-works) | **Self-described estimate:** "The only metric we estimate — we tell you so plainly." Computed as saved calls × a calibrated per-call round-trip time. Underlying task set undisclosed. |
| "40–60% fewer tokens" | [how-it-works](https://www.wozcode.com/how-it-works) | The effect of AST truncation **on file reads** ("replaces function bodies with stubs, keeps types and exports intact") — scoped to file-read tool calls, not the whole session. |
| "80.2% TerminalBench 2.0 · vs 69% Claude Code" | [how-it-works](https://www.wozcode.com/how-it-works), [tbench.ai leaderboard](https://www.tbench.ai/leaderboard/terminal-bench/2.0) | Terminal-Bench 2.0, a genuine 89-task agentic terminal benchmark. The 80.2% is corroborated on the official leaderboard — **for WOZCODE on Claude Opus 4.7.** The official plain Claude Code entry is **58.0% on Opus 4.6**, not the "69%" Woz's marketing uses. |

### What we observed

Woz reduced the main model's input tokens by a real 35% (203.1M versus the baseline's 312.2M) and solved
55/100 — two fewer than the no-compression baseline's 57. Its total cost fell **13%**, the second-largest
cost reduction of any arm and enough to place it below the baseline on cost per solved task ($2.33 versus
$2.58). But it was the **slowest arm in the benchmark at 17.8 hours, 23% slower than the no-compression
baseline**, with the highest mean per-call latency (14.3 s versus the baseline's 8.8 s).

### Why the gap

Woz shifts work onto a Haiku exploration sub-agent. That genuinely removes input tokens from the main
model's context (hence −35% input), but it adds round-trips, and those round-trips dominate wall-clock time
on long, hard tasks — which is why the arm that markets "30–40% faster" is in fact the slowest one here. On
cache-aware pricing, where a re-read of a cached prefix is already cheap, trimming the *visible* prompt
removes less dollar cost than it removes tokens, so a 35% token cut becomes a 13% bill cut. The "up to 50%
cheaper / up to 10× faster" figures are favorable-case ceilings; on these SWE-bench Verified tasks, priced
on real usage, they do not appear.

On quality: Woz's 80.2% TerminalBench result is legitimate, but it is measured on **Claude Opus 4.7**, a
stronger model than the **Opus 4.6** of the "Claude Code" line it is plotted against — and stronger than the
`claude-sonnet-4-6` every arm runs in this benchmark. A quality benchmark that changes the model is
measuring the model as much as the layer. Here, with the model held fixed, Woz solved slightly fewer tasks
than the baseline (55 versus 57).

**Verdict.** Real token reduction and a real agentic quality benchmark — but the headline cost and speed
claims are ceilings/estimates that do not hold on hard tasks: it is the slowest arm, its 13% cost cut comes
with two fewer solves than the baseline, and the quality number is run on a stronger model than its own
baseline.

---

## RTK (Rust Token Killer)

**What it is.** A single Rust binary installed as a Claude Code PreToolUse (Bash) hook that compresses
shell-command output before it enters the context. Integration: CLI hook (the model talks to the gateway
directly, as in the baseline). Repo: [github.com/rtk-ai/rtk](https://github.com/rtk-ai/rtk).

### Claims (verbatim, sourced)

| Claim | Source | What it was measured on |
|---|---|---|
| "reduces LLM token consumption by 60-90% on common dev commands" | [github.com/rtk-ai/rtk](https://github.com/rtk-ai/rtk) | Repo description. Scoped by "on common dev commands" — i.e. shell-command output. |
| "89% noise reduction measured across 2,900+ real-world dev commands" (cargo test 91.8%, git status 80.8%, find 78.3%, grep 49.5%) | [rtk-ai.app](https://www.rtk-ai.app/) | The corpus, repos, and whether outputs were measured in isolation or inside a real agent are all undisclosed. The product's own [/benchmarks](https://www.rtk-ai.app/benchmarks) page reads: "No benchmark data yet." |
| "~118,000 → ~23,900 tokens, −80%" over a "30-min Claude Code Session" | [github.com/rtk-ai/rtk](https://github.com/rtk-ai/rtk) | README footnote, verbatim: **"Estimates based on medium-sized TypeScript/Rust projects. Actual savings vary by project size."** Each row is an estimate, not an observed run. |

### The admission that settles it

RTK's own README states, verbatim:

> "**Important: the hook only runs on Bash tool calls. Claude Code built-in tools like `Read`, `Grep`, and
> `Glob` do not pass through the Bash hook, so they are not auto-rewritten.**"

### What we observed

RTK *increased* input tokens by 16% and total cost by 13% — it was **more expensive than running no
compression at all** — while solving 54/100. Its cache reuse was healthy (46.4:1); the problem was not the
cache, it was that there was almost nothing for it to compress.

### Why the gap

A coding agent's token bill is dominated by file reads, search results, and the growing model transcript —
exactly the native `Read` / `Grep` / `Glob` / `Edit` tools that, by RTK's own admission, bypass the hook.
Shell output is a sliver of that bill. RTK compresses the sliver it can see, the real context keeps growing
untouched, and the hook's per-call rewriting plus a few extra turns push the run slightly above baseline.
The "60–90%" is a true statement about `cargo test` output in isolation and a misleading one about what a
coding agent actually pays. The headline figures are also self-labeled estimates; the published-benchmark
page is empty.

**Verdict.** The advertised reduction is real but applies to the wrong denominator (shell output, not the
agent's bill). On a real agent it bypasses the tools that generate the cost and ends up costing more than
doing nothing.

---

## Headroom

**What it is.** A reversible context-compression layer (library / proxy / MCP server) that compresses tool
outputs, logs, files, and RAG chunks. Repo: [github.com/chopratejas/headroom](https://github.com/chopratejas/headroom).

### Claims (verbatim, sourced)

| Claim | Source | What it was measured on |
|---|---|---|
| "60-95% fewer tokens, same answers" | [github.com/chopratejas/headroom](https://github.com/chopratejas/headroom) | Headline range over "real workloads" — boilerplate-heavy tool outputs, logs, JSON, RAG chunks. The author's own "honest version" splits this to "70-90% for tool-heavy coding/RAG, 20-40% for prompt-heavy chat." |
| "87% Avg Token Reduction / 100% Answer Accuracy" | [headroomlabs-ai.github.io/headroom](https://headroomlabs-ai.github.io/headroom/) | Homepage hero. The "100% accuracy" generalizes from benchmark deltas that were **not** 100%: GSM8K 0.870, TruthfulQA 0.560. |
| Accuracy validated on GSM8K, TruthfulQA, SQuAD v2, BFCL | [evals/README.md](https://github.com/chopratejas/headroom/blob/main/headroom/evals/README.md) | **All single-shot QA**, N=100 each: grade-school math, factual QA, single-passage reading comprehension, single function-call selection. None is a multi-turn coding agent on a repository. |
| "CacheAligner — stabilizes prefixes so provider KV caches actually hit" | [github.com/chopratejas/headroom](https://github.com/chopratejas/headroom) | Mechanism claim; the docs also concede: "Anthropic's prompt caching is still better for fixed prefixes." |

### The admission that settles it

Headroom's own documentation states that on coding work, its flagship code compressor mostly does not run:

> "If the most recent user message contains keywords like 'analyze', 'review', 'explain', 'fix', 'debug' —
> ALL code in the conversation is protected." … "Code in the last 4 messages is never compressed." …
> "Code-only sessions (reading/writing files) — code passes through." … "Compressing function bodies would
> remove exactly what they need."

— Headroom documentation, [limitations](https://headroom-docs.vercel.app/docs/limitations) and
[how compression works](https://headroom-docs.vercel.app/docs/how-compression-works).

### What we observed

Headroom moved input tokens by only **+6%** (an increase, essentially flat) and was the **most expensive arm
at +44%**. It had the lowest cache reuse on the board (11.3:1, roughly half the next arm) and the lowest
cache-hit rate (92.4%).

### Why the gap

The two failures are precisely what its own docs predict. (1) On agentic coding, the keyword guard fires on
nearly every turn ("fix", "debug", "explain"…) and the recent-code guard protects the working set, so the
code — the bulk of the context — passes through uncompressed. Tokens barely move (+6%). (2) To compress the
*non-code* remainder, Headroom rewrites the prompt prefix, and rewriting the prefix every turn invalidates
the prompt cache. Under cache-aware pricing a cache-write costs roughly 12× a cache-read, so a layer that
keeps re-writing the prefix re-pays the expensive rate over and over — which is why the most token-neutral
arm became the most expensive one, and why its "better caching" claim is contradicted by its measured cache
behavior (lowest read:write and lowest hit-rate on the board). Its single-shot QA benchmarks (GSM8K et al.)
say nothing about either failure, because they have no growing prefix and no code.

**Verdict.** Its headline applies to logs and JSON, not code, and by its own design code is exempt — so on a
coding agent it removes almost nothing while its prefix-rewriting churns the cache into the most expensive
result on the board. The "same answers" QA benchmarks do not transfer to agentic coding.

---

## Compresr

**What it is.** An LLM context-compression SDK plus "Context Gateway," an open-source Go proxy that compacts
conversation history and tool outputs. Homepage: [compresr.ai](https://compresr.ai/). YC W2026; founded by
four EPFL researchers.

### Claims (verbatim, sourced)

| Claim | Source | What it was measured on |
|---|---|---|
| "boost context management with 100x compression" | [ycombinator.com/companies/compresr](https://www.ycombinator.com/companies/compresr) | Headline / aspirational; the homepage's own benchmark substantiates only ~2× (accuracy-preserving) to ~10× (cost mode). |
| FinanceBench: 77% vs 73% baseline, "~47% cheaper" at ~2× | [compresr.ai](https://compresr.ai/) | **FinanceBench — 128 questions over SEC filings.** Single-document financial QA, not coding. |
| "Tokens: 112,552→498, 226× fewer · 86% cheaper" | [compresr.ai](https://compresr.ai/) | A single illustrative example: one Boeing 10-K SEC filing. Not an aggregate. |
| "At light ~2x compression accuracy matches or beats full context. Push to ~10x when cost matters more than peak accuracy" | [compresr.ai](https://compresr.ai/) | An explicit admission that accuracy parity holds only at ~2×; beyond that, accuracy is traded for cost. |

### What we observed

Compresr produced **no graded result**: its Context Gateway proxy did not complete a live coding-agent run in
our harness (an infrastructure failure, not a graded loss). We report this rather than omit it.

### Why the gap

Compresr's evidence is single-document financial QA (FinanceBench) and cherry-picked document examples (a
Boeing 10-K). Those measure compression of one static document fed as context — the opposite shape of an
agentic coding session, which is a long, growing, multi-turn transcript that changes every step. The "100×"
is, per a third-party YC analysis, aspirational ("not a weekend task"), and the vendor concedes accuracy
parity only holds at ~2×. The Go proxy did not survive a live, long coding-agent run under our harness.
Nothing in the published numbers speaks to agentic coding, because none of it was measured there.

**Verdict.** A strong story on single-document financial QA; no evidence for agentic coding, and the proxy
could not complete a live agent run in our benchmark.

---

## Edgee

**What it is.** An open-source Rust agent gateway with token compression, run as a drop-in CLI wrapper.
Homepage: [edgee.ai](https://www.edgee.ai/). Notably, Edgee's compression engine is, by its own attribution,
"initially based on" RTK.

Edgee is the most candid of these layers about its own scope, and that candor is the point.

### Claims (verbatim, sourced)

| Claim | Source | What it was measured on |
|---|---|---|
| "Cut Token Costs up to 50%." | [edgee.ai](https://www.edgee.ai/) | "Up to" ceiling; attributed to undisclosed internal benchmarks, tagged "Your mileage may vary." |
| "Customer aggregate token-bill reduction … sits at approximately 20%" | [edgee.ai/docs](https://www.edgee.ai/docs/features/overview) | Edgee's **own measured aggregate over real customer traffic — ~20%, less than half the 50% headline.** |
| "60–90% reduction on common dev commands" | [edgee.ai/token-compression](https://www.edgee.ai/token-compression) | Explicitly "Public RTK benchmarks" — i.e. RTK's shell-output-in-isolation numbers, inherited. |
| "Semantically lossless for coding tasks" / "zero measurable drift on SWE-Bench Verified samples" | [edgee.ai](https://www.edgee.ai/), [docs](https://www.edgee.ai/docs/features/overview) | "Lossless" is scoped in the docs to `tool_result` payload framing, not code generation. SWE-Bench is used only as a prompt-equivalence drift check on "samples"; **no resolved-rate / task-success figure is reported.** |

### What we observed

Edgee produced no graded result on the matched set. Its own published benchmarks are informative about why a
clean number is hard to get: every one of them measures token/cost counts and explicitly **not** task
success, at a sample size of **n = 1** per scenario ("This benchmark is based on a single Codex baseline run
and a single Codex + Edgee run"; "This analysis covers `dirCount: 1` per scenario — results should be
validated at higher sample sizes" — Edgee's own benchmark repo,
[github.com/edgee-ai/compression-lab](https://github.com/edgee-ai/compression-lab)). On one of Edgee's own
endurance runs in that repo, absolute API cost was 19.6% *higher* through Edgee.

### Why the gap

Edgee shares RTK's mechanism (tool-output trimming) and therefore RTK's ceiling: it trims the parts of the
context that are cheap under cache-aware pricing and leaves the growing model transcript. Its honest measured
aggregate is ~20%, not the 50% headline, and even that is a token-count number, not a graded
cost-per-solved result — Edgee's benchmarks never check whether the agent still solved the task. We could not
produce a clean graded number for it on the matched set; on the evidence Edgee itself publishes, a
50%-cheaper-while-solving claim is not supported.

**Verdict.** The most transparent of the layers: its own aggregate (~20%) undercuts its "up to 50%" headline,
its "lossless" claim is scoped to tool-output framing, and it never measures task success. Inherits RTK's
shell-output ceiling.

---

## bear-1.2 (The Token Company)

**What it is.** A prompt-compression API that deletes the least-important tokens from a prompt (no
summarizing or paraphrasing). Homepage: [thetokencompany.com](https://thetokencompany.com/). YC W2026.
Access-gated (no public self-serve API).

### Claims (verbatim, sourced)

| Claim | Source | What it was measured on |
|---|---|---|
| "Typically 10–40% while maintaining full accuracy" | [thetokencompany.com](https://thetokencompany.com/) | Range; "depending on how dense your input is." The one production case study (Helonic) showed a real-world reduction of only **4.7%**. |
| "up to 20% fewer tokens … +2.7 percentage points on financial QA" | [/benchmarks/financebench](https://thetokencompany.com/benchmarks/financebench) | FinanceBench — 150 single-shot questions over SEC filings. |
| "up to 37% faster end-to-end latency" | [/benchmarks/latency](https://thetokencompany.com/benchmarks/latency) | A pure latency-timing benchmark over LongBench documents at the 100K-token tier; measures time only, not task quality. |
| Accuracy on FinanceBench, SQuAD v2, CoQA, LongBench v2 | [github.com/TheTokenCompany/Benchmarks](https://github.com/TheTokenCompany/Benchmarks) | **All single-shot or single-document QA.** The SQuAD eval drew all 150 questions from a single Wikipedia article (the Normans). |

### The admission that settles it

The Token Company states, verbatim, on its own homepage:

> "**Not designed for code or highly structured languages (JSON schemas, SQL, config files).**" … "Not
> recommended for code editing or syntax fixing … the compressed output is no longer compilable."

### What we observed

bear-1.2 was not evaluated: it is access-gated (no self-serve API), and by the vendor's own statement it is
not designed for the workload this benchmark measures.

### Why the gap

This is the cleanest case of benchmark/workload mismatch, stated by the vendor itself. The mechanism is pure
token deletion, which necessarily breaks code, JSON, SQL, and config syntax — the vendor says so, and says
the output is "no longer compilable." Its designed inputs are verbose natural-language input: transcripts, chat
histories, documents, RAG context. Every benchmark it publishes is document/QA, and even there the
real-world reduction in its one production case study was 4.7%, far below the 10–40% headline. There is no
version of this product that targets agentic coding.

**Verdict.** By the vendor's own explicit admission, not for code — and deletion-based compression makes code
non-compilable. Every benchmark is document QA; none is agentic coding. Out of scope by design.

---

## The pattern

Line the six layers up by what their headline number was actually measured on:

- **Single-shot / single-document QA:** Headroom (GSM8K, TruthfulQA, SQuAD, BFCL), Compresr (FinanceBench),
  bear-1.2 (FinanceBench, SQuAD, CoQA, LongBench). None has a growing multi-turn context; none contains code
  the layer actually compresses.
- **Shell-command output in isolation:** RTK (and Edgee, which inherits RTK's engine and its 60–90% figure).
  A real fraction of one tool's output, but a sliver of the agent's bill — and by RTK's own admission the
  native file/search tools bypass it.
- **Token counts with no task-success check, n = 1:** Edgee's own coding-agent benchmarks measure tokens and
  plan consumption, explicitly not whether the task was solved, at a single run per scenario.
- **Favorable-case ceilings and an estimate:** Woz's "up to 50% cheaper / up to 10× faster" (speed
  self-labeled an estimate), and a quality number run on a stronger model than its baseline.

This benchmark measures the one thing none of them do: **the cache-aware dollar cost of solving real
repository tasks, graded by the official SWE-bench Docker harness, with the agent, model, and price table
held fixed for every arm.** Under that measurement the headlines do not reproduce. Two layers (RTK,
Headroom) cost more than running no compression at all; Woz cuts total cost 13% but runs 23% slower and
solves two fewer tasks than the baseline. [Dasein](README.md), which curates the agent's working context —
the part that actually grows every turn — rather than one document, one tool's output, or one-shot QA, solved
the most tasks of any arm (62 of 100) at the lowest total cost and the lowest cost per solved task on the
board, cutting total cost 39% and the context delivered to the model 54%.

---

_All vendor quotes are verbatim from the cited primary sources, retrieved 2026-06-24; benchmark run
2026-07-04. Our measured numbers are reproducible from
[`results/2026-07-04/paired.csv`](results/2026-07-04/paired.csv) and
[`summary.json`](results/2026-07-04/summary.json). Benchmark sponsored and operated by
[Dasein](https://daseinlabs.ai); the methodology is identical for every arm, including Dasein's._
