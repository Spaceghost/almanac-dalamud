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
                              local coder (aider or Codex on
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
   day otherwise) and the same step continues on a **local coder** (aider, or
   Codex CLI pointed at the local model) in the same worktree. Local coders
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
base = "main"
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
                  mcp_server, gateway, codex_compat, guard, residency, agent, chat,
                  upstream, cli
src/almanac/autopilot/  store (queue), sources, planner, pool, budget, coder, sandbox,
                  git, game, runner, digest, app/cli/mcp_tools
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
- Autopilot has only run against fakes and in dry-run against a scratch git
  repository: no real `claude -p`, `codex exec`, aider, `gh pr create`, push,
  XivMcp approval ticket or remote model backend has been exercised. The
  ticket tools (`request_action`, `get_ticket`) follow the interface XivMcp
  is adding on its deferred-approvals branch; argument names are read from the
  tool's schema, but the result shape is assumed (`ticket_id`/`id`,
  `state`/`status`, `result`). No quest-tracker objective tool exists in
  XivMcp yet, so that path is feature-detected and untested. The bubblewrap
  profile has not been run with the real coding CLIs.
