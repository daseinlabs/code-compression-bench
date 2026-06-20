# Edgee arm — forked, Anthropic-native local gateway

The `edgee` arm runs the **real** [edgee-ai/edgee](https://github.com/edgee-ai/edgee)
product (open-source AI gateway in Rust, with a token-compression engine for Claude
Code), with **one** surgical fork so it can forward to *this* bench's run gateway.

## Why a fork (the one gap)

Real Edgee ships a local gateway two ways:

* `edgee local-gateway` — a standalone headless HTTP gateway. Routes
  `POST /v1/messages` through `AnthropicPassthroughService` + the Claude
  `CompressionLayer`, and `POST /v1/responses` through the OpenAI path. Anthropic-native:
  Claude Code's content blocks / `tool_use` / `tool_result` / `cache_control` pass
  through unchanged (the passthrough service forwards the body and headers verbatim).
* `edgee launch claude --local-gateway` — the same gateway on an ephemeral port, with
  `ANTHROPIC_BASE_URL` wired into a spawned `claude`.

Both call `local_gateway::start()`, which built the `AnthropicPassthroughService` with
the **default** `AnthropicPassthroughConfig` — whose `base_url` is hardcoded to
`https://api.anthropic.com`. The config type has *always* exposed a `with_base_url(...)`
override (it's unit-tested in `edgee_gateway_core`: `target_uri_custom_base_url`), but
`start()` never called it, so there was **no way** to point the Anthropic upstream at
anything but Anthropic. That blocks the bench topology, which needs the compressed
`/v1/messages` traffic forwarded to the run's usage gateway (→ Vertex), not to Anthropic.

## The fork

`anthropic_upstream.patch` (cut against pinned commit
`402004f` = edgee-cli 0.2.9) wires the existing override into `start()` from an env var:

* `EDGEE_ANTHROPIC_UPSTREAM` → `AnthropicPassthroughConfig::with_base_url(...)` for the
  `/v1/messages` route. Unset/empty ⇒ unchanged behavior (production `api.anthropic.com`).
* `EDGEE_OPENAI_UPSTREAM` → both OpenAI Responses endpoints (symmetry; the bench only
  uses the Anthropic route).

Nothing else changes — the compression engine, passthrough, header forwarding, and CLI
are the real product. The patch adds 4 unit tests (`cargo test -p edgee-cli local_gateway::`)
covering the default, the override, empty-env, and the OpenAI pin.

## Build (on the runner box, Linux)

```sh
bash selfhost/edgee/build.sh        # installs rustup if needed, clones @ pinned commit,
                                    # applies the patch, runs the fork's unit tests,
                                    # cargo build --release, installs to ~/.local/bin/edgee
edgee --version
```

## Launch (per run)

```sh
GATEWAY_URL=http://127.0.0.1:<gw-port> EDGEE_PORT=8787 bash selfhost/edgee/launch.sh
```

This runs `edgee local-gateway --port 8787 --bind 127.0.0.1` with
`EDGEE_ANTHROPIC_UPSTREAM=$GATEWAY_URL`. The bench arm points
`ANTHROPIC_BASE_URL=EDGEE_BASE_URL=http://127.0.0.1:8787` at it and TCP-probes the port
in `ready()`. The `<gw-port>` is the run gateway URL the runner prints per run
(`its UPSTREAM must be http://127.0.0.1:<port>`).

## Default port

`edgee local-gateway`'s real default port is **8787** (see
`crates/cli/src/commands/local_gateway.rs`, `default_value_t = 8787`). The old bench
default of `8801` and the `edgee/edgee:latest` Docker image were both fictitious and have
been dropped.
