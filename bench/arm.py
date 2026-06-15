"""The Arm adapter contract + registry.

An "arm" is one compression strategy. Every arm runs the SAME fixed agent
scaffold against the SAME model; the only thing that varies is HOW the prompt
is compressed before it reaches the model. There are exactly THREE adapter
patterns, plus a no-op baseline:

  (a) TransformArm  — rewrites the message array client-side, then the scaffold
                      calls the model normally. The arm never sees the model.
                      Hook: transform(messages) -> messages
                      (e.g. bear: call a compress API on the array)

  (b) ProxyArm      — routes the litellm model call through the arm's own
                      OpenAI-compatible endpoint, which compresses server-side.
                      The scaffold swaps base_url + headers and calls as usual.
                      Hooks: model_base_url() -> str, headers() -> dict[str,str]
                      (e.g. dasein hosted, edgee/rtk/headroom self-hosted)

  (c) ToolArm       — attaches an MCP tool server and adjusts the agent's tool
                      set; compression happens via tools the agent calls.
                      Hook: attach() -> ToolAttach(tools, mcp_server_cmd)
                      (e.g. woz: a Claude Code MCP server)

The runner inspects `arm.kind` to decide which hook(s) to wire. An arm declares
its identity (`name`, `kind`), the env keys it needs (`needs`), and a
`ready()` check the runner calls before scheduling work for that arm.

Concrete arm classes live in the top-level `arms/` package (one module per
vendor); they subclass one of the bases below and register via @register.
This module is pure stdlib and has NO knowledge of any specific vendor.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


# ── message type (the OpenAI/litellm chat-message shape the scaffold passes) ──
# A message is a plain dict: {"role": str, "content": str | list[dict], ...}.
# Arms must preserve this shape. We alias it for signature clarity only.
Message = dict
Messages = list[Message]


class ArmKind(str, Enum):
    """Which adapter pattern an arm implements (selects the runner's wiring)."""

    BASELINE = "baseline"      # no-op: messages and endpoint pass through unchanged
    TRANSFORM = "transform"    # rewrites the message array client-side
    PROXY = "proxy"            # routes the model call through the arm's endpoint
    TOOL = "tool"              # attaches an MCP tool server / adjusts agent tools


@dataclass
class ToolAttach:
    """What a ToolArm contributes to the scaffold for a run.

    tools           : extra OpenAI-format tool/function specs to advertise to the model
                      (or replacements for the default scaffold tools).
    mcp_server_cmd  : argv for the MCP stdio server to spawn (None if the arm uses
                      a hosted/remote MCP). The runner launches and tears this down.
    replace_tools   : if True, `tools` REPLACES the scaffold's default tool set;
                      if False (default), `tools` is appended to it.
    """

    tools: list[dict] = field(default_factory=list)
    mcp_server_cmd: Optional[list[str]] = None
    replace_tools: bool = False


class Arm(abc.ABC):
    """Base adapter every arm subclasses (indirectly, via one of the 3 patterns).

    Identity / capability surface every arm MUST expose:
      name  : str        — registry key (e.g. "bear", "edgee", "baseline")
      kind  : ArmKind    — which adapter pattern (selects runner wiring)
      needs : list[str]  — env var names this arm requires to run
    """

    name: str = "arm"
    kind: ArmKind = ArmKind.BASELINE
    needs: list[str] = []

    def ready(self) -> tuple[bool, str]:
        """Whether this arm can run now. Returns (ok, reason).

        Default check: every env var in `needs` is present and non-empty.
        Subclasses override for richer checks (e.g. ping a self-hosted proxy).
        """
        missing = [k for k in self.needs if not os.environ.get(k)]
        if missing:
            return False, f"missing env: {', '.join(missing)}"
        return True, "ok"

    def setup(self) -> None:
        """Optional one-time prep before a batch of runs (e.g. warm a session).
        No-op by default."""

    def teardown(self) -> None:
        """Optional cleanup after a batch of runs (e.g. close a session/MCP).
        No-op by default."""


# ── pattern (a): client-side message transform ──────────────────────────────
class TransformArm(Arm):
    """Rewrites the message array before the scaffold calls the model.

    The runner calls `transform(messages)` and passes the result to the model
    via the normal (un-proxied) endpoint. Implementations should be PURE w.r.t.
    the model call — no side effects on the returned list's caller.
    """

    kind = ArmKind.TRANSFORM

    @abc.abstractmethod
    def transform(self, messages: Messages) -> Messages:
        """Return a (compressed) message array of the same chat-message shape."""
        raise NotImplementedError


# ── pattern (b): proxy the model call through the arm's endpoint ─────────────
class ProxyArm(Arm):
    """Routes the model call through the arm's OpenAI-compatible endpoint.

    The runner builds the litellm call with base_url = model_base_url() and the
    arm's headers() merged in. The arm's endpoint compresses server-side and
    forwards to the underlying model. Messages are NOT transformed client-side.
    """

    kind = ArmKind.PROXY

    @abc.abstractmethod
    def model_base_url(self) -> str:
        """The OpenAI-compatible base URL the runner points litellm at."""
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        """Extra HTTP headers for the proxied call (e.g. Authorization).
        Empty by default (self-hosted proxies often need none)."""
        return {}


# ── pattern (c): attach an MCP tool server / adjust the agent's tools ────────
class ToolArm(Arm):
    """Attaches an MCP tool server and/or adjusts the agent's tool set.

    The runner calls `attach()` once per run, wires the returned tools into the
    scaffold, spawns mcp_server_cmd if present, and tears it down after.
    """

    kind = ArmKind.TOOL

    @abc.abstractmethod
    def attach(self) -> ToolAttach:
        """Return the tools + optional MCP server command for this arm."""
        raise NotImplementedError


# ── no-op baseline (the control) ─────────────────────────────────────────────
class BaselineArm(Arm):
    """The control arm: no compression. Messages and endpoint pass through.

    Treated by the runner as a transform that returns its input unchanged, so
    the same call path is exercised as the other arms (only the layer differs).
    Always ready (needs nothing).
    """

    name = "baseline"
    kind = ArmKind.BASELINE
    needs: list[str] = []

    def transform(self, messages: Messages) -> Messages:
        return messages


# ── registry ──────────────────────────────────────────────────────────────
_REGISTRY: dict[str, Callable[[], Arm]] = {}


def register(name: str) -> Callable[[Callable[[], Arm]], Callable[[], Arm]]:
    """Class/factory decorator: register an Arm factory under `name`.

    Usage:
        @register("bear")
        class BearArm(TransformArm):
            name = "bear"
            ...
    """

    def deco(factory: Callable[[], Arm]) -> Callable[[], Arm]:
        key = name.lower()
        if key in _REGISTRY:
            raise ValueError(f"arm already registered: {name}")
        _REGISTRY[key] = factory
        return factory

    return deco


def get_arm(name: str) -> Arm:
    """Instantiate the registered arm by name. Raises KeyError if unknown."""
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(f"unknown arm '{name}'. registered: {available_arms()}")
    return _REGISTRY[key]()


def available_arms() -> list[str]:
    """Sorted list of registered arm names."""
    return sorted(_REGISTRY)


# register the built-in control
register("baseline")(BaselineArm)
