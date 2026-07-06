"""Concrete arm adapters — one module per compression vendor/strategy.

Each module defines an Arm subclass (TransformArm / ProxyArm / ToolArm) and
registers it via `@bench.arm.register(name)`. Importing this package imports
every arm module so registration side-effects run; the runner does
`import arms` (or `from arms import *`) once at startup and then resolves arms
by name through `bench.arm.get_arm`.

Clean-room note: none of these modules import or vendor any proprietary
compression logic. Proxy arms only hold a base_url + headers; the transform arm
(bear) calls a public vendor HTTP API; the tool arm (woz) only describes an MCP
attach. The actual compression always happens on the other side of the wire.
"""

from __future__ import annotations

# Import each arm module for its registration side-effect. Keep alphabetical.
from . import bear, compresr, dasein, edgee, headroom, rtk, woz  # noqa: F401

__all__ = ["bear", "compresr", "dasein", "edgee", "headroom", "rtk", "woz"]
