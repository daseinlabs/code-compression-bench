"""Cache-aware per-model pricing (pure stdlib).

Agent runs are long multi-turn loops where the prompt grows monotonically: each
call re-sends the whole prior transcript plus the newest turn. With prompt
caching, that re-sent prefix is billed at the cheap CACHE-READ rate and only the
newly-appended tokens are billed at the expensive CACHE-WRITE rate; output is
billed flat. Pricing a run at a single flat input rate massively overstates the
bill, so we reconstruct the cache frame from the per-call usage series.

PRICE_TABLE is $/MTok. Rates are seeded for common Sonnet/Opus/Haiku and a few
OpenAI/Gemini rows; override or extend at the call site. All figures are public
list-price approximations — confirm against your provider's current rates.

The cache-frame cost function reimplements the logic of the reference
`cache_price()` (in meta_learning/scripts/g200_artifacts.py) as standalone code:
per call, the GROWTH in prompt_tokens over the prior call is priced as a write,
the prior prefix is priced as a read, and completion tokens are priced flat. A
prompt that SHRINKS (a compression arm trims the transcript) is handled by the
`shrink_fresh_tokens` hook so the dropped prefix is not mis-billed as a write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional


# ── price table: $/MTok = dollars per million tokens ────────────────────────
# cache_write : first-seen / freshly-appended input tokens (full input rate)
# cache_read  : cached prefix re-sent on a later call (discounted)
# output      : completion tokens (flat)
PRICE_TABLE: dict[str, dict[str, float]] = {
    # Anthropic Claude (list price, $/MTok)
    "claude-sonnet": {"cache_write": 3.75, "cache_read": 0.30, "output": 15.0},
    "claude-opus":   {"cache_write": 6.25, "cache_read": 0.50, "output": 25.0},
    "claude-haiku":  {"cache_write": 1.25, "cache_read": 0.10, "output": 5.0},
    # OpenAI (approximate; confirm current rates)
    "gpt-4o":        {"cache_write": 2.50, "cache_read": 1.25, "output": 10.0},
    "gpt-4o-mini":   {"cache_write": 0.15, "cache_read": 0.075, "output": 0.60},
    # Google Gemini (approximate; implicit caching)
    "gemini-1.5-pro":   {"cache_write": 1.25, "cache_read": 0.3125, "output": 5.0},
    "gemini-1.5-flash": {"cache_write": 0.075, "cache_read": 0.01875, "output": 0.30},
}

# Fallback used when a model id matches no table row (and no override given).
DEFAULT_RATES: dict[str, float] = {"cache_write": 3.75, "cache_read": 0.30, "output": 15.0}


@dataclass
class CostBreakdown:
    """Dollar decomposition of one run plus the token counts that produced it."""

    write_usd: float          # freshly-appended input tokens, priced at cache_write
    read_usd: float           # re-sent cached prefix, priced at cache_read
    output_usd: float         # completion tokens, priced at output
    cache_write_tok: int      # total tokens billed as writes
    cache_read_tok: int       # total tokens billed as reads
    output_tok: int           # total completion tokens

    @property
    def input_usd(self) -> float:
        """Input bill = writes + reads (excludes output)."""
        return self.write_usd + self.read_usd

    @property
    def total_usd(self) -> float:
        return self.write_usd + self.read_usd + self.output_usd


def rates_for(model_id: str,
              table: Optional[dict[str, dict[str, float]]] = None) -> dict[str, float]:
    """Resolve {cache_write, cache_read, output} for a model id.

    Matching is by longest case-insensitive substring of a table key against the
    model id (so 'vertex_ai/claude-sonnet-4-6' resolves to the 'claude-sonnet'
    row). Falls back to DEFAULT_RATES when nothing matches.
    """
    tbl = table if table is not None else PRICE_TABLE
    mid = model_id.lower()
    best_key, best_len = None, -1
    for key in tbl:
        k = key.lower()
        if k in mid and len(k) > best_len:
            best_key, best_len = key, len(k)
    return dict(tbl[best_key]) if best_key is not None else dict(DEFAULT_RATES)


def cache_frame_cost(
    prompt_tokens: Iterable[int],
    completion_tokens: Iterable[int],
    rates: dict[str, float],
    *,
    shrink_fresh_tokens: Optional[Callable[[int], int]] = None,
) -> CostBreakdown:
    """Cache-aware cost from a run's per-call usage series.

    prompt_tokens / completion_tokens : per-assistant-call usage, in call order.
        prompt_tokens[i] is the full input the model saw on call i (prior
        transcript + newest turn); completion_tokens[i] is what it produced.
    rates : {cache_write, cache_read, output} in $/MTok (see rates_for()).
    shrink_fresh_tokens : optional. When call i's prompt is SMALLER than call
        i-1's (a compression layer trimmed the transcript), the cached prefix
        is shorter than before; this callback returns how many of call i's
        tokens are genuinely NEW (a write), and the remainder is the surviving
        cached prefix (a read). If omitted, a shrink is treated as: everything
        is a read except a small fixed allowance — conservative, never negative.

    Mirrors g200_artifacts.cache_price(): growth = write, prior prefix = read,
    output flat; shrink ("tail transient") handled without mis-billing.
    """
    pw = rates["cache_write"] / 1e6
    pr = rates["cache_read"] / 1e6
    po = rates["output"] / 1e6

    prompts = list(prompt_tokens)
    comps = list(completion_tokens)

    w_tok = r_tok = o_tok = 0
    prev_p = 0
    for i, p in enumerate(prompts):
        ct = comps[i] if i < len(comps) else 0
        o_tok += ct
        if p >= prev_p:
            # prompt grew (the normal monotonic case): new = write, prefix = read
            w_tok += (p - prev_p)
            r_tok += prev_p
        else:
            # prompt shrank: only the genuinely-new tokens are a write
            fresh = shrink_fresh_tokens(i) if shrink_fresh_tokens else 50
            fresh = max(0, min(fresh, p))
            common = p - fresh
            w_tok += fresh
            r_tok += common
        prev_p = p

    return CostBreakdown(
        write_usd=w_tok * pw,
        read_usd=r_tok * pr,
        output_usd=o_tok * po,
        cache_write_tok=w_tok,
        cache_read_tok=r_tok,
        output_tok=o_tok,
    )


def flat_cost(input_tokens: int, output_tokens: int, rates: dict[str, float]) -> float:
    """Simple list-price bill (no caching): input at cache_write, output flat.

    The pessimistic upper bound to report alongside the cache-aware figure.
    """
    return input_tokens / 1e6 * rates["cache_write"] + output_tokens / 1e6 * rates["output"]
