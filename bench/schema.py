"""On-disk record schema for the benchmark.

A run is one (instance, arm) solve: the fixed scaffold attempts one SWE-bench
task under one compression arm. `RunRecord` captures everything the runner
writes per solve; the ledger is one JSON object per line (resume-safe), and
`AggResult` is the per-arm rollup the report/figures consume.

Pure stdlib dataclasses + a TypedDict mirror for the JSON shape. Token/cost
fields line up with bench.pricing.CostBreakdown so a record can be priced and
its dollar fields filled in one pass.

KPI parity note (read, don't import): the field set mirrors a reference KPI bundle —
per-run rows, metrics.csv columns, and the cache-priced (paired) frame. It captures every observable KPI uniformly across ALL arms: the real
cache write/read tokens from the model API usage series, latency, effort, the
limit-death outcome, both price frames (cache-aware + list upper bound), and a
measured cache hit rate. Vendor-internal evictions are not observable, so we
measure only what the API + the run expose — but we measure all of it.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Optional, TypedDict


# ── per-call usage series (drives cache-aware pricing) ──────────────────────
class CallUsage(TypedDict, total=False):
    """One assistant call's token usage, in call order within a run.

    Captured straight from the model-API usage object so pricing reads the
    REAL cache split (no inference) whenever the provider reports it.

    prompt_tokens               : full input the model saw on this call (billable
                                  input = uncached new + cache-write + cache-read)
    completion_tokens           : tokens it produced
    cache_creation_input_tokens : tokens billed at the cache-WRITE rate — the
                                  freshly-appended prefix the provider just cached
                                  (Anthropic/litellm: usage.cache_creation_input_tokens)
    cache_read_input_tokens     : tokens billed at the cache-READ rate — the
                                  re-sent prefix served from cache
                                  (Anthropic/litellm: usage.cache_read_input_tokens;
                                  also accept OpenAI-style
                                  usage.prompt_tokens_details.cached_tokens)
    latency_s                   : wall-clock seconds for this single call
    """

    # required (every call has these)
    prompt_tokens: int
    completion_tokens: int
    # real cache split from the provider usage object (0 / absent when not reported)
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    # per-call latency
    latency_s: float
    # served model id for THIS call (gateway stamps it from the response; absent on legacy
    # rows -> pricing falls back to the run-level rates). Subagent calls may differ from
    # the run's --model (e.g. Claude Code's Explore requests haiku).
    model: str


# ── one (instance, arm) solve ───────────────────────────────────────────────
@dataclass
class RunRecord:
    """A single graded solve under one arm.

    Core (load-bearing for the headline metrics)
      instance        : SWE-bench instance id (e.g. "django__django-12345")
      arm             : arm name (e.g. "baseline", "bear", "dasein")
      success         : did the official grader pass the task
      ftp             : fail-to-pass fraction in [0,1] (partial credit signal)
      input_tokens    : total input tokens across the run (sum of prompts)
      output_tokens   : total completion tokens across the run
      cache_write_tok : tokens billed at the cache-write rate (= sum of per-call
                        cache_creation_input_tokens across the usage series)
      cache_read_tok  : tokens billed at the cache-read rate (= sum of per-call
                        cache_read_input_tokens across the usage series)
      calls           : number of model calls (agent steps)
      wall_s          : wall-clock seconds for the solve
      cost_usd        : cache-aware total $ for this run (writes+reads+output)
      patch           : the unified diff the agent submitted ("" if none)

    Outcome (exit accounting)
      pass_to_pass_ok : the official grader's pass-to-pass guard held (no
                        regression introduced by the patch)
      limit_death     : the run hit its call/wall cap WITHOUT ever submitting —
                        a productive-death failure mode distinct from a graded
                        wrong answer (a "Limits"-class exit)

    Effort / latency
      steps           : agent loop iterations (alias of calls when 1 call/step;
                        kept distinct for scaffolds that batch tool calls)
      tool_calls      : number of tool invocations the agent issued
      time_to_submit_s: wall-clock seconds from start to the submit action
                        (NaN / 0.0 when the run never submitted)
      mean_call_latency_s : mean of the per-call latency_s series

    Tokens (peak + uncached)
      max_prompt_tokens   : peak single-call prompt_tokens (the WORST call —
                            drives context-window risk)
      uncached_input_tokens : input tokens billed at the full input rate —
                            neither cache-written nor cache-read this run
                            (= sum over calls of
                            prompt_tokens − cache_creation − cache_read)

    Cache
      cache_hit_rate  : measured read share of total input work, in [0,1] =
                        cache_read_tok /
                        max(1, cache_read_tok + cache_write_tok
                                + uncached_input_tokens)

    Cost (both frames)
      cost_usd        : cache-aware total $ (the headline frame; see above)
      cost_usd_list   : list-frame UPPER BOUND $ = input_rate*input_tokens +
                        output_rate*output_tokens (no cache credit) — the
                        "what a naive bill would be" ceiling
      cache_write_usd : $ spent at the cache-write rate
      cache_read_usd  : $ spent at the cache-read rate
      output_usd      : $ spent on completion tokens

    Reliability
      retries         : infra retries this run incurred before producing a row
      degraded        : the run completed but in a degraded mode (e.g. a
                        compression/curation component fell back) — measured,
                        not fatal

    Optional context fields aid analysis and resume but are not load-bearing for
    the headline metrics.
    """

    # ── core ──
    instance: str
    arm: str
    success: bool
    ftp: float
    input_tokens: int
    output_tokens: int
    cache_write_tok: int
    cache_read_tok: int
    calls: int
    wall_s: float
    cost_usd: float
    patch: str = ""

    # ── outcome ──
    pass_to_pass_ok: bool = True
    limit_death: bool = False

    # ── effort / latency ──
    steps: int = 0
    tool_calls: int = 0
    time_to_submit_s: float = 0.0
    mean_call_latency_s: float = 0.0

    # ── tokens (peak + uncached) ──
    max_prompt_tokens: int = 0
    uncached_input_tokens: int = 0

    # ── cache ──
    cache_hit_rate: float = 0.0

    # ── cost (both frames) ──
    cost_usd_list: float = 0.0
    reported_cost_usd: float = 0.0  # model-service-reported $ (DefaultAgent.cost); authoritative headline
    cache_write_usd: float = 0.0
    cache_read_usd: float = 0.0
    output_usd: float = 0.0

    # ── reliability ──
    retries: int = 0
    degraded: bool = False

    # ── optional / diagnostic ──
    model: str = ""
    exit_status: str = ""
    usage: list[CallUsage] = field(default_factory=list)  # per-call series (for re-pricing)
    infra_failed: bool = False                            # True => excluded from metrics
    error: str = ""
    ts: float = field(default_factory=time.time)
    # billed gateway requests INCLUDING subagent traffic (calls = parent num_turns only
    # and hides ~30% of requests for spawning arms)
    requests: int = 0
    haiku_requests: int = 0

    def to_json(self) -> dict:
        """Plain dict for one ledger line."""
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "RunRecord":
        """Rebuild from a ledger line, ignoring unknown keys (forward-compatible)."""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# ── per-arm rollup ───────────────────────────────────────────────────────────
@dataclass
class AggResult:
    """Aggregate metrics for one arm across all its graded runs.

    Built by the report layer from the RunRecords of a single arm (infra
    failures excluded). Dollar fields are the cache-aware sums; *_flat / list
    fields are the list-price upper bound for context. The vs-baseline delta
    fields are left at their 0.0 default by the aggregator and filled by the
    report once the baseline arm is known.

    Core
      arm                  : arm name
      n                    : graded runs included
      n_success            : runs the grader passed
      success_rate         : n_success / n
      input_tokens         : total input tokens across the arm
      output_tokens        : total completion tokens across the arm
      cache_write_tok      : total cache-write tokens across the arm
      cache_read_tok       : total cache-read tokens across the arm
      cost_usd             : cache-aware total $ across the arm
      cost_usd_flat        : list-price upper bound $ across the arm
      cost_per_solved_usd  : cost_usd / max(n_success, 1)
      mean_calls           : mean model calls / run
      mean_wall_s          : mean wall-clock seconds / run
      total_wall_s         : total wall-clock seconds across the arm

    Outcome / effort
      limit_death_rate     : fraction of runs that hit a cap without submitting
      mean_steps           : mean agent steps / run
      mean_time_to_submit_s: mean time-to-submit over runs that submitted
      mean_max_prompt_tokens : mean of per-run peak single-call prompt size

    Cache (token-weighted)
      cache_hit_rate       : token-weighted mean hit rate across the arm =
                             Σ cache_read_tok /
                             max(1, Σ cache_read + Σ cache_write + Σ uncached_input)
      cache_write_usd      : $ at the cache-write rate (arm total)
      cache_read_usd       : $ at the cache-read rate (arm total)
      output_usd           : $ on completion tokens (arm total)

    Cost summaries
      median_cost_usd      : median cache-aware $ / run (tail-robust headline)
      cost_per_success     : alias of cost_per_solved_usd (report-friendly name)

    vs-baseline deltas (filled by the report against the baseline arm; 0.0 for
    the baseline itself). Positive = this arm SAVES vs baseline.
      input_saving_pct     : 100 * (1 − arm.input_tokens / baseline.input_tokens)
      output_saving_pct    : 100 * (1 − arm.output_tokens / baseline.output_tokens)
      cost_saving_pct      : 100 * (1 − arm.cost_usd / baseline.cost_usd)
    """

    # ── core ──
    arm: str
    n: int                       # graded runs included
    n_success: int               # runs the grader passed
    success_rate: float          # n_success / n

    input_tokens: int
    output_tokens: int
    cache_write_tok: int
    cache_read_tok: int

    cost_usd: float              # cache-aware total $ across the arm
    cost_usd_flat: float         # list-price upper bound across the arm
    cost_per_solved_usd: float   # cost_usd / max(n_success, 1)

    mean_calls: float
    mean_wall_s: float
    total_wall_s: float

    # ── outcome / effort ──
    limit_death_rate: float = 0.0
    mean_steps: float = 0.0
    mean_time_to_submit_s: float = 0.0
    mean_max_prompt_tokens: float = 0.0

    # ── cache (token-weighted) ──
    cache_hit_rate: float = 0.0
    cache_write_usd: float = 0.0
    cache_read_usd: float = 0.0
    output_usd: float = 0.0

    # ── cost summaries ──
    median_cost_usd: float = 0.0
    cost_per_success: float = 0.0   # alias of cost_per_solved_usd

    # ── vs-baseline deltas (filled by report) ──
    input_saving_pct: float = 0.0
    output_saving_pct: float = 0.0
    cost_saving_pct: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)
