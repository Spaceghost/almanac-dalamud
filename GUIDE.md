# almanac

A headless, local AI tool runner for a handful of machines you own. You feed
it what matters, as plain Markdown notes and small declared tools, and it
lets AI clients use that knowledge and those tools:

- **Claude Code and Codex** call almanac's tools and knowledge over **MCP**
  (streamable HTTP or stdio), with their usual cloud models;
- or they run **entirely on a local model** through almanac's **gateway**,
  which speaks the Anthropic Messages and OpenAI Chat/Responses APIs in front
  of Ollama;
- or, **when the cloud runs out**, `ai` ([When Claude runs out](#when-claude-runs-out))
  shows which of your own GPUs are free and starts a coding or chat session on one;
- or you ask almanac itself: `almanac ask`, `almanac chat`, `almanac run
  <runbook>` (also from systemd timers), with no cloud tokens at all.

It was written for a desk of Linux machines with a gaming PC among them, so
inference loads on demand, unloads when idle, stays on the GPU you pick, and
stays off when memory is tight. It includes a first-class FFXIV/Dalamud
integration: Dalamud log tools, plugin dev build/install tools, and the
[XivMcp](#ffxiv-and-dalamud-engine) in-game MCP server as a companion, with progress
shown on the in-game agent board.

Nothing here is a pile of opaque scripts: knowledge is Markdown, tools are one
TOML file each, and every change-making call is confirmed and audited.

**FFXIV players:** you do not need any of the Python below. The
[**Almanac** Dalamud plugin](#almanac-in-game-the-dalamud-plugin) in
[`dalamud/`](dalamud/) connects a model running on your own PC (Ollama, LM
Studio, llama.cpp or any OpenAI-compatible server) to the game through the
XivMcp plugin, recommends models for your GPU, and runs a
[community benchmark](#community-benchmark) so players can work out together
which local models are worth their VRAM.

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
almanac ask --new-thread "which retainers are full?"   # saved as a thread; its id is printed
almanac ask --continue "and the second one?"           # a follow-up in the latest thread
almanac ask --thread ID --stream-json -- "..."         # JSON lines, for programs (see below)
almanac chat --continue                # the REPL, saving every turn in the thread
almanac threads [list|show ID|rm ID] [--json]
almanac run daily-health-check         # a runbook, with the local model
almanac schedule daily-health-check --on-calendar daily --enable   # systemd user timer
almanac audit                          # what ran, who asked, what was declined
almanac model status | unload
```

`ask`, `chat` and `run` use only the local model. Scheduled runs that find
memory tight exit 75, which the unit treats as "skipped, try next time".
Transcripts are saved under `~/.local/state/almanac/runs/`.

**Threads.** With `--thread ID` (created when missing), `--continue` (the
most recent thread) or `--new-thread`, `ask` and `chat` keep the conversation
under `~/.local/state/almanac/threads/<id>.json` (mode 0600), so a follow-up
reaches the model with what was said before. Only the conversation is kept;
the system prompt and tool list are rebuilt every turn. What is sent back is
trimmed to `[agent] context_tokens` (default 8192): whole turns, newest first,
with old tool output shortened. Without a thread option `ask` stays one-shot.

**`--stream-json`** prints one ASCII JSON object per line and never prompts
(change tools stay unapproved; game actions still need the in-game
confirmation): `thread` {id, title, new, turns}, `note` {text}, `text` {text}
as the answer streams, `tool` {name, args}, `result` {name, summary, lines},
then `done` {answer, thread, title} or `error` {message, code}.
`almanac threads --json` lists `thread_info` lines; `threads show ID --json`
replays a thread as `message` {role, text} and `tool` lines; both end with
`end`.

## When Claude runs out

```sh
ai                  # what is up right now, picks the best backend, starts a session
ai code ~/src/repo  # coding session in that repo on your own GPU
ai ask "question"   # one answer; pipe text in:  ai ask "why does this fail" < error.log
```

`ai` is `almanac local` (`deploy/install.sh` links both into `~/.local/bin`).
It works from any machine that has almanac, the config and the token files.

| command | what it does |
| --- | --- |
| `ai status` | every configured backend: GPU, model, context, tok/s from a 64-token probe, and whether it is down, busy, loading or free. `*` marks the one a session would use. `--no-probe` for health only, `--json` for programs |
| `ai code [path]` | coding session in that directory on the local model: Claude Code in bare mode. `--tool codex` for the Codex CLI instead; `-p "task"` does one task and exits, with either |
| `ai chat` | plain chat, no tools. `/new` clears it, `/quit` leaves |
| `ai ask "..."` | one question. Piped text is appended; the banner goes to stderr, so `ai ask -q ... > out.txt` is clean |
| `ai use NAME` / `ai use auto` | pin a backend on this machine, or go back to automatic |
| `ai webui` | the address of your Open WebUI, for chat in a browser or on a phone (`[local] webui_url`) |
| `ai limits` | the expectations below |
| `ai claude [path]` | the same as `ai code --tool claude` (kept from when it was opt-in) |

With no pin, the choice is: up, then free before busy, then `priority`, then
speed. `-b NAME` picks a backend for one run. When the pinned backend is down
the next best is used and it says so.

**Set your expectations at the door.** Every session starts by printing the
model, the context it really has and how fast it is, and this:

```
What to expect (a ~9B local model is not Claude):
  good at   focused edits in one or two files, writing and fixing tests, explaining
            code and errors, shell/git/regex help, summaries, commit messages
  weak at   large refactors, reasoning across many files, long sessions (it forgets
            once the context fills), unfamiliar APIs (it invents them), subtle bugs
  so        give it one small task with the file names and the test command, read
            every diff, and keep the big jobs for when Claude is back
```

`ai code` starts **Claude Code** unless told otherwise, because on the same
tasks it was at least as good as Codex and quicker
([the comparison](#claude-code-on-the-local-model)). Choose per run with
`ai code --tool codex`, or for good with

```toml
[local]
coder = "codex"      # or "claude" (the default)
```

When the chosen tool is not installed the other one is used, and one line says
so. Extra options for the tool go after the path: `ai code . -- --verbose`.

- **claude**: `claude --bare` with `CLAUDE_CONFIG_DIR=<state>/local/claude-config`,
  never your `~/.claude`, and your cloud login is taken out of its environment.
  Bare means no plugins, MCP servers, hooks, `CLAUDE.md` or memory, and three
  tools; that is what leaves the 32k context free. Thinking is off
  (`MAX_THINKING_TOKENS=0`), which was a little quicker over the six tasks below
  and passed the same six; set `MAX_THINKING_TOKENS` yourself to turn it back on. With `-p` it runs headless: edits are accepted,
  and `sudo`, `git push`, `gh`, `ssh` and the like are denied as autopilot
  denies them, but there is **no sandbox** around it.
- **codex**: autopilot's `LOCAL_PRESETS["codex"]`
  ([Local coding sessions](#local-coding-sessions)) with `exec --json` taken off
  for a person. Web search is off, the context window is declared, the tool
  list is short, the sandbox is `workspace-write` with no approvals and no
  network, and `CODEX_HOME` is `<state>/local/codex-home`, never your `~/.codex`.
  Pick it when you want the session fenced in.

### Backends

```toml
[local]
webui_url = "https://chat.example.ts.net"

[local.backends.gpu-box]
url = "http://100.64.0.20:41881"     # an address every machine of yours can reach
kind = "almanac"                     # almanac (gateway) | ollama | openai
gpu = "RTX 3060 12 GB"
model = "qwen3.5:9b"
context = 32768                      # what it really serves
expected_tok_s = 35                  # from `ai status`; shown before each session
token_file = "~/.config/almanac/gpu-box.token"
priority = 10
```

Without `[local.backends]` the `[autopilot.pool.*]` backends are used, and
without those the `[gateway]` on this machine. On a second machine (a laptop):
install almanac, copy the config and each backend's token file (mode 0600)
into `~/.config/almanac/`, and use addresses that are reachable from there,
not `127.0.0.1`. "Busy" comes from the gateway's `/healthz` `inflight` count;
a gateway older than that field, or a plain Ollama, is called busy only when
the speed probe times out.

### Claude Code on the local model

The gateway serves the Anthropic Messages API (Ollama speaks it natively), so
Claude Code can run on the local model. Whether that is *useful* depends on
how it is started. Measured on a Quadro P4000 (qwen3.5:9b, 32k context), same
scratch repository, tests red before and green after:

| how | opening request | result |
| --- | --- | --- |
| `claude --bare`, own config dir (what `ai claude` does) | ~1.5k tokens, 3 tools (Bash, Edit, Read) | fix a one-line bug: green in 31 s, 5 turns. Add a function and its tests: green in 62 s, 6 turns. An empty-sequence bug, through `ai claude` itself: green in 67 s. No failed tool calls |
| plain `claude` pointed at the gateway | ~28k tokens, 24 tools | the one-line fix: green in 119 s. The tool list alone nearly fills the 32k context, so there is no room for real work |

So `ai claude` exists and is opt-in (`ai claude --experimental`, or
`allow_claude = true` under `[local]`): it runs `claude --bare` with
`CLAUDE_CONFIG_DIR=<state>/local/claude-config`, which means no plugins, MCP
servers, hooks, `CLAUDE.md` or memory, and a first-run setup screen the first
time. That is four tiny tasks, not an evaluation: nothing larger than a
one-file change was tried, and tool-call fidelity of a 9B model over a long
session is exactly where it would be expected to fail. `ai code` has more
mileage. Pointing your everyday `claude` at the gateway is not recommended.


### Claude or Codex on the local model

Six tasks, each a failing or missing test in a scratch repo that had to end green,
run against the fedora P4000 (qwen3.5:9b, 32k). Both tools passed all six; the
numbers are seconds, and "bad calls" counts tool calls the model got wrong.

| Task | Claude | Codex |
| --- | --- | --- |
| one-line fix | 19 | 44 |
| add a function and its tests | 47 | 77 |
| empty-sequence bug | 21 | 226 |
| two-file change | 36 | 51 |
| run the tests and fix what fails | 104 | 75 |
| add a CLI flag and a test | 75 | 45 |
| **total** | **302** | **518** |
| bad calls | 3 | 3 |

Claude is the default on that: same six passed, about 40% less wall time, and it
never met a tool it could not call (Codex asks for `apply_patch`, which is not
registered for a model without catalogue metadata, and recovers by writing the file
from the shell). Codex wins on two of the six, so it is one flag away
(`ai code --tool codex`). An earlier pass had both tools slower and noisier because
an interactive session was using the same GPU; these are the quiet numbers.

Claude bare also runs inside autopilot's bubblewrap profile: two of the tasks were
re-run there and passed (20s, 52s) with the sandbox holding — no `~/.config/almanac`,
no `~/.ssh`, and the home directory read-only.

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

Claude Code **on the local model**: use `ai claude` ([above](#claude-code-on-the-local-model)),
which sets this up bare. By hand it is:

```sh
ANTHROPIC_BASE_URL=http://127.0.0.1:41881 ANTHROPIC_API_KEY="$ALMANAC_TOKEN" \
ANTHROPIC_MODEL=claude-sonnet-4-5 ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5 \
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 CLAUDE_CONFIG_DIR=~/.local/state/almanac/local/claude-config \
claude --bare
```

Without `--bare` the tool list alone is ~28k tokens, most of a 32k context.
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
  with `confirm=<token>`, a token bound to exactly those arguments. On MCP
  2026-07-28 sessions the form travels as an `InputRequiredResult`; the state
  that comes back with the answer is sealed, bound to that tool and those
  arguments, expires after 10 minutes and is single-use, so one approval runs
  one call. An unanswered form (10 minutes) runs nothing. The local
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

## Almanac in game: the Dalamud plugin

`dalamud/` is a self-contained C# plugin (Windows, and Linux through
XIVLauncher.Core/Wine). It needs no Python and no almanac engine.

- **Setup wizard** (`/almanac setup`, opens on first load): finds model
  servers on their usual local ports (Ollama 11434, LM Studio 1234, llama.cpp
  8080, KoboldCpp 5001, vLLM 8000) or any URL you give it; reads your GPU and
  its VRAM through DXGI; subtracts what the game needs when both share the
  GPU; and recommends models for that budget from the
  [leaderboard](https://spacegho.st/mods/ffxiv/almanac/)'s
  `recommendations.json`, falling back to a
  [bundled list](benchmark/recommendations.json) offline. Every model shows
  whether it calls tools natively, through a prompted fallback, or is
  unknown until you press **Test tool calling**.
- **Chat** (`/almanac`, or `/almanac <question>`): threads kept in SQLite,
  follow-ups, branching a thread from any message, streaming replies, and
  the agent loop running in the plugin against your model with XivMcp's tools
  (a small, standard or full tool set). Anything that changes your game still
  goes through XivMcp's in-game approval.
- **Benchmark** (`/almanac bench`): the [suite](#community-benchmark) against
  your model, with mock game data or live through XivMcp; results stay in
  SQLite and are shared only when you press **Share** and confirm the exact
  JSON. Sharing needs a one-time sign-in: the window shows a code and opens
  spacegho.st in your browser to approve it.
- **Almanac engine** (power users): point the plugin at an almanac gateway
  (`http://127.0.0.1:41881/v1` and its token) to get the engine's model
  residency and memory guard while keeping the in-game chat.
- **XivMcp link:** XivMcp's `feature/local-model` build hands Almanac its own
  named client token over Dalamud IPC, so nothing is copied by hand, and its
  new *Local model* setting is picked up when Almanac has no model of its own.
  With an older XivMcp, create a client token in XivMcp and enter it under
  Almanac → Settings → XivMcp.
- **IPC for other plugins:** `Almanac.Ask` (`Func<string, bool>`) opens the
  chat and sends a question; `Almanac.ApiVersion` (`Func<int>`).
- **Extension point:** tools reach the agent through `IToolSource`
  (`dalamud/src/Almanac.Core/Tools/ToolSources.cs`). XivMcp and the
  benchmark's mock tools implement it today; sandboxed WebAssembly tools are
  meant to plug in as another source.

### Install (one click, from the plugin repository)

Almanac is published in the author's own third-party plugin repository, next to
the other FFXIV mods here. In game:

1. `/xlsettings` → **Experimental** → **Custom Plugin Repositories** → paste

   ```
   https://spacegho.st/mods/ffxiv/plugins.json
   ```

   → **+** → **Save and Close**.
2. `/xlplugins` → **All Plugins** → search **Almanac** → **Install**.
3. Updates arrive like any other plugin's. To take test builds ahead of a
   release, tick **Testing** on Almanac's entry in `/xlplugins`; that opts this
   one plugin in, and nothing else.

The page at <https://spacegho.st/mods/ffxiv/plugins/> walks through the same
steps and says what every other mod in the repository is.

This is a third-party repository, not the official Dalamud one: Dalamud will
warn you that nobody but the author has reviewed it, which is true.

### Install (build it yourself, the dev plugin path)

You do not need the repository above. Building it yourself takes a .NET SDK and
about a minute, and is the same path the author develops on — good for friends
who want to read the code first, and for anyone who wants to change it.

1. Install the .NET 10 SDK, then build:
   `dotnet build dalamud/Almanac.Dalamud.slnx -c Release`. The plugin lands in
   `../almanac-dalamud-build/artifacts/bin/Almanac.Plugin/release/Almanac.dll`
   (outside the checkout; set `ALMANAC_ARTIFACTS` to change it).
2. In game: `/xlsettings` → **Experimental** → **Dev Plugin Locations** → add
   the full path of `Almanac.dll` (under Wine, `Z:\path\to\Almanac.dll`) →
   **Save and Close**.
3. `/xlplugins` → **Dev Tools** → **Installed Dev Plugins** → enable
   **Almanac**. The setup wizard opens.
4. Wizard: **1** pick the detected server (or type a URL) → **Next**; **2**
   check the GPU, tick *The game runs on this GPU too* if it does, copy a
   recommended model's `ollama pull` command and run it in a terminal →
   **Next**; **3** pick the model, press **Test tool calling** → **Next**;
   **4** keep *Connect automatically through XivMcp*, press **Test the
   connection** → **Finish and open the chat**.

Notes for that path:

- The build needs Dalamud's reference assemblies. If XIVLauncher is installed
  they are already there (Linux `~/.xlcore/dalamud/Hooks/dev/`, Windows
  `%APPDATA%\XIVLauncher\addon\Hooks\dev\`) and nothing else is needed. On a
  machine without them — a build box, a container, CI — run
  `dalamud/tools/fetch-dalamud.sh`, which downloads goatcorp's public
  distribution, and build with `DALAMUD_HOME` pointing at it.
- **Dev Plugin Locations** takes the `Almanac.dll` path or the folder holding
  it; the folder must also contain `Almanac.json` and the DLLs next to it, so
  point it at the build output folder, not at a copy of the DLL alone.
- Rebuilt while the game was running? `/xlplugins` → **Dev Tools** →
  **Installed Dev Plugins** → the reload arrow on **Almanac**; no relaunch.
- `dalamud/tools/package.sh` builds the same `latest.zip` the releases carry,
  if you would rather install it as a normal plugin from a file.
- The same steps work from a Windows checkout; only the paths differ.

Development: `dotnet test dalamud/tests/Almanac.Core.Tests` runs the non-UI
logic (model client, MCP client, agent loop, scorer against the shared
vectors, setup detection, SQLite store) with no game and no network.

## Community benchmark

[`benchmark/`](benchmark/) holds a fixed, versioned suite of FFXIV tasks
(`ffxiv-core` 1.0.0: location, weather, aetherytes, map flag, quest lookup,
slash-command formatting, restraint and a multi-step task), the exact
[scoring rules](benchmark/README.md), and the JSON schemas of a result and of
the recommendations. Both clients implement the same rules and are tested
against the same [scoring vectors](benchmark/testdata/scoring-vectors.json):

- in game: **Almanac → Benchmark**;
- headless: `almanac bench --model qwen3:8b` (mock tools, no game needed), or
  `almanac bench --live --model qwen3:8b` against XivMcp; `--submit` shows the
  JSON and asks before sending it, and signs you in through the browser the
  first time (see benchmark/README.md; `--unlink` signs out).

A result scores tool-call success and validity, answer quality (exact match
and rubric checks), tokens per second, time to first token and peak VRAM.
A submitted result names nobody: GPU model, VRAM, rounded RAM, OS family, backend,
model, quantisation, context and scores; no names, paths or addresses. You sign in
to send one, so the server knows which account it came from. The
leaderboard at <https://spacegho.st/mods/ffxiv/almanac/> turns them into
per-VRAM-tier recommendations.

## FFXIV and Dalamud (engine)

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
  Ghostty for Dalamud's `/ask` panel runs `almanac ask --stream-json` in a
  thread and shows the answer as chat bubbles, with follow-ups.

## Autopilot

`almanac autopilot` is an autonomous loop that works through a queue of
coding tasks day and night and keeps you posted in game. Local models (free,
on your GPUs) do the planning, triage, reviews, summaries and, when the cloud
budget is spent, the coding too. Cloud coding sessions (Claude Code or Codex,
headless) do real code changes within hard per-run and per-day caps. It never
pushes to your main branch: every change arrives as a **draft pull request**
with test evidence, and CI must pass before a task counts as done.

**Nothing leaves the sandbox without your yes.** A task's first coding session
and every push are approval-gated, game actions are gated by XivMcp, and each
request waits in the queue database until you answer - minutes or days later,
across restarts and reboots. `almanac autopilot allow` opens a sudo-like
5-minute window when you want to clear a pile in one go.

It is off until you start it, and `dry_run = true` is the default.

```
 sources (every 15 min)                       queue.sqlite (tasks, plan steps, log, spend)
 ---------------------------------            ------------------------------------------------
 GitHub issues labelled "autopilot" --+
 inbox file "- [ ] ..." items -------+-->  task: queued -> ready -> (waiting on ticket) -> review -> done
 vote site top ideas (read-only) ----+       |                                                  \-> needs you
 failing CI on the base branch ------+       v
 almanac autopilot add / MCP --------+    dispatcher: highest value first, up to max_parallel steps at once
                                             |
      +--------------------------------------+-------------------------------------------+
      | plan / local / summaries      code                         review          test / pr / ci / game
      v                               v                            v               v
  model pool: planner role     cloud: claude -p | codex exec   pool: reviewer   repo's own test entry point,
  (failover between backends)  (caps, turns, wall time)        role, not the    git push autopilot/<task>,
                               |  cap / rate limit / quota /   author's GPU     gh pr create --draft,
                               |  auth error detected          PR comment,      gh pr checks,
                               v                               fix round        XivMcp request_action -> ticket
                              local coder (Codex or aider on
                              a local model) on a coder backend
                              lease; tasks split smaller
      all coding and tests run in <state>/autopilot/worktrees/<repo>-<task> (bubblewrap when installed)
                                             |
                                             v
          XivMcp (in game): agent board, "needs you" tracker objectives, morning digest toast
```

### How a task runs

1. **Plan.** A local model turns the task into steps (`local`, `code`,
   `game_read`, `game_action`). Rules no model can override: code only for an
   allow-listed repo; every code step is followed by the repo's tests; a plan
   that changes code ends with *draft PR*, *cross-review*, *CI*. If no model
   answers, a fixed plan is used.
2. **Code.** On the cloud while today's caps allow (`coding_runs_per_day`,
   `tokens_per_day`, `cost_usd_per_day`; per run `max_turns` and
   `max_wall_minutes`). When a session fails with a usage limit, rate limit,
   quota/billing or authentication error (detected from the CLI's output),
   cloud coding is switched off (30 minutes for a rate limit, until the next
   day otherwise) and the same step continues on a **local coder** (Codex CLI
   pointed at the local model, or aider) in the same worktree. Local coders
   get smaller jobs: a code step is split into up to `local_split_parts`
   parts, each followed by the tests. Cloud is used again once the block
   expires or the day rolls over.
3. **Test.** The repo's own entry point (`test = [...]`), in the worktree. A
   failure keeps the output as evidence and inserts a fix step and a re-test.
4. **Draft PR.** Push `autopilot/<id>-<slug>` (never the base or a protected
   branch, never `--force`), `gh pr create --draft` with a summary, the test
   output, remaining risks, and labels naming who wrote it (`autopilot`,
   `by:claude`, `by:local-qwen3.5-9b-<backend>`).
5. **Cross-review.** A local model on a different backend than the author
   reviews the diff and comments on the PR. If it asks for changes (bugs,
   missing tests), a fix round follows (`review_rounds`).
6. **CI.** `gh pr checks` until it passes (done), fails (fix round) or reports
   nothing (needs you).

Failures back off exponentially (`backoff_base_seconds` doubling up to
`backoff_max_seconds`); after `max_attempts` the task stops and shows up
under "needs you". Waiting for a free backend, a closed game or running CI is
not a failure.

### Model pool

`[autopilot.pool.<name>]` lists model backends: almanac gateways on other
machines (`kind = "almanac"`, with that gateway's token file), Ollama
servers, or any OpenAI-compatible server. Each has `roles` (`planner`,
`coder`, `reviewer`), `slots`, `priority`, and optional rules:
`unavailable_while_process = ["game.exe"]` keeps a GPU free while that
process runs on this machine (a job already on it is stopped within seconds
and an Ollama backend is asked to unload), `check_command` for rules about
another machine, `enabled = false`. Backends are health-checked (`/healthz`,
`/api/version` or `/v1/models`) and short calls fail over to the next one.
Long jobs take a lease, so a coder on one GPU and a reviewer on another run
at the same time as a cloud session. `almanac autopilot pool` shows health
and load. With no pool configured, the `[gateway]` backend is used.

`coder_model` gives a backend a different model for coding sessions, and
`context` (default 32768) says what context it actually serves — the local
coder is told, so it compacts instead of silently overflowing.

### Local coding sessions

`[autopilot.local_coder] tool` is `codex` (the default), `aider`, or a
`custom` argv with `{prompt} {model} {base_url} {worktree} {test} {context}
{compact}` placeholders. The session runs in the task's worktree, against the
leased backend's OpenAI-compatible endpoint, and commits nothing autopilot
cannot see: whatever it leaves uncommitted is committed after the session, and
the repo's tests decide whether the step passed.

The `codex` preset is Codex CLI pointed at the local endpoint, and every option
in it exists because of an observed failure against Ollama:

- `web_search="disabled"` — Ollama serves no web search, so the built-in
  `web_search` tool ends the session: the model calls it, Ollama answers
  `ollama cloud is disabled: web search is unavailable` and the stream drops.
- `model_context_window` / `model_auto_compact_token_limit` — codex has no
  catalogue entry for a local model (it says so and uses fallback metadata),
  assumes a large context and never compacts.
- `--disable` for goals, sub-agents, images, apps, skills, hooks and plugins —
  a small model picks the wrong tool more often the longer the tool list is,
  and each definition costs context. What is left is
  `exec_command`/`write_stdin`: the model edits files by writing them from the
  shell. It will sometimes try `apply_patch`, which codex does not register for
  a model without catalogue metadata; the call fails and the model writes the
  file instead.
- `CODEX_HOME` is autopilot's own directory (`<state>/autopilot/codex`, or
  `codex_home`), never the owner's `~/.codex`, whose model, hooks, plugins and
  MCP servers are for interactive use.

`aider` is a fallback: `aider-chat` requires Python < 3.13, so it needs its own
interpreter (a container or a managed Python) on a host that ships a newer one.

### FFXIV

Through [XivMcp](#ffxiv-and-dalamud), configured as `[upstreams.<name>]` and
named in `[autopilot.game] upstream`:

- **Observe and UI, freely:** read-tier tools in `game_read` steps, progress
  on the agent board (`post_status`), a toast with the morning digest, and
  pending approvals plus "needs you" items as quest-tracker objectives when
  the server lists an objective tool (feature-detected from `tools/list`).
- **Actions only through approvals:** a `game_action` step calls XivMcp's
  `request_action` and gets a ticket. The step is parked (the task waits,
  other tasks keep running), the ticket is polled with `get_ticket`, and the
  plan resumes right after that step once you approve it in game, now or
  later. Denied, expired or cancelled tickets re-plan the task without that
  action. Autopilot never calls action or chat tools directly and never
  answers an approval itself.
- **No gameplay automation, ever.** Automating movement, combat, gathering,
  crafting, trading, the market board or chat breaks FINAL FANTASY XIV's
  terms of service. Tools that look like these are refused even if a plan
  asks for them, and dry-run never files tickets.

### Controls

```sh
almanac autopilot run [--once] [--dry-run|--live]   # foreground loop
almanac autopilot status [--json]                   # state, spend vs caps, what waits on you
almanac autopilot list [--all]
almanac autopilot log <task>                        # plan, step states, event log
almanac autopilot add "fix the resize flicker" --repo my-plugin [--priority 80]
almanac autopilot pause | resume                    # stop/start taking new steps
almanac autopilot stop                              # exit after the current step
almanac autopilot stop --now                        # kill switch: running sessions die within a second
almanac autopilot retry <task> | cancel <task>
almanac autopilot pool                              # model backends
almanac autopilot digest                            # write the digest now

almanac autopilot pending                           # what waits for your yes, oldest first
almanac autopilot show ap-3                         # exactly what that one would do
almanac autopilot approve ap-3 [--minutes 5]        # yes (and optionally open a window)
almanac autopilot deny ap-3 [--reason "not now"]
almanac autopilot approve all | deny all
almanac autopilot allow [--minutes 5] [--scope all|code|push|game]
```

The kill switch is a file (default `~/.local/state/almanac/autopilot/STOP`,
`[autopilot] kill_switch`). While it exists nothing runs and the service is
not restarted; delete it to allow runs again. Over MCP, `autopilot_status` and
`autopilot_pending` are read tools; `autopilot_add`, `autopilot_pause` and
`autopilot_approve` are change tools and need confirmation like every other
change.

### Approval

```toml
[autopilot.approval]
require = ["code", "push", "game_action"]   # remove a kind to stop asking for it
code_scope = "task"                          # one yes per task | "step": every session
allow_session_minutes = 5                    # `almanac autopilot allow` default
max_allow_session_minutes = 60               # a longer --minutes is clamped to this
```

| Gate | When it asks | What approving means |
| --- | --- | --- |
| `code` | before a task's first coding session | that task may edit its own worktree, all night if it wants |
| `push` | before every `git push` and draft PR | this branch goes to GitHub as a draft; nothing is merged |
| `game_action` | before any XivMcp action tool | XivMcp's own ticket, approved in game |

A request is a row in `queue.sqlite`: it survives `stop`, a crash and a reboot,
and the step that asked parks on it (the loop keeps working on other tasks).
When the answer arrives the step **runs from where it stopped** - approval
unblocks a step, it never stands in for it. A denial is final: the task goes to
"needs you" with your reason and is never retried on its own. Pending items are
also pushed to the in-game quest tracker, so you can see them without leaving
the game, and `autopilot_pending` / `autopilot_approve` answer them from a chat
with almanac.

An allow session is deliberately dumb: a timestamp in the database, matching
requests approved as they arrive while it lasts, nothing extended or renewed
implicitly. `--scope code` does not cover pushes.

### Safety model

- **Where code runs:** only in per-task git worktrees under
  `<state>/autopilot/worktrees`, never in your checkout. With bubblewrap
  installed (`[autopilot.sandbox] mode = "auto"`), coding sessions and tests
  see a read-only filesystem except the worktree, the repo's git directory and
  the coding CLI's own state; `~/.ssh`, almanac's token, `gh` and `op` config
  are hidden; `no_new_privs` means `sudo` cannot work. Without bubblewrap the
  same environment rules apply and the coding CLI's own permission rules
  (Claude Code `--disallowedTools` for sudo, ssh, `git push`, `gh`, `op`;
  Codex `--sandbox workspace-write`) are the fence.
- **ssh:** hidden from sessions unless a repo sets `allow_ssh_hosts`.
- **Secrets:** `[autopilot] secrets` maps variable names to `env:NAME` or
  `op://vault/item/field` (1Password CLI, read when a session starts); literal
  values are refused. Sessions get only `pass_env` plus those secrets.
  Resolved values are masked in every log line, PR body and digest.
- **GitHub:** sources use an allow-list of read-only `gh` subcommands. Writes
  are limited to pushing the task branch, opening a draft PR, labelling it and
  commenting reviews on it, and none of that happens in dry-run.
- **Approval:** see above. Falling back to a local coder is not a way around a
  gate: the gate is checked before either coder starts.
- **Repositories:** only the paths listed under `[autopilot.repos.*]`, and a
  repo whose path is inside `[autopilot] deny_paths` (`~/.config`, `~/.ssh`,
  `~/.gnupg`, `~/.claude`, `~/.codex`, almanac's own state and knowledge repo,
  `~/.xlcore` by default) is dropped at load time and named in `status`.
- **Hard caps:** `max_parallel` steps in flight, `cloud_slots` cloud sessions,
  `max_turns` / `max_wall_minutes` per session, the daily run/token/cost caps,
  `max_files_per_task` (a bigger branch goes to you instead of a PR) and
  `min_free_memory_mb` (coding and test steps wait rather than compete for
  memory with whatever else the machine is doing).
- **Never:** force-push, push to a protected or base branch, push a branch that
  is not `autopilot/<task>`, merge, deploy, or push at all before a `push`
  approval and a passing test step.
- **Audit:** coding sessions, limits hit, PRs, tickets, approvals asked and
  answered, and failures go to `audit.jsonl` (`almanac audit`) as well as the
  task log and `<state>/autopilot/autopilot.log`, a plain-text append-only
  line per action with timestamps.

### Adding repositories and sources

```toml
[autopilot.repos.my-plugin]
path = "~/src/my-plugin"          # your checkout; autopilot only adds worktrees and branches to it
github = "you/my-plugin"
remote = "origin"                 # "" = no remote: work stays on a local branch, no PR ("needs you")
base = "master"
test = ["tests/run.sh"]           # required: without it tasks for this repo go to "needs you"
setup = []                        # optional command before tests
coder = "claude"                  # or "codex"
allow_ssh_hosts = []              # hosts a session may ssh to
priority = 0                      # added to every task's value
```

Sources: `[autopilot.sources.github]` (label, default `autopilot`),
`[autopilot.sources.inbox]` (a Markdown file: `- [ ] text @repo !high`),
`[autopilot.sources.vote]` (a vote site's public `api/tallies` and
`ideas.json`; top N by 2 x want + maybe - skip), `[autopilot.sources.ci]`
(latest failed run on each repo's base branch). Tasks from all sources are
de-duplicated, so polling is safe.

### Costs and caps

Local work (planning, reviews, triage, tests, local coding) costs nothing but
electricity. Cloud spend is bounded per day by `coding_runs_per_day`,
`tokens_per_day` (input + output + cache writes, as reported by the CLI) and
`cost_usd_per_day` (Claude Code reports cost; Codex usage is priced with
`[autopilot.codex] usd_per_mtok`, 0 if unset), and per run by `max_turns`
and `max_wall_minutes`. Caps are checked before a session starts, so one run
can overshoot the token or cost cap by at most its own size; keep
`max_turns` small. `almanac autopilot status` and the morning digest show
today's spend.

### Enabling it

```sh
deploy/install.sh --no-start                     # installs almanac-autopilot.service, does not enable it
$EDITOR ~/.config/almanac/config.toml             # [autopilot] with repos, caps, pool; keep dry_run = true
almanac autopilot add "try the pipeline" --repo my-plugin
almanac autopilot run --once --dry-run           # repeat and read `almanac autopilot log 1`
systemctl --user enable --now almanac-autopilot  # when the dry runs look right; then set dry_run = false
```

For running it in an Incus container, see `deploy/incus/README.md`.

## Layout

```
src/almanac/      config, kb (index), tools (TOML runner), service (confirm+audit),
                  mcp_server, gateway, codex_compat, guard, residency, agent, chat, threads,
                  upstream, cli, local (`almanac local` / `ai`)
src/almanac/autopilot/  store (queue), sources, planner, pool, budget, coder, sandbox,
                  git, game, runner, digest, app/cli/mcp_tools
examples/         knowledge/ and tools/ to copy from
deploy/           install.sh, systemd user units, Quadlet, Incus profile
docs/TOOLS.md     tool file reference
tests/            pytest, no network
dalamud/          the Almanac Dalamud plugin (C#): Almanac.Core (no game), Almanac.Plugin, tests
benchmark/        suite, scoring rules, schemas, bundled recommendations, scoring vectors
```

Development: `python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest`.

## Changelog

`changelog.json` at the top of the repository is the changelog. It is the
single source of truth for both places a user reads it:

- **In game** — the plugin embeds it (`Almanac.Core.Changelog`) and the
  Settings window shows it under **What's new**.
- **[CHANGELOG.md](CHANGELOG.md)** — generated from it:

  ```sh
  tools/changelog.py           # rewrite CHANGELOG.md from changelog.json
  tools/changelog.py --check   # what CI runs: fails, with a diff, on drift
  ```

The convention, and it is not optional: **every change a user can see adds or
edits its entry in `changelog.json` in the same commit as the change**, and
regenerates `CHANGELOG.md`. Never edit `CHANGELOG.md` by hand. CI runs the
check on every push, and `tests/test_changelog.py` runs it under pytest.

A status word means exactly the same thing here as it does in Ghostty for
FFXIV's changelog, and nothing more:

| Status | Shown | Means |
| --- | --- | --- |
| `next` | SOON | still being built; not merged. |
| `beta` | BETA | merged, but **not yet verified** where it has to run — in game, or against a real model server. |
| `new` / `fix` | NEW / FIX | in a numbered release: seen working. |

An entry keeps its `beta` until the thing it describes has actually been
observed working; say what is unverified in the entry itself, the way
"Untested" below does, rather than writing around it.

## Roadmap

- `feature/autopilot`: an overnight loop that plans with the local model pool
  and runs coding sessions in sandboxed worktrees (not merged yet).
- WebAssembly tools: sandboxed tool modules behind the plugin's
  `IToolSource` and the engine's tool runner.
- A plugin repository entry so players can install Almanac without building
  it.

## Untested

- MCP elicitation with a real interactive client (tested with the MCP SDK's
  client only, on both the handshake protocol and 2026-07-28; Claude Code and
  Codex were tested calling read tools, with MCP SDK 1.x on the server).
- `claude mcp add` with the `${ALMANAC_TOKEN}` header (the same header in a
  `--mcp-config` file was tested).
- `deploy/quadlet` and `deploy/incus` on real hardware.
- Embeddings (`[kb] embed_model`) against a real embedding model.
- The screenshot helper on desktops other than GNOME.
- The Almanac plugin in a running game: the windows, DXGI GPU detection
  (Windows and Wine/DXVK), the SQLite native library inside Dalamud, the XivMcp
  IPC connection and live benchmark mode. Its Core logic is unit-tested with
  fakes; nothing has been run against a real model server or in game yet.
- `almanac bench` against a real model server and live XivMcp.
- The leaderboard API itself (`/mods/ffxiv/almanac/api/results`).
- Autopilot has only run against fakes and in dry-run against a scratch git
  repository: no real `claude -p`, `codex exec`, aider, `gh pr create`, push,
  XivMcp approval ticket or remote model backend has been exercised. The
  ticket tools (`request_action`, `get_ticket`) follow the interface XivMcp
  is adding on its deferred-approvals branch; argument names are read from the
  tool's schema, but the result shape is assumed (`ticket_id`/`id`,
  `state`/`status`, `result`). No quest-tracker objective tool exists in
  XivMcp yet, so that path is feature-detected and untested. The bubblewrap
  profile has not been run with the real coding CLIs.
- `almanac local` / `ai`: `status`, `ask` and one-shot `code -p` were run
  against two real backends (almanac gateways in front of Ollama). The
  interactive Codex session was only seen to start, not used for a typed turn;
  `ai chat` was not driven by a person; `kind = "ollama"` and `kind = "openai"`
  backends, a second machine (laptop) and the gateway's `inflight` busy signal
  on a deployed gateway are tested with fakes only. `ai claude` rests on four
  tiny tasks (see "Claude Code on the local model").
