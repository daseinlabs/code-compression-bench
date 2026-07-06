"""code-compression-bench — a clean-room benchmark harness.

One fixed agent scaffold runs against ONE OpenAI-compatible model; only the
compression layer (the "arm") differs between runs. This package holds the
generic plumbing: the Arm adapter contract + registry (`bench.arm`), the
cache-aware pricing model (`bench.pricing`), the on-disk record schema
(`bench.schema`), and the runner/grader/report/figure modules built on top.

Nothing here imports any proprietary compression logic. Each arm is a thin
adapter that either transforms the message array, routes the model call through
an external endpoint, or attaches a tool server.
"""

__version__ = "0.1.0"

__all__ = ["arm", "pricing", "schema"]
