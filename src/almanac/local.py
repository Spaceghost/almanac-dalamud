"""``almanac local`` (also installed as ``ai``): the front door to your own models.

Claude ran out?

    ai                  # what is up, pick the best backend, start a session
    ai code [path]      # coding session in a repo (Codex CLI, hardened local preset)
    ai ask "question"   # one answer; pipe a file in with  ai ask "explain" < file

Also: ``ai status``, ``ai chat``, ``ai use <backend>|auto``, ``ai webui``,
``ai limits`` and the opt-in ``ai claude``.

Backends come from ``[local.backends.<name>]`` in the config; without that
section the ``[autopilot.pool.*]`` backends are used, and without those the
``[gateway]`` on this machine. Nothing is hardcoded, so the same command works
from every machine that has the config and the token files.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import httpx

from .config import Config

HEALTH_PATHS = {"almanac": "/healthz", "ollama": "/api/version", "openai": "/v1/models"}
PROBE_PROMPT = "Count from 1 to 30, separated by spaces. Output only the numbers."
CHAT_SYSTEM = (
    "You are a local assistant running on the owner's own GPU. Be direct and brief. "
    "You have no tools and no internet: say so rather than guessing at facts you cannot check."
)

LIMITS = """\
What to expect (a ~9B local model is not Claude):
  good at   focused edits in one or two files, writing and fixing tests, explaining
            code and errors, shell/git/regex help, summaries, commit messages
  weak at   large refactors, reasoning across many files, long sessions (it forgets
            once the context fills), unfamiliar APIs (it invents them), subtle bugs
  so        give it one small task with the file names and the test command, read
            every diff, and keep the big jobs for when Claude is back"""

CLAUDE_WARNING = """\
Claude Code on the local model: opt-in and lightly tested (README "When Claude runs out").
  - It runs with --bare and its own config directory: no plugins, MCP servers, hooks,
    CLAUDE.md or memory. Bare, the opening request is ~1.5k tokens with three tools
    (Bash, Edit, Read). Measured on a Quadro P4000 with qwen3.5:9b: three tiny
    red->green tasks passed (31-67 s each), no failed tool calls.
  - Do not point your normal `claude` at the gateway instead: its full tool list
    (24 tools) is ~28k tokens, which fills a 32k context before your first word.
    It passed one trivial fix in 119 s and has no room left for real work.
  - Nothing larger than a one-file change was tested. `ai code` (Codex, hardened
    preset) is the path with more mileage."""


# -- backends ----------------------------------------------------------------
@dataclass
class LocalBackend:
    name: str
    url: str
    kind: str = "almanac"  # almanac (gateway) | ollama | openai
    model: str = "qwen3.5:9b"
    context: int = 32768  # tokens the backend actually serves
    gpu: str = ""  # free text, shown in status
    expected_tok_s: float = 0.0  # measured once, written down; shown before a session starts
    token_file: str = ""
    token_env: str = ""
    priority: int = 0  # higher first
    enabled: bool = True

    @classmethod
    def from_config(cls, name: str, raw: dict[str, Any]) -> "LocalBackend":
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__ and k != "name"}
        if raw.get("coder_model"):  # an [autopilot.pool] entry: code with its coding model
            known["model"] = raw["coder_model"]
        return cls(name=name, **known)

    @property
    def base_url(self) -> str:
        return self.url.rstrip("/")

    def token(self) -> str:
        """The bearer token. Never log or print the result."""
        if self.token_env:
            return os.environ.get(self.token_env, "")
        if self.token_file:
            path = Path(os.path.expandvars(self.token_file)).expanduser()
            return path.read_text().strip() if path.is_file() else ""
        return ""

    def headers(self) -> dict[str, str]:
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}


def load_backends(cfg: Config) -> list[LocalBackend]:
    """``[local.backends]``, else ``[autopilot.pool]``, else this machine's ``[gateway]``."""
    declared = cfg.section("local").get("backends") or cfg.section("autopilot").get("pool") or {}
    backends = [LocalBackend.from_config(name, dict(raw)) for name, raw in declared.items()]
    if not backends:
        gateway = cfg.section("gateway")
        listen = str((gateway.get("listen") or ["127.0.0.1:41881"])[0])
        backends = [LocalBackend("this-machine", f"http://{listen}", "almanac", str(gateway["default_model"]),
                                 int(cfg.section("agent").get("context_tokens", 8192)), token_file=str(cfg.raw["token_file"]))]
    return [b for b in backends if b.enabled]


# -- probing -----------------------------------------------------------------
@dataclass
class Probe:
    backend: LocalBackend
    up: bool = False
    reason: str = ""
    loaded: list[str] = field(default_factory=list)
    inflight: int | None = None  # requests in flight, when the gateway reports it
    tok_s: float | None = None
    ttft_s: float | None = None
    probe_error: str = ""

    @property
    def busy(self) -> bool:
        return bool(self.inflight) or self.probe_error.startswith("timed out")

    @property
    def state(self) -> str:
        if not self.up:
            return f"DOWN: {self.reason}"
        if self.inflight:
            return f"busy ({self.inflight} request{'s' if self.inflight != 1 else ''} in flight)"
        if self.probe_error:
            if self.busy and self.backend.kind == "almanac" and not self.loaded:
                return f"loading the model ({self.probe_error}); try again in a minute"
            return f"busy? ({self.probe_error})" if self.busy else f"up, probe failed ({self.probe_error})"
        if self.backend.kind == "almanac" and not self.loaded:
            return "free (model not loaded: first reply waits for the load)"
        return "free"


StreamFn = Callable[[LocalBackend, dict[str, Any], float], Iterable["str | dict[str, Any]"]]


def stream_chat(backend: LocalBackend, payload: dict[str, Any], timeout: float) -> Iterator[str | dict[str, Any]]:
    """Content deltas (str) of one streamed chat completion, then its usage (dict) when the server reports it."""
    body = {**payload, "model": backend.model, "stream": True, "stream_options": {"include_usage": True}}
    with httpx.stream("POST", f"{backend.base_url}/v1/chat/completions", json=body, headers=backend.headers(),
                      timeout=httpx.Timeout(timeout, connect=5)) as response:
        if response.status_code >= 400:
            response.read()
            raise httpx.HTTPStatusError(f"HTTP {response.status_code}: {response.text[:200]}", request=response.request, response=response)
        for line in response.iter_lines():
            if not line.startswith("data:") or line.strip() == "data: [DONE]":
                continue
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            if event.get("usage"):
                yield dict(event["usage"])
            choices = event.get("choices") or [{}]
            delta = (choices[0].get("delta") or {}).get("content") or ""
            if delta:
                yield delta


def check_health(backend: LocalBackend, get: Callable[..., Any] | None = None) -> Probe:
    probe = Probe(backend)
    try:
        response = (get or httpx.get)(backend.base_url + HEALTH_PATHS.get(backend.kind, "/v1/models"), headers=backend.headers(), timeout=5)
    except (httpx.HTTPError, OSError) as exc:
        probe.reason = f"unreachable ({exc.__class__.__name__})"
        return probe
    status = int(getattr(response, "status_code", 500))
    if status >= 400:
        hint = " (token missing or wrong)" if status in (401, 403) else ""
        probe.reason = f"HTTP {status}{hint}"
        return probe
    probe.up, probe.reason = True, "healthy"
    try:
        data = response.json()
    except ValueError:
        data = {}
    if isinstance(data, dict):
        probe.loaded = [str(m) for m in data.get("loaded") or []]
        if isinstance(data.get("inflight"), int):
            probe.inflight = data["inflight"]
    return probe


def measure_speed(probe: Probe, stream: StreamFn = stream_chat, timeout: float = 45.0, clock: Callable[[], float] = time.monotonic) -> Probe:
    """Generate 64 tokens and time them, first byte of the request to last token (so a little conservative).

    The token count is the server's ``usage``; a streamed delta is often more
    than one token, so deltas are only the fallback.
    """
    payload = {"messages": [{"role": "user", "content": PROBE_PROMPT}], "max_tokens": 64, "temperature": 0, "reasoning_effort": "none"}
    start = clock()
    first = last = None
    count = reported = 0
    try:
        for item in stream(probe.backend, payload, timeout):
            if isinstance(item, dict):
                reported = int(item.get("completion_tokens") or 0)
                continue
            last = clock()
            first = first if first is not None else last
            count += 1
    except httpx.TimeoutException:
        probe.probe_error = f"timed out after {timeout:.0f}s"
        return probe
    except (httpx.HTTPError, OSError) as exc:
        probe.probe_error = str(exc)[:80] or exc.__class__.__name__
        return probe
    if first is None or last is None or count < 2 or last <= first:
        probe.probe_error = "no tokens came back"
        return probe
    probe.ttft_s = first - start
    probe.tok_s = reported / (last - start) if reported else (count - 1) / (last - first)
    return probe


def discover(backends: list[LocalBackend], speed: bool = True, get: Callable[..., Any] | None = None,
             stream: StreamFn = stream_chat, timeout: float = 45.0) -> list[Probe]:
    def one(backend: LocalBackend) -> Probe:
        probe = check_health(backend, get)
        return measure_speed(probe, stream, timeout) if (speed and probe.up and not probe.inflight) else probe

    if not backends:
        return []
    with ThreadPoolExecutor(max_workers=len(backends)) as pool:
        return list(pool.map(one, backends))


def choose(probes: list[Probe], preferred: str = "") -> Probe | None:
    """The preferred backend if it is up; else free before busy, then priority, then speed."""
    up = [p for p in probes if p.up]
    for probe in up:
        if probe.backend.name == preferred:
            return probe
    up.sort(key=lambda p: (p.busy, -p.backend.priority, -(p.tok_s or p.backend.expected_tok_s), p.backend.name))
    return up[0] if up else None


# -- the pinned choice (`ai use`) ---------------------------------------------
def _use_file(cfg: Config) -> Path:
    return cfg.state_dir / "local-use"


def preferred_name(cfg: Config, override: str = "") -> str:
    if override:
        return override
    path = _use_file(cfg)
    if path.is_file():
        return path.read_text().strip()
    return str(cfg.section("local").get("default", ""))


# -- text ----------------------------------------------------------------------
def _ctx(tokens: int) -> str:
    return f"{tokens // 1024}k" if tokens >= 1024 else str(tokens)


def _speed(probe: Probe) -> str:
    expected = probe.backend.expected_tok_s
    if probe.tok_s is not None:
        return f"{probe.tok_s:.0f} tok/s now" + (f" (usual ~{expected:g})" if expected else "")
    return f"~{expected:g} tok/s usual" if expected else "speed unknown"


def status_table(probes: list[Probe], chosen: Probe | None) -> str:
    rows = [("", "BACKEND", "GPU", "MODEL", "CTX", "SPEED", "STATE")]
    for p in probes:
        b = p.backend
        rows.append(("*" if p is chosen else "", b.name, b.gpu or "-", b.model, _ctx(b.context), _speed(p) if p.up else "-", p.state))
    widths = [max(len(r[i]) for r in rows) for i in range(6)]
    return "\n".join("  ".join(cell.ljust(widths[i]) if i < 6 else cell for i, cell in enumerate(row)).rstrip() for row in rows)


def one_line(probe: Probe) -> str:
    b = probe.backend
    return f"[{b.name}: {b.model}{f' on {b.gpu}' if b.gpu else ''}, {_ctx(b.context)} context, {_speed(probe)}]"


def banner(probe: Probe) -> str:
    b = probe.backend
    words = int(b.context * 0.75 / 1000)
    return (f"local model  {b.model}{f' on {b.gpu}' if b.gpu else ''}  ({b.name})\n"
            f"context      {_ctx(b.context)} tokens, about {words}k words, shared by your files and the whole conversation\n"
            f"speed        {_speed(probe)}; Claude is several times faster\n{LIMITS}")


# -- sessions --------------------------------------------------------------------
def codex_argv(backend: LocalBackend, worktree: Path, prompt: str, interactive: bool, extra: list[str] | None = None) -> list[str]:
    """Autopilot's hardened Codex preset (LOCAL_PRESETS["codex"]), for a person instead of a job.

    The preset is ``codex exec --json ... {prompt}``. Interactive use drops
    ``exec``/``--json`` (and the prompt when there is none); every hardening
    option stays as autopilot ships it.
    """
    from .autopilot.coder import LOCAL_PRESETS, compact_limit

    values = {"model": backend.model, "base_url": f"{backend.base_url}/v1", "worktree": str(worktree), "prompt": prompt,
              "test": "", "context": str(backend.context), "compact": str(compact_limit(backend.context))}
    argv = [part for part in LOCAL_PRESETS["codex"] if part != "--json" and not (interactive and part == "exec")]
    if interactive and not prompt:
        argv = [part for part in argv if part != "{prompt}"]
    for key, value in values.items():
        argv = [part.replace("{" + key + "}", value) for part in argv]
    if not interactive and not (worktree / ".git").exists():
        argv.insert(2, "--skip-git-repo-check")
    at = len(argv) - 1 if prompt else len(argv)  # extra options go before the prompt
    argv[at:at] = list(extra or [])
    return argv


def claude_argv(extra: list[str] | None = None) -> list[str]:
    return ["claude", "--bare", *(extra or [])]


def claude_env(backend: LocalBackend) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": backend.base_url, "ANTHROPIC_API_KEY": backend.token() or "local",
        # any claude-* name maps to the local model in the gateway ([gateway.models])
        "ANTHROPIC_MODEL": "claude-sonnet-4-5", "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "API_TIMEOUT_MS": "900000",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "4096",
    }


def _say(text: str, quiet: bool = False) -> None:
    if not quiet:
        print(text, file=sys.stderr)


def _pick(cfg: Config, ns: argparse.Namespace, speed: bool = False) -> Probe | None:
    backends = load_backends(cfg)
    probes = discover(backends, speed=speed, timeout=float(cfg.section("local").get("probe_timeout", 45)))
    wanted = preferred_name(cfg, getattr(ns, "backend", "") or "")
    chosen = choose(probes, wanted)
    if chosen is None:
        _say("no local backend is reachable right now:\n" + status_table(probes, None))
        _say("check the tailnet (tailscale status) and the token files; `almanac local status` shows this table again")
        return None
    if wanted and chosen.backend.name != wanted:
        _say(f"{wanted} is not available ({next((p.state for p in probes if p.backend.name == wanted), 'not configured')}); using {chosen.backend.name}")
    return chosen


def run_status(cfg: Config, ns: argparse.Namespace) -> int:
    backends = load_backends(cfg)
    probes = discover(backends, speed=not ns.no_probe, timeout=float(cfg.section("local").get("probe_timeout", 45)))
    chosen = choose(probes, preferred_name(cfg))
    if ns.json:
        print(json.dumps([{"name": p.backend.name, "gpu": p.backend.gpu, "model": p.backend.model, "context": p.backend.context,
                           "up": p.up, "state": p.state, "busy": p.busy, "tok_s": p.tok_s, "ttft_s": p.ttft_s,
                           "expected_tok_s": p.backend.expected_tok_s, "chosen": p is chosen} for p in probes], indent=1))
        return 0 if chosen else 1
    print(status_table(probes, chosen))
    pinned = preferred_name(cfg)
    if chosen:
        print(f"\n* = the one a session would use now{f' (pinned: {pinned}; `ai use auto` to unpin)' if pinned else ''}")
    else:
        print("\nnothing reachable: check the tailnet (tailscale status) and the token files")
    webui = str(cfg.section("local").get("webui_url", ""))
    if webui:
        print(f"browser chat (Open WebUI): {webui}")
    return 0 if chosen else 1


def run_ask(cfg: Config, ns: argparse.Namespace, stream: StreamFn = stream_chat) -> int:
    question = " ".join(ns.question)
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            question = f"{question}\n\n```\n{piped}\n```" if question else piped
    if not question.strip():
        print('usage: ai ask "question"   (or pipe text in)', file=sys.stderr)
        return 2
    chosen = _pick(cfg, ns)
    if chosen is None:
        return 1
    _say(one_line(chosen), ns.quiet)
    return _stream_answer(chosen.backend, [{"role": "system", "content": CHAT_SYSTEM}, {"role": "user", "content": question}], stream)[0]


def _stream_answer(backend: LocalBackend, messages: list[dict[str, str]], stream: StreamFn) -> tuple[int, str]:
    parts: list[str] = []
    try:
        for delta in stream(backend, {"messages": messages, "reasoning_effort": "none"}, 900.0):
            if not isinstance(delta, str):
                continue
            parts.append(delta)
            sys.stdout.write(delta)
            sys.stdout.flush()
    except (httpx.HTTPError, OSError) as exc:
        print(f"\n[{backend.name} failed: {str(exc)[:200] or exc.__class__.__name__}]", file=sys.stderr)
        return 1, "".join(parts)
    except KeyboardInterrupt:
        print("\n[stopped]", file=sys.stderr)
    print()
    return 0, "".join(parts)


def run_chat(cfg: Config, ns: argparse.Namespace, stream: StreamFn = stream_chat) -> int:
    chosen = _pick(cfg, ns)
    if chosen is None:
        return 1
    _say(banner(chosen))
    _say("\nplain chat, no tools. /new clears the conversation, /quit or Ctrl-D leaves.\n")
    history: list[dict[str, str]] = []
    budget = int(chosen.backend.context * 3)  # ~characters of history kept: about three quarters of the context
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line in ("/quit", "/exit", "/q"):
            return 0
        if line == "/new":
            history.clear()
            continue
        if not line:
            continue
        history.append({"role": "user", "content": line})
        while len(history) > 1 and sum(len(m["content"]) for m in history) > budget:
            del history[:2]
            _say("[context is full: the oldest exchange was dropped]")
        code, answer = _stream_answer(chosen.backend, [{"role": "system", "content": CHAT_SYSTEM}, *history], stream)
        if code:
            history.pop()
        else:
            history.append({"role": "assistant", "content": answer})


def _exec(argv: list[str], env: dict[str, str], cwd: Path) -> int:
    os.chdir(cwd)
    os.execvpe(argv[0], argv, env)
    return 127  # not reached


def run_code(cfg: Config, ns: argparse.Namespace, launch: Callable[[list[str], dict[str, str], Path], int] = _exec) -> int:
    from .autopilot.coder import local_coder_env

    worktree = Path(ns.path).expanduser().resolve()
    if not worktree.is_dir():
        print(f"{worktree} is not a directory", file=sys.stderr)
        return 2
    if shutil.which("codex") is None:
        print("`ai code` needs the Codex CLI (`codex` on PATH): npm install -g @openai/codex\n`ai chat` and `ai ask` work without it.", file=sys.stderr)
        return 1
    chosen = _pick(cfg, ns)
    if chosen is None:
        return 1
    _say(banner(chosen))
    _say(f"\ncoding in {worktree} with Codex: sandboxed to this directory, no network, no web search.\n")
    home = cfg.state_dir / "local" / "codex-home"  # never the owner's ~/.codex
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    argv = codex_argv(chosen.backend, worktree, ns.prompt or "", interactive=not ns.prompt, extra=ns.extra)
    env = {**os.environ, **local_coder_env(f"{chosen.backend.base_url}/v1", chosen.backend.token(), str(home))}
    return launch(argv, env, worktree)


def run_claude(cfg: Config, ns: argparse.Namespace, launch: Callable[[list[str], dict[str, str], Path], int] = _exec) -> int:
    if not (ns.experimental or cfg.section("local").get("allow_claude")):
        print(CLAUDE_WARNING + "\n\nOpt in with `ai claude --experimental`, or allow_claude = true under [local].", file=sys.stderr)
        return 2
    worktree = Path(ns.path).expanduser().resolve()
    if shutil.which("claude") is None or not worktree.is_dir():
        print("needs `claude` on PATH and an existing directory", file=sys.stderr)
        return 1
    backends = [b for b in load_backends(cfg) if b.kind in ("almanac", "ollama")]  # they serve /v1/messages
    probes = discover(backends, speed=False)
    chosen = choose(probes, preferred_name(cfg, ns.backend or ""))
    if chosen is None:
        print("no reachable backend serves the Anthropic Messages API (kind almanac or ollama)", file=sys.stderr)
        return 1
    _say(banner(chosen) + "\n\n" + CLAUDE_WARNING + "\n")
    config_dir = cfg.state_dir / "local" / "claude-config"  # never the owner's ~/.claude
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {**os.environ, **claude_env(chosen.backend), "CLAUDE_CONFIG_DIR": str(config_dir)}
    for name in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(name, None)
    return launch(claude_argv(ns.extra), env, worktree)


def run_use(cfg: Config, ns: argparse.Namespace) -> int:
    path = _use_file(cfg)
    names = [b.name for b in load_backends(cfg)]
    if not ns.name:
        print(f"pinned: {preferred_name(cfg) or 'none (automatic)'}\nbackends: {', '.join(names)}")
        return 0
    if ns.name == "auto":
        path.unlink(missing_ok=True)
        print("automatic: the best free backend is used")
        return 0
    if ns.name not in names:
        print(f"no backend named {ns.name!r}; configured: {', '.join(names)}", file=sys.stderr)
        return 2
    path.write_text(ns.name + "\n")
    print(f"pinned {ns.name} (when it is down the best other backend is used; `ai use auto` to unpin)")
    return 0


def run_webui(cfg: Config, _: argparse.Namespace) -> int:
    url = str(cfg.section("local").get("webui_url", ""))
    print(url or "no webui_url under [local] in the config")
    return 0 if url else 1


def run_limits(_: Config, __: argparse.Namespace) -> int:
    print(LIMITS)
    return 0


def run_default(cfg: Config, ns: argparse.Namespace) -> int:
    """No sub-command: the status table, then a session (code inside a git repo, chat otherwise)."""
    ns.no_probe, ns.json = False, False
    if run_status(cfg, ns) != 0:
        return 1
    print()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('next: ai code [path] | ai chat | ai ask "question"')
        return 0
    in_repo = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True).returncode == 0
    ns.backend, ns.path, ns.prompt, ns.extra, ns.quiet = "", ".", "", [], False
    if in_repo and shutil.which("codex"):
        print("this is a git repo: starting a coding session (`ai chat` for plain chat)\n")
        return run_code(cfg, ns)
    print("starting plain chat (`ai code [path]` for a coding session in a repo)\n")
    return run_chat(cfg, ns)


# -- argument handling --------------------------------------------------------------
def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(local_func=run_default)
    sub = parser.add_subparsers(dest="local_command", metavar="{status,code,chat,ask,use,webui,limits,claude}")

    def add(name: str, func: Any, help_text: str, backend: bool = True) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(local_func=func)
        if backend:
            p.add_argument("--backend", "-b", default="", help="use this backend for this run (default: pinned, else best available)")
        return p

    p = add("status", run_status, "which local backends are up, how fast, busy or free", backend=False)
    p.add_argument("--no-probe", action="store_true", help="health only; skip the ~60-token speed probe")
    p.add_argument("--json", action="store_true")
    p = add("code", run_code, "coding session in a repo: Codex CLI on the local model, hardened preset")
    p.add_argument("path", nargs="?", default=".", help="repository or directory (default: here)")
    p.add_argument("-p", "--prompt", default="", help="do this one task and exit, instead of an interactive session")
    p.add_argument("extra", nargs="*", help="after --: extra arguments for codex")
    p = add("chat", run_chat, "plain interactive chat (no tools)")
    p = add("ask", run_ask, "one question, one answer; text piped in is appended")
    p.add_argument("question", nargs="*")
    p.add_argument("-q", "--quiet", action="store_true", help="no banner on stderr")
    p = add("use", run_use, "pin a backend (`use auto` to unpin, no name to show)", backend=False)
    p.add_argument("name", nargs="?")
    add("webui", run_webui, "print the Open WebUI address for browser chat", backend=False)
    add("limits", run_limits, "what a small local model is and is not good at", backend=False)
    p = add("claude", run_claude, "EXPERIMENTAL: Claude Code itself on the local model (opt-in; read the warning)")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--experimental", action="store_true", help="yes, I read the warning")
    p.add_argument("extra", nargs="*", help="after --: extra arguments for claude")


def dispatch(cfg: Config, ns: argparse.Namespace) -> int:
    return int(ns.local_func(cfg, ns))


def main(argv: list[str] | None = None) -> int:
    """The ``ai`` command: ``ai ...`` is ``almanac local ...``."""
    parser = argparse.ArgumentParser(prog="ai", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="config file (default $ALMANAC_CONFIG or ~/.config/almanac/config.toml)")
    add_arguments(parser)
    ns = parser.parse_args(argv)
    return dispatch(Config.load(ns.config), ns)


if __name__ == "__main__":
    raise SystemExit(main())
