"""The benchmark runner: one fixed coding-agent scaffold, swappable compression arm.

This is a standalone (open-swe-lite / mini-swe-agent style) SWE-bench driver. The
agent loop is deliberately tiny and FIXED across every arm: a system prompt, a
single ``bash`` tool the model drives, and a step loop that executes commands in
the instance's canonical Docker container until the model submits a patch (or a
cap trips). Every arm runs the SAME ``MODEL`` against the SAME OpenAI-compatible
endpoint (``OPENAI_BASE_URL`` / ``OPENAI_API_KEY``). The ONLY thing that varies
between arms is HOW the prompt is compressed at the model-call seam:

    TransformArm  -> we call ``arm.transform(messages)`` and send the rewritten
                     array to the normal endpoint (client-side compression).
    ProxyArm      -> we point the litellm call at ``arm.model_base_url()`` and
                     merge ``arm.headers()`` (the arm compresses server-side).
    ToolArm       -> we fold ``arm.attach()`` tools (and, TODO, an MCP server)
                     into the scaffold's tool set.
    BaselineArm   -> the control: messages and endpoint pass through unchanged.

Scale-out: a ``ProcessPoolExecutor`` fans the full (instance x arm) grid across
``--workers`` processes. A JSONL ledger makes the run resumable — a completed
(instance, arm) pair is skipped on restart; infra failures are retried once and
never counted. Per-(task, arm) there are hard 50-call and wall-clock caps so a
runaway agent can't burn the budget.

Each finished solve is graded by the official SWE-bench Docker harness
(:mod:`bench.grader`), priced cache-aware (:mod:`bench.pricing`), and written as a
:class:`bench.schema.RunRecord`.

Reimplemented from the PATTERNS in meta_learning (gate2.py worker pool / resume
ledger / caps; mini_swe.py litellm call + bash tool + per-call usage capture;
swebench.py grading) as standalone clean-room code. No proprietary import.
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
from bench.pricing import cache_frame_cost, rates_for
from bench.schema import RunRecord


# ── defaults / caps ─────────────────────────────────────────────────────────
DEFAULT_WORKERS = 8
CALL_CAP = 50                 # max model calls per (task, arm) — matches the gate2 cap
WALL_CAP_S = 50 * 60          # hard wall-clock watchdog per solve
DEFAULT_MAX_TOKENS = 8000     # completion cap per call
PATCH_SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"  # how the agent signals "done"


# ── system prompt for the fixed scaffold ─────────────────────────────────────
SYSTEM_PROMPT = """You are a coding agent fixing a bug in a Python repository.

You are at the repository root inside a Linux shell. Investigate the codebase,
locate the defect described in the task, and edit the source so the project's
tests pass. You act ONLY through the `bash` tool — one shell command per call.

Guidelines:
- Explore with standard tools (ls, cat, grep/rg, sed, python). Read before you edit.
- Apply edits in place (e.g. with `python - <<'PY' ... PY`, sed, or a heredoc to a file).
- Do NOT modify the test files; fix the source.
- When the fix is complete, run exactly:  echo {sentinel}
  on its own, and the harness will collect your diff against the base commit.
""".format(sentinel=PATCH_SENTINEL)

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Run a single shell command at the repository root and return its "
            "combined stdout/stderr. Use this for everything: reading files, "
            "searching, editing, running tests."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."}
            },
            "required": ["command"],
        },
    },
}


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


# ── docker execution environment (canonical swebench instance image) ─────────
class DockerEnv:
    """A throwaway container started from the instance's canonical SWE-bench image.

    The agent's bash commands exec inside it; on teardown the patch is captured
    as ``git diff`` against the base commit. Mirrors what mini-swe-agent's
    ``get_sb_environment`` does, reimplemented over the docker CLI so the public
    repo carries no proprietary harness code.
    """

    # the official image naming the swebench harness builds/pulls
    IMAGE_FMT = "swebench/sweb.eval.x86_64.{key}:latest"

    def __init__(self, instance_id: str, repo_dir: str = "/testbed", timeout_s: int = 120):
        self.instance_id = instance_id
        self.repo_dir = repo_dir
        self.timeout_s = timeout_s
        self.container: Optional[str] = None

    @classmethod
    def image_for(cls, instance_id: str) -> str:
        # swebench munges the instance id for the image tag: lowercased, '__' -> '_1776_'.
        key = instance_id.lower().replace("__", "_1776_")
        return cls.IMAGE_FMT.format(key=key)

    def start(self) -> None:
        import subprocess
        name = f"ccb_{self.instance_id.replace('__', '_')}_{os.getpid()}_{int(time.time())}"
        image = self.image_for(self.instance_id)
        subprocess.run(
            ["docker", "run", "-d", "--name", name, "-w", self.repo_dir, image,
             "sleep", "infinity"],
            check=True, capture_output=True, text=True, timeout=300,
        )
        self.container = name
        # stash the pristine commit so we can diff at the end regardless of what the agent does.
        self.exec("git config --global --add safe.directory '*' || true")

    def exec(self, command: str) -> str:
        import subprocess
        if not self.container:
            raise RuntimeError("DockerEnv not started")
        try:
            proc = subprocess.run(
                ["docker", "exec", self.container, "bash", "-lc", command],
                capture_output=True, text=True, timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return f"[command timed out after {self.timeout_s}s]"
        out = (proc.stdout or "") + (proc.stderr or "")
        return out

    def get_patch(self) -> str:
        """The agent's work as a unified diff against the base commit (excludes tests)."""
        # add untracked source files so brand-new files show up in the diff, then diff.
        self.exec("git add -A >/dev/null 2>&1 || true")
        return self.exec("git diff --cached HEAD 2>/dev/null || git diff HEAD 2>/dev/null")

    def cleanup(self) -> None:
        import subprocess
        if self.container:
            try:
                subprocess.run(["docker", "rm", "-f", self.container],
                               capture_output=True, timeout=60)
            except Exception:
                pass
            self.container = None


# ── the model-call seam (where the arm is installed) ─────────────────────────
def _call_model(arm, messages, tools, *, model, max_tokens, usage_sink):
    """One model call with the selected arm wired at the seam.

    - TransformArm / Baseline: rewrite ``messages`` client-side, hit the normal endpoint.
    - ProxyArm:                point base_url + headers at the arm's endpoint.
    - ToolArm:                 endpoint normal; tool wiring already folded in by the caller.

    Records per-call (prompt_tokens, completion_tokens) into ``usage_sink``.
    """
    import litellm

    base_url = os.environ.get("OPENAI_BASE_URL") or None
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
    headers: dict[str, str] = {}
    call_messages = messages

    if arm.kind in (ArmKind.TRANSFORM, ArmKind.BASELINE):
        # arm rewrites the array; baseline returns it unchanged.
        call_messages = arm.transform(messages)  # type: ignore[attr-defined]
    elif arm.kind == ArmKind.PROXY:
        base_url = arm.model_base_url()           # type: ignore[attr-defined]
        headers = {**headers, **(arm.headers() or {})}  # type: ignore[attr-defined]
    # ToolArm: tools already merged upstream; nothing to change on the call itself.

    kwargs: dict = {
        "model": model,
        "messages": call_messages,
        "tools": tools,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    if base_url:
        kwargs["api_base"] = base_url
    if api_key:
        kwargs["api_key"] = api_key
    if headers:
        kwargs["extra_headers"] = headers

    resp = litellm.completion(**kwargs)
    usage = getattr(resp, "usage", None)
    pt = int(getattr(usage, "prompt_tokens", 0) or 0)
    ct = int(getattr(usage, "completion_tokens", 0) or 0)
    usage_sink.append({"prompt_tokens": pt, "completion_tokens": ct})
    return resp


# ── the fixed agent loop ──────────────────────────────────────────────────────
def run_agent(
    arm,
    instance_id: str,
    *,
    model: str,
    call_cap: int = CALL_CAP,
    wall_cap_s: int = WALL_CAP_S,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    exec_timeout_s: int = 120,
) -> dict:
    """Drive one (instance, arm) solve. Returns a dict of raw run signals.

    Pure scaffold logic; the arm is consulted only at the model-call seam and
    (for ToolArm) at tool-assembly time. No grading here — the caller grades the
    returned patch.
    """
    tools = [dict(BASH_TOOL)]
    tool_attach: Optional[ToolAttach] = None
    if arm.kind == ArmKind.TOOL:
        tool_attach = arm.attach()  # type: ignore[attr-defined]
        if tool_attach.replace_tools:
            tools = list(tool_attach.tools)
        else:
            tools = tools + list(tool_attach.tools)
        # TODO(woz): spawn tool_attach.mcp_server_cmd as a stdio MCP server and
        # bridge its tools into `tools` here; tear it down in the finally block.
        # The Claude Code MCP attach is stubbed until the woz server command lands.

    env = DockerEnv(instance_id, timeout_s=exec_timeout_s)
    usage: list[dict] = []
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Task instance: {instance_id}\n\n"
                                     f"Begin by exploring the repository to locate the bug."},
    ]
    calls = 0
    exit_status = "incomplete"
    patch = ""
    t0 = time.time()

    arm.setup()
    try:
        env.start()
        while calls < call_cap:
            if time.time() - t0 > wall_cap_s:
                exit_status = "wall_cap"
                break
            resp = _call_model(arm, messages, tools, model=model,
                               max_tokens=max_tokens, usage_sink=usage)
            calls += 1
            choice = resp.choices[0]
            msg = choice.message
            tool_calls = getattr(msg, "tool_calls", None) or []
            # record the assistant turn (preserve tool_calls for the API contract).
            asst = {"role": "assistant", "content": msg.content or ""}
            if tool_calls:
                asst["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ]
            messages.append(asst)

            if not tool_calls:
                # model answered without acting; nudge it back to the tool, or stop
                # if it's clearly done.
                if PATCH_SENTINEL in (msg.content or ""):
                    exit_status = "submitted"
                    break
                messages.append({
                    "role": "user",
                    "content": "Respond with a `bash` tool call. When the fix is "
                               f"complete, run: echo {PATCH_SENTINEL}",
                })
                continue

            done = False
            for tc in tool_calls:
                if tc.function.name != "bash":
                    out = f"[unknown tool '{tc.function.name}' — only `bash` is available]"
                else:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception as e:
                        args, parse_err = {}, str(e)
                    else:
                        parse_err = ""
                    command = args.get("command", "")
                    if parse_err:
                        out = f"[could not parse tool arguments: {parse_err}]"
                    elif command:
                        out = env.exec(command)
                        if PATCH_SENTINEL in command:
                            done = True
                    else:
                        out = "[empty command]"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": (out or "")[:20000],  # cap a single observation
                })
            if done:
                exit_status = "submitted"
                break
        else:
            exit_status = "call_cap"

        patch = env.get_patch()
    except Exception as e:
        exit_status = f"infra:{type(e).__name__}"
        raise RunInfraError(str(e)[:300]) from e
    finally:
        env.cleanup()
        try:
            arm.teardown()
        except Exception:
            pass

    in_tok = sum(u["prompt_tokens"] for u in usage)
    out_tok = sum(u["completion_tokens"] for u in usage)
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
            arm, instance_id, model=model,
            call_cap=call_cap, wall_cap_s=wall_cap_s,
            max_tokens=max_tokens, exec_timeout_s=exec_timeout_s,
        )
        grader = SWEBenchGrader(dataset=dataset, split=split, timeout_s=grade_timeout_s)
        g = grader.grade(instance_id, raw["patch"])

        rates = rates_for(model)
        prompts = [u["prompt_tokens"] for u in raw["usage"]]
        comps = [u["completion_tokens"] for u in raw["usage"]]
        cb = cache_frame_cost(prompts, comps, rates)

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
            model=model,
            exit_status=raw["exit_status"],
            cache_write_usd=round(cb.write_usd, 6),
            cache_read_usd=round(cb.read_usd, 6),
            output_usd=round(cb.output_usd, 6),
            usage=raw["usage"],
            infra_failed=False,
            error=("grade: " + g.error) if g.error else "",
        )
        # The list-price (flat) bound is recomputed by the report layer from the
        # canonical token fields, so the ledger stays to the RunRecord schema.
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
