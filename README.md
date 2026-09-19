# almanac

A headless, local AI tool runner for a handful of machines you own. You feed
it what matters, as plain Markdown notes and small declared tools, and it
lets AI clients use that knowledge and those tools:

- **Claude Code and Codex** call almanac's tools and knowledge over **MCP**
  (streamable HTTP or stdio), with their usual cloud models;
- or they run **entirely on a local model** through almanac's **gateway**,
  which speaks the Anthropic Messages and OpenAI Chat/Responses APIs in front
  of Ollama;
- or you ask almanac itself: `almanac ask`, `almanac chat`, `almanac run
  <runbook>` (also from systemd timers), with no cloud tokens at all.

It was written for a desk of Linux machines with a gaming PC among them, so
inference loads on demand, unloads when idle, stays on the GPU you pick, and
stays off when memory is tight. It includes a first-class FFXIV/Dalamud
integration: Dalamud log tools, plugin dev build/install tools, and the
[XivMcp](#ffxiv-and-dalamud) in-game MCP server as a companion, with progress
shown on the in-game agent board.

Nothing here is a pile of opaque scripts: knowledge is Markdown, tools are one
TOML file each, and every change-making call is confirmed and audited.

By Johnneylee Jack Rollins ([github.com/Spaceghost](https://github.com/Spaceghost)). MIT licensed.

## Architecture

```
   Claude Code / Codex / any MCP client          Claude Code / Codex on a local model
          |  MCP (HTTP :41880 or stdio)                 |  /v1/messages, /v1/chat/completions,
          |  Bearer token                               |  /v1/responses   (Bearer token)
          v                                             v
  +------------------ almanac ------------------+  +------------ gateway -------------+
  |  MCP server  ->  Almanac.call()              |  | auth, model-name mapping,        |
  |                   | validate args (schema)   |  | allow-list, memory/VRAM guard,   |
  |                   | plan + confirm (change)  |  | count_tokens, Codex namespaces   |
  |                   | audit.jsonl              |  +---------------+------------------+
  |                   v                          |                  |
  |   knowledge/*.md --> SQLite FTS5 index       |                  v
  |   tools/*.toml   --> argv (no shell) ------------> local / ssh host / incus remote
  +---------^------------------------------------+          Ollama (one GPU, keep_alive)
            |                                                        ^
  almanac ask / chat / run  (local agent loop) ----------------------+
            |  companion MCP servers (e.g. XivMcp in game): tools + post_status progress
            v
      systemd --user: almanac-mcp, almanac-gateway, almanac-run@<runbook>.timer
```

The engine (this repository) is generic. **Your** knowledge, tools and config
live elsewhere (a private repo is a good home) and are referenced from
`~/.config/almanac/config.toml`. The repository ships examples under
`examples/` so a fresh checkout works.

## Install

Requirements: Linux, Python 3.11+ with SQLite FTS5, `systemd --user`, and
Ollama (or any server with the same APIs) for the local model.

```sh
git clone https://github.com/Spaceghost/almanac-dalamud ~/almanac
~/almanac/deploy/install.sh        # venv, token, config, user units; starts almanac-mcp + almanac-gateway
almanac doctor                     # config, knowledge, tools, backend, guard (never prints the token)
```

`install.sh` is rootless and touches only `~/.local/share/almanac`,
`~/.config/almanac`, `~/.config/systemd/user` and `~/.local/bin/almanac`.
For the services to run while you are logged out, enable lingering once:
`loginctl enable-linger $USER`.

Edit `~/.config/almanac/config.toml` (see `config.example.toml`): point
`knowledge_dirs` and `tools_dirs` at your own directories, list your `hosts`,
and add addresses to `listen` to serve other machines (for example your
Tailscale IP). Then `systemctl --user restart almanac-mcp almanac-gateway`.

Other deployments: `deploy/quadlet/almanac-ollama.container` (Ollama in rootless
Podman limited to one GPU through CDI) and `deploy/incus/` (Ollama + almanac in
an Incus container with a passed-through GPU).

### Model

The default is **`qwen3.5:9b`** (Q4_K_M, 6.6 GB on disk, about 5.8 GB of
VRAM with 32k context and a q8 KV cache): it fits an 8 GB GPU entirely, it is
trained for tool calling, and with thinking off it answers tool-using
questions in seconds once loaded. Larger models (12B+, 20B, 27B) do not fit
8 GB without spilling into system RAM, which is exactly what a gaming machine
cannot spare. `ollama pull qwen3.5:9b` downloads it (~6.6 GB).

Optional embeddings for semantic search: pull a small embedding model (for
example `ollama pull embeddinggemma`), set `[kb] embed_model`, run
`almanac index --embed`. Without it search is SQLite FTS5 (BM25) only.

### Why a small gateway rather than LiteLLM

Ollama (0.14 and later) already implements `/v1/messages`,
`/v1/chat/completions` and `/v1/responses` itself. What clients additionally
need is authentication, mapping of `claude-*`/`gpt-*` model names to the local
model, an allow-list so no client can load a model that does not fit, the
memory guard, `count_tokens` (Claude Code calls it), and flattening of Codex's
tool namespaces. That is a few hundred readable lines (`gateway.py`,
`codex_compat.py`, `guard.py`). A LiteLLM container would add a second
translation layer, a large image and a few hundred MB of resident memory on
the machine that can least afford it.

## Using it

```sh
almanac search incus snapshot          # knowledge base search
almanac read hosts/example-host.md
almanac tools                          # every tool, its safety class and description
almanac tool host_status host=example-host
almanac tool incus_snapshot remote=srv instance=web snapshot=pre-upgrade   # shows the plan, asks y/N
almanac ask "is anything failing on example-server?"
almanac chat                           # interactive; /help, /tools, /actions on|off, /quit
almanac run daily-health-check         # a runbook, with the local model
almanac schedule daily-health-check --on-calendar daily --enable   # systemd user timer
almanac audit                          # what ran, who asked, what was declined
almanac model status | unload
```

`ask`, `chat` and `run` use only the local model. Scheduled runs that find
memory tight exit 75, which the unit treats as "skipped, try next time".
Transcripts are saved under `~/.local/state/almanac/runs/`.

## Connect Claude Code

Keep the token out of files and history: export it from the token file.

```sh
export ALMANAC_TOKEN="$(cat ~/.config/almanac/token)"     # e.g. in your shell profile

# almanac's tools and knowledge over MCP; single quotes keep the literal
# ${ALMANAC_TOKEN} in the config so Claude Code expands it at runtime
claude mcp add --transport http almanac http://127.0.0.1:41880/mcp \
  --header 'Authorization: Bearer ${ALMANAC_TOKEN}'
```

Or in a project's `.mcp.json` (or a file passed with `--mcp-config`):

```json
{ "mcpServers": { "almanac": { "type": "http", "url": "http://127.0.0.1:41880/mcp",
  "headers": { "Authorization": "Bearer ${ALMANAC_TOKEN}" } } } }
```

Local stdio (no network, no token): `claude mcp add almanac -- almanac mcp --stdio`.

Run Claude Code **on the local model** for routine jobs:

```sh
ANTHROPIC_BASE_URL=http://127.0.0.1:41881 ANTHROPIC_AUTH_TOKEN="$ALMANAC_TOKEN" \
ANTHROPIC_MODEL=claude-sonnet-4-5 ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5 \
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 claude
```

Any `claude-*` name maps to the local model (`[gateway.models]`).

## Connect Codex

In `~/.codex/config.toml`:

```toml
[mcp_servers.almanac]
url = "http://127.0.0.1:41880/mcp"
bearer_token_env_var = "ALMANAC_TOKEN"

# Optional: run Codex on the local model
[model_providers.almanac]
name = "almanac local"
base_url = "http://127.0.0.1:41881/v1"
env_key = "ALMANAC_TOKEN"
wire_api = "responses"

[profiles.local]
model_provider = "almanac"
model = "gpt-5-codex"          # any gpt-* name maps to the local model
model_reasoning_effort = "low"
```

`codex --profile local` then runs on the local model; `codex` alone keeps
your normal model and still sees almanac's tools. Codex logs a harmless
"failed to refresh available models" line against the gateway; set
`model_catalog_json` if you want local model metadata.

## Adding knowledge

Knowledge is Markdown with a small front matter block (see
`examples/knowledge/README.md`). Add a file under `hosts/`, `services/`,
`projects/` or `runbooks/` in your knowledge directory; the index notices on
the next search. From a client, `kb_note` proposes a note: the first call
returns the unified diff, nothing is written until the human approves, and
updating an existing note requires the sha256 returned by `kb_read` so a
concurrent edit is never overwritten. Anything that looks like a credential is
refused.

Runbooks are notes with `kind: runbook`, a `tools:` list limiting what the
run may use, and optionally `approve:` listing change tools the owner
pre-approves for unattended runs.

## Adding a tool

One TOML file in a tools directory. Full reference: [docs/TOOLS.md](docs/TOOLS.md).

```toml
description = "Status and recent log lines of one system service."
safety = "read"                 # read | change | destructive
run_on = "{host}"               # a host param is added automatically, enum = hosts
hosts = ["*"]                   # any configured host
timeout = 30

[params.unit]
type = "string"
pattern = "[A-Za-z0-9][A-Za-z0-9@._-]{0,80}"
required = true
description = "Service name."

[[commands]]
argv = ["systemctl", "status", "--no-pager", "--lines=20", "{unit}"]
init = ["systemd"]

[[commands]]
argv = ["rc-service", "{unit}", "status"]
init = ["openrc"]
```

Arguments are validated (type, enum, pattern, range; strings cannot start
with `-` unless `allow_dash = true`) and substituted into argv elements. There
is no shell anywhere; for ssh hosts each element is quoted with `shlex` so the
remote shell sees exactly the same words. Output is captured, time-limited and
truncated. `almanac tools -v` prints the JSON schema clients see.

## Security model

- **Who can call:** MCP over HTTP and the gateway require the bearer token in
  `~/.config/almanac/token` (0600, generated by `almanac init`, never printed
  or logged). Bind them to loopback, or add a private VPN address; do not
  expose them publicly. stdio MCP runs as the local user who starts it.
- **What runs:** only declared tools, with validated arguments, as argv. A
  tool can do only what its TOML says.
- **Confirmation:** `read` tools run immediately. `change` and `destructive`
  tools, and `kb_note`, never run on the first call: the client gets the exact
  plan (host, argv or diff). Clients that support MCP elicitation show an
  approve/decline form to the human; others must show the plan and call again
  with `confirm=<token>`, a token bound to exactly those arguments. The local
  agent never gets destructive tools, gets change tools only with
  `--allow-change`, and then asks y/N (or uses a runbook's `approve:` list).
- **Audit:** every tool run, plan, approval, refusal and companion call is
  appended to `~/.local/state/almanac/audit.jsonl` (`almanac audit`).
- **Secrets:** never in notes (writes are scanned), config, logs or the
  audit log. Companion tokens are read at call time from wherever the
  companion keeps them.
- **Resources:** the gateway will not load a model when available memory or
  the inference GPU's free VRAM is below the configured floor, and unloads the
  resident model when memory runs low. Residency pins (above) go through the
  same guard.

## Model residency

By default the model loads on the first request and unloads after Ollama's
`keep_alive` (e.g. 5 minutes idle). On a gaming machine you may prefer the
opposite while a game runs: the model stays loaded, so asking almanac
something mid-session never waits for a cold load and never has to find
free VRAM at a bad moment.

```toml
[residency]
keep_loaded_while_process = ["ffxiv_dx11.exe"]
idle_keep_alive = "5m"
poll_seconds = 20
preload = true
```

A loop inside `almanac-gateway` (no extra daemon) checks every
`poll_seconds`:

- **A listed process is running:** the model (`[residency] model`, default
  `[gateway] default_model`, which must be in `allowed_models`) is loaded and
  pinned with Ollama's `POST /api/generate {"model": ..., "keep_alive": -1}`
  (no prompt: it loads without generating). With `preload = false` it is only
  pinned once a request has loaded it. The pin is re-asserted whenever the
  model disappears (backend restart, memory-guard unload) or a request resets
  its `keep_alive`.
- **The guard still applies:** if the model is not resident and the guard
  refuses to load it (host memory below `min_mem_available_mb`, or the
  `gpu_uuid` GPU below `min_gpu_free_mb`), the state is `deferred` and it
  retries on the next poll. The guard's low-memory unload also still wins
  over a pin; the pin comes back once memory recovers.
- **They have all exited:** `keep_alive` is set back to `idle_keep_alive`
  (same call), so the model unloads later exactly as before.

Processes match on the basename of `argv[0]` in `/proc/*/cmdline`,
case-insensitively; Windows paths as Wine shows them
(`Z:\...\game\ffxiv_dx11.exe`) match, and under a Wine loader argv[1] is
checked too. A shell or editor that merely mentions the name does not match.

State changes (`pinned`, `idle`/released, `deferred`, `backend_down`) are
logged once to the gateway's journal. The current state is in the gateway's
`/healthz` under `residency`, and the read tool `model_residency` (MCP and
the local agent) returns the state the gateway last published to
`<state_dir>/residency.json`.

## FFXIV and Dalamud

- Tools: `dalamud_log_tail`, `dalamud_log_grep` (XIVLauncher.Core logs under
  `~/.xlcore/logs`), `dalamud_plugin_build`, `dalamud_plugin_install_dev`
  (examples: point them at your plugin), `xivmcp_ping`, `screenshot`
  (xdg-desktop-portal, non-interactive).
- **XivMcp as a companion:** configure `[upstreams.xivmcp]` (see
  `config.example.toml`). The local agent then sees XivMcp's tools as
  `xivmcp__get_player`, `xivmcp__read_chat`, ... Read and UI tools are always
  offered; action and chat tools only with `--allow-game-actions` or
  `/actions on` in chat, and XivMcp still asks the player to confirm each one
  in game. If the game is not running, almanac says so and carries on.
- **In-game progress:** while working, almanac posts `started`, `step n` and
  `done`/`failed` to XivMcp's agent board (the XivMcp window and its Umbra
  toolbar widget), so you can follow along without leaving the game.
- **In-game chat:** in a terminal plugin such as Ghostty for Dalamud, open a
  tab and run `almanac chat`, or send a one-shot question from the game's chat
  box, e.g. `/term send almanac ask "which retainers have full inventories?"`.
  Answers stream into that terminal; progress shows on the agent board. A
  terminal profile whose command is `almanac chat` makes it one keystroke.

## Layout

```
src/almanac/      config, kb (index), tools (TOML runner), service (confirm+audit),
                  mcp_server, gateway, codex_compat, guard, residency, agent, chat,
                  upstream, cli
examples/         knowledge/ and tools/ to copy from
deploy/           install.sh, systemd user units, Quadlet, Incus profile
docs/TOOLS.md     tool file reference
tests/            pytest, no network
```

Development: `python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest`.

## Untested

- MCP elicitation with a real interactive client (tested with the MCP SDK's
  client only; Claude Code and Codex were tested calling read tools).
- `claude mcp add` with the `${ALMANAC_TOKEN}` header (the same header in a
  `--mcp-config` file was tested).
- `deploy/quadlet` and `deploy/incus` on real hardware.
- Embeddings (`[kb] embed_model`) against a real embedding model.
- The screenshot helper on desktops other than GNOME.
