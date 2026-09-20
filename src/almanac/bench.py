"""Headless benchmark runner: ``almanac bench``.

Implements ``benchmark/README.md`` (the spec; when this file disagrees with it,
this file has a bug). It runs a versioned suite of FFXIV tasks against any
OpenAI-compatible Chat Completions backend, answers tool calls from the
suite's fixtures (mock) or from XivMcp in the running game (live), scores the
run and builds a result document that conforms to
``benchmark/schema/results.schema.json`` (v1).

Every run is stored in ``<state_dir>/bench.sqlite``. Submitting to the
community leaderboard is explicit (``--submit``) and shows the exact JSON
first. The result contains only what the schema allows: no hostnames, paths,
usernames, IP addresses or tokens.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from . import __version__
from .config import REPO_ROOT

DEFAULT_SUITE = REPO_ROOT / "benchmark" / "suites" / "ffxiv-core.json"
RESULTS_SCHEMA = REPO_ROOT / "benchmark" / "schema" / "results.schema.json"
LEADERBOARD_URL = "https://spacegho.st/mods/ffxiv/almanac/api/results"
CLIENT_NAME = "almanac-py"
LINK_API = "https://spacegho.st/mods/ffxiv/term/vote/api"
LINK_CLIENT_ID = "almanac"
LINK_SCOPE = "almanac:submit"
LINK_TOKEN_FILE = "leaderboard-token"
RELINK_ERRORS = {"invalid_token", "token_revoked", "token_expired", "sign_in_required"}
# Absolute slack for every numeric tolerance comparison (approx matcher,
# numbers_from_tool), so 9.55 vs 9.4 +- 0.15 passes despite binary floats.
EPS = 1e-9
MISSING: Any = object()

PROMPTED_HEADER = (
    "You can call these tools. To call one, reply with only a JSON object on one line:\n"
    '{"tool": "<name>", "arguments": {...}}\n'
    'After a call you will get its result as the next user message, starting "Tool result:". '
    "When you have the answer, reply normally without JSON."
)
WARMUP_PROMPT = "Reply with OK"


def compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


# -- suite ------------------------------------------------------------------


@dataclass
class Suite:
    raw: dict[str, Any]
    sha256: str

    @property
    def id(self) -> str:
        return str(self.raw["id"])

    @property
    def version(self) -> str:
        return str(self.raw["version"])

    @property
    def defaults(self) -> dict[str, Any]:
        return dict(self.raw.get("defaults") or {})

    @property
    def tools(self) -> dict[str, dict[str, Any]]:
        return {t["name"]: t for t in self.raw.get("tools") or []}

    @property
    def tasks(self) -> list[dict[str, Any]]:
        return list(self.raw.get("tasks") or [])

    def task(self, task_id: str) -> dict[str, Any]:
        for task in self.tasks:
            if task["id"] == task_id:
                return task
        raise KeyError(f"no task {task_id!r} in suite {self.id}")

    def offered(self, task: dict[str, Any]) -> dict[str, dict[str, Any]]:
        tools = self.tools
        return {name: tools[name] for name in task.get("tools") or [] if name in tools}


def load_suite(path: str | os.PathLike[str] = DEFAULT_SUITE) -> Suite:
    data = Path(path).read_bytes()
    return Suite(json.loads(data.decode("utf-8")), hashlib.sha256(data).hexdigest())


# -- matchers and paths -----------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def json_equal(a: Any, b: Any) -> bool:
    """JSON value equality; numbers compare numerically (135 == 135.0), booleans are not numbers."""
    if _is_number(a) and _is_number(b):
        return float(a) == float(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if _is_number(a) or _is_number(b):
        return False
    return type(a) is type(b) and a == b


def _matcher_form(matcher: Any) -> str | None:
    if isinstance(matcher, dict):
        for form in ("eq", "approx", "icontains_any"):
            if form in matcher:
                return form
    return None


def match_value(matcher: Any, value: Any) -> bool:
    """One argument matcher (spec: Argument matchers). A bare JSON value means eq."""
    form = _matcher_form(matcher)
    if form == "eq":
        return json_equal(value, matcher["eq"])
    if form == "approx":
        tolerance = float(matcher.get("tolerance", 0))
        return _is_number(value) and abs(float(value) - float(matcher["approx"])) <= tolerance + EPS
    if form == "icontains_any":
        return isinstance(value, str) and any(str(s).lower() in value.lower() for s in matcher["icontains_any"])
    return json_equal(value, matcher)


def args_match(when: dict[str, Any] | None, args: Any) -> bool:
    """All matchers match; a missing argument never matches; ``{}`` matches anything."""
    if not isinstance(args, dict):
        return False
    return all(key in args and match_value(m, args[key]) for key, m in (when or {}).items())


_SEGMENT = re.compile(r"\.?([^.\[\]]+)|\[(\d+)\]|\[([^\]~]+)~([^\]]*)\]")


def resolve_path(doc: Any, path: str) -> Any:
    """``a.b[1].c`` and ``list[key~text].field``; returns MISSING when absent."""
    pos, current = 0, doc
    while pos < len(path):
        m = _SEGMENT.match(path, pos)
        if not m or m.end() == pos:
            raise ValueError(f"bad path {path!r} at {pos}")
        pos = m.end()
        key, index, sel_key, sel_text = m.groups()
        if key is not None:
            if not isinstance(current, dict) or key not in current:
                return MISSING
            current = current[key]
        elif index is not None:
            i = int(index)
            if not isinstance(current, list) or i >= len(current):
                return MISSING
            current = current[i]
        else:
            if not isinstance(current, list):
                return MISSING
            needle = sel_text.lower()
            for item in current:
                if isinstance(item, dict) and isinstance(item.get(sel_key), str) and needle in item[sel_key].lower():
                    current = item
                    break
            else:
                return MISSING
    return current


# -- JSON Schema subset -----------------------------------------------------

_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": _is_number,
    "integer": lambda v: _is_number(v) and float(v).is_integer(),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(schema: dict[str, Any], value: Any, where: str = "$") -> list[str]:
    """Errors for the subset both suite tools and result schemas use:
    type, const, enum, required, properties, additionalProperties, minimum,
    maximum, maxLength, pattern, items, maxItems. Other keywords are ignored."""
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_TYPES.get(t, lambda _: False)(value) for t in names):
            return [f"{where}: expected {'/'.join(names)}"]
    if "const" in schema and not json_equal(value, schema["const"]):
        errors.append(f"{where}: must be {compact(schema['const'])}")
    if "enum" in schema and not any(json_equal(value, e) for e in schema["enum"]):
        errors.append(f"{where}: must be one of {compact(schema['enum'])}")
    if _is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: above maximum {schema['maximum']}")
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{where}: does not match {schema['pattern']}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{where}: missing required {key!r}")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                errors += validate(props[key], item, f"{where}.{key}")
            elif extra is False:
                errors.append(f"{where}: unexpected property {key!r}")
            elif isinstance(extra, dict):
                errors += validate(extra, item, f"{where}.{key}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errors += validate(schema["items"], item, f"{where}[{i}]")
    return errors


# -- calls, fixtures, scoring -----------------------------------------------


@dataclass
class Call:
    name: str
    arguments: str  # raw JSON text as the model produced it
    args: dict[str, Any] | None = None
    invalid: str | None = None  # reason, None when valid
    result: Any = MISSING  # what the tool returned; MISSING when not executed
    text: str = ""  # the tool message content sent back to the model

    @property
    def valid(self) -> bool:
        return self.invalid is None


def check_call(offered: dict[str, dict[str, Any]], name: str, arguments: str) -> Call:
    call = Call(name, arguments)
    if name not in offered:
        call.invalid = f"unknown tool {name!r}"
        return call
    text = (arguments or "").strip()
    try:
        args = json.loads(text) if text else {}
    except json.JSONDecodeError:
        call.invalid = "arguments are not valid JSON"
        return call
    if not isinstance(args, dict):
        call.invalid = "arguments are not a JSON object"
        return call
    errors = validate(offered[name].get("inputSchema") or {}, args, "arguments")
    if errors:
        call.invalid = errors[0]
        return call
    call.args = args
    return call


def fixture_result(suite: Suite, name: str, args: dict[str, Any]) -> Any:
    fixture = (suite.raw.get("fixtures") or {}).get(name, MISSING)
    if fixture is MISSING:
        return {"error": "no data"}
    if isinstance(fixture, list):
        for entry in fixture:
            if args_match(entry.get("when") or {}, args):
                return entry.get("result")
        return {"error": "no data"}
    return fixture


_FENCE_BLOCK = re.compile(r"^```[A-Za-z0-9_+.-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```$", re.S)
_FENCE_LINE = re.compile(r"^```(.*?)```$", re.S)
_WRAP = "`\"'"


def strip_fence(text: str) -> str:
    """Trim, then remove one surrounding ``` or ```lang fence."""
    text = text.strip()
    m = _FENCE_BLOCK.match(text) or _FENCE_LINE.match(text)
    return m.group(1).strip() if m else text


def normalise_exact(text: str) -> str:
    """spec ``exact``: trim, one fence, wrapping backticks/quotes, collapse whitespace, one trailing '.'."""
    text = strip_fence(text).strip(_WRAP)
    text = re.sub(r"\s+", " ", text).strip()
    if text.endswith("."):
        text = text[:-1].rstrip()
    return text


_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def answer_numbers(answer: str) -> list[float]:
    return [float(n) for n in _NUMBER.findall(_THOUSANDS.sub("", answer))]


def scalar_text(value: Any) -> str | None:
    """Text a *_from_tool value must appear as: strings as-is, numbers in shortest
    round-trip form without a trailing .0, booleans as true/false; else None."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if _is_number(value):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return repr(value) if isinstance(value, float) else str(value)
    return None


def answer_checks(spec: dict[str, Any], answer: str, first_results: dict[str, Any]) -> list[bool]:
    low = answer.lower()
    checks: list[bool] = []
    checks += [str(s).lower() in low for s in spec.get("contains_all") or []]
    if spec.get("contains_any"):
        checks.append(any(str(s).lower() in low for s in spec["contains_any"]))
    checks += [str(s).lower() not in low for s in spec.get("not_contains") or []]
    for entry in spec.get("contains_from_tool") or []:
        result = first_results.get(entry["tool"], MISSING)
        text = scalar_text(resolve_path(result, entry["path"])) if result is not MISSING else None
        checks.append(text is not None and text.lower() in low)
    numbers = answer_numbers(answer) if spec.get("numbers_from_tool") else []
    for entry in spec.get("numbers_from_tool") or []:
        result = first_results.get(entry["tool"], MISSING)
        target = resolve_path(result, entry["path"]) if result is not MISSING else MISSING
        tolerance = float(entry.get("tolerance", 0))
        checks.append(_is_number(target) and any(abs(n - float(target)) <= tolerance + EPS for n in numbers))
    if "exact" in spec:
        checks.append(normalise_exact(answer).lower() == normalise_exact(str(spec["exact"])).lower())
    if "max_chars" in spec:
        checks.append(len(answer) <= int(spec["max_chars"]))
    return checks


@dataclass
class TaskScore:
    tool_score: float
    answer_score: float | None
    score: float
    success: bool
    error: str | None
    valid_calls: int
    total_calls: int


def answer_skipped(task: dict[str, Any], mode: str) -> bool:
    return mode == "live" and (task.get("live") or {}).get("answer") == "skip"


def final_answer(content: str | None) -> str | None:
    """The trimmed reply content; empty or whitespace-only counts as no final answer."""
    text = (content or "").strip()
    return text or None


def score_task(suite: Suite, task: dict[str, Any], mode: str, calls: list[Call], answer: str | None, failure: str | None = None) -> TaskScore:
    expect = task.get("expect") or {}
    expected = expect.get("calls") or []
    valid = [c for c in calls if c.valid]
    if not expected:
        tool = 0.0 if expect.get("forbid_any_call") and calls else 1.0
    else:
        used: set[int] = set()
        matched = 0
        for exp in expected:
            for i, call in enumerate(valid):
                if i not in used and call.name == exp["name"] and args_match(exp.get("args") or {}, call.args):
                    used.add(i)
                    matched += 1
                    break
        tool = matched / len(expected)
    if calls:
        tool *= len(valid) / len(calls)

    answer = final_answer(answer)
    answer_score: float | None
    if answer_skipped(task, mode):
        answer_score = None
    elif answer is None:
        answer_score = 0.0
    else:
        first_results: dict[str, Any] = {}
        for call in calls:
            if call.result is not MISSING and call.name not in first_results:
                first_results[call.name] = call.result
        checks = answer_checks(expect.get("answer") or {}, answer, first_results)
        answer_score = sum(checks) / len(checks) if checks else 1.0

    weights = suite.defaults.get("weights") or {"tools": 0.5, "answer": 0.5}
    score = tool if answer_score is None else float(weights["tools"]) * tool + float(weights["answer"]) * answer_score
    if failure:
        error: str | None = failure
    elif answer is None:
        error = "no_answer"
    elif len(valid) < len(calls):
        error = "bad_tool_call"
    elif answer_score is not None and answer_score < 1 - EPS:
        error = "wrong_answer"
    else:
        error = None
    return TaskScore(tool, answer_score, score, abs(score - 1) < EPS, error, len(valid), len(calls))


# -- model backend ----------------------------------------------------------


class BackendError(RuntimeError):
    def __init__(self, kind: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind  # "timeout" | "http"
        self.status = status


@dataclass
class Turn:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    sent: float = 0.0
    first_delta: float | None = None
    end: float = 0.0
    deltas: int = 0
    usage_tokens: int | None = None

    @property
    def output_tokens(self) -> int:
        return self.usage_tokens if self.usage_tokens is not None else self.deltas


def api_root(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


class ChatClient:
    """Streaming OpenAI Chat Completions against any compatible base URL (``.../v1``)."""

    def __init__(self, base_url: str, api_key: str | None = None, client: httpx.Client | None = None, timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = client or httpx.Client()
        self.timeout = httpx.Timeout(timeout, connect=10.0)

    def stream(self, payload: dict[str, Any]) -> Turn:
        body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        turn = Turn(sent=time.monotonic())
        try:
            with self.client.stream("POST", f"{self.base_url}/chat/completions", json=body, headers=self.headers, timeout=self.timeout) as response:
                if response.status_code >= 400:
                    response.read()
                    raise BackendError("http", f"backend returned {response.status_code}: {response.text[:300]}", response.status_code)
                self._fold(response.iter_lines(), turn)
        except httpx.TimeoutException as exc:
            raise BackendError("timeout", f"backend timed out ({exc.__class__.__name__})") from exc
        except httpx.HTTPError as exc:
            raise BackendError("http", f"backend unreachable ({exc.__class__.__name__})") from exc
        turn.end = time.monotonic()
        return turn

    @staticmethod
    def _fold(lines: Any, turn: Turn) -> None:
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        for line in lines:
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            if not data:
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError as exc:
                raise BackendError("http", "backend sent a malformed stream chunk") from exc
            if chunk.get("error"):
                raise BackendError("http", f"backend error: {str(chunk['error'])[:300]}")
            usage = chunk.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("completion_tokens"), int):
                turn.usage_tokens = usage["completion_tokens"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                got = False
                if delta.get("content"):
                    content.append(delta["content"])
                    got = True
                thought = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(thought, str) and thought:
                    reasoning.append(thought)
                    got = True
                for tc in delta.get("tool_calls") or []:
                    got = True
                    index = tc.get("index")
                    if index is None:
                        index = len(calls) if tc.get("id") or not calls else max(calls)
                    slot = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    args = fn.get("arguments")
                    slot["function"]["arguments"] += compact(args) if isinstance(args, (dict, list)) else (args or "")
                if got:
                    turn.deltas += 1
                    if turn.first_delta is None:
                        turn.first_delta = time.monotonic()
        turn.content = "".join(content)
        turn.reasoning = "".join(reasoning)
        turn.tool_calls = [calls[i] for i in sorted(calls)]


def tools_rejected(exc: BackendError) -> bool:
    """The backend refused a request because of ``tools`` (e.g. Ollama: 'does not support tools')."""
    return exc.kind == "http" and exc.status is not None and "tool" in str(exc).lower()


def prompted_system(system: str, offered: dict[str, dict[str, Any]]) -> str:
    if not offered:
        return system
    lines = [f"{t['name']}: {t.get('description', '')} Arguments (JSON Schema): {compact(t.get('inputSchema') or {})}" for t in offered.values()]
    return system + "\n\n" + PROMPTED_HEADER + "\n" + "\n".join(lines)


def parse_prompted_call(content: str) -> tuple[str, str] | None:
    """(tool name, raw arguments JSON) when the reply is a prompted tool call."""
    text = strip_fence(content)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("tool"), str):
        return None
    return obj["tool"], compact(obj["arguments"]) if "arguments" in obj else "{}"


# -- tool executors ---------------------------------------------------------

Executor = Callable[[str, dict[str, Any]], tuple[Any, str]]  # -> (result value, tool message text)


def mock_executor(suite: Suite) -> Executor:
    def run(name: str, args: dict[str, Any]) -> tuple[Any, str]:
        result = fixture_result(suite, name, args)
        return result, compact(result)

    return run


def live_executor(upstream: Any) -> Executor:
    """XivMcp over MCP streamable HTTP, via the companion-server client in upstream.py."""
    from .upstream import UpstreamError

    def run(name: str, args: dict[str, Any]) -> tuple[Any, str]:
        try:
            text, _is_error = upstream.call(name, args)
        except UpstreamError as exc:
            result = {"error": str(exc)}
            return result, compact(result)
        try:
            return json.loads(text), text
        except json.JSONDecodeError:
            return text, text

    return run


# -- running ----------------------------------------------------------------


@dataclass
class TaskRun:
    task_id: str
    score: TaskScore
    calls: list[Call]
    answer: str | None
    ttft_ms: float | None
    tokens_per_s: float | None
    output_tokens: int
    duration_ms: float
    detail: str = ""


@dataclass
class RunOutcome:
    tasks: list[TaskRun]
    total_s: float
    tool_calling: str  # native | prompted (as run; see tool_calling_capability)


class Runner:
    def __init__(self, suite: Suite, chat: ChatClient, model: str, executor: Executor, mode: str = "mock", tool_calling: str = "auto") -> None:
        self.suite = suite
        self.chat = chat
        self.model = model
        self.executor = executor
        self.mode = mode
        self.auto = tool_calling == "auto"
        self.tool_calling = "prompted" if tool_calling == "prompted" else "native"

    def warm_up(self) -> None:
        self.chat.stream({"model": self.model, "messages": [{"role": "user", "content": WARMUP_PROMPT}], "temperature": 0, "max_tokens": 8})

    def run(self, task_ids: list[str] | None = None, on_task: Callable[[TaskRun], None] | None = None) -> RunOutcome:
        tasks = [self.suite.task(t) for t in task_ids] if task_ids else self.suite.tasks
        self.warm_up()
        start = time.monotonic()
        runs = []
        for task in tasks:
            result = self.run_task(task)
            runs.append(result)
            if on_task:
                on_task(result)
        return RunOutcome(runs, time.monotonic() - start, self.tool_calling)

    def run_task(self, task: dict[str, Any]) -> TaskRun:
        try:
            return self._run_task(task)
        except BackendError as exc:
            if self.auto and self.tool_calling == "native" and tools_rejected(exc) and self.suite.offered(task):
                self.tool_calling = "prompted"  # for the rest of the run
                return self._run_task(task)
            raise

    def _run_task(self, task: dict[str, Any]) -> TaskRun:
        defaults = self.suite.defaults
        offered = self.suite.offered(task)
        prompted = self.tool_calling == "prompted"
        system = str(self.suite.raw["system_prompt"])
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompted_system(system, offered) if prompted else system},
            {"role": "user", "content": task["prompt"]},
        ]
        tools = [{"type": "function", "function": {"name": n, "description": t.get("description", ""), "parameters": t.get("inputSchema") or {}}} for n, t in offered.items()]
        calls: list[Call] = []
        answer: str | None = None
        failure: str | None = None
        detail = ""
        first_request: float | None = None
        first_delta: float | None = None
        tokens = 0
        gen_s = 0.0
        started = time.monotonic()
        for step in range(int(task.get("max_steps") or defaults.get("max_steps", 6))):
            payload: dict[str, Any] = {
                "model": self.model, "messages": messages,
                "temperature": defaults.get("temperature", 0), "max_tokens": defaults.get("max_tokens", 512),
            }
            if tools and not prompted:
                payload["tools"] = tools
            try:
                turn = self.chat.stream(payload)
            except BackendError as exc:
                if self.auto and step == 0 and tools_rejected(exc) and "tools" in payload:
                    raise
                failure, detail = exc.kind, str(exc)
                first_request = first_request or time.monotonic()
                break
            first_request = first_request or turn.sent
            if turn.first_delta is not None:
                first_delta = first_delta or turn.first_delta
                tokens += turn.output_tokens
                gen_s += max(0.0, turn.end - turn.first_delta)
            if prompted:
                parsed = parse_prompted_call(turn.content)
                if parsed is None:
                    answer = final_answer(turn.content)
                    break
                call = self._execute(offered, *parsed)
                calls.append(call)
                messages.append({"role": "assistant", "content": turn.content})
                messages.append({"role": "user", "content": f"Tool result: {self._message(call)}"})
                continue
            if not turn.tool_calls:
                answer = final_answer(turn.content)
                break
            for i, tc in enumerate(turn.tool_calls):
                tc["id"] = tc["id"] or f"call_{step}_{i}"
            messages.append({"role": "assistant", "content": turn.content, "tool_calls": turn.tool_calls})
            for tc in turn.tool_calls:
                call = self._execute(offered, tc["function"]["name"], tc["function"]["arguments"])
                calls.append(call)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": self._message(call)})
        duration = time.monotonic() - started
        score = score_task(self.suite, task, self.mode, calls, answer, failure)
        ttft = (first_delta - first_request) * 1000 if first_delta is not None and first_request is not None else None
        tps = tokens / gen_s if gen_s > 0 else None
        return TaskRun(task["id"], score, calls, answer, ttft, tps, tokens, duration * 1000, detail)

    def _execute(self, offered: dict[str, dict[str, Any]], name: str, arguments: str) -> Call:
        call = check_call(offered, name, arguments)
        if call.valid:
            call.result, call.text = self.executor(name, call.args or {})
        return call

    @staticmethod
    def _message(call: Call) -> str:
        if not call.valid:
            return compact({"error": f"invalid call: {call.invalid}"})
        return call.text


def tool_calling_capability(suite: Suite, outcome: RunOutcome) -> str:
    expecting = [r for r in outcome.tasks if (suite.task(r.task_id).get("expect") or {}).get("calls")]
    if expecting and not any(r.calls for r in expecting):
        return "none"
    return outcome.tool_calling


# -- machine facts ----------------------------------------------------------


def _nvidia_smi(query: str) -> list[list[str]]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [[part.strip() for part in line.split(",")] for line in out.splitlines() if line.strip()]


def gpu_vendor(name: str) -> str:
    low = name.lower()
    if not low or low == "unknown":
        return "unknown"
    for vendor, words in (("nvidia", ("nvidia", "geforce", "quadro", "tesla", "rtx")), ("amd", ("amd", "radeon")), ("intel", ("intel", "arc ")), ("apple", ("apple",))):
        if any(w in low for w in words):
            return vendor
    return "other"


def hardware_facts() -> dict[str, Any]:
    """GPU (largest nvidia-smi adapter), rounded RAM and OS family. Nothing identifying."""
    name, vram = "unknown", 0
    gpus = []
    for row in _nvidia_smi("name,memory.total"):
        try:
            gpus.append((int(float(row[1])), row[0]))
        except (IndexError, ValueError):
            continue
    if gpus:
        vram, name = max(gpus)
    facts: dict[str, Any] = {"gpu_model": name[:80] or "unknown", "gpu_vendor": gpu_vendor(name), "vram_mb": vram // 256 * 256}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                facts["system_ram_gb"] = round(int(line.split()[1]) / 1024 / 1024)
    except (OSError, ValueError, IndexError):
        pass
    system = platform.system()
    facts["os"] = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(system, "other")
    return facts


def _get_json(client: httpx.Client, url: str, headers: dict[str, str]) -> Any:
    try:
        response = client.get(url, headers=headers, timeout=5)
        return response.json() if response.status_code == 200 else MISSING
    except (httpx.HTTPError, ValueError):
        return MISSING


def detect_backend(client: httpx.Client, base_url: str, headers: dict[str, str] | None = None) -> tuple[str, str | None]:
    headers = headers or {}
    root = api_root(base_url)
    data = _get_json(client, f"{root}/api/version", headers)
    if isinstance(data, dict) and isinstance(data.get("version"), str):
        return "ollama", data["version"][:32]
    if _get_json(client, f"{root}/api/v0/models", headers) is not MISSING:
        return "lmstudio", None
    props = _get_json(client, f"{root}/props", headers)
    if isinstance(props, dict) and any(k in props for k in ("default_generation_settings", "total_slots", "build_info", "model_path")):
        build = props.get("build_info")
        return "llamacpp", build[:32] if isinstance(build, str) else None
    health = _get_json(client, f"{root}/health", headers)
    if isinstance(health, dict) and isinstance(health.get("status"), str) and set(health) <= {"status", "slots_idle", "slots_processing"}:
        return "llamacpp", None
    return "openai-compatible", None


def ollama_model_info(client: httpx.Client, base_url: str, model: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """quant/context (+ family, params_b) from Ollama /api/show; {} when unavailable."""
    try:
        response = client.post(f"{api_root(base_url)}/api/show", json={"model": model}, headers=headers or {}, timeout=10)
        data = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return {}
    info: dict[str, Any] = {}
    details = data.get("details") or {}
    if details.get("quantization_level"):
        info["quant"] = str(details["quantization_level"])[:24]
    if details.get("family"):
        info["family"] = str(details["family"])[:40]
    size = re.fullmatch(r"\s*([\d.]+)\s*([MBT])\s*", str(details.get("parameter_size") or ""), re.I)
    if size:
        info["params_b"] = round(float(size.group(1)) * {"M": 0.001, "B": 1, "T": 1000}[size.group(2).upper()], 3)
    num_ctx = re.search(r"(?m)^\s*num_ctx\s+(\d+)", str(data.get("parameters") or ""))
    if num_ctx:
        info["context"] = int(num_ctx.group(1))
    else:
        for key, value in (data.get("model_info") or {}).items():
            if key.endswith(".context_length") and isinstance(value, int):
                info["context"] = value
                break
    return info


class VramSampler:
    """Peak GPU memory (MB) sampled before, every ``interval`` s during, and after a run."""

    def __init__(self, read: Callable[[], int | None] | None, interval: float = 1.0) -> None:
        self.read = read
        self.interval = interval
        self.peak: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample(self) -> None:
        if self.read is None:
            return
        try:
            value = self.read()
        except Exception:  # a failed reading is simply not a reading
            value = None
        if value is not None and (self.peak is None or value > self.peak):
            self.peak = value

    def __enter__(self) -> "VramSampler":
        self.sample()
        if self.read is not None:
            self._thread = threading.Thread(target=self._loop, name="vram-sampler", daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.sample()

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 5)
        self.sample()


def vram_reader(kind: str, base_url: str) -> Callable[[], int | None] | None:
    if kind == "ollama":
        url = f"{api_root(base_url)}/api/ps"

        def ollama() -> int | None:
            models = httpx.get(url, timeout=5).json().get("models") or []
            return sum(int(m.get("size_vram") or 0) for m in models) // (1024 * 1024)

        return ollama
    if shutil.which("nvidia-smi"):

        def smi() -> int | None:
            rows = _nvidia_smi("memory.used")
            return sum(int(float(r[0])) for r in rows) if rows else None

        return smi
    return None


# -- result document --------------------------------------------------------


def _median(values: list[float | None]) -> float:
    present = [v for v in values if v is not None]
    return float(statistics.median(present)) if present else 0.0


def build_result(
    suite: Suite, mode: str, hardware: dict[str, Any], backend: dict[str, Any], model: dict[str, Any],
    outcome: RunOutcome, peak_vram_mb: int | None,
) -> dict[str, Any]:
    runs = outcome.tasks
    n = len(runs) or 1
    all_calls = sum(r.score.total_calls for r in runs)
    valid_calls = sum(r.score.valid_calls for r in runs)
    answers = [r.score.answer_score for r in runs if r.score.answer_score is not None]
    metrics: dict[str, Any] = {
        "score": round(100 * sum(r.score.score for r in runs) / n, 1),
        "success_rate": round(sum(r.score.success for r in runs) / n, 4),
        "tool_call_validity": round(valid_calls / all_calls, 4) if all_calls else 1.0,
        "tokens_per_s": round(_median([r.tokens_per_s for r in runs]), 2),
        "ttft_ms": round(_median([r.ttft_ms for r in runs]), 1),
        "peak_vram_mb": peak_vram_mb,
        "total_s": round(outcome.total_s, 2),
    }
    if answers:
        metrics["quality"] = round(sum(answers) / len(answers), 4)
    tasks = [
        {
            "id": r.task_id,
            "success": r.score.success,
            "score": round(r.score.score, 4),
            "tool_calls": r.score.total_calls,
            "tool_calls_valid": r.score.valid_calls,
            "ttft_ms": None if r.ttft_ms is None else round(r.ttft_ms, 1),
            "tokens_per_s": None if r.tokens_per_s is None else round(r.tokens_per_s, 2),
            "output_tokens": r.output_tokens,
            "duration_ms": round(r.duration_ms, 1),
            "error": r.score.error,
        }
        for r in runs
    ]
    return {
        "schema_version": 1,
        "suite": {"id": suite.id, "version": suite.version, "sha256": suite.sha256},
        "client": {"name": CLIENT_NAME, "version": __version__},
        "mode": mode,
        "hardware": hardware,
        "backend": {k: v for k, v in backend.items() if v is not None},
        "model": {**model, "tool_calling": tool_calling_capability(suite, outcome)},
        "metrics": metrics,
        "tasks": tasks,
    }


def result_errors(doc: dict[str, Any], schema_path: Path = RESULTS_SCHEMA) -> list[str]:
    return validate(json.loads(schema_path.read_text()), doc)


# -- persistence and submission ---------------------------------------------


class Store:
    """Every run in ``<state_dir>/bench.sqlite``."""

    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, suite TEXT NOT NULL, "
            "mode TEXT NOT NULL, model TEXT NOT NULL, score REAL NOT NULL, result TEXT NOT NULL, "
            "submitted INTEGER NOT NULL DEFAULT 0, submitted_at TEXT)"
        )
        self.db.commit()

    def add(self, doc: dict[str, Any]) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (created_at, suite, mode, model, score, result) VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), f"{doc['suite']['id']}@{doc['suite']['version']}", doc["mode"], doc["model"]["name"], doc["metrics"]["score"], json.dumps(doc)),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def get(self, run_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT result FROM runs WHERE id = ?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def mark_submitted(self, run_id: int) -> None:
        self.db.execute("UPDATE runs SET submitted = 1, submitted_at = ? WHERE id = ?", (_now(), run_id))
        self.db.commit()

    def rows(self, limit: int = 50) -> list[tuple[Any, ...]]:
        return self.db.execute(
            "SELECT id, created_at, suite, mode, model, score, submitted FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def leaderboard_url() -> str:
    return os.environ.get("ALMANAC_LEADERBOARD_URL") or LEADERBOARD_URL


def submit(doc: dict[str, Any], client: httpx.Client | None = None, url: str | None = None, token: str | None = None) -> httpx.Response:
    client = client or httpx.Client()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(url or leaderboard_url(), content=json.dumps(doc), headers=headers, timeout=30)


# -- device link (sign-in for submissions) ------------------------------------
#
# The leaderboard only takes results from a signed-in player. The token and the
# device code are secrets: they travel in request bodies and the Authorization
# header only, and nothing here prints or logs them.


class LinkError(Exception):
    """Linking stopped; ``str(exc)`` is what to show the player."""


def link_api() -> str:
    return (os.environ.get("ALMANAC_LINK_API") or LINK_API).rstrip("/")


def server_message(response: httpx.Response) -> tuple[str, str]:
    """(error, message) from a failure body. Never the raw body: it can hold a token."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = {}
    error = str(data.get("error") or "")[:64]
    message = str(data.get("message") or "").strip()[:300]
    return error, message or (f"HTTP {response.status_code} {error}".strip())


def read_link_token(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def write_link_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")


def open_browser(url: str) -> None:
    """Best effort. Without a display this does nothing rather than start a text browser."""
    if os.name == "posix" and platform.system() != "Darwin" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


def device_link(
    client: httpx.Client,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    browser: Callable[[str], None] | None = None,
) -> str:
    """Run the device-link flow and return the access token. Raises LinkError with the server's message."""
    sleep, clock, browser = sleep or time.sleep, clock or time.monotonic, browser or open_browser
    try:
        response = client.post(f"{link_api()}/device/code", json={"client_id": LINK_CLIENT_ID, "scope": LINK_SCOPE}, timeout=30)
    except httpx.HTTPError as exc:
        raise LinkError(f"could not reach the sign-in server: {exc.__class__.__name__}") from None
    if response.status_code != 200:
        raise LinkError(server_message(response)[1])
    try:
        grant = response.json()
        device_code, user_code, where = str(grant["device_code"]), str(grant["user_code"]), str(grant["verification_uri"])
        interval, expires = max(1.0, float(grant.get("interval", 5))), float(grant.get("expires_in", 900))
    except (ValueError, KeyError, TypeError):
        raise LinkError("the sign-in server sent an answer this client does not understand") from None
    say(f"To submit results, sign in: open {where} and enter the code {user_code}")
    say("Waiting for you to approve Almanac there (Ctrl-C to give up)...")
    complete = grant.get("verification_uri_complete")
    browser(str(complete) if complete else where)
    deadline = clock() + expires
    while True:
        if clock() + interval >= deadline:
            raise LinkError("the code expired before it was approved; run the command again")
        sleep(interval)
        try:
            response = client.post(f"{link_api()}/device/token", json={"client_id": LINK_CLIENT_ID, "device_code": device_code}, timeout=30)
        except httpx.HTTPError:
            continue
        if response.status_code == 200:
            try:
                token = response.json()["access_token"]
            except (ValueError, KeyError, TypeError):
                token = None
            if not isinstance(token, str) or not token:
                raise LinkError("the sign-in server sent an answer this client does not understand")
            return token
        error, message = server_message(response)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise LinkError(message)


def submit_linked(
    doc: dict[str, Any],
    token_path: Path,
    client: httpx.Client | None = None,
    url: str | None = None,
    link: Callable[[httpx.Client], str] | None = None,
) -> httpx.Response:
    """Submit with the stored token, linking first if there is none. A refused token is dropped and linked again, once."""
    client = client or httpx.Client()
    link = link or device_link
    token = read_link_token(token_path)
    relinked = token is None
    if token is None:
        token = link(client)
        write_link_token(token_path, token)
    response = submit(doc, client, url, token)
    if response.status_code == 401 and not relinked and server_message(response)[0] in RELINK_ERRORS:
        token_path.unlink(missing_ok=True)
        token = link(client)
        write_link_token(token_path, token)
        response = submit(doc, client, url, token)
    if response.status_code == 401:
        token_path.unlink(missing_ok=True)
    return response


def unlink(token_path: Path, client: httpx.Client | None = None) -> bool:
    """Revoke the stored token and delete it. True if there was one. The file goes even if the server is unreachable."""
    token = read_link_token(token_path)
    if token is None:
        return False
    try:
        (client or httpx.Client()).post(f"{link_api()}/token/revoke", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except httpx.HTTPError:
        pass
    finally:
        token_path.unlink(missing_ok=True)
    return True
