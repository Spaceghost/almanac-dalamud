"""Task sources. Each returns ``TaskSpec``s; the store de-duplicates by ``source_ref``.

* ``github``  open issues labelled ``autopilot`` (``[autopilot.sources.github] label``)
              in every configured repo that names ``github = "owner/name"``
* ``inbox``   unchecked ``- [ ]`` items in a Markdown file; ``@repo`` picks a
              repository, ``!high`` / ``!low`` adjusts priority
* ``vote``    the top ideas of a public vote site, read-only from its tallies
              endpoint (``{id: {want, maybe, skip}}``) plus its ideas catalogue
* ``ci``      the latest failed workflow run on each repo's base branch
* ``manual``  ``almanac autopilot add "..."`` (and the MCP ``autopilot_add`` tool)

GitHub is read through the ``gh`` CLI with an allow-list of read-only
subcommands (``ReadOnlyGh``); a source can never create, edit or comment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .settings import Repo, expand
from .store import ref_for

SOURCE_VALUE = {"manual": 60.0, "ci": 70.0, "inbox": 55.0, "github": 45.0, "vote": 30.0}

# gh subcommands autopilot may run for reading. Anything else is refused.
READ_ONLY_GH = {("issue", "list"), ("issue", "view"), ("run", "list"), ("run", "view"), ("pr", "checks"), ("pr", "view"), ("pr", "list")}


@dataclass
class TaskSpec:
    source: str
    source_ref: str
    title: str
    body: str = ""
    repo: str = ""
    value: float = 50.0


class GhError(RuntimeError):
    pass


class ReadOnlyGh:
    """Runs ``gh`` for reads only. ``run`` is ``(argv) -> (exit code, stdout)``."""

    def __init__(self, run: Callable[[list[str]], tuple[int, str]]) -> None:
        self._run = run

    def json(self, args: list[str]) -> Any:
        if tuple(args[:2]) not in READ_ONLY_GH:
            raise GhError(f"gh {' '.join(args[:2])} is not a read-only command")
        code, out = self._run(["gh", *args])
        if code != 0:
            raise GhError(f"gh {' '.join(args[:2])} failed: {out.strip()[:300]}")
        return json.loads(out or "null")


def github_issues(gh: ReadOnlyGh, repos: dict[str, Repo], label: str, limit: int = 20) -> list[TaskSpec]:
    out = []
    for repo in repos.values():
        if not repo.github:
            continue
        issues = gh.json(
            ["issue", "list", "--repo", repo.github, "--label", label, "--state", "open", "--limit", str(limit),
             "--json", "number,title,body,labels,url"]
        )
        for issue in issues or []:
            labels = {str(lab.get("name", "")).lower() for lab in issue.get("labels", [])}
            bonus = 20 if "priority" in labels or "p1" in labels else 0
            out.append(
                TaskSpec(
                    "github", f"github:{repo.github}#{issue['number']}", f"#{issue['number']} {issue['title']}",
                    f"{issue.get('url', '')}\n\n{issue.get('body') or ''}", repo.name, SOURCE_VALUE["github"] + bonus + repo.priority,
                )
            )
    return out


def failing_ci(gh: ReadOnlyGh, repos: dict[str, Repo], limit: int = 5) -> list[TaskSpec]:
    """The newest run on the base branch, if it failed. A later green run clears it."""
    out = []
    for repo in repos.values():
        if not repo.github:
            continue
        runs = gh.json(
            ["run", "list", "--repo", repo.github, "--branch", repo.base, "--limit", str(limit),
             "--json", "databaseId,displayTitle,workflowName,conclusion,status,url,headSha"]
        ) or []
        by_workflow: dict[str, dict[str, Any]] = {}
        for run in runs:  # newest first
            by_workflow.setdefault(str(run.get("workflowName", "")), run)
        for workflow, run in by_workflow.items():
            if run.get("conclusion") != "failure":
                continue
            out.append(
                TaskSpec(
                    "ci", f"ci:{repo.github}:{run.get('headSha', run['databaseId'])}:{workflow}",
                    f"Fix failing CI on {repo.base}: {workflow} ({run.get('displayTitle', '')})"[:200],
                    f"{run.get('url', '')}\nRun {run['databaseId']} on {repo.base} failed. Read its logs "
                    f"(gh run view {run['databaseId']} --log-failed) and fix the cause.",
                    repo.name, SOURCE_VALUE["ci"] + repo.priority,
                )
            )
    return out


INBOX_ITEM = re.compile(r"^\s*[-*]\s+\[ \]\s+(?P<text>.+?)\s*$")


def inbox(path: Path, repos: dict[str, Repo]) -> list[TaskSpec]:
    """``- [ ] fix the resize flicker @ghostty-dalamud !high`` -> a task. Checked items are ignored."""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        match = INBOX_ITEM.match(line)
        if not match:
            continue
        text = match.group("text")
        repo = ""
        for name in re.findall(r"(?<!\S)@([\w.-]+)", text):
            if name in repos:
                repo = name
        value = SOURCE_VALUE["inbox"] + (25 if "!high" in text else 0) - (20 if "!low" in text else 0)
        title = re.sub(r"(?<!\S)(@[\w.-]+|![a-z]+)", "", text).strip()
        out.append(TaskSpec("inbox", f"inbox:{ref_for(title)}", title, text, repo, value + (repos[repo].priority if repo else 0)))
    return out


def vote_ideas(get_json: Callable[[str], Any], tallies_url: str, ideas_url: str, top: int, repo: str) -> list[TaskSpec]:
    """Top ``top`` ideas by (2*want + maybe - skip). Read-only public endpoints."""
    tallies = get_json(tallies_url) or {}
    ideas: dict[str, dict[str, Any]] = {}
    if ideas_url:
        catalogue = get_json(ideas_url) or {}
        ideas = {str(i["id"]): i for i in catalogue.get("ideas", [])}
    scored = []
    for idea_id, t in tallies.items():
        score = 2 * int(t.get("want", 0)) + int(t.get("maybe", 0)) - int(t.get("skip", 0))
        scored.append((score, str(idea_id)))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for score, idea_id in scored[: max(0, top)]:
        if score <= 0:
            continue
        idea = ideas.get(idea_id, {})
        title = str(idea.get("title") or idea_id)
        body = f"Top-voted idea {idea_id} (score {score}) from the vote site.\n{idea.get('summary') or idea.get('description') or ''}"
        out.append(TaskSpec("vote", f"vote:{idea_id}", f"Vote idea: {title}", body, repo, SOURCE_VALUE["vote"] + min(40.0, float(score))))
    return out


def collect(
    settings_sources: dict[str, Any], repos: dict[str, Repo], gh: ReadOnlyGh | None, get_json: Callable[[str], Any] | None,
    on_error: Callable[[str, Exception], None],
) -> list[TaskSpec]:
    """Poll every enabled source; one failing source never stops the others."""
    specs: list[TaskSpec] = []
    gh_cfg = settings_sources.get("github", {})
    inbox_cfg = settings_sources.get("inbox", {})
    vote_cfg = settings_sources.get("vote", {})
    ci_cfg = settings_sources.get("ci", {})
    jobs: list[tuple[str, Callable[[], list[TaskSpec]]]] = []
    if gh_cfg.get("enabled") and gh:
        jobs.append(("github", lambda: github_issues(gh, repos, str(gh_cfg.get("label", "autopilot")), int(gh_cfg.get("limit", 20)))))
    if ci_cfg.get("enabled") and gh:
        jobs.append(("ci", lambda: failing_ci(gh, repos, int(ci_cfg.get("limit", 5)))))
    if inbox_cfg.get("enabled") and inbox_cfg.get("path"):
        jobs.append(("inbox", lambda: inbox(expand(inbox_cfg["path"]), repos)))
    if vote_cfg.get("enabled") and vote_cfg.get("url") and get_json:
        base = str(vote_cfg["url"]).rstrip("/")
        jobs.append(
            ("vote", lambda: vote_ideas(get_json, f"{base}/api/tallies", str(vote_cfg.get("ideas_url") or f"{base}/ideas.json"),
                                        int(vote_cfg.get("top", 3)), str(vote_cfg.get("repo", ""))))
        )
    for name, job in jobs:
        try:
            specs += job()
        except Exception as exc:  # noqa: BLE001 - a source is best effort
            on_error(name, exc)
    return specs
