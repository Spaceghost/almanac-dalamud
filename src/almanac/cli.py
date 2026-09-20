"""Command line: ``almanac <command>``. Run ``almanac -h`` for the list."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import REPO_ROOT, Config
from .service import Almanac


def _serve(app: Any, listen: list[str]) -> None:
    """Serve an ASGI app on several host:port addresses with one uvicorn."""
    import uvicorn

    sockets = []
    for address in listen:
        host, _, port = address.rpartition(":")
        host = host.strip("[]")
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        sockets.append(sock)
        logging.info("listening on %s", address)
    server = uvicorn.Server(uvicorn.Config(app, log_level="info", access_log=False, lifespan="on"))
    asyncio.run(server.serve(sockets=sockets))


def _kv(pairs: list[str]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"expected key=value, got {pair!r}")
        args[key] = value
    return args


def cmd_init(cfg: Config, _: argparse.Namespace) -> int:
    created = cfg.ensure_token()
    print(f"token file: {cfg.token_file} ({'created, mode 0600' if created else 'already present'}; value not shown)")
    cfg_path = Path("~/.config/almanac/config.toml").expanduser()
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text((REPO_ROOT / "config.example.toml").read_text())
        print(f"wrote {cfg_path} from config.example.toml; edit knowledge_dirs, tools_dirs and hosts")
    return 0


def cmd_doctor(cfg: Config, _: argparse.Namespace) -> int:
    import httpx

    from .guard import Guard

    alm = Almanac(cfg)
    print(f"knowledge dirs: {', '.join(map(str, cfg.knowledge_dirs))}")
    print(f"tools dirs:     {', '.join(map(str, cfg.tools_dirs))}")
    print(f"notes indexed:  {alm.kb.refresh()['notes']}   tools: {len(alm.tools)}   hosts: {', '.join(cfg.hosts)}")
    print(f"this host:      {cfg.this_host}")
    print(f"token file:     {cfg.token_file} {'present' if cfg.token_file.exists() else 'MISSING (run almanac init)'}")
    backend = cfg.section("gateway")["backend"]
    try:
        loaded = [m["name"] for m in httpx.get(f"{backend}/api/ps", timeout=5).json()["models"]]
        print(f"backend:        {backend} reachable; loaded: {', '.join(loaded) or 'none'}")
    except Exception as exc:
        print(f"backend:        {backend} unreachable ({exc.__class__.__name__})")
    print(f"guard:          {Guard(cfg.section('guard')).may_load(False).reason}")
    return 0


def cmd_index(cfg: Config, ns: argparse.Namespace) -> int:
    print(json.dumps(Almanac(cfg).kb.refresh(embed=ns.embed)))
    return 0


def _print_outcome(alm: Almanac, name: str, args: dict[str, Any], yes: bool) -> int:
    outcome = alm.call(name, args, caller="cli")
    if outcome.needs_confirmation:
        print(outcome.plan)
        if not (yes or (sys.stdin.isatty() and input("Run it? [y/N] ").strip().lower() == "y")):
            alm.audit("declined", name, args, "cli")
            print("not run")
            return 1
        outcome = alm.call(name, args, caller="cli", approved=True)
    print(outcome.text)
    return 1 if outcome.is_error else 0


def cmd_search(cfg: Config, ns: argparse.Namespace) -> int:
    for hit in Almanac(cfg).kb.search(" ".join(ns.query), ns.host, ns.tag, limit=ns.limit):
        print(f"{hit['path']:48} {hit['title']}\n    {hit['snippet']}")
    return 0


def cmd_read(cfg: Config, ns: argparse.Namespace) -> int:
    return _print_outcome(Almanac(cfg), "kb_read", {"path": ns.path}, False)


def cmd_note(cfg: Config, ns: argparse.Namespace) -> int:
    body = Path(ns.body_file).read_text() if ns.body_file != "-" else sys.stdin.read()
    args: dict[str, Any] = {"path": ns.path, "title": ns.title, "body": body, "safety": ns.safety}
    if ns.hosts:
        args["hosts"] = ns.hosts.split(",")
    if ns.tags:
        args["tags"] = ns.tags.split(",")
    alm = Almanac(cfg)
    if Path(alm.kb.resolve(ns.path, for_write=True)).exists():
        args["base_sha256"] = alm.kb.load(ns.path).sha256
    return _print_outcome(alm, "kb_note", args, ns.yes)


def cmd_tools(cfg: Config, ns: argparse.Namespace) -> int:
    alm = Almanac(cfg)
    for spec in alm.catalogue():
        if ns.verbose:
            print(json.dumps(spec, indent=1))
        else:
            print(f"{spec['name']:22} {spec['safety']:12} {spec['description'][:90]}")
    return 0


def cmd_tool(cfg: Config, ns: argparse.Namespace) -> int:
    return _print_outcome(Almanac(cfg), ns.name, _kv(ns.args), ns.yes)


def cmd_ask(cfg: Config, ns: argparse.Namespace) -> int:
    from .chat import run_ask

    return run_ask(Almanac(cfg), " ".join(ns.task), ns.model, ns.allow_change, ns.allow_game_actions, ns.verbose,
                   thread_id=ns.thread, continue_last=ns.continue_last, new_thread=ns.new_thread, stream_json=ns.stream_json)


def cmd_chat(cfg: Config, ns: argparse.Namespace) -> int:
    from .chat import run_chat

    return run_chat(Almanac(cfg), ns.model, ns.allow_change, ns.allow_game_actions,
                    thread_id=ns.thread, continue_last=ns.continue_last, new_thread=ns.new_thread)


def cmd_threads(cfg: Config, ns: argparse.Namespace) -> int:
    from .chat import run_threads

    return run_threads(Almanac(cfg), ns.action, ns.id, ns.json, ns.n)


def cmd_run(cfg: Config, ns: argparse.Namespace) -> int:
    from .agent import GuardRefused, runbook_agent, save_run

    alm = Almanac(cfg)
    agent, text = runbook_agent(alm, ns.runbook, ns.allow_change, model=ns.model, allow_game_actions=ns.allow_game_actions)
    try:
        transcript = agent.run("Carry out this runbook now.", context=text)
    except GuardRefused as exc:
        print(str(exc), file=sys.stderr)
        return 75  # EX_TEMPFAIL: the timer simply tries again next time
    path = save_run(alm, Path(ns.runbook).stem, transcript)
    print(transcript.answer)
    print(f"\n(transcript: {path})", file=sys.stderr)
    return 0


def cmd_mcp(cfg: Config, ns: argparse.Namespace) -> int:
    from .mcp_server import http_app, serve_stdio

    alm = Almanac(cfg)
    if ns.stdio:
        asyncio.run(serve_stdio(alm))
    else:
        _serve(http_app(alm), ns.listen or cfg.section("mcp")["listen"])
    return 0


def cmd_gateway(cfg: Config, ns: argparse.Namespace) -> int:
    from .gateway import Gateway

    _serve(Gateway(cfg).app(), ns.listen or cfg.section("gateway")["listen"])
    return 0


def cmd_model(cfg: Config, ns: argparse.Namespace) -> int:
    import httpx

    backend = cfg.section("gateway")["backend"]
    loaded = [m["name"] for m in httpx.get(f"{backend}/api/ps", timeout=5).json()["models"]]
    if ns.action == "unload":
        for model in loaded:
            httpx.post(f"{backend}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
            print(f"unloaded {model}")
    else:
        print("loaded: " + (", ".join(loaded) or "none"))
    return 0


def cmd_schedule(cfg: Config, ns: argparse.Namespace) -> int:
    from .agent import load_runbook

    load_runbook(Almanac(cfg), ns.runbook)  # fail early if it is not a runbook
    name = Path(ns.runbook).stem
    unit_dir = Path("~/.config/systemd/user").expanduser()
    timer = unit_dir / f"almanac-run@{name}.timer"
    unit_dir.mkdir(parents=True, exist_ok=True)
    timer.write_text(
        f"[Unit]\nDescription=almanac runbook {name} ({ns.on_calendar})\n\n"
        f"[Timer]\nOnCalendar={ns.on_calendar}\nRandomizedDelaySec=5m\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    print(f"wrote {timer}")
    commands = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", timer.name]]
    if ns.enable:
        for command in commands:
            subprocess.run(command, check=True)
    else:
        print("enable with:\n  " + "\n  ".join(" ".join(c) for c in commands))
    return 0


def cmd_audit(cfg: Config, ns: argparse.Namespace) -> int:
    path = cfg.state_dir / "audit.jsonl"
    lines = path.read_text().splitlines()[-ns.n :] if path.exists() else []
    for line in lines:
        record = json.loads(line)
        print(f"{record['ts']} {record['event']:9} {record['tool']:20} {record['caller']:28} {json.dumps(record.get('args', {}))[:80]}")
    return 0


def _bench_print_task(run: Any) -> None:
    s = run.score
    answer = "skip" if s.answer_score is None else f"{s.answer_score:.2f}"
    ttft = "-" if run.ttft_ms is None else f"{run.ttft_ms:.0f}"
    tps = "-" if run.tokens_per_s is None else f"{run.tokens_per_s:.1f}"
    print(f"{run.task_id:22} {s.score:5.2f} {s.tool_score:5.2f} {answer:>6} {s.valid_calls:>3}/{s.total_calls:<3} {ttft:>7} {tps:>7}  {s.error or 'ok'}", flush=True)


def _bench_submit(store: Any, run_id: int, doc: dict[str, Any], yes: bool) -> int:
    import httpx

    from . import bench

    errors = bench.result_errors(doc)
    if errors:
        print("result does not conform to results.schema.json, not submitting:\n  " + "\n  ".join(errors[:10]), file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=2))
    url = bench.leaderboard_url()
    if not yes:
        if not sys.stdin.isatty():
            print("not submitted (no terminal to confirm; pass --yes)", file=sys.stderr)
            return 1
        if input(f"Submit exactly this JSON to {url}? [y/N] ").strip().lower() != "y":
            print("not submitted")
            return 1
    try:
        response = bench.submit(doc, url=url)
    except httpx.HTTPError as exc:
        print(f"submit failed: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    if response.status_code >= 300:
        print(f"submit rejected: HTTP {response.status_code} {response.text[:300]}", file=sys.stderr)
        return 1
    store.mark_submitted(run_id)
    print(f"submitted run #{run_id} (HTTP {response.status_code})")
    return 0


def cmd_bench(cfg: Config, ns: argparse.Namespace) -> int:
    import os

    import httpx

    from . import bench

    store = bench.Store(cfg.state_dir / "bench.sqlite")
    if ns.list:
        for rid, created, suite, mode, model, score, submitted in store.rows():
            print(f"#{rid:<5} {created}  {suite:18} {mode:5} {model:32} {score:6.1f}  {'submitted' if submitted else ''}")
        return 0
    if ns.run is not None:
        doc = store.get(ns.run)
        if doc is None:
            print(f"no stored run #{ns.run}", file=sys.stderr)
            return 1
        if ns.json:
            Path(ns.json).write_text(json.dumps(doc, indent=2) + "\n")
        if ns.submit:
            return _bench_submit(store, ns.run, doc, ns.yes)
        print(json.dumps(doc, indent=2))
        return 0
    if not ns.model:
        print("--model is required", file=sys.stderr)
        return 2

    suite = bench.load_suite(ns.suite or bench.DEFAULT_SUITE)
    tasks = [t for t in (ns.tasks or "").split(",") if t] or None
    for task_id in tasks or []:
        suite.task(task_id)
    mode = "live" if ns.live else "mock"
    base_url = ns.base_url or str(cfg.section("gateway")["backend"]).rstrip("/") + "/v1"
    api_key = os.environ.get(ns.api_key_env) if ns.api_key_env else None
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    client = httpx.Client()

    if mode == "live":
        from .upstream import Upstream, UpstreamError

        upstream = Upstream(name="xivmcp", url=ns.xivmcp_url, token_file=ns.xivmcp_token_file or "", token_env="" if ns.xivmcp_token_file else "XIVMCP_TOKEN")
        try:
            available = {t.name for t in upstream.list_tools()}
        except UpstreamError as exc:
            print(f"live mode needs XivMcp: {exc}", file=sys.stderr)
            return 1
        missing = sorted({n for t in suite.tasks for n in t.get("tools") or []} - available)
        if missing:
            print(f"XivMcp does not offer: {', '.join(missing)}", file=sys.stderr)
            return 1
        executor = bench.live_executor(upstream)
    else:
        executor = bench.mock_executor(suite)

    kind, version = (ns.backend_kind, None) if ns.backend_kind else bench.detect_backend(client, base_url, headers)
    info = bench.ollama_model_info(client, base_url, ns.model, headers) if kind == "ollama" else {}
    model: dict[str, Any] = {"name": ns.model}
    for key in ("family", "params_b"):
        if key in info:
            model[key] = info[key]
    model["quant"] = ns.quant or info.get("quant", "unknown")
    model["context"] = ns.context if ns.context is not None else int(info.get("context", 0))

    chat = bench.ChatClient(base_url, api_key, client, timeout=ns.timeout)
    runner = bench.Runner(suite, chat, ns.model, executor, mode, ns.tool_calling)
    print(f"suite {suite.id} {suite.version}  mode {mode}  backend {kind}  model {ns.model}  quant {model['quant']}  context {model['context']}")
    print(f"{'task':22} {'score':>5} {'tools':>5} {'answer':>6} {'calls':>7} {'ttft ms':>7} {'tok/s':>7}  error")
    try:
        with bench.VramSampler(bench.vram_reader(kind, base_url)) as sampler:
            outcome = runner.run(tasks, on_task=_bench_print_task)
    except bench.BackendError as exc:
        print(f"backend failed: {exc}", file=sys.stderr)
        return 1
    backend: dict[str, Any] = {"kind": kind, "version": version}
    doc = bench.build_result(suite, mode, bench.hardware_facts(), backend, model, outcome, sampler.peak)
    m = doc["metrics"]
    print(
        f"\nscore {m['score']}  success {m['success_rate']:.0%}  tool validity {m['tool_call_validity']:.0%}  "
        f"quality {m.get('quality', 0):.2f}  ttft {m['ttft_ms']:.0f} ms  {m['tokens_per_s']:.1f} tok/s  "
        f"peak VRAM {m['peak_vram_mb'] if m['peak_vram_mb'] is not None else '-'} MB  {m['total_s']:.1f} s  "
        f"tool calling {doc['model']['tool_calling']}"
    )
    errors = bench.result_errors(doc)
    if errors:
        print("warning: result does not conform to results.schema.json:\n  " + "\n  ".join(errors[:10]), file=sys.stderr)
    run_id = store.add(doc)
    print(f"stored as run #{run_id} in bench.sqlite")
    if ns.json:
        Path(ns.json).write_text(json.dumps(doc, indent=2) + "\n")
    return _bench_submit(store, run_id, doc, ns.yes) if ns.submit else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="almanac", description=__doc__)
    parser.add_argument("--config", help="config file (default $ALMANAC_CONFIG or ~/.config/almanac/config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func: Any, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        return p

    add("init", cmd_init, "create the token file and a starter config")
    add("doctor", cmd_doctor, "show configuration and health (never prints the token)")
    add("index", cmd_index, "rebuild the knowledge index").add_argument("--embed", action="store_true", help="also compute embeddings")
    p = add("search", cmd_search, "search the knowledge base")
    p.add_argument("query", nargs="+")
    p.add_argument("--host")
    p.add_argument("--tag")
    p.add_argument("--limit", type=int, default=8)
    add("read", cmd_read, "print one note").add_argument("path")
    p = add("note", cmd_note, "add or update a note (shows the diff, asks before writing)")
    p.add_argument("path")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True, help="Markdown body file, or - for stdin")
    p.add_argument("--hosts")
    p.add_argument("--tags")
    p.add_argument("--safety", default="read", choices=["read", "change", "destructive"])
    p.add_argument("--yes", action="store_true")
    add("tools", cmd_tools, "list tools").add_argument("-v", "--verbose", action="store_true")
    p = add("tool", cmd_tool, "run one tool: almanac tool NAME key=value ...")
    p.add_argument("name")
    p.add_argument("args", nargs="*")
    p.add_argument("--yes", action="store_true", help="approve a change/destructive plan without prompting")
    for name, func, text in (
        ("ask", cmd_ask, "ask the local model to do a task (one shot)"),
        ("run", cmd_run, "run a runbook with the local model"),
        ("chat", cmd_chat, "interactive chat with the local model"),
    ):
        p = add(name, func, text)
        if name != "chat":
            p.add_argument("task" if name == "ask" else "runbook", nargs="+" if name == "ask" else None)
        p.add_argument("--allow-change", action="store_true", help="offer change tools (each still needs approval)")
        p.add_argument("--allow-game-actions", action="store_true", help="offer companion action/chat tools (the game asks to confirm)")
        p.add_argument("--model")
        p.add_argument("-v", "--verbose", action="store_true")
        if name != "run":
            which = p.add_mutually_exclusive_group()
            which.add_argument("--thread", metavar="ID", help="continue thread ID (created when missing) and save this turn in it")
            which.add_argument("--continue", dest="continue_last", action="store_true", help="continue the most recent thread")
            which.add_argument("--new-thread", action="store_true", help="start a new saved thread (its id is printed)")
        if name == "ask":
            p.add_argument("--stream-json", action="store_true", help="print JSON lines (thread, text, tool, result, done, error) for programs")
    p = add("threads", cmd_threads, "list, show or delete saved conversation threads")
    p.add_argument("action", choices=["list", "show", "rm"], nargs="?", default="list")
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true", help="JSON lines, as ask --stream-json")
    p.add_argument("-n", type=int, default=50, help="list at most this many (newest first)")
    p = add("mcp", cmd_mcp, "serve MCP (HTTP by default)")
    p.add_argument("--stdio", action="store_true")
    p.add_argument("--listen", action="append")
    add("gateway", cmd_gateway, "serve the model gateway").add_argument("--listen", action="append")
    add("model", cmd_model, "show or unload the resident model").add_argument("action", choices=["status", "unload"], nargs="?", default="status")
    p = add("schedule", cmd_schedule, "write a systemd user timer for a runbook")
    p.add_argument("runbook")
    p.add_argument("--on-calendar", required=True, help="systemd OnCalendar=, e.g. daily or 'Mon *-*-* 09:00'")
    p.add_argument("--enable", action="store_true")
    add("audit", cmd_audit, "show the audit log tail").add_argument("-n", type=int, default=20)
    p = add("bench", cmd_bench, "run the FFXIV model benchmark (benchmark/README.md)")
    p.add_argument("--suite", help="suite file (default benchmark/suites/ffxiv-core.json)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true", help="answer tool calls from the suite fixtures (default)")
    group.add_argument("--live", action="store_true", help="call XivMcp in the running game")
    p.add_argument("--xivmcp-url", default="http://127.0.0.1:41800/mcp")
    p.add_argument("--xivmcp-token-file", help="XivMcp bearer token file (default: $XIVMCP_TOKEN)")
    p.add_argument("--base-url", help="OpenAI-compatible base URL (default: [gateway] backend + /v1)")
    p.add_argument("--model")
    p.add_argument("--api-key-env", help="environment variable holding the backend API key")
    p.add_argument("--backend-kind", choices=["ollama", "lmstudio", "llamacpp", "openai-compatible", "almanac"], help="skip detection")
    p.add_argument("--tool-calling", choices=["auto", "native", "prompted"], default="auto", help="auto: native, prompted if the backend rejects tools")
    p.add_argument("--quant")
    p.add_argument("--context", type=int)
    p.add_argument("--tasks", help="comma-separated task ids (default: all)")
    p.add_argument("--timeout", type=float, default=300.0, help="per-request read timeout in seconds")
    p.add_argument("--json", help="also write the result document to this file")
    p.add_argument("--submit", action="store_true", help="show the JSON, confirm, then submit it to the leaderboard")
    p.add_argument("--yes", action="store_true", help="submit without asking")
    p.add_argument("--run", type=int, help="use stored run N instead of running (print it, or --submit it)")
    p.add_argument("--list", action="store_true", help="list stored runs")

    from .autopilot.cli import register as register_autopilot

    register_autopilot(sub)

    ns = parser.parse_args(argv)
    level = logging.INFO if ns.command in ("mcp", "gateway") else logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return int(ns.func(Config.load(ns.config), ns))


if __name__ == "__main__":
    raise SystemExit(main())
