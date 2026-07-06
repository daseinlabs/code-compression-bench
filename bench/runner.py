"""The benchmark runner: the REAL mini-swe-agent scaffold, swappable compression arm.

This driver runs the open-source ``minisweagent`` package — the same scaffold
a reference eval uses — so the bench is byte-identical across arms and the
baseline and arm rows are directly comparable. We build the
same ``LitellmModel`` (Vertex ``model_kwargs``, ``set_cache_control``) + the
``minisweagent.agents.default.DefaultAgent`` + the swebench env via
``minisweagent.run.benchmarks.swebench.get_sb_environment``, mirroring a reference eval — WITHOUT importing any proprietary code.

Clean-room rule: this public repo must NOT import any vendor internals.
``minisweagent`` is open-source and IS used directly. The Dasein compression
happens server-side behind the dasein ProxyArm; the bench never needs it.

The ONLY thing that varies between arms is HOW the prompt is compressed at the
model-call seam — installed at the model-call seam:
we wrap ``model.query`` so every call routes ``messages -> arm -> orig(messages)``
AND records a CallUsage row off the litellm response the model produced.

    TransformArm  -> arm.transform(messages); the rewritten array is sent to the
                     normal endpoint (client-side compression).
    ProxyArm      -> swap the litellm ``api_base`` + merge ``headers()`` (server-side).
    ToolArm       -> spawn ``attach().mcp_server_cmd`` as a real MCP stdio server,
                     handshake + ``tools/list`` to discover its REAL tools, advertise
                     those to the model, and dispatch the model's tool calls over the
                     stdio pipe (``bench.mcp_client``). bash/non-MCP tools still run in
                     the container env. The server is torn down in finally.
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
import hashlib
import json
import multiprocessing as mp
import os
import shutil
import subprocess
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
CALL_CAP = 100                # max model turns (steps) per (task, arm) — MUST match the reference run
                              # (mini_swe solve_instance step_limit=100) so all arms' rows and
                              # live vendor arms share the identical turn budget.
WALL_CAP_S = 50 * 60          # hard wall-clock watchdog per solve
DEFAULT_MAX_TOKENS = 8000     # completion cap per call
# Vertex project — set via env (no hardcoded default in the public repo).
VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")


# ── the core scaffold tool (mirrors minisweagent's BASH_TOOL) ────────────────
# We import the package's own BASH_TOOL lazily inside the run path. For a ToolArm
# the REAL tools discovered from the live MCP ``tools/list`` are folded into the
# model's tool set there (replacing BASH when the arm sets replace_tools). The
# proprietary SEARCH_TOOL is NOT used: a ToolArm brings its own server-side tools.


# ── MCP tool-schema -> OpenAI function-tool spec ─────────────────────────────
def _mcp_tool_to_openai_spec(tool: dict) -> dict:
    """Convert a REAL MCP tool schema (from ``tools/list``) into the OpenAI
    function-tool dict the model expects.

    MCP advertises ``{name, description, inputSchema}`` where ``inputSchema`` is
    a JSON Schema object; OpenAI/litellm wants
    ``{"type":"function","function":{"name","description","parameters"}}`` where
    ``parameters`` IS that JSON Schema. So this is a near-passthrough — we just
    re-nest under ``function`` and default an empty object schema when absent.
    """
    schema = tool.get("inputSchema") or tool.get("input_schema") or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", "") or "",
            "parameters": schema,
        },
    }


# ── task set loading ─────────────────────────────────────────────────────────
def load_tasks(path: str) -> list[str]:
    """Return the ordered list of instance ids from a task-set JSON file.

    Accepts either the task-set shape ({"instances": [...]}) or a bare JSON
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
        # per-call client-side arm effect (TransformArm only): json char counts
        # of the message array before vs after arm.transform — quantifies what a
        # client-side arm stripped. Empty for proxy/tool arms (server-side).
        self.transform_log: list[dict] = []


class _WallCapExceeded(Exception):
    """Raised inside the wrapped query to abort a run that blew its wall budget."""


def _install_arm_seam(model, arm, seam: _SeamState, max_retries: int = 2):
    """Wrap ``model.query`` at the model-call seam.

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
            # arm rewrites the array; baseline returns it unchanged. Record the
            # per-call client-side effect: json char count before vs after the
            # transform (proxy/tool arms compress server-side — invisible here).
            pre_chars = len(json.dumps(messages, default=str))
            call_messages = arm.transform(messages)  # type: ignore[attr-defined]
            post_chars = len(json.dumps(call_messages, default=str))
            seam.transform_log.append(
                {"pre_prompt_chars": pre_chars, "post_prompt_chars": post_chars})
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
            # message at extra.response (the scaffold reads
            # msg["extra"]["response"]["usage"]).
            resp = (msg.get("extra", {}) or {}).get("response", {}) or {}
            usage = resp.get("usage") if isinstance(resp, dict) else getattr(resp, "usage", None)
            seam.usage.append(_extract_call_usage(usage, latency_s))
            return msg

    model.query = wrapped


def _make_model(
    model_name: str,
    arm,
    *,
    max_tokens: int,
    mcp_tool_specs: Optional[list[dict]] = None,
    replace_bash: bool = False,
):
    """Build the LitellmModel (Vertex model_kwargs, cache_control) — the core
    scaffold model, minus any proprietary model/tool. The bench advertises ONLY the core BASH_TOOL.

    For a ToolArm, ``mcp_tool_specs`` are the REAL OpenAI-format tool specs we
    derived from the live ``tools/list`` of the spawned MCP server (the caller in
    ``run_agent`` owns the server lifecycle and passes them in). They are folded
    into the model's advertised tool set by subclassing the model's ``_query``
    (the SAME hook mini_swe.py uses to add SEARCH_TOOL). When ``replace_bash`` is
    True (the woz ``replace_tools`` contract) the native BASH_TOOL is REMOVED so
    the model can only reach the MCP tools.

    Critically, the stock ``parse_toolcall_actions`` HARD-REJECTS any tool whose
    name is not ``bash`` (raises FormatError). To let the model call MCP tools we
    must override the model's action parsing so the tool NAME and raw ARGUMENTS
    survive onto each action dict; the agent's dispatcher (see ``_McpAgent`` in
    run_agent) then routes by name to the MCP client vs the bash env.

    For a ProxyArm, api_base + headers are merged into ``model_kwargs`` so the
    underlying ``litellm.completion`` routes through the arm endpoint.
    """
    import json as _json  # lazy (local alias; module-level json already imported)
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
    if arm.kind == ArmKind.PROXY:
        base = arm.model_base_url()  # type: ignore[attr-defined]
        if base:
            mk["api_base"] = base
        hdrs = arm.headers() or {}  # type: ignore[attr-defined]
        if hdrs:
            mk["extra_headers"] = hdrs

    if mcp_tool_specs:
        # Advertised tool set: MCP tools, plus BASH unless the arm replaces it.
        tools = list(mcp_tool_specs) if replace_bash else [BASH_TOOL] + list(mcp_tool_specs)
        mcp_names = {t["function"]["name"] for t in mcp_tool_specs}

        class _ToolLitellmModel(LitellmModel):
            def _query(self, messages, **kwargs):
                # Same hook mini_swe.py uses for SEARCH_TOOL — advertise the live
                # MCP tools (and BASH unless replaced) on every completion.
                return litellm.completion(
                    model=self.config.model_name, messages=messages,
                    tools=tools, **(self.config.model_kwargs | kwargs))

            def _parse_actions(self, response):
                # Override the stock bash-only parser. The stock
                # parse_toolcall_actions raises FormatError on any non-bash tool
                # and drops the tool name; we keep name + arguments so the agent
                # can dispatch MCP calls to the MCP client. We still emit a
                # FormatError-shaped message when the model returns NO tool call
                # (so the scaffold's format-error handling is unchanged).
                from minisweagent.exceptions import FormatError  # lazy
                msg = response.choices[0].message
                tool_calls = getattr(msg, "tool_calls", None) or []
                if not tool_calls:
                    raise FormatError({
                        "role": "user",
                        "content": ("No tool calls found in the response. Every "
                                    "response MUST include at least one tool call."),
                        "extra": {"interrupt_type": "FormatError"},
                    })
                actions: list[dict] = []
                for tc in tool_calls:
                    tname = tc.function.name
                    try:
                        targs = _json.loads(tc.function.arguments or "{}")
                    except Exception:
                        targs = {}
                    if not isinstance(targs, dict):
                        targs = {}
                    action = {"tool_call_id": tc.id, "tool_name": tname, "tool_args": targs}
                    if tname == "bash":
                        # Keep the native shape too: env.execute reads "command".
                        action["command"] = targs.get("command", "")
                    actions.append(action)
                return actions

        return _ToolLitellmModel(
            model_name=model_name, set_cache_control=cache, model_kwargs=mk,
        ), mcp_names

    return LitellmModel(model_name=model_name, set_cache_control=cache, model_kwargs=mk), set()


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


# ── MCP server lifecycle + tool discovery for a ToolArm ──────────────────────
def _spawn_mcp_for_arm(attach, *, cwd: Optional[str]):
    """Spawn the arm's MCP server, handshake, and discover its REAL tools.

    Returns ``(client, openai_specs, mcp_names)``. ``openai_specs`` are the live
    tools converted to OpenAI function-tool dicts (advertised to the model);
    ``mcp_names`` is the set of names the agent dispatcher routes to the MCP
    client (everything else falls through to the bash env).

    Falls back to the arm's documented static tool specs (``attach.tools``) ONLY
    if discovery fails AND the arm provided them — so the bench still has a tool
    surface to advertise rather than crashing. On any spawn/handshake failure the
    client is closed and the exception propagates (surfaces as an infra failure
    the worker retries once).
    """
    from bench.mcp_client import MCPClient, merged_env  # lazy (stdlib-only, but keep imports tidy)

    client = MCPClient()
    # The API key + server env reach the child via the ENVIRONMENT, never argv.
    # The arm exposes them on the ToolAttach; merge over a copy of os.environ.
    extra_env = dict(getattr(attach, "server_env", None) or {})
    env = merged_env(extra_env)
    try:
        client.spawn(list(attach.mcp_server_cmd), env=env, cwd=cwd)
        live_tools = client.list_tools()
    except Exception:
        client.close()
        raise
    if live_tools:
        specs = [_mcp_tool_to_openai_spec(t) for t in live_tools]
    else:
        # Discovery returned nothing: fall back to the arm's static mirrors if it
        # documented any (woz keeps none by default), else advertise nothing.
        specs = list(attach.tools or [])
    mcp_names = {s["function"]["name"] for s in specs}
    return client, specs, mcp_names


def _make_mcp_agent_class(DefaultAgent, mcp_client, mcp_names: set, exec_timeout_s: int,
                          tool_cwd: Optional[str] = None):
    """Build a DefaultAgent subclass that DISPATCHES tool calls by name.

    The only change vs the stock agent is ``execute_actions``: each parsed action
    carries ``tool_name``/``tool_args`` (see _ToolLitellmModel._parse_actions).
    An action whose name is an MCP tool is serviced by ``mcp_client.call_tool``
    and its text becomes the observation ``output``; every other action (bash,
    or any non-MCP tool) falls through to ``self.env.execute`` exactly as before.
    Observations are then formatted by the stock ``format_observation_messages``
    (keyed by ``tool_call_id``), so the transcript shape is byte-identical to a
    normal run — only the SOURCE of an observation differs.

    ``tool_cwd`` (general — NOT woz-specific): a DIRECT MCP client gets none of
    the auto-injection a real Claude Code harness does. Claude Code injects the
    working dir into every tool call (the tools' schemas say "do NOT set cwd" —
    but that instruction is for the HARNESS; a raw MCP client receives nothing).
    So when set, we inject ``cwd=<tool_cwd>`` into the MCP tool-call arguments so
    the server (e.g. Woz's Search) greps/reads the intended path. We do NOT
    overwrite a cwd the model itself supplied. This is the path the server
    searches against.

    *** SMOKE-TIME OPEN ITEM (host vs container) ***************************
    For the woz arm, the MCP server runs on the HOST runner but the swebench
    repo lives inside the per-task DOCKER CONTAINER. Injecting a host ``cwd``
    only helps if the task repo is reachable at that host path (copied/mounted),
    OR if Woz is run INSIDE the container instead. Until that is settled on the
    Linux box, Search will grep the host working dir, not the task repo. See the
    same warning in arms/woz.py. Do not assume host cwd injection alone works.
    ************************************************************************
    """

    # MCP errors come back as observation TEXT (mcp_client.call_tool prefixes an
    # isError result with "[tool error]" — e.g. Woz's auth.login_required, which
    # tells the agent to fall back to Bash). We surface that text to the agent as
    # the observation and NEVER crash the run on it.
    def _inject_cwd(args: dict) -> dict:
        if not tool_cwd:
            return args
        a = dict(args or {})
        a.setdefault("cwd", tool_cwd)  # don't clobber a model-supplied cwd
        return a

    class _McpAgent(DefaultAgent):  # type: ignore[valid-type, misc]
        def execute_actions(self, message: dict) -> list[dict]:
            actions = (message.get("extra", {}) or {}).get("actions", []) or []
            outputs: list[dict] = []
            for action in actions:
                name = action.get("tool_name")
                if name in mcp_names:
                    try:
                        text = mcp_client.call_tool(
                            name, _inject_cwd(action.get("tool_args", {})),
                            timeout_s=float(exec_timeout_s) if exec_timeout_s else 120.0,
                        )
                        # text may be an "[tool error] ..." string (graceful: a
                        # deauth'd / isError result is the OBSERVATION, not a
                        # crash). returncode 0 keeps the agent loop going so it
                        # can act on the message (e.g. fall back to Bash).
                        outputs.append({"output": text, "returncode": 0, "exception_info": ""})
                    except Exception as e:  # noqa: BLE001 — surface as an observation, never crash the loop
                        outputs.append({
                            "output": "",
                            "returncode": -1,
                            "exception_info": f"MCP tool '{name}' failed: {e}",
                            "extra": {"exception_type": type(e).__name__, "exception": str(e)[:300]},
                        })
                else:
                    # bash / non-MCP tool: the real env executes it (unchanged path).
                    outputs.append(self.env.execute(action))
            return self.add_messages(
                *self.model.format_observation_messages(
                    message, outputs, self.get_template_vars()))

    return _McpAgent


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
    traj_path: Optional[str] = None,
) -> dict:
    """Drive one (instance, arm) solve with the real minisweagent scaffold.

    Builds the model + DefaultAgent + swebench env (mirroring the reference eval),
    installs the arm at the model.query seam, runs the agent, and returns a dict
    of raw run signals. No grading here — the caller grades the returned patch.
    """
    import copy
    from minisweagent.agents.default import DefaultAgent  # lazy
    from minisweagent.run.benchmarks.swebench import get_sb_environment  # lazy

    seam = _SeamState()
    seam.wall_cap_s = float(wall_cap_s)

    # One-time arm prep (e.g. the Woz CLI login that authenticates its MCP
    # server). A setup failure (bad key / missing CLI / old node) must surface as
    # an infra failure for THIS arm — retried once, never counted — not crash the
    # worker silently. We raise RunInfraError so _worker handles it like any other
    # infra fault.
    try:
        arm.setup()
    except Exception as e:  # noqa: BLE001
        raise RunInfraError(f"arm.setup() failed for '{arm.name}': "
                            f"{type(e).__name__}: {str(e)[:200]}") from e

    # ── ToolArm: spawn the REAL MCP server, discover its REAL tools ───────────
    # The MCP server is launched here (run scope) so its lifecycle is owned by
    # run_agent and torn down in the finally below — even on a wall-cap/infra
    # abort. Tools come from the live tools/list, NOT a hardcoded mirror.
    mcp_client = None
    mcp_specs: list[dict] = []
    mcp_names: set = set()
    replace_bash = False
    # The path an MCP tool (e.g. Woz's Search) should grep/read. A DIRECT MCP
    # client must pass cwd in the tool-call args — Claude Code injects it
    # automatically; a raw client gets nothing. General (not woz-hardcoded):
    # WOZ_SEARCH_CWD overrides; default is the run's working/repo path (the
    # process cwd here). NOTE the host/container open item flagged in
    # _make_mcp_agent_class: a host cwd only reaches the task repo if that repo is
    # mounted/copied to the host path (or Woz runs inside the container).
    tool_cwd: Optional[str] = os.environ.get("WOZ_SEARCH_CWD") or os.getcwd()
    if arm.kind == ArmKind.TOOL:
        attach = arm.attach()  # type: ignore[attr-defined]
        if attach.mcp_server_cmd:
            # Spawn the server with the same cwd we inject into tool calls so the
            # server's own cwd hook and the per-call cwd agree on one path. (The
            # swebench env runs in a container; reaching the task repo from a
            # host-spawned server is the unresolved smoke-time item above.)
            mcp_client, mcp_specs, mcp_names = _spawn_mcp_for_arm(attach, cwd=tool_cwd)
        else:
            # No server command (e.g. a hosted MCP we don't spawn): there is
            # nothing for the runner to dispatch tool calls TO. Advertising tools
            # the agent can't reach would just produce dead tool calls, so we
            # advertise nothing extra and keep the native bash surface.
            mcp_specs = []
            mcp_names = set()
        # replace_tools (the woz contract): drop BASH so the model can only reach
        # the MCP tools — but ONLY when we have a live client AND discovered tools
        # to dispatch to (never strand the agent with no reachable tool).
        replace_bash = bool(attach.replace_tools) and bool(mcp_specs) and mcp_client is not None

    model_obj, _model_mcp_names = _make_model(
        model, arm, max_tokens=max_tokens,
        mcp_tool_specs=(mcp_specs or None), replace_bash=replace_bash,
    )
    _install_arm_seam(model_obj, arm, seam)

    instance = _fetch_instance(instance_id, dataset, split)
    config = copy.deepcopy(_load_swebench_config())
    agent_cfg = config.get("agent", {})
    # Map the bench caps onto the agent's native limits. step_limit == call cap
    # (one model call per step); cost_limit guards $; the wall cap is enforced at
    # the seam (the agent loop has no wall clock).
    agent_cfg["step_limit"] = call_cap
    agent_cfg.setdefault("cost_limit", 0.0)  # 0 == unlimited in minisweagent
    # Let mini-swe-agent write its OWN native trajectory to output_path — the same
    # contract the reference eval uses (DefaultAgent persists the
    # full {info, messages, trajectory_format} transcript itself). We do NOT build
    # a transcript dict by hand; this file IS the durable behavioral trace.
    if traj_path:
        agent_cfg["output_path"] = traj_path

    exit_status = "incomplete"
    patch = ""
    submitted = False
    calls = 0
    steps = 0
    tool_call_count = 0
    time_to_submit_s = 0.0
    t0 = seam.t0

    env = get_sb_environment(config, instance)
    agent = None
    # When an MCP server is live, drive the dispatch-aware agent so MCP tool
    # calls route to the MCP client and bash/non-MCP tools route to the env.
    AgentClass = DefaultAgent
    if mcp_client is not None and mcp_names:
        AgentClass = _make_mcp_agent_class(
            DefaultAgent, mcp_client, mcp_names, exec_timeout_s, tool_cwd=tool_cwd)
    try:
        agent = AgentClass(model_obj, env, **agent_cfg)
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
        # mini-swe-agent has already flushed whatever it accumulated to output_path.
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
        # tear down the MCP server first (it may hold the repo / a child node
        # proc); robust to a hung/dead server — close() never blocks the run.
        if mcp_client is not None:
            try:
                mcp_client.close()
            except Exception:
                pass
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
        # model-service-reported spend (mini-swe DefaultAgent.cost).
        # This is the AUTHORITATIVE headline $, matching how the model reports spend
        # (actual reported usage), not our price-table approximation.
        "reported_cost_usd": float(getattr(agent, "cost", 0.0) or 0.0),
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
     max_tokens, exec_timeout_s, grade_timeout_s, out_dir, run_id) = job
    t0 = time.time()
    # Trace tag `{iid}_{run_id}_{arm}`
    # so paired arms/runs NEVER collide. The native mini-swe `.traj.json` and the
    # outcome sidecar both key off this tag, in the durable traj dir that gets rsync'd.
    traj_dir = Path(out_dir) / "traj"
    traj_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{instance_id}_{run_id}_{arm_name}"
    traj_path = str(traj_dir / f"{tag}.traj.json")
    try:
        arm = get_arm(arm_name)
        raw = run_agent(
            arm, instance_id, model=model, dataset=dataset, split=split,
            call_cap=call_cap, wall_cap_s=wall_cap_s,
            max_tokens=max_tokens, exec_timeout_s=exec_timeout_s,
            traj_path=traj_path,
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
            # headline $ = model-reported spend when present, else price-table.
            cost_usd=round((float(raw.get("reported_cost_usd", 0.0) or 0.0)) or cb.total_usd, 6),
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
            reported_cost_usd=round(float(raw.get("reported_cost_usd", 0.0) or 0.0), 6),
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
        run_record = rec.to_json()

        # Per-trace OUTCOME sidecar next to the native mini-swe `.traj.json`,
        # a compact outcome schema next to the transcript: attaches a
        # reward without re-grading. The transcript itself is mini-swe's native
        # output_path file (written by DefaultAgent) — NOT a reinvented dict.
        # Best-effort: a write failure must never crash a paid run.
        try:
            (traj_dir / f"{tag}.outcome.json").write_text(json.dumps(dict(
                success=bool(g.success), ftp=float(g.ftp),
                in_tok=raw["input_tokens"], out_tok=raw["output_tokens"],
                steps=raw["calls"], exit=raw["exit_status"])), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — never crash a paid run on the sidecar write
            print(f"  WARN: outcome sidecar write failed for {instance_id} [{arm_name}]: "
                  f"{type(e).__name__}: {str(e)[:160]}", flush=True)

        return run_record
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


# ── durable GCS trace store ──────────────────────────────────────────────────
def _resolve_run_id(tasks: str, arms: list[str], run_id: Optional[str]) -> str:
    """A STABLE run id keying the durable trace store. Deterministic so a resumed
    run targets the SAME GCS prefix (never Date.now()-style nondeterminism).

    If --run-id is given, use it verbatim. Otherwise derive a stable slug from the
    tasks file basename + the sorted arm set + an 8-char hash of (tasks, arms).
    """
    if run_id:
        return run_id.strip()
    stem = Path(tasks).stem
    arms_sorted = sorted(a.lower() for a in arms)
    key = f"{stem}|{','.join(arms_sorted)}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    arms_slug = "-".join(arms_sorted)[:48]
    return f"{stem}__{arms_slug}__{h}"


_GSUTIL: Optional[str] = None


def _gsutil_path() -> Optional[str]:
    """Locate the gsutil binary once; None if it isn't installed (sync is skipped)."""
    global _GSUTIL
    if _GSUTIL is None:
        _GSUTIL = shutil.which("gsutil") or ""
    return _GSUTIL or None


def _rsync_traj(traj_dir: Path, bus: str) -> None:
    """Durable sync of the whole traj dir to ``{bus}/traj`` — a durable-sync
    mechanism (``gsutil -q -m rsync -r <traj_dir> <bus>/traj``), run
    PERIODICALLY (per window/batch) rather than once per trace.

    Best-effort + idempotent: warn and return on ANY failure — a sync failure must
    NEVER break the eval. Skipped silently when bus is empty or
    gsutil isn't installed."""
    if not bus:
        return
    gsutil = _gsutil_path()
    if not gsutil:
        return
    dest = bus.rstrip("/") + "/traj"
    try:
        out = subprocess.run(
            [gsutil, "-q", "-m", "rsync", "-r", str(traj_dir), dest],
            capture_output=True, text=True, timeout=900,
        )
        if out.returncode != 0:
            print(f"  WARN: gsutil rsync {traj_dir} -> {dest} failed: "
                  f"{(out.stderr or '').strip()[:160]}", flush=True)
    except Exception as e:  # noqa: BLE001 — gsutil missing/hung/etc. (never crash the eval)
        print(f"  WARN: gsutil rsync raised: {type(e).__name__}: {str(e)[:160]}", flush=True)


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
    ap.add_argument("--tasks", default="tasks.json", help="task-set JSON path")
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
    ap.add_argument("--bus", default="",
                    help="GCS bus prefix; traj dir is rsync'd to <bus>/traj (same "
                         "durable-sync mechanism, separate codebench/ namespace). Empty "
                         "string disables sync.")
    ap.add_argument("--sync-every", type=int, default=20,
                    help="rsync the traj dir to <bus>/traj after every N completed "
                         "runs (and once at the end); periodic, not per-trace")
    ap.add_argument("--run-id", default="",
                    help="stable run id keying the trace tag (default: a deterministic "
                         "slug derived from the tasks file + arm set)")
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
    traj_dir = out_dir / "traj"
    traj_dir.mkdir(exist_ok=True)
    ledger = out_dir / "ledger.jsonl"

    done = _load_done(ledger)
    print(f"resume: {len(done)} completed (instance, arm) pairs in {ledger}")

    # durable trace bus: a STABLE run id keys each trace's tag so a resumed run
    # re-emits the SAME filenames. The native mini-swe `.traj.json` + outcome
    # sidecar in traj_dir are rsync'd to <bus>/traj (separate codebench/
    # namespace). Empty --bus disables sync entirely.
    bus = (a.bus or "").strip()
    run_id = _resolve_run_id(a.tasks, ready_arms, a.run_id)
    if bus:
        if _gsutil_path():
            print(f"trace bus: {bus}/traj  (run_id={run_id})")
        else:
            print(f"trace bus: {bus}/traj  (run_id={run_id}) "
                  f"-- gsutil NOT found; sync will be skipped")
    else:
        print("trace bus: disabled (empty --bus)")

    jobs = [
        (iid, arm, a.model, a.dataset, a.split, a.call_cap, a.wall_cap_s,
         a.max_tokens, a.exec_timeout_s, a.grade_timeout_s, str(out_dir), run_id)
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
    completed_since_sync = 0
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
                # durable sync: rsync the whole traj dir to <bus>/traj PERIODICALLY
                # (every N completed runs) — NOT per trace.
                # Best-effort + idempotent; a failure here never crashes the run.
                completed_since_sync += 1
                if a.sync_every > 0 and completed_since_sync >= a.sync_every:
                    _rsync_traj(traj_dir, bus)
                    completed_since_sync = 0
    # final: one last rsync so the tail of the run lands on the bus.
    _rsync_traj(traj_dir, bus)
    log("BENCH_RUN_DONE")


def SWEBenchGraderDefault(field: str) -> str:
    """Defer to the grader module's env-driven defaults for dataset/split."""
    from bench import grader as _g
    return _g.DEFAULT_DATASET if field == "dataset" else _g.DEFAULT_SPLIT


if __name__ == "__main__":
    main()
