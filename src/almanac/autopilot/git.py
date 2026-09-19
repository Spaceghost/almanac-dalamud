"""Git and GitHub operations for one task: worktree, commit, push, draft PR, CI.

Hard rules, enforced here and not by configuration:

* the main checkout is never modified: work happens in
  ``<state>/autopilot/worktrees/<repo>-<task>`` on branch ``autopilot/<task>-<slug>``;
* pushes go only to that task branch, never to a protected branch or the base
  branch, and never with ``--force``;
* pull requests are always opened with ``--draft`` and labelled with who wrote
  them (``autopilot``, ``by:claude``, ``by:local-qwen3.5-9b`` ...);
* in dry-run mode push and PR creation are logged, not run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .sandbox import ProcResult
from .settings import Repo

Run = Callable[[list[str], Path, float], ProcResult]


class GitError(RuntimeError):
    pass


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-") or "task"


def branch_for(task_id: int, title: str) -> str:
    return f"autopilot/{task_id}-{slug(title)}"


@dataclass
class GitOps:
    run: Run
    worktrees: Path
    protected: set[str]
    dry_run: bool
    log: Callable[[str], None]
    run_stdin: Callable[[list[str], Path, str], ProcResult] | None = None  # for gh pr create --body-file -

    def _ok(self, argv: list[str], cwd: Path, timeout: float = 300) -> str:
        result = self.run(argv, cwd, timeout)
        if not result.ok:
            raise GitError(f"{' '.join(argv[:3])} failed ({result.stopped or result.code}): {result.output.strip()[-500:]}")
        return result.output

    def check_branch(self, repo: Repo, branch: str) -> None:
        if not branch.startswith("autopilot/") or branch in self.protected or branch == repo.base:
            raise GitError(f"refusing to use branch {branch!r}: only autopilot/* task branches are allowed")

    def git_dir(self, repo: Repo) -> Path:
        out = self._ok(["git", "-C", str(repo.path), "rev-parse", "--path-format=absolute", "--git-common-dir"], repo.path)
        return Path(out.strip())

    def ensure_worktree(self, repo: Repo, task_id: int, branch: str) -> Path:
        self.check_branch(repo, branch)
        path = self.worktrees / f"{repo.name}-{task_id}"
        if (path / ".git").exists():
            return path
        self.worktrees.mkdir(parents=True, exist_ok=True)
        self._ok(["git", "-C", str(repo.path), "fetch", "--quiet", repo.remote, repo.base], repo.path, 600)
        self._ok(["git", "-C", str(repo.path), "worktree", "add", "-B", branch, str(path), f"{repo.remote}/{repo.base}"], repo.path)
        self.log(f"worktree {path} on {branch} from {repo.remote}/{repo.base}")
        return path

    def remove_worktree(self, repo: Repo, path: Path) -> None:
        if path.exists():
            self._ok(["git", "-C", str(repo.path), "worktree", "remove", "--force", str(path)], repo.path)

    def commit_leftovers(self, worktree: Path, message: str) -> bool:
        if not self._ok(["git", "-C", str(worktree), "status", "--porcelain"], worktree).strip():
            return False
        self._ok(["git", "-C", str(worktree), "add", "-A"], worktree)
        self._ok(["git", "-C", str(worktree), "commit", "-q", "-m", message], worktree)
        return True

    def ahead(self, repo: Repo, worktree: Path) -> int:
        out = self._ok(["git", "-C", str(worktree), "rev-list", "--count", f"{repo.remote}/{repo.base}..HEAD"], worktree)
        return int(out.strip() or 0)

    def diffstat(self, repo: Repo, worktree: Path) -> str:
        return self._ok(["git", "-C", str(worktree), "diff", "--stat", f"{repo.remote}/{repo.base}...HEAD"], worktree)

    def push(self, repo: Repo, worktree: Path, branch: str) -> None:
        self.check_branch(repo, branch)
        argv = ["git", "-C", str(worktree), "push", "--quiet", repo.remote, f"HEAD:refs/heads/{branch}"]
        if self.dry_run:
            self.log("dry-run: would run " + " ".join(argv))
            return
        self._ok(argv, worktree, 600)

    def diff(self, repo: Repo, worktree: Path, limit: int = 40000) -> str:
        return self._ok(["git", "-C", str(worktree), "diff", f"{repo.remote}/{repo.base}...HEAD"], worktree)[:limit]

    def ensure_labels(self, repo: Repo, worktree: Path, labels: list[str]) -> None:
        for label in labels:
            # --force makes it idempotent (creates or updates); failures are not fatal
            self.run(["gh", "label", "create", label, "--repo", repo.github, "--force", "--color", "5319e7",
                      "--description", "written by almanac autopilot"], worktree, 60)

    def open_draft_pr(self, repo: Repo, worktree: Path, branch: str, title: str, body: str, labels: list[str] | None = None) -> str:
        self.check_branch(repo, branch)
        if not repo.github:
            raise GitError(f"repo {repo.name} has no github = 'owner/name'; cannot open a pull request")
        labels = labels or []
        argv = ["gh", "pr", "create", "--draft", "--repo", repo.github, "--base", repo.base, "--head", branch, "--title", title[:200]]
        for label in labels:
            argv += ["--label", label]
        argv += ["--body-file", "-"]
        if self.dry_run:
            self.log("dry-run: would run " + " ".join(argv[:-2]))
            return f"(dry-run) https://github.com/{repo.github}/pull/new/{branch}"
        if labels:
            self.ensure_labels(repo, worktree, labels)
        existing = self.run(["gh", "pr", "view", branch, "--repo", repo.github, "--json", "url"], worktree, 60)
        if existing.ok:
            try:
                url = str(json.loads(existing.output)["url"])
            except (ValueError, KeyError):
                url = ""
            if url:
                if labels:
                    self.run(["gh", "pr", "edit", branch, "--repo", repo.github, *[a for lab in labels for a in ("--add-label", lab)]], worktree, 60)
                return url
        if self.run_stdin is None:
            raise GitError("no stdin-capable runner configured")
        result = self.run_stdin(argv, worktree, body)
        if not result.ok:
            raise GitError(f"gh pr create failed: {result.output.strip()[-500:]}")
        urls = re.findall(r"https://\S+/pull/\d+", result.output)
        return urls[-1] if urls else result.output.strip().splitlines()[-1]

    def comment_pr(self, repo: Repo, worktree: Path, branch: str, body: str) -> None:
        argv = ["gh", "pr", "comment", branch, "--repo", repo.github, "--body-file", "-"]
        if self.dry_run:
            self.log("dry-run: would run " + " ".join(argv[:-2]))
            return
        if self.run_stdin is None:
            raise GitError("no stdin-capable runner configured")
        result = self.run_stdin(argv, worktree, body)
        if not result.ok:
            raise GitError(f"gh pr comment failed: {result.output.strip()[-300:]}")

    def ci_status(self, repo: Repo, branch: str, cwd: Path) -> str:
        """pass | fail | pending | none (no checks reported)."""
        if self.dry_run:
            return "pass"
        result = self.run(["gh", "pr", "checks", branch, "--repo", repo.github, "--json", "bucket,name"], cwd, 120)
        try:
            checks = json.loads(result.output or "[]")
        except ValueError:
            return "pending" if "no checks" not in result.output.lower() else "none"
        buckets = {str(c.get("bucket", "")) for c in checks}
        if not checks:
            return "none"
        if buckets & {"fail", "cancel"}:
            return "fail"
        if "pending" in buckets:
            return "pending"
        return "pass"
