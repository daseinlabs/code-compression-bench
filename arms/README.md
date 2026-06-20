# Arms — how to run & self-host each compression layer

Every arm runs the **same** agent scaffold against the **same** model
(`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `MODEL` from `.env`). Only the
compression layer differs. There are three adapter patterns:

| pattern | what it does | hook |
|---|---|---|
| **TransformArm** | rewrites the message array client-side, then the scaffold calls the model normally | `transform(messages) -> messages` |
| **ProxyArm** | routes the litellm call through the arm's own OpenAI-compatible endpoint (compresses server-side) | `model_base_url()`, `headers()` |
| **ToolArm** | attaches an MCP tool server / adjusts the agent's tools | `attach() -> ToolAttach` |

List registered arms: `python -c "import arms, bench.arm as a; print(a.available_arms())"`
Check readiness: each arm's `.ready()` returns `(ok, reason)` based on its `needs` env vars.

---

## baseline (control) — no compression
Built in (`bench.arm.BaselineArm`), always ready, needs nothing. `transform` returns the
messages unchanged so the same call path is exercised as every other arm.

## dasein — hosted, keyed (ProxyArm)
Dasein's hosted endpoint compresses server-side. The public repo holds only the thin client.

- **Env:** `DASEIN_API_KEY` (`dsk_...`), `DASEIN_BASE_URL`.
- **Run:** set both env vars, then `make bench ARM=dasein`. No local service to launch.
- **Topology:** Claude Code points `ANTHROPIC_BASE_URL` at `DASEIN_BASE_URL`; the Dasein
  service's OWN upstream must forward to this run's gateway URL (the runner prints it), so the
  chain is `Claude Code -> Dasein (compresses) -> gateway -> Vertex`. The Dasein service
  authenticates its own key and is configured (at provisioning) with the gateway as upstream;
  Claude Code talks Anthropic to Dasein directly.

## woz — Claude Code MCP server (ToolArm, paid)
Woz is a paid Claude Code plugin that ships a **real MCP server**
(`servers/code-server.js` in [github.com/WithWoz/wozcode-plugin](https://github.com/WithWoz/wozcode-plugin)).
It does **not** compress the prompt stream or proxy the model — it changes the agent's
**tools**. The arm replaces the scaffold's broad shell/grep surface (`replace_tools=True`)
with Woz's narrow, index-backed code-query/search/edit surface. Sharper tools → shorter tool
calls → fewer big `cat`/`grep` dumps in the transcript, so the prompt that accrues across
turns stays small. That indirect effect is the whole compression mechanism.

**The tools are discovered live — not hardcoded.** On run start the runner spawns the MCP
server, performs the MCP handshake (`initialize` + `notifications/initialized`), calls
`tools/list`, and advertises the **REAL** schemas the server returns to the model. When the
model calls one, the runner dispatches it over the stdio pipe via `tools/call` and feeds the
text result back as the observation (bash/non-MCP tools still go to the container env). The
server is torn down in `finally`. This runs on the **same** mini-swe-agent scaffold as every
other arm — it is not Claude Code and not a stub. See `bench/mcp_client.py` (the stdio
JSON-RPC bridge) and `bench/runner.py` (`_spawn_mcp_for_arm`, `_make_mcp_agent_class`).

- **What it is:** paid plugin (Claude Code MCP server, `node servers/code-server.js`). Not self-host/free.
- **Env:**
  - `WOZ_API_KEY` (license/account key, **required**) — passed to the spawned MCP server via
    its **environment**, never inlined into argv.
  - `WOZ_PLUGIN_DIR` (**required** unless `WOZ_MCP_CMD` is set) — path to a clone of the
    plugin repo on the runner box (`${CLAUDE_PLUGIN_ROOT}`); the server file is
    `<WOZ_PLUGIN_DIR>/servers/code-server.js`.
  - `WOZ_MCP_CMD` (optional override) — a full launch command for the MCP stdio server, used
    if the plugin layout differs. Wins over the `WOZ_PLUGIN_DIR` default.
  - `WOZ_NODE` (optional) — pin a specific `node` binary (default: `node` on `PATH`).
- **Setup on the runner box (Linux):**
  ```sh
  git clone https://github.com/WithWoz/wozcode-plugin "$WOZ_PLUGIN_DIR"
  cd "$WOZ_PLUGIN_DIR" && npm ci   # build the native addon — see the note below
  ```
  Node.js is required. The plugin ships a **platform-specific native addon**
  (`build/Release/queryparser.node` / a `node-gyp` build). It is **not** portable from this
  Windows dev box — it must be built/run on the **Linux runner** (matching node ABI + arch).
- **Default launch (reproduces the plugin's `.mcp.json`):**
  `node --no-warnings=ExperimentalWarning <WOZ_PLUGIN_DIR>/servers/code-server.js`, with the
  plugin's env (`WOZCODE_MCP_CWD_HOOK_INJECTED=1`, `WOZCODE_POSTHOG_*`) plus `WOZ_API_KEY`
  forwarded via the **environment**.
- **Run:** `WOZ_API_KEY=... WOZ_PLUGIN_DIR=/clones/wozcode-plugin make bench ARM=woz`.
- **`ready()`** requires `WOZ_API_KEY`, a resolvable `node`, **and** the server entrypoint on
  disk (`<WOZ_PLUGIN_DIR>/servers/code-server.js`) — otherwise it SKIPs with a precise reason
  (the runner never crashes mid-run on a misconfigured woz).

## bear — The Token Company API (TransformArm)
Calls bear's compress API on the message array (`target_ratio = COMPRESSION_TARGET_RATIO`,
default `0.5`), then the scaffold calls the model normally.

- **Env:** `BEAR_API_KEY`, `BEAR_BASE_URL`, `COMPRESSION_TARGET_RATIO`.
- **Run:** set env, then `make bench ARM=bear`. On any API error the arm degrades to
  identity (returns the input messages unchanged) so a hiccup never drops the prompt.

## rtk — rtk-ai/rtk CLI binary, run as a PreToolUse HOOK (not a proxy)
**rtk is NOT a proxy.** [rtk-ai/rtk](https://github.com/rtk-ai/rtk) ("Rust Token Killer") is a
single Rust CLI binary that compresses **shell-command stdout** by 60–90% — it has no `serve`
mode, no `--upstream`, and never sits on the model endpoint. It integrates with Claude Code
**only** as a **PreToolUse hook** that rewrites Bash commands before they run (its own installer,
`rtk init -g`, writes exactly this hook): `git status` → `rtk git status`, so the `rtk` wrapper
runs the command and emits *compressed* output into context. The native `Read`/`Grep`/`Glob`
tools **bypass** it (rtk only touches the shell boundary).

In the bench, `RtkArm` is a **hook arm** (`bench.arm.Arm`, `kind = BASELINE` so the model goes
**straight to the gateway like A0**). It overrides `pre_tool_hook(tool_name, tool_input)`: when
`tool_name == "Bash"` and the command's first token is one rtk wraps (git, gh, ls, cat, grep,
find, tree, diff, pytest, jest, vitest, cargo, go, tsc, ruff, eslint, pnpm, pip, docker, kubectl,
aws, curl, wget, …), it returns `{"tool_input": {...command: "rtk <cmd>"...}}`; otherwise `None`
(leave the call untouched — faithful to rtk's no-op on unsupported commands). The runner's
PreToolUse capability turns that into the SDK's `updatedInput` (see
`bench/cc_runner.py::_build_harness_hooks` + `tests/test_harness_hooks.py::RewriteArm`).

- **The product is the binary — install it on the runner box (any one):**
  ```sh
  brew install rtk
  # or
  curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh
  # or
  cargo install --git https://github.com/rtk-ai/rtk
  ```
- **Env:** none required. Optional `RTK_BIN` pins the binary path/name (default `rtk` on `PATH`).
- **`ready()`** runs `rtk --version` (`shutil.which('rtk')` + a version probe) and **SKIPs** with a
  precise reason if the binary is absent or broken — mirroring how `woz.ready()` gates on node +
  plugin presence. There is **no** proxy and **no** `RTK_BASE_URL` to provision.
- **Run:** install the binary, then `make bench ARM=rtk`. The KPI path is unchanged: because the
  rewritten (smaller) Bash output is what accrues in context, the usage gateway bills the **real
  post-compression** tokens. The chain log shows the gateway-direct form
  (`chain [rtk]: ClaudeCode -> <gateway> (gateway) -> Vertex`), and a Bash call in the trajectory
  shows the `rtk ` prefix.

## edgee / headroom — self-hosted open-source proxies (ProxyArm)
Each is an Anthropic-API-speaking compression proxy you run locally; it compresses the prompt
and forwards to **its configured upstream — which MUST be this run's gateway**. The arm only
points Claude Code's `ANTHROPIC_BASE_URL` at the local proxy; the proxy holds no model key
(the gateway, below it, bridges to Vertex via ADC).

### Topology (the single bottom bridge)
The usage gateway is the **single bottom bridge to Vertex** (claude-sonnet on Vertex via
litellm + ADC). The full chain for a proxy arm is:

```
Claude Code  ──(ANTHROPIC_BASE_URL = <ARM>_BASE_URL)──>  vendor proxy (compresses)
             ──(vendor's UPSTREAM = the gateway URL)──>  gateway  ──>  Vertex
```

The gateway sits at the bottom, so it captures the **real post-compression usage** (cache
split included). **Per-vendor upstream config (provisioning requirement):** each vendor proxy
must be told to forward to the gateway URL the runner prints per run
(`its UPSTREAM must be http://127.0.0.1:<port>`). Where to set it:

| arm | where the vendor's upstream is configured |
|---|---|
| **edgee** | `EDGEE_ANTHROPIC_UPSTREAM=<gateway_url>` on the forked `edgee local-gateway` (the patch wires this env var into the Anthropic passthrough's `with_base_url`; see `selfhost/edgee/`). Launch: `GATEWAY_URL=<gateway_url> EDGEE_PORT=8787 bash selfhost/edgee/launch.sh` |
| **headroom** | `ANTHROPIC_TARGET_API_URL` (or the `--anthropic-api-url` flag) on `headroom proxy --port 8787 --mode token --anthropic-api-url <gateway>` — Anthropic-native upstream, NOT LiteLLM |
| **compresr** | `ANTHROPIC_PROVIDER_URL=<gateway_url>` set ON THE COMPRESR PROCESS (the Context Gateway's upstream-redirect var; see `internal/gateway/providers.go`) — Claude Code points `COMPRESR_GATEWAY_URL=http://127.0.0.1:18081` at the gateway |

> **rtk is not in this table** — it is a hook arm (see its section above), not a proxy: there is
> no upstream to configure, and the model goes straight to the gateway like the baseline.

We do **not** set `CLAUDE_CODE_USE_VERTEX` and we do **not** inject the arm's `headers()`:
Claude Code speaks the Anthropic API straight to the vendor proxy, so any vendor auth lives
**on the vendor proxy** (configured at provisioning), and Vertex auth is **ADC on the box**
held by the gateway.

- **Env (defaults):** `EDGEE_BASE_URL=http://127.0.0.1:8787` (edgee local-gateway's real
  default port), `HEADROOM_BASE_URL=http://127.0.0.1:8787` (also Headroom's real default).
  Both real defaults are 8787 — harmless because vendor arms run **one at a time**; set
  `EDGEE_PORT`/`EDGEE_BASE_URL` (or `--port`) if you ever co-locate them.
- **Launch headroom:** `make selfhost-up` (wraps `docker compose -f selfhost/docker-compose.yml up -d`).
- **Launch edgee:** build the fork once (`bash selfhost/edgee/build.sh`), then per run
  `GATEWAY_URL=<gateway_url> EDGEE_PORT=8787 bash selfhost/edgee/launch.sh`.
- **Stop:** `make selfhost-down` (headroom).
- **Per-project setup notes:**
  - **edgee** — open-source Rust CLI (`edgee-ai/edgee`), **Anthropic-native**. Its
    `edgee local-gateway` routes `POST /v1/messages` through the real Anthropic passthrough +
    Claude `CompressionLayer` (content blocks / `tool_use` / `tool_result` / `cache_control`
    pass through unchanged). Upstream Anthropic was hardcoded to `api.anthropic.com`; the
    fork in `selfhost/edgee/` (`anthropic_upstream.patch`, pinned to edgee-cli 0.2.9) wires
    the already-present `with_base_url` override into `start()` from `EDGEE_ANTHROPIC_UPSTREAM`,
    so it forwards to the run gateway instead. Build/launch via `selfhost/edgee/build.sh` +
    `launch.sh` — NOT Docker (the `edgee/edgee:latest` image was fictitious and has been dropped).
  - **headroom** — open-source, **Anthropic-native**, reversible compression
    (github.com/chopratejas/headroom, PyPI `headroom-ai`). Its proxy natively speaks the
    Anthropic Messages API at `/v1/messages`, so Claude Code talks to it directly. Either run
    the container in `selfhost/docker-compose.yml` or self-host the CLI:
    `pip install "headroom-ai[all]"` then
    `headroom proxy --port 8787 --mode token --anthropic-api-url <gateway>`. Point
    `HEADROOM_BASE_URL` at it (default `http://127.0.0.1:8787`) and set its UPSTREAM Anthropic
    endpoint to the gateway URL via `ANTHROPIC_TARGET_API_URL` (or the `--anthropic-api-url`
    flag) — NOT a LiteLLM upstream.

> The compose file ships **skeleton** services (placeholder `image:` tags marked `TODO`).
> Replace each with the project's real published image or a `build:` context before
> `make selfhost-up`.

## compresr — Context Gateway (ProxyArm)
Compresr's [Context Gateway](https://github.com/Compresr-ai/Context-Gateway) (compresr.ai,
YC W2026; **Go**, Apache-2.0) is an open-source **Anthropic-native** reverse proxy that
compacts conversation history + tool outputs before they reach the model. It detects format
and routes `/v1/messages`-shaped Anthropic traffic, so Claude Code's content blocks /
`tool_use` / `tool_result` / `cache_control` pass through unchanged. The documented Claude
Code path is exactly `ANTHROPIC_BASE_URL=http://localhost:18081 claude` — no client-side
translation. In the bench `CompresrArm` is a **ProxyArm**: it points Claude Code's
`ANTHROPIC_BASE_URL` at the local gateway and injects **no** client header.

- **Env:** `COMPRESR_GATEWAY_URL` — base URL of the local Context Gateway Claude Code points at
  (default `http://127.0.0.1:18081`, the product's real default port; the old `8804` was an
  audit gap). This is the **only** var the arm reads.
- **Launch (self-host):** run the Go gateway on `GATEWAY_PORT` (= `18081`) — build/run
  `Compresr-ai/Context-Gateway` (e.g. `go run ./cmd/...` with `cmd/configs/fast_setup.yaml`, or
  its container). `ready()` does a real TCP probe of `COMPRESR_GATEWAY_URL` and **SKIPs** with a
  precise reason if the gateway is not listening, so a dead/un-launched gateway never burns a
  paid run.
- **Upstream (provisioning requirement):** the gateway's **UPSTREAM Anthropic endpoint** must
  be THIS run's gateway, set via **`ANTHROPIC_PROVIDER_URL=<gateway_url>` ON THE COMPRESR
  PROCESS** (its real upstream-redirect var; default `https://api.anthropic.com` — matches
  `internal/gateway/providers.go`). The runner prints the gateway URL per run. Chain:
  `Claude Code -> Compresr (compacts) -> gateway -> Vertex`.
- **Auth — no client bearer.** The gateway uses Claude Code's own auth (`skip_api_key_setup`
  → Claude Code's bridge token passes straight through), so the arm sends no header.
  `COMPRESR_BASE_URL` / `COMPRESR_API_KEY` in the real product are the **compression-SERVICE**
  creds (the gateway's call OUT to `https://api.compresr.ai`) and are configured **ON THE
  GATEWAY**, never sent by the client — so this arm holds no key.
- **COST CAVEAT (measurement):** Compresr's preemptive summarizer itself calls an LLM
  (`claude-haiku-4-5`) by egressing to `api.compresr.ai`. That leg does **NOT** pass through
  the bottom-bridge usage gateway, so its tokens/cost are **NOT** captured in this arm's usage
  rows — note it when reading Compresr's cost numbers.
- **Run:** launch the gateway (with `ANTHROPIC_PROVIDER_URL=<gateway_url>`), then
  `COMPRESR_GATEWAY_URL=http://127.0.0.1:18081 make bench ARM=compresr`.

---

### Adding a new arm
1. Create `arms/<name>.py`, subclass one of the three patterns, set `name`/`needs`,
   and decorate the class with `@bench.arm.register("<name>")`.
2. Import it in `arms/__init__.py` so registration runs when the package is imported.
3. Add its env vars to `.env.example`.
