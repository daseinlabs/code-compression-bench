"""The benchmark runner: the REAL mini-swe-agent scaffold, swappable compression arm.

This driver runs the open-source ``minisweagent`` package — the EXACT scaffold
our internal v5 eval uses — so the bench is byte-identical to v5 and baseline
(A0) / Dasein (A3S) rows are directly portable from the v5 run. We build the
same ``LitellmModel`` (Vertex ``model_kwargs``, ``set_cache_control``) + the
``minisweagent.agents.default.DefaultAgent`` + the swebench env via
``minisweagent.run.benchmarks.swebench.get_sb_environment``, mirroring
``adaptive_context/eval/mini_swe.py`` — WITHOUT importing any proprietary code.

Clean-room rule: this public repo must NOT import ``adaptive_context`` (our
curator/governor IP). ``minisweagent`` is open-source and IS used directly. The
Dasein compression happens server-side behind the dasein ProxyArm; the bench
never needs the curator code.

The ONLY thing that varies between arms is HOW the prompt is compressed at the
model-call seam — installed at the SAME point ``scripts/ab_curator.py`` uses:
we wrap ``model.query`` so every call routes ``messages -> arm -> orig(messages)``
AND records a CallUsage row off the litellm response the model produced.

    TransformArm  -> arm.transform(messages); the rewritten array is sent to the
                     normal endpoint (client-side compression).
    ProxyArm      -> swap the litellm ``api_base`` + merge ``headers()`` (server-side).
    ToolArm       -> fold ``attach().tools`` into the model's tool set (MCP spawn
                     stays a documented TODO).
    BaselineArm   -> the control: messages and endpoint pass through unchanged.

Scale-out: a ``ProcessPoolExecutor`` fans the full (instance x arm) grid across
``--workers`` processes. A JSONL ledger makes the run resumable — a completed
(instance, arm) pair is skipped on restart; infra failures are retried once and
never counted. Per-(task, arm) there are hard 50-call and wall-clock caps so a
runaway agent can't burn the budget (mapped onto the agent's step/cost limits +
a wall watchdog at the seam).

Each finished solve is graded by the official SWE-bench Docker harness
(:mod:`bench.grader`), priced cache-aware (:mod:`bench.pricing`), and written as a
:class:`bench.schema.RunRecord`.

Heavy imports (``minisweagent``, ``litellm``) are LAZY so ``--list-arms`` and
``import bench.runner`` work on a box where neither is installed.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

# arms self-register on import of the package; the registry lives in bench.arm.
import arms  # noqa: F401  (import side effect: registers every arm)
from bench.arm import ArmKind, ToolAttach, get_arm, available_arms
from bench.grader import SWEBenchGrader
from bench.pricing import price_run, rates_for
from bench.schema import RunRecord


# ── defaults / caps ─────────────────────────────────────────────────────────
DEFAULT_WORKERS = 8
CALL_CAP = 50                 # max model calls per (task, arm) — matches the gate2 cap
WALL_CAP_S = 50 * 60          # hard wall-clock watchdog per solve
DEFAULT_MAX_TOKENS = 8000     # completion cap per call
# Vertex project mirrors the v5 eval default (adaptive_context/eval/mini_swe.py).
VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "dasein-473321")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")


# ── the core scaffold tool (mirrors minisweagent's BASH_TOOL) ────────────────
# We import the package's own BASH_TOOL lazily inside the run path; ToolArm
# extras are folded into the model's tool set there. (SEARCH_TOOL is dropped:
# it needs the proprietary repo index, so the bench uses BASH only — the core.)


# ── task set loading ─────────────────────────────────────────────────────────
def load_tasks(path: str) -> list[str]:
    """Return the ordered list of instance ids from a task-set JSON file.

    Accepts either the bloated-50 shape ({"instances": [...]}) or a bare JSON
    list of instance ids.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return list(data.get("instances", []))
    if isinstance(data, list):
        return [x if isinstance(x, str) else x["instance_id"] for x in data]
    raise ValueError(f"unrecognized task file shape: {type(data)}")


# ── usage extraction (unchanged — REAL cache fields + normalization) ─────────
def _usage_get(usage, key: str) -> int:
    """Read an int field off a litellm usage object whether it's attr- or dict-shaped."""
    v = getattr(usage, key, None)
    if v is None and isinstance(usage, dict):
        v = usage.get(key)
    return int(v or 0)


def _extract_call_usage(usage, latency_s: float) -> dict:
    """One CallUsage dict from a litellm usage object: tokens + REAL cache split + latency.

    ``usage`` is the litellm response's ``usage`` (attr- or dict-shaped); the
    real scaffold surfaces it on the message it returns (``extra.response.usage``),
    so we read it straight off the response the model produced — no inference.
      - cache_creation_input_tokens : the cache WRITE (Anthropic/litellm)
      - cache_read_input_tokens     : the cache READ (Anthropic/litellm); also
                                      accept the OpenAI shape
                                      usage.prompt_tokens_details.cached_tokens
    Cache keys are emitted ONLY when the provider reported them (presence is the
    signal pricing uses to choose the real-cache path over the inferred-growth
    fallback); absent fields are simply omitted.
    """
    pt = _usage_get(usage, "prompt_tokens")
    ct = _usage_get(usage, "completion_tokens")
    out: dict = {"prompt_tokens": pt, "completion_tokens": ct, "latency_s": latency_s}
    if usage is None:
        return out

    # cache WRITE: Anthropic/litellm field
    has_write = (getattr(usage, "cache_creation_input_tokens", None) is not None) or (
        isinstance(usage, dict) and usage.get("cache_creation_input_tokens") is not None)
    if has_write:
        out["cache_creation_input_tokens"] = _usage_get(usage, "cache_creation_input_tokens")

    # cache READ: Anthropic/litellm field, OR OpenAI-style prompt_tokens_details.cached_tokens
    read = getattr(usage, "cache_read_input_tokens", None)
    if read is None and isinstance(usage, dict):
        read = usage.get("cache_read_input_tokens")
    if read is None:
        details = getattr(usage, "prompt_tokens_details", None)
        if details is None and isinstance(usage, dict):
            details = usage.get("prompt_tokens_details")
        if details is not None:
            read = getattr(details, "cached_tokens", None)
            if read is None and isinstance(details, dict):
                read = details.get("cached_tokens")
    if read is not None:
        out["cache_read_input_tokens"] = int(read or 0)

    # Provider-convention normalization so prompt_tokens == FULL billable input
    # (uncached + write + read) — the contract pricing.real_cache_cost expects.
    # Anthropic/litellm reports prompt_tokens as the UNCACHED portion only, with
    # cache_creation/cache_read as SEPARATE top-level fields, so fold them back in.
    # OpenAI reports prompt_tokens INCLUSIVE of cached_tokens (no cache_creation
    # field in that shape), so it's already full and we leave it.
    if "cache_creation_input_tokens" in out:  # Anthropic shape => prompt excludes cache
        out["prompt_tokens"] = (
            pt
            + int(out.get("cache_creation_input_tokens", 0) or 0)
            + int(out.get("cache_read_input_tokens", 0) or 0)
        )

    return out


# transient errors that warrant an in-run retry (rate limit / timeout / 5xx).
_RETRYABLE = ("RateLimit", "Timeout", "APIConnection", "ServiceUnavailable",
              "InternalServer", "Overloaded", "APIError")


# ── arm seam state (collected per run, read back into the RunRecord) ─────────
class _SeamState:
    """Mutable holder the wrapped ``model.query`` writes into during one solve.

    Lives in the worker process for the duration of one (instance, arm) solve.
    The wrapped query appends one CallUsage row per model call, counts retries,
    and flips ``degraded`` if a transform arm reports a fallback.
    """

    def __init__(self) -> None:
        self.usage: list[dict] = []
        self.retries: int = 0
        self.degraded: bool = False
        self.t0: float = time.time()
        self.wall_cap_s: float = float(WALL_CAP_S)


class _WallCapExceeded(Exception):
    """Raised inside the wrapped query to abort a run that blew its wall budget."""


def _install_arm_seam(model, arm, seam: _SeamState, max_retries: int = 2):
    """Wrap ``model.query`` at the SAME point scripts/ab_curator.py installs.

    Every model call routes ``messages -> arm transform -> orig(messages)`` and
    records a CallUsage row off the litellm response the model produced. This is
    the one seam where the arm is consulted (plus tool-set wiring for ToolArm,
    done by the caller before the agent runs).

      - TransformArm / Baseline : rewrite the message list client-side, then call
                                  the real query unchanged.
      - ProxyArm                : api_base + headers are merged into the model's
                                  model_kwargs by the caller (so the underlying
                                  litellm.completion routes through the arm
                                  endpoint); query just records usage.
      - ToolArm                 : tools already folded into the model; query just
                                  records usage.
    """
    orig = model.query

    is_transform = arm.kind in (ArmKind.TRANSFORM, ArmKind.BASELINE)

    def wrapped(messages, **kw):
        # wall-clock watchdog: the agent's own loop has no wall cap, so we trip
        # one at the seam (every call passes through here).
        if time.time() - seam.t0 > seam.wall_cap_s:
            raise _WallCapExceeded(f"wall cap {seam.wall_cap_s:.0f}s exceeded")

        call_messages = messages
        if is_transform:
            # arm rewrites the array; baseline returns it unchanged.
            call_messages = arm.transform(messages)  # type: ignore[attr-defined]
            if getattr(arm, "last_degraded", False):
                seam.degraded = True

        attempt = 0
        while True:
            t_call = time.time()
            try:
                msg = orig(call_messages, **kw)
            except Exception as e:  # noqa: BLE001
                name = type(e).__name__
                transient = any(tok in name for tok in _RETRYABLE)
                if not transient or attempt >= max_retries:
                    raise
                attempt += 1
                seam.retries += 1
                time.sleep(min(2 ** attempt, 8))
                continue
            latency_s = round(time.time() - t_call, 3)
            # the real scaffold surfaces the litellm response on the returned
            # message at extra.response (mirrors AdaptiveAgent.query reading
            # msg["extra"]["response"]["usage"]).
            resp = (msg.get("extra", {}) or {}).get("response", {}) or {}
            usage = resp.get("usage") if isinstance(resp, dict) else getattr(resp, "usage", None)
            seam.usage.append(_extract_call_usage(usage, latency_s))
            return msg

    model.query = wrapped


def _make_model(model_name: str, arm, *, max_tokens: int):
    """Build the v5 LitellmModel (Vertex model_kwargs, cache_control), mirroring
    adaptive_context/eval/mini_swe.py::make_model — minus the proprietary
    AdaptiveLitellmModel/SEARCH_TOOL. The bench advertises ONLY the core BASH_TOOL.

    For a ToolArm, the arm's extra tools are folded into the model's advertised
    tool set by subclassing the model's ``_query`` (the same hook mini_swe.py
    uses to add SEARCH_TOOL). For a ProxyArm, api_base + headers are merged into
    ``model_kwargs`` so the underlying ``litellm.completion`` routes through the
    arm endpoint.
    """
    import litellm  # lazy
    from minisweagent.models.litellm_model import LitellmModel  # lazy
    from minisweagent.models.utils.actions_toolcall import BASH_TOOL  # lazy

    name_l = model_name.lower()
    # Claude: explicit cache_control (no min). Gemini: implicit caching (no
    # cache_control). Mirrors mui_swe.py make_model.
    cache = "default_end" if "claude" in name_l else None
    mk = {"max_tokens": max_tokens, "temperature": 0.0}
    # Vertex routing only when the model id is a vertex model (keeps OpenAI-
    # compatible / proxied calls clean).
    if "vertex" in name_l or model_name.startswith("vertex_ai/"):
        mk["vertex_project"] = VERTEX_PROJECT
        mk["vertex_location"] = VERTEX_LOCATION
    if "claude" not in name_l and ("gemini" in name_l or "vertex" in name_l):
        # reasoning_effort is a Gemini-3 param, not Claude (see mini_swe.py).
        mk["reasoning_effort"] = "low"

    # ProxyArm: route the underlying litellm call through the arm endpoint by
    # merging api_base + extra_headers into model_kwargs (LitellmModel._query
    # does litellm.completion(..., **(model_kwargs | kwargs))).
    extra_tools: list[dict] = []
    if arm.kind == ArmKind.PROXY:
        base = arm.model_base_url()  # type: ignore[attr-defined]
        if base:
            mk["api_base"] = base
        hdrs = arm.headers() or {}  # type: ignore[attr-defined]
        if hdrs:
            mk["extra_headers"] = hdrs
    elif arm.kind == ArmKind.TOOL:
        attach = arm.attach()  # type: ignore[attr-defined]
        extra_tools = list(attach.tools or [])
        # TODO(woz): spawn attach.mcp_server_cmd as a stdio MCP server and bridge
        # its tools in here; tear it down after the run. Stubbed until the woz
        # server command lands (documented seam, same as the prior runner).

    if extra_tools:
        # Fold the arm's tools into the model's advertised tool set the same way
        # mini_swe.py's AdaptiveLitellmModel adds SEARCH_TOOL: override _query.
        tools = [BASH_TOOL] + extra_tools

        class _ToolLitellmModel(LitellmModel):
            def _query(self, messages, **kwargs):
                return litellm.completion(
                    model=self.config.model_name, messages=messages,
                    tools=tools, **(self.config.model_kwargs | kwargs))

        return _ToolLitellmModel(model_name=model_name, set_cache_control=cache, model_kwargs=mk)

    return LitellmModel(model_name=model_name, set_cache_control=cache, model_kwargs=mk)


def _load_swebench_config() -> dict:
    """The package's own swebench.yaml — same config mini_swe.py loads."""
    import yaml  # lazy
    import minisweagent.config as cfg  # lazy
    return yaml.safe_load(
        (Path(cfg.__file__).parent / "benchmarks" / "swebench.yaml").read_text())


def _fetch_instance(instance_id: str, dataset: str, split: str) -> dict:
    """Resolve the SWE-bench instance dict get_sb_environment needs.

    Uses the HuggingFace ``datasets`` loader the swebench harness ships with;
    falls back to a minimal dict carrying just the instance_id if the dataset is
    unavailable (get_sb_environment keys the image off instance_id).
    """
    try:
        from datasets import load_dataset  # lazy
        ds = load_dataset(dataset, split=split)
        for row in ds:
            if row.get("instance_id") == instance_id:
                return dict(row)
    except Exception:
        pass
    return {"instance_id": instance_id}


# ── the fixed agent loop (now the REAL minisweagent.DefaultAgent) ─────────────
def run_agent(
    arm,
    instance_id: str,
    *,
    model: str,
    dataset: str,
    split: str,
    call_cap: int = CALL_CAP,
    wall_cap_s: int = WALL_CAP_S,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    exec_timeout_s: int = 120,
) -> dict:
    """Drive one (instance, arm) solve with the real minisweagent scaffold.

    Builds the v5 model + DefaultAgent + swebench env (mirroring mini_swe.py),
    installs the arm at the model.query seam, runs the agent, and returns a dict
    of raw run signals. No grading here — the caller grades the returned patch.
    """
    import copy
    from minisweagent.agents.default import DefaultAgent  # lazy
    from minisweagent.run.benchmarks.swebench import get_sb_environment  # lazy

    seam = _SeamState()
    seam.wall_cap_s = float(wall_cap_s)

    arm.setup()
    model_obj = _make_model(model, arm, max_tokens=max_tokens)
    _install_arm_seam(model_obj, arm, seam)

    instance = _fetch_instance(instance_id, dataset, split)
    config = copy.deepcopy(_load_swebench_config())
    agent_cfg = config.get("agent", {})
    # Map the bench caps onto the agent's native limits. step_limit == call cap
    # (one model call per step); cost_limit guards $; the wall cap is enforced at
    # the seam (the agent loop has no wall clock).
    agent_cfg["step_limit"] = call_cap
    agent_cfg.setdefault("cost_limit", 0.0)  # 0 == unlimited in minisweagent

    exit_status = "incomplete"
    patch = ""
    submitted = False
    calls = 0
    steps = 0
    tool_call_count = 0
    time_to_submit_s = 0.0
    t0 = seam.t0

    env = get_sb_environment(config, instance)
    try:
        agent = DefaultAgent(model_obj, env, **agent_cfg)
        info = agent.run(task=instance.get("problem_statement", "") or f"Fix instance {instance_id}")
        exit_status = info.get("exit_status", "?") or "?"
        patch = info.get("submission", "") or ""
        submitted = bool(patch) or exit_status.lower().startswith("submit")
        calls = int(getattr(agent, "n_calls", 0) or 0)
        # one model call per step in DefaultAgent; count assistant turns + their
        # tool calls for the effort KPIs.
        msgs = getattr(agent, "messages", []) or []
        asst = [m for m in msgs if m.get("role") == "assistant"]
        steps = len(asst) or calls
        for m in asst:
            tool_call_count += len(m.get("tool_calls") or [])
        if submitted:
            time_to_submit_s = round(time.time() - t0, 1)
    except _WallCapExceeded:
        exit_status = "wall_cap"
    except Exception as e:
        # surface infra failures to the worker (retried once, never counted).
        exit_status = f"infra:{type(e).__name__}"
        try:
            env.cleanup()
        except Exception:
            pass
        try:
            arm.teardown()
        except Exception:
            pass
        raise RunInfraError(str(e)[:300]) from e
    finally:
        try:
            env.cleanup()
        except Exception:
            pass
        try:
            arm.teardown()
        except Exception:
            pass

    usage = seam.usage
    calls = calls or len(usage)
    steps = steps or calls
    in_tok = sum(u["prompt_tokens"] for u in usage)
    out_tok = sum(u["completion_tokens"] for u in usage)
    max_prompt = max((u["prompt_tokens"] for u in usage), default=0)
    lats = [u["latency_s"] for u in usage if u.get("latency_s") is not None]
    mean_lat = round(sum(lats) / len(lats), 3) if lats else 0.0
    # limit-death: hit a call/wall cap WITHOUT ever submitting (productive-death).
    # minisweagent reports a LimitsExceeded exit; treat that + our wall cap as caps.
    el = exit_status.lower()
    hit_cap = exit_status == "wall_cap" or "limit" in el or calls >= call_cap
    limit_death = hit_cap and not submitted
    return {
        "instance": instance_id,
        "arm": arm.name,
        "patch": patch,
        "calls": calls,
        "exit_status": exit_status,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "usage": usage,
        "wall_s": round(time.time() - t0, 1),
        # outcome / effort / reliability signals
        "submitted": submitted,
        "limit_death": limit_death,
        "steps": steps,
        "tool_calls": tool_call_count,
        "time_to_submit_s": time_to_submit_s,
        "mean_call_latency_s": mean_lat,
        "max_prompt_tokens": max_prompt,
        "retries": seam.retries,
        "degraded": seam.degraded,
    }


class RunInfraError(Exception):
    """Raised on an infrastructure failure (docker/model/network) — retried once."""


# ── worker: run + grade + price one (instance, arm) ──────────────────────────
def _worker(job: tuple) -> dict:
    """Process-pool task: solve, grade, price one (instance, arm). Never raises.

    Returns a ``RunRecord.to_json()`` dict. On infra failure, returns a stub with
    ``infra_failed=True`` (excluded from metrics, retried once by the driver).
    """
    (instance_id, arm_name, model, dataset, split, call_cap, wall_cap_s,
     max_tokens, exec_timeout_s, grade_timeout_s) = job
    t0 = time.time()
    try:
        arm = get_arm(arm_name)
        raw = run_agent(
            arm, instance_id, model=model, dataset=dataset, split=split,
            call_cap=call_cap, wall_cap_s=wall_cap_s,
            max_tokens=max_tokens, exec_timeout_s=exec_timeout_s,
        )
        grader = SWEBenchGrader(dataset=dataset, split=split, timeout_s=grade_timeout_s)
        g = grader.grade(instance_id, raw["patch"])

        rates = rates_for(model)
        # Price from the REAL per-call cache fields when the provider reported
        # them; fall back to the inferred-from-prompt-growth frame otherwise.
        cb = price_run(raw["usage"], rates)
        uncached = cb.uncached_input_tok

        rec = RunRecord(
            instance=instance_id,
            arm=arm_name,
            success=bool(g.success),
            ftp=float(g.ftp),
            input_tokens=raw["input_tokens"],
            output_tokens=raw["output_tokens"],
            cache_write_tok=cb.cache_write_tok,
            cache_read_tok=cb.cache_read_tok,
            calls=raw["calls"],
            wall_s=raw["wall_s"],
            cost_usd=round(cb.total_usd, 6),
            patch=raw["patch"],
            # ── outcome ──
            pass_to_pass_ok=(g.n_pass_to_pass_passed >= g.n_pass_to_pass),
            limit_death=bool(raw["limit_death"]),
            # ── effort / latency ──
            steps=raw["steps"],
            tool_calls=raw["tool_calls"],
            time_to_submit_s=raw["time_to_submit_s"],
            mean_call_latency_s=raw["mean_call_latency_s"],
            # ── tokens (peak + uncached) ──
            max_prompt_tokens=raw["max_prompt_tokens"],
            uncached_input_tokens=uncached,
            # ── cache ──
            cache_hit_rate=round(cb.cache_hit_rate, 4),
            # ── cost (both frames) ──
            cost_usd_list=round(cb.list_usd, 6),
            cache_write_usd=round(cb.write_usd, 6),
            cache_read_usd=round(cb.read_usd, 6),
            output_usd=round(cb.output_usd, 6),
            # ── reliability ──
            retries=raw["retries"],
            degraded=bool(raw["degraded"]),
            # ── diagnostic ──
            model=model,
            exit_status=raw["exit_status"],
            usage=raw["usage"],
            infra_failed=False,
            error=("grade: " + g.error) if g.error else "",
        )
        return rec.to_json()
    except RunInfraError as e:
        return _infra_stub(instance_id, arm_name, model, str(e), t0)
    except Exception as e:  # noqa: BLE001 — worker must never crash the pool
        return _infra_stub(instance_id, arm_name, model,
                           f"{type(e).__name__}: {str(e)[:200]}", t0)


def _infra_stub(instance_id: str, arm_name: str, model: str, err: str, t0: float) -> dict:
    return RunRecord(
        instance=instance_id, arm=arm_name, success=False, ftp=0.0,
        input_tokens=0, output_tokens=0, cache_write_tok=0, cache_read_tok=0,
        calls=0, wall_s=round(time.time() - t0, 1), cost_usd=0.0,
        model=model, exit_status="infra_failed", infra_failed=True, error=err,
    ).to_json()


# ── resume ledger ─────────────────────────────────────────────────────────────
def _load_done(ledger: Path) -> set[tuple[str, str]]:
    """The set of (instance, arm) pairs already completed (non-infra) in the ledger."""
    done: set[tuple[str, str]] = set()
    if not ledger.exists():
        return done
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue  # truncated tail from a prior crash — skip
        if not r.get("infra_failed"):
            done.add((r["instance"], r["arm"]))
    return done


# ── arm readiness listing ─────────────────────────────────────────────────────
def list_arms() -> None:
    print("registered arms (env readiness):")
    for name in available_arms():
        arm = get_arm(name)
        ok, reason = arm.ready()
        flag = "READY" if ok else "SKIP "
        print(f"  [{flag}] {name:10s} kind={arm.kind.value:9s} {reason}")


# ── driver ─────────────────────────────────────────────────────────────────────
def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="code-compression-bench runner")
    ap.add_argument("--tasks", default="tasks_bloated50.json", help="task-set JSON path")
    ap.add_argument("--arms", default="baseline",
                    help="comma-separated arm names (default: baseline)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the number of instances (0 = all); smoke uses 1")
    ap.add_argument("--out", default="runs", help="output dir for the ledger + per-run JSON")
    ap.add_argument("--model", default=os.environ.get("MODEL", "claude-sonnet-4-5"))
    ap.add_argument("--dataset", default=SWEBenchGraderDefault("dataset"))
    ap.add_argument("--split", default=SWEBenchGraderDefault("split"))
    ap.add_argument("--call-cap", type=int, default=CALL_CAP)
    ap.add_argument("--wall-cap-s", type=int, default=WALL_CAP_S)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--exec-timeout-s", type=int, default=120)
    ap.add_argument("--grade-timeout-s", type=int, default=1800)
    ap.add_argument("--list-arms", action="store_true", help="list arms + readiness and exit")
    a = ap.parse_args()

    if a.list_arms:
        list_arms()
        return

    arm_names = [x.strip() for x in a.arms.split(",") if x.strip()]
    # gate on readiness: don't schedule work for an arm that can't run.
    ready_arms: list[str] = []
    for name in arm_names:
        try:
            arm = get_arm(name)
        except KeyError as e:
            print(f"  skip unknown arm: {e}")
            continue
        ok, reason = arm.ready()
        if ok:
            ready_arms.append(name)
        else:
            print(f"  skip arm '{name}': {reason}")
    if not ready_arms:
        print("no ready arms — nothing to run.")
        return

    instances = load_tasks(a.tasks)
    if a.limit:
        instances = instances[:a.limit]

    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = out_dir / "runs"
    runs_dir.mkdir(exist_ok=True)
    ledger = out_dir / "ledger.jsonl"

    done = _load_done(ledger)
    print(f"resume: {len(done)} completed (instance, arm) pairs in {ledger}")

    jobs = [
        (iid, arm, a.model, a.dataset, a.split, a.call_cap, a.wall_cap_s,
         a.max_tokens, a.exec_timeout_s, a.grade_timeout_s)
        for iid in instances
        for arm in ready_arms
        if (iid, arm) not in done
    ]
    print(f"scheduling {len(jobs)} runs over {len(instances)} instances x "
          f"{len(ready_arms)} arms ({a.workers} workers)")
    if not jobs:
        print("nothing to do (all pairs already in the ledger).")
        return

    def log(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    retried: set[tuple[str, str]] = set()
    with ProcessPoolExecutor(max_workers=a.workers,
                             mp_context=mp.get_context("spawn")) as ex:
        futs = {ex.submit(_worker, j): j for j in jobs}
        while futs:
            for fut in as_completed(list(futs)):
                j = futs.pop(fut)
                iid, arm_name = j[0], j[1]
                try:
                    row = fut.result()
                except Exception as e:  # executor-level failure
                    row = _infra_stub(iid, arm_name, j[2],
                                      f"executor: {type(e).__name__}: {str(e)[:200]}",
                                      time.time())
                # retry an infra failure exactly once; graded results never retried.
                if row.get("infra_failed") and (iid, arm_name) not in retried:
                    retried.add((iid, arm_name))
                    log(f"  RETRY {iid} [{arm_name}] after infra failure: {row.get('error')}")
                    futs[ex.submit(_worker, j)] = j
                    continue
                # persist: per-run JSON sidecar + one ledger line.
                (runs_dir / f"{iid}__{arm_name}.json").write_text(
                    json.dumps(row), encoding="utf-8")
                with ledger.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
                if not row.get("infra_failed"):
                    done.add((iid, arm_name))
                    log(f"  [{arm_name}] {iid}: success={row['success']} "
                        f"in={row['input_tokens']:,} calls={row['calls']} "
                        f"cost=${row['cost_usd']:.4f} ({row['exit_status']})")
                else:
                    log(f"  [{arm_name}] {iid}: infra_failed {row.get('error', '')[:80]}")
    log("BENCH_RUN_DONE")


def SWEBenchGraderDefault(field: str) -> str:
    """Defer to the grader module's env-driven defaults for dataset/split."""
    from bench import grader as _g
    return _g.DEFAULT_DATASET if field == "dataset" else _g.DEFAULT_SPLIT


if __name__ == "__main__":
    main()
