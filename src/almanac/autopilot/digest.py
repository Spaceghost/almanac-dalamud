"""The morning digest: what happened since the last one, as a Markdown file.

Written once a day after ``digest_hour`` (local time) to
``<digest_dir>/YYYY-MM-DD.md``; a one-line headline is also shown as an
in-game toast when the game is running.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .store import Store


def _when(ts: float) -> str:
    return time.strftime("%a %H:%M", time.localtime(ts))


def build_digest(store: Store, since: float, now: float, needs_you: list[str], backends: list[dict[str, Any]] | None = None) -> tuple[str, str]:
    tasks = store.tasks()
    done = [t for t in tasks if t.state == "done" and t.updated >= since]
    prs = [t for t in tasks if t.pr_url and t.updated >= since]
    waiting = [t for t in tasks if t.state == "waiting"]
    owner = [t for t in tasks if t.state == "needs_owner"]
    review = [t for t in tasks if t.state == "review"]
    queued = [t for t in tasks if t.state in ("queued", "ready")]
    spend = store.usage_since(since, "cloud")
    local = store.usage_since(since, "local")
    failures = [e for e in store.events(since=since) if e["kind"] == "fail"]
    lines = [
        f"# autopilot digest {time.strftime('%Y-%m-%d', time.localtime(now))}",
        "",
        f"Covers {_when(since)} to {_when(now)}.",
        "",
        f"- done: {len(done)}   draft PRs: {len(prs)}   in review (CI): {len(review)}",
        f"- waiting on your approval: {len(waiting)}   needs you: {len(owner)}   queued: {len(queued)}",
        f"- cloud coding: {spend['runs']} run(s), {spend['tokens']:,} tokens, ${spend['cost']:.2f}",
        f"- local coding: {local['runs']} run(s) (no cloud cost)",
        "",
        "## Done",
        "",
        *([f"- #{t.id} {t.title}" + (f" - {t.pr_url}" if t.pr_url else "") + f" ({t.note})" for t in done] or ["- nothing"]),
        "",
        "## Pull requests (drafts)",
        "",
        *([f"- #{t.id} {t.pr_url} [{t.state}]" for t in prs] or ["- none"]),
        "",
        "## Waiting on you",
        "",
        *([f"- {item}" for item in needs_you] or ["- nothing"]),
        "",
        "## Failures overnight",
        "",
        *([f"- {_when(e['ts'])} #{e['task_id']}: {e['message'][:200]}" for e in failures[-20:]] or ["- none"]),
        "",
        "## Model backends",
        "",
        *([f"- {b['name']} ({b['model']}, {', '.join(b['roles'])}): {'up' if b['healthy'] else 'down'} - {b['reason']}"
           + (f", last used {_when(b['last_used'])}" if b.get('last_used') else "") for b in (backends or [])] or ["- (none reported)"]),
        "",
        "## Next up",
        "",
        *([f"- #{t.id} [{t.value:.0f}] {t.title}" for t in sorted(queued, key=lambda t: -t.value)[:8]] or ["- queue empty"]),
        "",
    ]
    headline = f"autopilot: {len(done)} done, {len(prs)} PRs, {len(waiting) + len(owner)} need you, ${spend['cost']:.2f} spent"
    return "\n".join(lines), headline


def write_digest(
    store: Store, directory: Path, since: float, now: float, needs_you: list[str], redact: Callable[[str], str],
    backends: list[dict[str, Any]] | None = None,
) -> tuple[Path, str]:
    text, headline = build_digest(store, since, now, needs_you, backends)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.strftime('%Y-%m-%d', time.localtime(now))}.md"
    path.write_text(redact(text))
    return path, headline
