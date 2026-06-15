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
- The client points litellm at `DASEIN_BASE_URL` and sends the key as a bearer +
  `X-Dasein-Api-Key` header.

## woz — Claude Code MCP server (ToolArm, paid)
Woz is a paid Claude Code plugin that ships an MCP server. It does **not** compress the
prompt stream or proxy the model — it changes the agent's **tools**. The arm replaces the
scaffold's broad shell/grep surface (`replace_tools=True`) with Woz's narrow, index-backed
trio: `woz_search` (ranked semantic search), `woz_edit` (structured anchor-based edit), and
`woz_sql` (structured code-index query). Sharper tools → shorter tool calls → fewer big
`cat`/`grep` dumps in the transcript, so the prompt that accrues across turns stays small.
That indirect effect is the whole compression mechanism.

- **What it is:** paid plugin (Claude Code MCP server, `node code-server.js`). Not self-host/free.
- **Env:** `WOZ_API_KEY` (license/account key, required) — passed to the spawned MCP server
  via its **environment**, never inlined into argv. `WOZ_MCP_CMD` (optional) — the full
  launch command for the MCP stdio server; if unset the arm reports "not configured" via
  `ready()` (the runner skips it cleanly instead of crashing).
- **Run:** install the Woz plugin, then
  `WOZ_API_KEY=... WOZ_MCP_CMD="node /opt/woz/code-server.js --stdio" make bench ARM=woz`.
- **TODO (woz-license):** `arms/woz.py::_mcp_server_cmd()` returns `None` by default. Fill in
  the real launch argv there (e.g. `["node", "/opt/woz/code-server.js", "--stdio"]`) or set
  `WOZ_MCP_CMD`. The runner spawns it as an stdio subprocess, forwards the env (so
  `WOZ_API_KEY` reaches it), wires in the `attach()` tools, and tears the process down after.
- **`ready()`** requires `WOZ_API_KEY` **and** a resolvable launcher (on `PATH` or disk).

## bear — The Token Company API (TransformArm)
Calls bear's compress API on the message array (`target_ratio = COMPRESSION_TARGET_RATIO`,
default `0.5`), then the scaffold calls the model normally.

- **Env:** `BEAR_API_KEY`, `BEAR_BASE_URL`, `COMPRESSION_TARGET_RATIO`.
- **Run:** set env, then `make bench ARM=bear`. On any API error the arm degrades to
  identity (returns the input messages unchanged) so a hiccup never drops the prompt.

## edgee / rtk / headroom — self-hosted open-source proxies (ProxyArm)
Each is an OpenAI-compatible compression proxy you run locally; it compresses the prompt
and forwards to the shared model (`OPENAI_BASE_URL` / `MODEL`). The arm only routes litellm
at the local endpoint — no client-side auth (the proxy holds the upstream key).

- **Env (defaults):** `EDGEE_BASE_URL=http://127.0.0.1:8801`,
  `RTK_BASE_URL=http://127.0.0.1:8802`, `HEADROOM_BASE_URL=http://127.0.0.1:8803`.
- **Launch all three:** `make selfhost-up` (wraps `docker compose -f selfhost/docker-compose.yml up -d`).
- **Stop:** `make selfhost-down`.
- **Per-project setup notes:**
  - **edgee** — open-source Rust gateway. Pin the published image (or add a `build:` context)
    in `selfhost/docker-compose.yml` and configure it as a compression proxy forwarding to
    `UPSTREAM_BASE_URL=${OPENAI_BASE_URL}`.
  - **rtk** — open-source CLI proxy. Can also run outside Docker (`rtk serve --port 8802 ...`);
    point `RTK_BASE_URL` at wherever it listens.
  - **headroom** — open-source, LiteLLM-native, reversible compression. Either run the
    container in `selfhost/docker-compose.yml` or use its LiteLLM integration directly and
    point `HEADROOM_BASE_URL` at it.

> The compose file ships **skeleton** services (placeholder `image:` tags marked `TODO`).
> Replace each with the project's real published image or a `build:` context before
> `make selfhost-up`.

---

### Adding a new arm
1. Create `arms/<name>.py`, subclass one of the three patterns, set `name`/`needs`,
   and decorate the class with `@bench.arm.register("<name>")`.
2. Import it in `arms/__init__.py` so registration runs when the package is imported.
3. Add its env vars to `.env.example`.
