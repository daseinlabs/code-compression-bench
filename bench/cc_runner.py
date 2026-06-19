"""The benchmark runner: headless Claude Code (Python Claude Agent SDK) as the
fixed agent for ALL arms.

This SUPERSEDES the mini-swe-agent driver in ``bench.runner``. The fixed agent is
now real, headless Claude Code, driven through the Python Claude Agent SDK
(``claude_agent_sdk.query`` + ``ClaudeAgentOptions``). Every arm runs the SAME
Claude Code scaffold against the SAME model; the ONLY thing that varies is the
compression layer at the model-call seam — and we install that seam OUTSIDE the
SDK, by pointing Claude Code's ``ANTHROPIC_BASE_URL`` at a per-run usage gateway
whose UPSTREAM is the arm's endpoint:

    ProxyArm  (dasein/edgee/rtk/compresr/headroom)
        gateway upstream = arm.model_base_url(); arm.headers() are merged onto
        every forwarded request. The arm compresses server-side; Claude Code is
        unchanged. This is the product seam — A3S/Dasein under Claude Code.

    BaselineArm (A0)
        gateway upstream = the real model endpoint (direct). The control.

    ToolArm   (woz)
        gateway upstream = the real model (same MODEL as A0), AND the arm's MCP
        server is attached to the SDK via ClaudeAgentOptions.mcp_servers (command
        + args + env from arm.attach()); its tools are allowed
        (mcp__<server>__* ) and, per replace_tools, the native edit/grep tools
        are disallowed so the agent reaches the model through Woz's tools. The
        Woz explorer subagent is enabled via ClaudeAgentOptions.agents.

The usage gateway (``bench.usage_gateway``) records the PER-REQUEST cache split
(cache_creation / cache_read) that the SDK's own ``ResultMessage`` omits, into a
per-run JSONL keyed by a run-id header we set via ANTHROPIC_CUSTOM_HEADERS. The
SDK's ``ResultMessage.total_cost_usd`` is the AUTHORITATIVE headline cost
(``reported_cost_usd``); the gateway rows drive the cache-aware token KPIs and
the price frames.

Clean-room rule: this public repo must NOT import ``adaptive_context``. Heavy
imports (``claude_agent_sdk``, and anything it pulls) are LAZY so ``--list-arms``
and ``import bench.cc_runner`` work on a box where the SDK / Claude Code is not
installed.

Scale-out, resume ledger, caps, and the durable GCS trace store mirror
``bench.runner`` exactly — only the per-(instance, arm) execution body changed.
Each finished solve is graded by the official SWE-bench Docker harness
(:mod:`bench.grader`), priced cache-aware (:mod:`bench.pricing`), and written as a
:class:`bench.schema.RunRecord` + a native SDK trajectory dump + the ab_curator
``.outcome.json`` sidecar, then rsync'd to ``<bus>/traj``.
"""

from __future__ import annotations

import argparse
import asyncio
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
from bench.arm import ArmKind, get_arm, available_arms
from bench.grader import SWEBenchGrader
from bench.pricing import price_run, rates_for
from bench.schema import RunRecord
# usage_gateway is pure stdlib (no claude_agent_sdk / litellm) — safe to import
# eagerly; we need RUN_ID_HEADER to tag Claude Code's forwarded usage.
from bench.usage_gateway import RUN_ID_HEADER, UsageGateway


# ── defaults / caps (mirror bench.runner so ported A0/A3S rows share budgets) ─
DEFAULT_WORKERS = 8
CALL_CAP = 100                # max agent turns per (task, arm) — ClaudeAgentOptions.max_turns.
                              # Matches the v5/mini_swe step_limit=100 so ported A0/A3S rows
                              # and live vendor arms share an identical turn budget.
WALL_CAP_S = 50 * 60          # hard wall-clock watchdog per solve (matches gate2 alarm(50*60))
# Default model: a Claude id (the SDK speaks to a Claude-shaped endpoint behind the
# gateway). Overridable via --model / MODEL. The gateway's upstream decides whether
# that resolves to direct Anthropic, Vertex, or a vendor proxy.
DEFAULT_MODEL = os.environ.get("MODEL", "claude-sonnet-4-5")

# The real model endpoint the BaselineArm (A0) and ToolArm (woz, for the MODEL)
# forward to. Overridable via env so A0 can target Anthropic direct OR a Vertex/
# gateway URL without code changes.
REAL_MODEL_BASE_URL = os.environ.get(
    "ANTHROPIC_UPSTREAM_BASE_URL",
    os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
)

# Native Claude Code tools the agent may use by default (the fixed scaffold's
# surface). A ToolArm with replace_tools removes the file-mutation/search tools
# so the agent must reach the repo through the arm's MCP tools instead.
DEFAULT_ALLOWED_TOOLS = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep", "MultiEdit", "TodoWrite",
]
# Tools a replace_tools ToolArm strips from the native surface (Woz replaces the
# grep/edit surface with Search/Edit). Bash/Read stay so the agent can still run
# the test suite and read files the MCP tool points it at.
_REPLACE_TOOLS_DROP = ["Edit", "Write", "MultiEdit", "Glob", "Grep"]


# ── task set loading (identical to bench.runner) ─────────────────────────────
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


# ── instance fetch (identical contract to bench.runner) ──────────────────────
def _fetch_instance(instance_id: str, dataset: str, split: str) -> dict:
    """Resolve the SWE-bench instance dict (problem_statement + repo info).

    Uses the HuggingFace ``datasets`` loader the swebench harness ships with;
    falls back to a minimal dict carrying just the instance_id when the dataset
    is unavailable.
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


# ── arm -> SDK config mapping ─────────────────────────────────────────────────
class _ArmConfig:
    """The resolved per-arm SDK wiring for one (instance, arm) solve.

    Computed once per solve from the arm's kind + hooks:
      upstream_base   : the gateway's UPSTREAM base URL.
                          ProxyArm   -> arm.model_base_url()
                          Baseline   -> REAL_MODEL_BASE_URL
                          ToolArm    -> REAL_MODEL_BASE_URL (model is direct; the
                                        arm contributes TOOLS, not a proxy)
      upstream_headers: headers merged onto every forwarded request.
                          ProxyArm   -> arm.headers() (auth for a hosted proxy)
                          others     -> {}
      mcp_servers     : ClaudeAgentOptions.mcp_servers dict (ToolArm only).
      allowed_tools   : the agent's tool surface.
      disallowed_tools: tools removed (ToolArm replace_tools).
      agents          : subagent definitions (ToolArm explorer subagent).
      server_env      : extra env for a spawned MCP server (kept off argv).
      setup_arm       : the Arm instance (its setup()/teardown() are run by the
                        worker; e.g. the Woz one-time CLI login).
    """

    def __init__(self) -> None:
        self.upstream_base: str = REAL_MODEL_BASE_URL
        self.upstream_headers: dict[str, str] = {}
        self.mcp_servers: dict = {}
        self.allowed_tools: list[str] = list(DEFAULT_ALLOWED_TOOLS)
        self.disallowed_tools: list[str] = []
        self.agents: dict = {}
        self.setup_arm = None


def _woz_mcp_server_config(attach) -> Optional[dict]:
    """Translate a ToolArm's ToolAttach into an SDK stdio mcp_server config.

    The SDK wants ``{"command": str, "args": [...], "env": {...}}`` for a stdio
    server. ``attach.mcp_server_cmd`` is the argv (``[cmd, *args]``); the API key
    + server config travel via ``server_env`` (NEVER argv), merged here onto the
    env the SDK passes to the spawned child. Returns None if the arm has no
    server command (a hosted MCP we don't spawn).
    """
    cmd = attach.mcp_server_cmd
    if not cmd:
        return None
    return {
        "command": cmd[0],
        "args": list(cmd[1:]),
        "env": dict(attach.server_env or {}),
    }


def build_arm_config(arm) -> _ArmConfig:
    """Map an arm onto its Claude Code SDK wiring (the arm->SDK contract)."""
    cfg = _ArmConfig()
    cfg.setup_arm = arm

    if arm.kind == ArmKind.PROXY:
        # The compression endpoint IS the gateway upstream; its headers (auth)
        # are merged onto every forwarded request. Claude Code is unchanged.
        base = arm.model_base_url()  # type: ignore[attr-defined]
        if base:
            cfg.upstream_base = base
        cfg.upstream_headers = dict(arm.headers() or {})  # type: ignore[attr-defined]

    elif arm.kind == ArmKind.TOOL:
        # The model goes direct (same as A0); the arm contributes TOOLS via MCP.
        cfg.upstream_base = REAL_MODEL_BASE_URL
        attach = arm.attach()  # type: ignore[attr-defined]
        server_cfg = _woz_mcp_server_config(attach)
        server_name = arm.name  # mcp server key == arm name (e.g. "woz")
        if server_cfg is not None:
            cfg.mcp_servers = {server_name: server_cfg}
            # Allow this server's tools (wildcard) so the agent can call them.
            cfg.allowed_tools = list(DEFAULT_ALLOWED_TOOLS) + [f"mcp__{server_name}__*"]
            # replace_tools (the woz contract): drop the native file/grep surface
            # so the agent reaches the repo through the arm's MCP tools — but keep
            # Bash/Read so it can still run tests and open files. Never strand it
            # with no reachable tool.
            if getattr(attach, "replace_tools", False):
                cfg.disallowed_tools = list(_REPLACE_TOOLS_DROP)
            # Enable the explorer subagent (the SDK supports subagents). A
            # read-only explorer that fans out repo discovery, on the same model.
            cfg.agents = _explorer_agent_def(server_name)
        # No server command (hosted MCP we don't spawn): advertise nothing extra,
        # keep the native surface — the runner has nothing to dispatch tool calls
        # TO, so dead tool calls would just waste turns.

    # BaselineArm / TransformArm: defaults (direct upstream, native tools). A
    # TransformArm has no server-side seam under Claude Code (the SDK owns the
    # message array), so it runs as the control here; client-side transforms are
    # out of scope for the Claude Code product harness.
    return cfg


def _explorer_agent_def(server_name: str) -> dict:
    """An 'explorer' subagent the main agent can delegate repo discovery to.

    Returned as the value for ClaudeAgentOptions.agents: a dict name->
    AgentDefinition. Built lazily (the SDK class is imported only here) so this
    module imports on a box without the SDK. Read-only tools + the arm's MCP
    search tool, on the inherited model; description steers the main agent to
    delegate broad searches to it (keeping the main transcript small).
    """
    from claude_agent_sdk import AgentDefinition  # lazy

    return {
        "explorer": AgentDefinition(
            description=(
                "Use to explore the repository and locate the code relevant to a "
                "task: find files, definitions, call sites, and tests. Delegate "
                "broad or multi-file searches here so the main agent's context "
                "stays focused on the fix."
            ),
            prompt=(
                "You are a read-only repository explorer. Search the codebase and "
                "report back the files, symbols, and line ranges relevant to the "
                "task. Do not edit files. Be concise: return paths and the minimal "
                "snippets needed, not whole files."
            ),
            tools=["Read", "Glob", "Grep", "Bash", f"mcp__{server_name}__*"],
            model="inherit",
        ),
    }


# ── the prompt the agent is given ─────────────────────────────────────────────
def _build_task_prompt(instance: dict, instance_id: str, repo_dir: str) -> str:
    """The single user prompt that kicks off the headless solve.

    Mirrors the swebench task framing: the problem statement + the instruction to
    fix the bug in the repo at ``cwd`` and leave the working tree edited (we
    capture the patch via ``git diff`` afterward — the agent does not need to
    produce a diff itself).
    """
    problem = (instance.get("problem_statement") or "").strip()
    if not problem:
        problem = f"Resolve the failing tests for instance {instance_id}."
    return (
        f"You are working in the repository checked out at {repo_dir}.\n\n"
        f"Resolve the following issue by editing the code in this repository. "
        f"Make the failing tests pass without breaking existing tests. Do not "
        f"write a patch file — edit the source directly; the harness will capture "
        f"your changes from the working tree.\n\n"
        f"--- ISSUE ---\n{problem}\n"
    )


# ── SDK run (async) — drive Claude Code to completion, collect the result ─────
async def _run_sdk(
    *,
    prompt: str,
    cwd: str,
    model: str,
    arm_cfg: _ArmConfig,
    gateway_base_url: str,
    run_id: str,
    call_cap: int,
    env_overrides: dict,
) -> dict:
    """Run one headless Claude Code solve via the SDK and collect raw signals.

    Returns a dict of raw signals (reported cost, sdk usage, num_turns, message
    list for the trajectory dump, exit/limit signals). No grading or token
    pricing here — the caller does that off the gateway usage rows.
    """
    from claude_agent_sdk import ClaudeAgentOptions, query  # lazy

    # Env the SDK subprocess inherits: point Claude Code at the gateway, set the
    # run-id header (Claude Code forwards ANTHROPIC_CUSTOM_HEADERS onto every
    # model request, so the gateway can tag usage), and the model. Auth is left
    # to whatever ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN the process already
    # has (the gateway forwards it untouched to the real upstream).
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = gateway_base_url
    # Claude Code forwards ANTHROPIC_CUSTOM_HEADERS onto every model request, so the
    # gateway reads RUN_ID_HEADER off it to tag usage into the right per-run JSONL.
    env["ANTHROPIC_CUSTOM_HEADERS"] = f"{RUN_ID_HEADER}: {run_id}"
    env.setdefault("ANTHROPIC_MODEL", model)
    env.update(env_overrides or {})

    options = ClaudeAgentOptions(
        allowed_tools=arm_cfg.allowed_tools,
        disallowed_tools=arm_cfg.disallowed_tools,
        max_turns=call_cap,
        cwd=cwd,
        model=model,
        mcp_servers=arm_cfg.mcp_servers,
        agents=arm_cfg.agents or None,
        env=env,
        # headless: never block on a permission prompt — auto-accept tool use so
        # the agent runs unattended (this is a sandboxed per-task container).
        permission_mode="bypassPermissions",
        # only our explicitly passed MCP servers; ignore any .mcp.json on disk.
        strict_mcp_config=True,
    )

    messages: list[dict] = []
    result_msg = None
    async for msg in query(prompt=prompt, options=options):
        messages.append(_message_to_jsonable(msg))
        if type(msg).__name__ == "ResultMessage":
            result_msg = msg

    return _collect_result(result_msg, messages)


def _message_to_jsonable(msg) -> dict:
    """Best-effort JSON-able dump of one SDK message (for the trajectory file).

    The SDK messages are dataclasses; we capture type + a shallow dict of their
    public fields, stringifying content blocks. Never raises — a message we can't
    introspect is dumped as its repr so the trajectory is always written.
    """
    out: dict = {"type": type(msg).__name__}
    try:
        for k, v in vars(msg).items():
            if k.startswith("_"):
                continue
            out[k] = _jsonable(v)
    except TypeError:
        # dataclass without __dict__ / slots: fall back to repr
        out["repr"] = repr(msg)[:2000]
    return out


def _jsonable(v):
    """Coerce a value (incl. content blocks) into something json.dumps handles."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    # content blocks (TextBlock/ToolUseBlock/...) are dataclasses
    if hasattr(v, "__dict__"):
        d = {"_type": type(v).__name__}
        for k, x in vars(v).items():
            if not k.startswith("_"):
                d[k] = _jsonable(x)
        return d
    return str(v)


def _collect_result(result_msg, messages: list[dict]) -> dict:
    """Pull raw run signals off the ResultMessage + message stream."""
    # usage dict shape: input_tokens / output_tokens / cache_creation_input_tokens
    # / cache_read_input_tokens (Anthropic). Carried for completeness; the cache
    # split KPIs come from the gateway rows (the per-CALL series).
    usage = {}
    reported_cost = 0.0
    num_turns = 0
    is_error = False
    subtype = ""
    session_id = ""
    if result_msg is not None:
        usage = getattr(result_msg, "usage", None) or {}
        reported_cost = float(getattr(result_msg, "total_cost_usd", 0.0) or 0.0)
        num_turns = int(getattr(result_msg, "num_turns", 0) or 0)
        is_error = bool(getattr(result_msg, "is_error", False))
        subtype = str(getattr(result_msg, "subtype", "") or "")
        session_id = str(getattr(result_msg, "session_id", "") or "")

    # count assistant turns + tool-use blocks for the effort KPIs off the stream.
    asst = [m for m in messages if m.get("type") == "AssistantMessage"]
    tool_calls = 0
    for m in asst:
        for blk in (m.get("content") or []):
            if isinstance(blk, dict) and blk.get("_type") == "ToolUseBlock":
                tool_calls += 1
    steps = num_turns or len(asst)

    return {
        "reported_cost_usd": reported_cost,
        "sdk_usage": usage,
        "num_turns": num_turns,
        "steps": steps,
        "tool_calls": tool_calls,
        "is_error": is_error,
        "subtype": subtype,
        "session_id": session_id,
        "messages": messages,
    }


class RunInfraError(Exception):
    """Raised on an infrastructure failure (SDK/model/network) — retried once."""


# ── patch capture (git diff of the working tree the agent edited) ─────────────
def _capture_patch(repo_dir: str) -> str:
    """The unified diff the agent produced: ``git add -A && git diff --cached``.

    Empty string when there is no repo / no change (a productive-death run). Never
    raises — a git failure yields "" so the run is still graded (as no-patch).
    """
    try:
        subprocess.run(["git", "-C", repo_dir, "add", "-A"],
                       capture_output=True, timeout=120)
        out = subprocess.run(["git", "-C", repo_dir, "diff", "--cached"],
                             capture_output=True, text=True, timeout=120)
        return out.stdout or ""
    except Exception:
        return ""


# ── the per-(instance, arm) solve ─────────────────────────────────────────────
def run_agent(
    arm,
    instance_id: str,
    *,
    model: str,
    dataset: str,
    split: str,
    out_dir: str,
    run_id: str,
    call_cap: int = CALL_CAP,
    wall_cap_s: int = WALL_CAP_S,
    repo_root: Optional[str] = None,
    traj_path: Optional[str] = None,
) -> dict:
    """Drive one headless Claude Code solve for (instance, arm) and return raw signals.

    Builds the arm->SDK config, starts a per-run usage gateway pointed at the arm's
    upstream, runs the SDK to completion against the task repo (cwd), captures the
    patch via git, and returns a dict of raw run signals + the gateway usage path.
    No grading here — the caller grades the returned patch.
    """
    # One-time arm prep (e.g. the Woz CLI login that authenticates its MCP
    # server). A setup failure surfaces as an infra failure for THIS arm.
    try:
        arm.setup()
    except Exception as e:  # noqa: BLE001
        raise RunInfraError(f"arm.setup() failed for '{arm.name}': "
                            f"{type(e).__name__}: {str(e)[:200]}") from e

    arm_cfg = build_arm_config(arm)

    # Resolve the task repo working dir. By default the SWE-bench grader runs the
    # repo inside a Docker image; for the headless agent to EDIT it, the repo must
    # be checked out on a path the SDK's cwd can reach. AC_REPO_ROOT/<iid> (or a
    # per-instance checkout) is the host path; this is a smoke-time wiring item on
    # the Linux box (see the report). Default to a per-instance dir under out_dir.
    repo_dir = _resolve_repo_dir(instance_id, repo_root, out_dir)

    instance = _fetch_instance(instance_id, dataset, split)
    prompt = _build_task_prompt(instance, instance_id, repo_dir)

    usage_dir = str(Path(out_dir) / "usage")
    gw = UsageGateway(
        upstream_base=arm_cfg.upstream_base,
        log_dir=usage_dir,
        default_run_id=run_id,
        default_headers=arm_cfg.upstream_headers,
        timeout_s=float(wall_cap_s),
    ).start()

    t0 = time.time()
    exit_status = "incomplete"
    raw: dict = {}
    try:
        raw = asyncio.run(
            _run_with_wall_cap(
                prompt=prompt, cwd=repo_dir, model=model, arm_cfg=arm_cfg,
                gateway_base_url=gw.base_url, run_id=run_id, call_cap=call_cap,
                wall_cap_s=wall_cap_s,
                env_overrides={},
            )
        )
        exit_status = raw.get("subtype") or ("error" if raw.get("is_error") else "success")
    except _WallCapExceeded:
        exit_status = "wall_cap"
        raw = raw or {}
    except Exception as e:  # noqa: BLE001 — surface infra faults to the worker (retried once)
        try:
            arm.teardown()
        except Exception:
            pass
        gw.stop()
        raise RunInfraError(f"{type(e).__name__}: {str(e)[:300]}") from e
    finally:
        gw.stop()
        try:
            arm.teardown()
        except Exception:
            pass

    wall_s = round(time.time() - t0, 1)
    patch = _capture_patch(repo_dir)
    submitted = bool(patch.strip())

    # the authoritative per-CALL usage series: the gateway JSONL rows (cache split).
    usage = _read_usage_rows(gw.usage_path(run_id))

    # write the native SDK trajectory dump (best-effort; never crash a paid run).
    if traj_path:
        try:
            Path(traj_path).write_text(json.dumps({
                "instance": instance_id, "arm": arm.name, "model": model,
                "run_id": run_id, "exit_status": exit_status,
                "num_turns": raw.get("num_turns", 0),
                "messages": raw.get("messages", []),
            }), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"  WARN: trajectory write failed for {instance_id} [{arm.name}]: "
                  f"{type(e).__name__}: {str(e)[:160]}", flush=True)

    # token rollups from the gateway series.
    in_tok = sum(u.get("prompt_tokens", 0) for u in usage)
    out_tok = sum(u.get("completion_tokens", 0) for u in usage)
    max_prompt = max((u.get("prompt_tokens", 0) for u in usage), default=0)
    lats = [u["latency_s"] for u in usage if u.get("latency_s") is not None]
    mean_lat = round(sum(lats) / len(lats), 3) if lats else 0.0
    calls = raw.get("num_turns", 0) or len(usage)
    steps = raw.get("steps", 0) or calls

    # limit-death: hit a cap WITHOUT producing a patch (productive death). The SDK
    # reports "error_max_turns"/"error_max_budget_usd" subtypes; our wall cap too.
    el = (exit_status or "").lower()
    hit_cap = exit_status == "wall_cap" or "max_turns" in el or "max_budget" in el or calls >= call_cap
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
        "wall_s": wall_s,
        "submitted": submitted,
        "limit_death": limit_death,
        "steps": steps,
        "tool_calls": raw.get("tool_calls", 0),
        "time_to_submit_s": wall_s if submitted else 0.0,
        "mean_call_latency_s": mean_lat,
        "max_prompt_tokens": max_prompt,
        "retries": 0,
        "degraded": False,
        # SDK-reported spend == the authoritative headline $ (mirrors v6 cost_usd_real).
        "reported_cost_usd": float(raw.get("reported_cost_usd", 0.0) or 0.0),
    }


def _resolve_repo_dir(instance_id: str, repo_root: Optional[str], out_dir: str) -> str:
    """The host path the agent's cwd points at for this instance's repo.

    Precedence: explicit --repo-root/<iid> if given; else AC_REPO_ROOT/<iid>; else
    a per-instance dir under out_dir/repos/<iid> (created so the SDK has a valid
    cwd even when no repo is mounted — the patch will then be empty, surfacing the
    mount gap at smoke rather than crashing). Real repo provisioning (checkout or
    container mount) is a smoke-time item on the Linux box.
    """
    root = repo_root or os.environ.get("AC_REPO_ROOT")
    if root:
        return str(Path(root) / instance_id)
    d = Path(out_dir) / "repos" / instance_id
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _read_usage_rows(path: Path) -> list[dict]:
    """Read the gateway's per-run usage JSONL into a CallUsage list (call order)."""
    rows: list[dict] = []
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return rows


# ── wall-clock cap around the async SDK run ──────────────────────────────────
class _WallCapExceeded(Exception):
    """The solve blew its wall-clock budget — aborted (productive-death)."""


async def _run_with_wall_cap(*, wall_cap_s: int, **kw) -> dict:
    """Run the SDK solve under a wall-clock deadline (asyncio.wait_for).

    The agent loop has no wall clock of its own; we bound the whole run. On
    timeout we raise _WallCapExceeded so the caller records a wall_cap exit
    (whatever the SDK flushed to the trajectory is lost, like gate2's alarm)."""
    try:
        return await asyncio.wait_for(_run_sdk(**kw), timeout=float(wall_cap_s))
    except asyncio.TimeoutError as e:
        raise _WallCapExceeded(f"wall cap {wall_cap_s}s exceeded") from e


# ── worker: run + grade + price one (instance, arm) ──────────────────────────
def _worker(job: tuple) -> dict:
    """Process-pool task: solve, grade, price one (instance, arm). Never raises.

    Returns a ``RunRecord.to_json()`` dict. On infra failure, returns a stub with
    ``infra_failed=True`` (excluded from metrics, retried once by the driver).
    """
    (instance_id, arm_name, model, dataset, split, call_cap, wall_cap_s,
     grade_timeout_s, out_dir, run_id, repo_root) = job
    t0 = time.time()
    # Trace tag mirrors bench.runner: `{iid}_{run_id}_{arm}` so paired arms/runs
    # never collide. The SDK `.traj.json` + outcome sidecar key off this tag, in
    # the durable traj dir that gets rsync'd.
    traj_dir = Path(out_dir) / "traj"
    traj_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{instance_id}_{run_id}_{arm_name}"
    traj_path = str(traj_dir / f"{tag}.traj.json")
    # the gateway tags usage by a per-(instance,arm) run id so rows never mix.
    gw_run_id = tag
    try:
        arm = get_arm(arm_name)
        raw = run_agent(
            arm, instance_id, model=model, dataset=dataset, split=split,
            out_dir=out_dir, run_id=gw_run_id, call_cap=call_cap,
            wall_cap_s=wall_cap_s, repo_root=repo_root, traj_path=traj_path,
        )
        grader = SWEBenchGrader(dataset=dataset, split=split, timeout_s=grade_timeout_s)
        g = grader.grade(instance_id, raw["patch"])

        rates = rates_for(model)
        # Price from the REAL per-call cache fields the gateway captured; falls
        # back to the inferred-from-prompt-growth frame if a run had no cache rows.
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
            # headline $ = SDK-reported spend (v6 parity) when present, else price-table.
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

        # Per-trace OUTCOME sidecar next to the SDK `.traj.json`, EXACT ab_curator
        # schema (scripts/ab_curator.py): the trainer attaches a reward without
        # re-grading. Best-effort: a write failure must never crash a paid run.
        try:
            (traj_dir / f"{tag}.outcome.json").write_text(json.dumps(dict(
                success=bool(g.success), ftp=float(g.ftp),
                in_tok=raw["input_tokens"], out_tok=raw["output_tokens"],
                steps=raw["calls"], exit=raw["exit_status"])), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
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


# ── resume ledger (identical to bench.runner) ─────────────────────────────────
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


# ── durable GCS trace store (identical mechanism to bench.runner) ─────────────
def _resolve_run_id(tasks: str, arms: list[str], run_id: Optional[str]) -> str:
    """A STABLE run id keying the durable trace store (deterministic for resume)."""
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
    """Locate the gsutil binary once; None if it isn't installed (sync skipped)."""
    global _GSUTIL
    if _GSUTIL is None:
        _GSUTIL = shutil.which("gsutil") or ""
    return _GSUTIL or None


def _rsync_traj(traj_dir: Path, bus: str) -> None:
    """Durable sync of the traj dir to ``{bus}/traj`` (same mechanism as gate2)."""
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
    except Exception as e:  # noqa: BLE001
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

    ap = argparse.ArgumentParser(description="code-compression-bench Claude Code runner")
    ap.add_argument("--tasks", default="tasks_bloated50.json", help="task-set JSON path")
    ap.add_argument("--arms", default="baseline",
                    help="comma-separated arm names (default: baseline)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the number of instances (0 = all); smoke uses 1")
    ap.add_argument("--out", default="runs", help="output dir for the ledger + per-run JSON")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dataset", default=_GraderDefault("dataset"))
    ap.add_argument("--split", default=_GraderDefault("split"))
    ap.add_argument("--call-cap", type=int, default=CALL_CAP)
    ap.add_argument("--wall-cap-s", type=int, default=WALL_CAP_S)
    ap.add_argument("--grade-timeout-s", type=int, default=1800)
    ap.add_argument("--repo-root", default="",
                    help="host path whose <iid> subdirs hold each task repo the agent "
                         "edits (else AC_REPO_ROOT, else a stub dir under --out)")
    ap.add_argument("--bus", default="gs://dasein-473321-ac-learning/codebench",
                    help="GCS bus prefix; traj dir is rsync'd to <bus>/traj. Empty disables.")
    ap.add_argument("--sync-every", type=int, default=20,
                    help="rsync the traj dir to <bus>/traj after every N completed runs")
    ap.add_argument("--run-id", default="",
                    help="stable run id keying the trace tag (default: a deterministic slug)")
    ap.add_argument("--list-arms", action="store_true", help="list arms + readiness and exit")
    a = ap.parse_args()

    if a.list_arms:
        list_arms()
        return

    arm_names = [x.strip() for x in a.arms.split(",") if x.strip()]
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

    bus = (a.bus or "").strip()
    run_id = _resolve_run_id(a.tasks, ready_arms, a.run_id)
    if bus:
        if _gsutil_path():
            print(f"trace bus: {bus}/traj  (run_id={run_id})")
        else:
            print(f"trace bus: {bus}/traj  (run_id={run_id}) -- gsutil NOT found; sync skipped")
    else:
        print("trace bus: disabled (empty --bus)")

    repo_root = (a.repo_root or "").strip()
    jobs = [
        (iid, arm, a.model, a.dataset, a.split, a.call_cap, a.wall_cap_s,
         a.grade_timeout_s, str(out_dir), run_id, repo_root)
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
                if row.get("infra_failed") and (iid, arm_name) not in retried:
                    retried.add((iid, arm_name))
                    log(f"  RETRY {iid} [{arm_name}] after infra failure: {row.get('error')}")
                    futs[ex.submit(_worker, j)] = j
                    continue
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
                completed_since_sync += 1
                if a.sync_every > 0 and completed_since_sync >= a.sync_every:
                    _rsync_traj(traj_dir, bus)
                    completed_since_sync = 0
    _rsync_traj(traj_dir, bus)
    log("BENCH_RUN_DONE")


def _GraderDefault(field: str) -> str:
    """Defer to the grader module's env-driven defaults for dataset/split."""
    from bench import grader as _g
    return _g.DEFAULT_DATASET if field == "dataset" else _g.DEFAULT_SPLIT


if __name__ == "__main__":
    main()
