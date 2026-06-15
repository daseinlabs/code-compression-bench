"""On-disk record schema for the benchmark.

A run is one (instance, arm) solve: the fixed scaffold attempts one SWE-bench
task under one compression arm. `RunRecord` captures everything the runner
writes per solve; the ledger is one JSON object per line (resume-safe), and
`AggResult` is the per-arm rollup the report/figures consume.

Pure stdlib dataclasses + a TypedDict mirror for the JSON shape. Token/cost
fields line up with bench.pricing.CostBreakdown so a record can be priced and
its dollar fields filled in one pass.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Optional, TypedDict


# ── per-call usage series (drives cache-aware pricing) ──────────────────────
class CallUsage(TypedDict):
    """One assistant call's token usage, in call order within a run."""

    prompt_tokens: int      # full input the model saw on this call
    completion_tokens: int  # tokens it produced


# ── one (instance, arm) solve ───────────────────────────────────────────────
@dataclass
class RunRecord:
    """A single graded solve under one arm.

    instance        : SWE-bench instance id (e.g. "django__django-12345")
    arm             : arm name (e.g. "baseline", "bear", "dasein")
    success         : did the official grader pass the task
    ftp             : fail-to-pass fraction in [0,1] (partial credit signal)
    input_tokens    : total input tokens across the run (sum of prompts)
    output_tokens   : total completion tokens across the run
    cache_write_tok : tokens billed at the cache-write rate (freshly appended)
    cache_read_tok  : tokens billed at the cache-read rate (re-sent prefix)
    calls           : number of model calls (agent steps)
    wall_s          : wall-clock seconds for the solve
    cost_usd        : cache-aware total $ for this run (writes+reads+output)
    patch           : the unified diff the agent submitted ("" if none)

    Optional context fields aid analysis and resume but are not load-bearing for
    the headline metrics.
    """

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

    # optional / diagnostic
    model: str = ""
    exit_status: str = ""
    cache_write_usd: float = 0.0
    cache_read_usd: float = 0.0
    output_usd: float = 0.0
    usage: list[CallUsage] = field(default_factory=list)  # per-call series (for re-pricing)
    infra_failed: bool = False                            # True => excluded from metrics
    error: str = ""
    ts: float = field(default_factory=time.time)

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
    failures excluded). Dollar fields are the cache-aware sums; *_usd_flat is the
    list-price upper bound for context.
    """

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

    def to_json(self) -> dict:
        return asdict(self)
