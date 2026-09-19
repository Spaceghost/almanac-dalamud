"""The autopilot loop.

Each tick:

1. stop if the kill switch file exists or ``stop`` was requested;
2. refresh the game link (the game may have started or closed);
3. poll task sources (every ``source_interval_seconds``);
4. check parked approval tickets: approved -> resume the plan after that step,
   denied/expired/cancelled -> re-plan the rest of the task;
5. write the morning digest once a day;
6. collect finished steps, then (unless paused) start the next step of the
   highest-value runnable tasks, up to ``max_parallel`` at once. Each task has
   at most one step in flight.

Resources: a ``code`` step runs on the cloud (Claude Code / Codex) while the
daily caps allow and the account is not limited, and otherwise on a local
coding agent with a lease on a ``coder`` backend of the pool. ``review``
steps lease a ``reviewer`` backend other than the one that wrote the code.
Planning and summaries use any healthy ``planner`` backend. Tests, pushes,
PRs, CI polling and game calls need no model.

A step either finishes, fails (attempts + exponential backoff; after
``max_attempts`` the task goes to the owner), parks on a ticket (the task
waits; others keep running), or is deferred (no backend free, game closed,
CI still running) without counting as a failure.
"""

from __future__ import annotations

import os
import shutil
import signal
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .budget import Budget, backoff_seconds, next_local_midnight
from .coder import (
    claude_argv, classify_limit, codex_argv, limit_backoff_seconds, local_coder_argv, local_coder_env, parse_claude,
    parse_codex, parse_local, session_prompt,
)
from .digest import write_digest
from .game import Game, GameError
from .git import GitError, GitOps, branch_for, slug
from .planner import Model, ModelUnavailable, Planner, review_diff, split_for_local
from .pool import Backend, BackendModel, Lease, Pool
from .sandbox import ProcResult, ProcRunner, Redactor, SecretError, bwrap_argv, child_env, resolve_secrets, sandbox_mode
from .settings import Repo, Settings, expand
from .sources import ReadOnlyGh, collect
from .store import Step, Store, Task

Audit = Callable[..., None]


@dataclass
class Outcome:
    status: str  # done | fail | park | defer | wait | skip | owner | split
    result: str = ""
    note: str = ""
    delay: float = 0.0
    ticket: str = ""
    supersede: bool = False  # a failed step replaced by newly inserted steps
    parts: list[str] = field(default_factory=list)  # for split


@dataclass
class Job:
    task: Task
    step: Step | None  # None = planning
    lease: Lease | None
    mode: str  # cloud | local | "" (no model)
    future: Future[Any] | None = None


class InlineExecutor:
    """Runs a job immediately (tests, ``--once``). Same interface as ThreadPoolExecutor."""

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001 - surfaced by reap()
            future.set_exception(exc)
        return future

    def shutdown(self, wait: bool = True) -> None:
        return None


def author_label(author: str) -> str:
    return ("by:" + slug(author.replace("@", "-").replace(":", "-"), 45)).strip("-")


class Autopilot:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        planner: Planner,
        game: Game,
        proc: ProcRunner,
        audit: Audit,
        pool: Pool | None = None,
        environ: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        gh: ReadOnlyGh | None = None,
        get_json: Callable[[str], Any] | None = None,
        op_read: Callable[[str], str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        executor: Any = None,
        backend_model: Callable[[Backend], Model] | None = None,
    ) -> None:
        self.s = settings
        self.store = store
        self.planner = planner
        self.game = game
        self.proc = proc
        self.audit_fn = audit
        self.pool = pool or Pool([], clock=clock)
        self.environ = dict(os.environ if environ is None else environ)
        self.clock = clock
        self.gh = gh
        self.get_json = get_json
        self.op_read = op_read
        self.which = which
        self.executor = executor or ThreadPoolExecutor(max_workers=max(1, int(settings.caps["max_parallel"])), thread_name_prefix="autopilot")
        self.backend_model = backend_model or (lambda b: BackendModel(b))
        self.redact = Redactor()
        self.dry_run = bool(settings["dry_run"])
        self.budget = Budget(store, settings.caps, clock)
        self.git = GitOps(self._git_run, settings.worktrees_dir, settings.protected, self.dry_run, lambda m: self.log(None, "git", m), self._git_run_stdin)
        self.jobs: dict[int, Job] = {}
        self.cloud_inflight = 0
        self._stop = False

    # -- plumbing ------------------------------------------------------------
    def log(self, task_id: int | None, kind: str, message: str) -> None:
        self.store.log(task_id, kind, self.redact(message))

    def audit(self, event: str, name: str, args: dict[str, Any], **extra: Any) -> None:
        clean = {k: self.redact(str(v)) if isinstance(v, str) else v for k, v in args.items()}
        self.audit_fn(event, name, clean, "autopilot", dry_run=self.dry_run, **extra)

    def _git_run(self, argv: list[str], cwd: Path, timeout: float) -> ProcResult:
        # git/gh run as the owner (push needs their credentials), never inside a coding session.
        return self.proc(argv, cwd, self.environ, timeout)

    def _git_run_stdin(self, argv: list[str], cwd: Path, stdin: str) -> ProcResult:
        return self.proc(argv, cwd, self.environ, 300, stdin)

    def request_stop(self, *_: Any) -> None:
        self._stop = True

    @property
    def killed(self) -> bool:
        return self.s.kill_switch.exists()

    def _due(self, key: str, interval: float) -> bool:
        last = float(self.store.flag(key, "0") or 0)
        if self.clock() - last >= interval:
            self.store.set_flag(key, str(self.clock()))
            return True
        return False

    # -- the loop ------------------------------------------------------------
    def run_forever(self, once: bool = False, sleep: Callable[[float], None] = time.sleep) -> int:
        if self.killed:
            print(f"kill switch {self.s.kill_switch} exists; remove it to run")
            return 3
        self.store.set_flag("stop", "")
        recovered = self.store.recover()
        if recovered:
            self.log(None, "recover", f"{recovered} interrupted step(s) returned to pending")
        try:
            signal.signal(signal.SIGTERM, self.request_stop)
        except ValueError:  # not the main thread (tests)
            pass
        self.store.set_flag("pid", str(os.getpid()))
        self.log(None, "start", f"autopilot started (dry_run={self.dry_run})")
        self.game.refresh(force=True)
        self.game.post_status("autopilot running" + (" (dry run)" if self.dry_run else ""), "running")
        try:
            while True:
                what = self.tick()
                if what in ("killed", "stopped") or once:
                    break
                sleep(float(self.s["idle_seconds"] if what in ("idle", "paused") else self.s["tick_seconds"]))
        finally:
            self.executor.shutdown(wait=True)  # sessions die within a second of the kill switch
            self.reap()
            self.store.set_flag("pid", "")
            self.log(None, "stop", "autopilot stopped")
            self.game.post_status("autopilot stopped", "info")
        return 0

    def tick(self) -> str:
        if self.killed:
            self.log(None, "kill", f"kill switch {self.s.kill_switch} present")
            return "killed"
        if self._stop or self.store.flag("stop"):
            return "stopped"
        self.game.refresh()
        if self._due("sources_at", float(self.s["source_interval_seconds"])):
            self.poll_sources()
        if self._due("tickets_at", float(self.s["ticket_poll_seconds"])):
            self.poll_tickets()
        self.maybe_digest()
        self.reap()
        if self.store.flag("paused"):
            self.sync_objectives()
            return "paused"
        started = self.dispatch()
        self.reap()
        self.sync_objectives()
        return "working" if started or self.jobs else "idle"

    # -- sources -------------------------------------------------------------
    def poll_sources(self) -> int:
        specs = collect(
            self.s.section("sources"), self.s.repos, self.gh, self.get_json,
            lambda name, exc: self.log(None, "source", f"{name} failed: {exc.__class__.__name__}: {str(exc)[:300]}"),
        )
        added = 0
        for spec in specs:
            _, created = self.store.add_task(spec.source, spec.title, spec.body, spec.repo, spec.value, spec.source_ref)
            added += int(created)
        if added:
            self.log(None, "source", f"{added} new task(s) from sources")
        return added

    # -- tickets -------------------------------------------------------------
    def poll_tickets(self) -> None:
        for step in self.store.waiting_steps():
            try:
                ticket = self.game.get_ticket(step.ticket_id)
            except GameError as exc:
                self.log(step.task_id, "ticket", f"cannot check ticket {step.ticket_id}: {exc}")
                continue
            if not ticket.settled:
                continue
            task = self.store.get(step.task_id)
            self.audit("ticket_resolved", str(step.args.get("tool", "")), {"task": task.id, "ticket": ticket.id}, state=ticket.state)
            if ticket.approved:
                self.store.update_step(step.id, state="done", result=f"ticket {ticket.id} {ticket.state}: {ticket.result}"[:4000])
                self.log(task.id, "ticket", f"ticket {ticket.id} {ticket.state}; resuming after step {step.idx + 1}")
            else:
                self.store.update_step(step.id, state="skipped", result=f"ticket {ticket.id} {ticket.state}")
                self.log(task.id, "ticket", f"ticket {ticket.id} {ticket.state}; re-planning")
                steps, how = self.planner.replan_after_denial(
                    task, f"{step.args.get('tool', '')} {step.args.get('args', {})}", self._has_repo(task),
                    self.game.read_tools(), self.game.action_tools(),
                )
                self.store.set_plan(task.id, steps)
                self.log(task.id, "plan", f"re-planned after {ticket.state} ({how})")
            if not self.store.get(task.id).blockers and task.state == "waiting":
                self.store.set_state(task.id, "ready", f"ticket {ticket.id} {ticket.state}")

    # -- dispatch ----------------------------------------------------------------
    def _repo(self, task: Task) -> Repo | None:
        return self.s.repos.get(task.repo) if task.repo else None

    def _has_repo(self, task: Task) -> bool:
        return self._repo(task) is not None

    def cloud_available(self) -> tuple[bool, str]:
        until = float(self.store.flag("cloud_blocked_until", "0") or 0)
        if until > self.clock():
            return False, f"cloud {self.store.flag('cloud_blocked_reason')} until {time.strftime('%H:%M', time.localtime(until))}"
        verdict = self.budget.may_code()
        return verdict.ok, verdict.reason

    def author_backends(self, task: Task) -> set[str]:
        return {a.rsplit("@", 1)[1] for a in task.authors.split(",") if a.startswith("local:") and "@" in a}

    def dispatch(self) -> int:
        started = 0
        limit = max(1, int(self.s.caps["max_parallel"]))
        for task in self.store.candidates(set(self.jobs)):
            if len(self.jobs) >= limit:
                break
            job = self.prepare(task)
            if job is None:
                continue
            self.jobs[task.id] = job
            job.future = self.executor.submit(self.execute, job)
            started += 1
        return started

    def prepare(self, task: Task) -> Job | None:
        """Pick the step and reserve what it needs. None = nothing to start for this task now."""
        if task.repo and task.repo not in self.s.repos:
            self.store.set_state(task.id, "needs_owner", f"repo {task.repo} is not in the autopilot allow-list")
            return None
        if task.state == "queued":
            return Job(task, None, None, "")
        step = task.next_step()
        if step is None:
            self.finish(task, "all steps done")
            return None
        lease: Lease | None = None
        mode = ""
        if step.kind == "code":
            cloud_ok, why = self.cloud_available()
            if cloud_ok and not step.args.get("local_only") and self.cloud_inflight < int(self.s.caps["cloud_slots"]):
                mode = "cloud"
                self.cloud_inflight += 1
            else:
                local = self.budget.may_code_local()
                lease = self.pool.acquire("coder", set(step.args.get("avoid_backends", []))) if local.ok else None
                if lease is None:
                    if cloud_ok and not step.args.get("local_only"):
                        return None  # the cloud slot is busy; try again next tick
                    reason = why if local.ok else local.reason
                    self.apply(task, step, Outcome("defer", note=f"{reason}; no local coder backend free", delay=600))
                    return None
                mode = "local"
        elif step.kind == "review":
            lease = self.pool.acquire("reviewer", self.author_backends(task))
            if lease is None:
                self.apply(task, step, Outcome("defer", note="no reviewer backend free or healthy", delay=600))
                return None
            mode = "local"
        self.store.update_step(step.id, state="running")
        total = max(1, len(task.steps))
        where = f" on {lease.label}" if lease else (" on cloud" if mode == "cloud" else "")
        self.game.post_status(f"#{task.id} step {step.idx + 1}/{total}: {step.kind}{where}"[:200], "running", progress=step.idx / total)
        self.log(task.id, "step", f"start {step.idx + 1}/{total} {step.kind}{where}: {step.title}")
        return Job(task, step, lease, mode)

    def execute(self, job: Job) -> Outcome | None:
        """Runs in a worker thread."""
        if job.step is None:
            self.plan(job.task)
            return None
        try:
            return getattr(self, f"step_{job.step.kind}")(job.task, job.step, job.lease, job.mode)  # type: ignore[no-any-return]
        except (GitError, GameError, SecretError, OSError, ModelUnavailable) as exc:
            return Outcome("fail", note=f"{exc.__class__.__name__}: {exc}")

    def reap(self) -> None:
        for task_id, job in list(self.jobs.items()):
            if job.future is None or not job.future.done():
                continue
            del self.jobs[task_id]
            self.pool.release(job.lease)
            if job.mode == "cloud":
                self.cloud_inflight = max(0, self.cloud_inflight - 1)
            try:
                outcome = job.future.result()
            except Exception as exc:  # noqa: BLE001 - a bug in one step must not stop the loop
                outcome = Outcome("fail", note=f"internal error: {exc.__class__.__name__}: {exc}")
            if job.step is not None and outcome is not None:
                self.apply(self.store.get(task_id), job.step, outcome)

    # -- state changes (main thread) -----------------------------------------
    def plan(self, task: Task) -> None:
        steps, how = self.planner.plan(task, self._has_repo(task), self.game.read_tools(), self.game.action_tools())
        branch = branch_for(task.id, task.title) if self._has_repo(task) else ""
        self.store.set_plan(task.id, steps)
        self.store.set_state(task.id, "ready", f"planned ({how})", branch=branch)
        self.game.post_status(f"#{task.id} planned: {task.title}"[:200], "running", progress=0.0, detail="\n".join(f"- {s['kind']}: {s['title']}" for s in steps))

    def apply(self, task: Task, step: Step, out: Outcome) -> None:
        now = self.clock()
        result = self.redact(out.result)[-8000:]
        if out.status == "done":
            self.store.update_step(step.id, state="done", result=result)
            self.log(task.id, "step", f"done {step.kind}: {out.note or 'ok'}")
            fresh = self.store.get(task.id)
            if fresh.next_step() is None:
                self.finish(fresh, out.note or "all steps done")
            elif fresh.state not in ("ready", "review"):
                self.store.set_state(task.id, "ready")
            else:
                self.store.update(task.id, state="ready", next_at=0)
        elif out.status == "split":
            parts = out.parts
            new = []
            for i, part in enumerate(parts, 1):
                new.append({"kind": "code", "title": f"Part {i}/{len(parts)} (local-sized)", "args": {"prompt": part, "split": True}})
                new.append({"kind": "test", "title": f"Tests after part {i}", "args": {}})
            self.store.update_step(step.id, state="skipped", result=f"split into {len(parts)} smaller steps for a local coder")
            self.store.append_steps(task.id, new, step.idx)
            self.log(task.id, "plan", f"code step split into {len(parts)} local-sized parts")
        elif out.status == "skip":
            self.store.update_step(step.id, state="skipped", result=result)
            self.log(task.id, "step", f"skipped {step.kind}: {out.note}")
            if self.store.get(task.id).next_step() is None:
                self.finish(self.store.get(task.id), out.note)
        elif out.status == "owner":
            self.store.update_step(step.id, state="pending", result=result)
            self.store.set_state(task.id, "needs_owner", out.note)
            self.game.post_status(f"#{task.id} needs you: {out.note}"[:200], "info")
        elif out.status == "park":
            self.store.update_step(step.id, state="waiting", ticket_id=out.ticket, resume_token=f"autopilot:{task.id}:{step.id}")
            self.store.set_state(task.id, "waiting", out.note)
            self.game.post_status(f"#{task.id} waiting for your approval: {step.title}"[:200], "info")
        elif out.status in ("defer", "wait"):
            self.store.update_step(step.id, state="pending", result=result or step.result)
            state = "review" if out.status == "wait" else ("queued" if task.state == "queued" else "ready")
            self.store.update(task.id, next_at=now + out.delay, state=state, note=out.note)
            self.log(task.id, "defer", f"{out.note} (until {time.strftime('%Y-%m-%d %H:%M', time.localtime(now + out.delay))})")
        else:  # fail
            attempts = task.attempts + 1
            self.store.update_step(step.id, state="skipped" if out.supersede else "failed", result=result)
            self.audit("step_failed", step.kind, {"task": task.id, "note": out.note[:300]}, attempts=attempts)
            if attempts >= int(self.s["max_attempts"]):
                self.store.set_state(task.id, "needs_owner", f"failed {attempts} times; last: {out.note[:300]}", attempts=attempts)
                self.game.post_status(f"#{task.id} needs you: {task.title}"[:200], "failed", detail=out.note[:1500])
            else:
                delay = backoff_seconds(attempts, float(self.s["backoff_base_seconds"]), float(self.s["backoff_max_seconds"]))
                self.store.update(task.id, attempts=attempts, next_at=now + delay, note=f"attempt {attempts} failed: {out.note[:200]}")
                self.log(task.id, "fail", f"{step.kind} failed (attempt {attempts}, retry in {int(delay)}s): {out.note[:500]}")

    def finish(self, task: Task, note: str) -> None:
        self.store.set_state(task.id, "done", note)
        self.game.post_status(f"#{task.id} done: {task.title}"[:200], "done", progress=1.0, detail=task.pr_url or note)
        repo = self._repo(task)
        if repo and task.worktree and not self.dry_run:
            try:
                self.git.remove_worktree(repo, Path(task.worktree))
            except GitError as exc:
                self.log(task.id, "git", f"worktree kept: {exc}")

    # -- step kinds (worker threads) -----------------------------------------------
    def _context(self, task: Task) -> str:
        done = [f"- {s.kind} {s.title}: {s.result[:400]}" for s in task.steps if s.state == "done" and s.result]
        return f"Task #{task.id}: {task.title}\n{task.body[:3000]}\n\nEarlier steps:\n" + "\n".join(done[-6:])

    def step_local(self, task: Task, step: Step, lease: Lease | None, mode: str) -> Outcome:
        try:
            text = self.planner.local(str(step.args.get("prompt") or step.title), self._context(task))
        except ModelUnavailable as exc:
            return Outcome("defer", note=f"local model unavailable: {exc}", delay=900)
        return Outcome("done", result=text, note="local model answered")

    def step_game_read(self, task: Task, step: Step, lease: Lease | None, mode: str) -> Outcome:
        if not self.game.refresh():
            return Outcome("defer", note="game not running", delay=1800)
        return Outcome("done", result=self.game.read(str(step.args.get("tool", "")), dict(step.args.get("args") or {})))

    def step_game_action(self, task: Task, step: Step, lease: Lease | None, mode: str) -> Outcome:
        tool = str(step.args.get("tool", ""))
        args = dict(step.args.get("args") or {})
        if self.dry_run:
            return Outcome("skip", note=f"dry-run: would request approval for {tool}")
        if not self.game.refresh():
            return Outcome("defer", note="game not running; the approval request will be filed when it is", delay=1800)
        ticket = self.game.request_action(tool, args, str(step.args.get("reason") or step.title), f"autopilot:{task.id}:{step.id}")
        if ticket.settled:  # e.g. an allow session was open and it ran at once
            return Outcome("done" if ticket.approved else "fail", result=f"ticket {ticket.id} {ticket.state}: {ticket.result}", note=f"ticket {ticket.state}")
        self.log(task.id, "ticket", f"filed ticket {ticket.id} for {tool}; parked")
        return Outcome("park", note=f"waiting on ticket {ticket.id} ({tool})", ticket=ticket.id)

    def _worktree(self, task: Task, repo: Repo) -> Path:
        branch = task.branch or branch_for(task.id, task.title)
        path = self.git.ensure_worktree(repo, task.id, branch)
        if task.worktree != str(path) or task.branch != branch:
            self.store.update(task.id, worktree=str(path), branch=branch)
            task.worktree, task.branch = str(path), branch
        return path

    def _sandboxed(self, argv: list[str], repo: Repo, worktree: Path) -> list[str]:
        sandbox = self.s.section("sandbox")
        if sandbox_mode(str(sandbox.get("mode", "auto")), self.which) != "bwrap":
            return argv
        try:
            git_dir: Path | None = self.git.git_dir(repo)
        except GitError:
            git_dir = None
        hide = [expand(p) for p in sandbox.get("hide", [])]
        return bwrap_argv(worktree, git_dir, list(sandbox.get("writable", [])), bool(repo.allow_ssh_hosts), repo.allow_network, hide) + argv

    def step_code(self, task: Task, step: Step, lease: Lease | None, mode: str) -> Outcome:
        repo = self._repo(task)
        if repo is None:
            return Outcome("skip", note="no repository: code step dropped")
        worktree = self._worktree(task, repo)
        if mode == "local" and lease is not None:
            return self.run_local(task, step, repo, worktree, lease)
        return self.run_cloud(task, step, repo, worktree)

    def run_cloud(self, task: Task, step: Step, repo: Repo, worktree: Path) -> Outcome:
        limits = self.budget.per_run()
        prompt = session_prompt(repo, task.branch, str(step.args.get("prompt") or task.title))
        coder = repo.coder
        if coder == "codex":
            argv = codex_argv(self.s.section("codex"), repo, worktree, prompt)
        else:
            argv = claude_argv(self.s.section("claude"), repo, prompt, limits["max_turns"])
        argv = self._sandboxed(argv, repo, worktree)
        self.audit("coding_session", coder, {"task": task.id, "repo": repo.name, "branch": task.branch, "max_turns": limits["max_turns"], "max_seconds": limits["max_seconds"]})
        if self.dry_run:
            self.log(task.id, "code", f"dry-run: would run {coder} in {worktree} (max {limits['max_turns']} turns, {limits['max_seconds']}s)")
            return Outcome("done", result="dry-run: no coding session run", note="dry-run")
        secrets = resolve_secrets(dict(self.s["secrets"]), self.environ, self.redact, self.op_read)
        env = child_env(list(self.s["pass_env"]), self.environ, secrets, bool(repo.allow_ssh_hosts))
        raw = self.proc(argv, worktree, env, float(limits["max_seconds"]))
        result = parse_codex(raw, float(self.s.section("codex").get("usd_per_mtok", 0))) if coder == "codex" else parse_claude(raw)
        self.budget.record(task.id, coder, result.tokens, result.cost, result.seconds)
        self.log(task.id, "code", f"{coder}: ok={result.ok} turns={result.turns} tokens={result.tokens} cost=${result.cost:.3f} {result.seconds}s {result.stopped}")
        limit = classify_limit(raw, result)
        if limit:
            now = self.clock()
            until = now + limit_backoff_seconds(limit, now, next_local_midnight(now))
            self.store.set_flag("cloud_blocked_until", str(until))
            self.store.set_flag("cloud_blocked_reason", f"{coder} {limit}")
            self.audit("cloud_limited", coder, {"task": task.id, "kind": limit, "until": time.strftime("%Y-%m-%d %H:%M", time.localtime(until))})
            self.log(task.id, "code", f"{coder} hit a {limit}; cloud coding off until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))}, switching to a local coder")
            fallback = self.pool.acquire("coder") if self.budget.may_code_local().ok else None
            if fallback is None:
                return Outcome("defer", note=f"cloud {limit}; waiting for a local coder backend", delay=300)
            try:
                return self.run_local(task, step, repo, worktree, fallback)
            finally:
                self.pool.release(fallback)
        self.git.commit_leftovers(worktree, f"autopilot: {task.title[:60]}\n\nUncommitted changes left by the {coder} session for task #{task.id}.")
        if not result.ok:
            return Outcome("fail", result=result.summary, note=f"{coder} session did not finish ({result.stopped or 'error'})")
        self.store.add_author(task.id, coder)
        return Outcome("done", result=result.summary, note=f"{coder} session finished")

    def run_local(self, task: Task, step: Step, repo: Repo, worktree: Path, lease: Lease) -> Outcome:
        backend = lease.backend
        parts = int(self.s.caps["local_split_parts"])
        prompt = str(step.args.get("prompt") or task.title)
        if not step.args.get("split") and parts > 1:
            pieces = split_for_local(self.backend_model(backend), prompt, parts)
            if len(pieces) > 1:
                return Outcome("split", note="split for a local coder", parts=pieces)
        settings = self.s.section("local_coder")
        tool = str(settings.get("tool", "aider"))
        model = backend.coder_model or backend.model
        author = f"local:{model}@{backend.name}"
        argv = local_coder_argv(settings, repo, worktree, backend.openai_base, model,
                                session_prompt(repo, task.branch, prompt), backend.context)
        argv = self._sandboxed(argv, repo, worktree)
        limit = int(self.budget.per_run()["local_max_seconds"])
        self.audit("coding_session", f"local:{tool}", {"task": task.id, "repo": repo.name, "branch": task.branch, "backend": backend.name, "model": model, "max_seconds": limit})
        if self.dry_run:
            self.log(task.id, "code", f"dry-run: would run {tool} on {backend.name} ({model}) in {worktree} (max {limit}s)")
            return Outcome("done", result="dry-run: no local coding session run", note="dry-run")
        token = backend.token()
        self.redact.add(token)
        codex_home = self.s.codex_home if tool == "codex" else None
        if codex_home is not None:
            codex_home.mkdir(parents=True, exist_ok=True)
        env = child_env(list(self.s["pass_env"]), self.environ,
                        local_coder_env(backend.openai_base, token, str(codex_home or "")), False)
        raw = self.proc(argv, worktree, env, float(limit), None, self.pool.abort_reason(lease))
        result = parse_local(tool, raw)
        self.budget.record(task.id, author, result.tokens, 0.0, result.seconds)
        self.log(task.id, "code", f"{tool} on {backend.name}: ok={result.ok} {result.seconds}s {result.stopped}")
        self.git.commit_leftovers(worktree, f"autopilot: {task.title[:60]}\n\nUncommitted changes left by {tool} ({model} on {backend.name}) for task #{task.id}.")
        if raw.stopped.startswith("aborted"):
            return Outcome("defer", result=result.summary, note=f"{backend.name} became unavailable ({raw.stopped}); will continue elsewhere", delay=60)
        if not result.ok:
            return Outcome("fail", result=result.summary, note=f"{tool} on {backend.name} did not finish ({result.stopped or 'error'})")
        self.store.add_author(task.id, author)
        return Outcome("done", result=result.summary, note=f"{tool} ({model} on {backend.name}) finished")

    def step_test(self, task: Task, step: Step, lease: Lease | None, mode: str) -> Outcome:
        repo = self._repo(task)
        if repo is None:
            return Outcome("skip", note="no repository")
        if not repo.test:
            return Outcome("owner", note=f"repo {repo.name} has no test command configured")
        worktree = self._worktree(task, repo)
        env = child_env(list(self.s["pass_env"]), self.environ, {}, False)
        timeout = float(self.s.caps["test_timeout_minutes"]) * 60
        output = ""
        for argv in ([repo.setup] if repo.setup else []) + [repo.test]:
            res = self.proc(self._sandboxed(argv, repo, worktree), worktree, env, timeout)
            output += f"$ {' '.join(argv)}\n{res.output[-6000:]}\n(exit {res.code}{', ' + res.stopped if res.stopped else ''}, {res.seconds}s)\n"
            if not res.ok:
                break
        else:
            return Outcome("done", result=output, note="tests passed")
        # failed: keep the evidence, add a fix (a coding step) and a re-test after this step
        self.store.append_steps(task.id, [
            {"kind": "code", "title": "Fix the failing tests", "args": {"prompt": f"The tests fail. Fix the cause (not the tests, unless they are wrong).\n\nTask: {task.title}\n\nTest output (tail):\n{self.redact(output[-4000:])}"}},
            {"kind": "test", "title": "Re-run the tests", "args": {}},
        ], step.idx)
        return Outcome("fail", result=output, note="tests failed; a fix step was added", supersede=True)

    def step_pr(self, task: Task, step: Step, lease: Lease | None, mode: str) -> Outcome:
        repo = self._repo(task)
        if repo is None:
            return Outcome("skip", note="no repository")
        if not repo.github or not repo.remote:
            return Outcome("owner", note=f"repo {repo.name} has no remote/github configured; branch {task.branch} is ready locally for review, no PR opened")
        worktree = self._worktree(task, repo)
        tests = [s for s in task.steps if s.kind == "test" and s.idx < step.idx and s.state != "skipped"]
        if not tests or tests[-1].state != "done":
            return Outcome("fail", note="refusing to push or open a PR: the last test step did not pass")
        if not self.dry_run and self.git.ahead(repo, worktree) == 0:
            return Outcome("owner", note="the coding session made no commits; nothing to push")
        self.git.push(repo, worktree, task.branch)
        fresh = self.store.get(task.id)
        authors = [a for a in fresh.authors.split(",") if a]
        labels = ["autopilot"] + [author_label(a) for a in authors]
        if fresh.pr_url and not fresh.pr_url.startswith("(dry-run)"):
            self.git.open_draft_pr(repo, worktree, task.branch, task.title, "", labels)  # existing PR: adds labels only
            return Outcome("done", result=fresh.pr_url, note="pushed to the draft PR")
        diffstat = "" if self.dry_run else self.git.diffstat(repo, worktree)
        notes = "\n\n".join(s.result[-1500:] for s in task.steps if s.kind == "code" and s.state == "done")
        body = self.planner.summarize_pr(task, diffstat, tests[-1].result, notes)
        body += f"\n\nWritten by: {', '.join(authors) or '(unknown)'}\n"
        url = self.git.open_draft_pr(repo, worktree, task.branch, task.title, self.redact(body), labels)
        self.store.update(task.id, pr_url=url)
        self.audit("draft_pr", repo.name, {"task": task.id, "branch": task.branch, "url": url, "labels": ",".join(labels)})
        self.log(task.id, "pr", f"draft PR {url} ({', '.join(labels)})")
        return Outcome("done", result=url, note="draft PR opened")

    def step_review(self, task: Task, step: Step, lease: Lease | None, mode: str) -> Outcome:
        repo = self._repo(task)
        if repo is None or lease is None:
            return Outcome("skip", note="no repository or reviewer")
        worktree = self._worktree(task, repo)
        diff = self.git.diff(repo, worktree)
        if not diff.strip():
            return Outcome("done", result="empty diff", note="nothing to review")
        tests = [s for s in task.steps if s.kind == "test" and s.state == "done"]
        try:
            review = review_diff(self.backend_model(lease.backend), task, diff, tests[-1].result if tests else "")
        except (ModelUnavailable, ValueError) as exc:
            return Outcome("defer", note=f"review on {lease.label} failed: {exc}", delay=600)
        rnd = int(step.args.get("round", 1))
        body = (
            f"**Automated review** by local model `{lease.label}` (autopilot, round {rnd}): **{review['verdict']}**\n\n"
            f"{review['summary']}\n\n" + "".join(f"- {c}\n" for c in review["comments"])
            + ("\nTests to add:\n" + "".join(f"- {t}\n" for t in review["tests_to_add"]) if review["tests_to_add"] else "")
        )
        fresh = self.store.get(task.id)
        if fresh.pr_url:
            self.git.comment_pr(repo, worktree, task.branch, self.redact(body))
        self.log(task.id, "review", f"{lease.label}: {review['verdict']} ({len(review['comments'])} comments, {len(review['tests_to_add'])} tests)")
        if review["verdict"] == "changes" and rnd <= int(self.s.caps["review_rounds"]):
            prompt = (
                f"A reviewer ({lease.label}) asked for changes on this branch for: {task.title}\n\n"
                + "".join(f"- {c}\n" for c in review["comments"])
                + ("\nAdd tests for:\n" + "".join(f"- {t}\n" for t in review["tests_to_add"]) if review["tests_to_add"] else "")
            )
            self.store.append_steps(task.id, [
                {"kind": "code", "title": f"Address review round {rnd}", "args": {"prompt": prompt}},
                {"kind": "test", "title": "Re-run the tests", "args": {}},
                {"kind": "pr", "title": "Push the review fixes", "args": {}},
                {"kind": "review", "title": f"Cross-review round {rnd + 1}", "args": {"round": rnd + 1}},
            ], step.idx)
        return Outcome("done", result=body, note=f"review: {review['verdict']}")

    def step_ci(self, task: Task, step: Step, lease: Lease | None, mode: str) -> Outcome:
        repo = self._repo(task)
        if repo is None:
            return Outcome("skip", note="no repository")
        status = self.git.ci_status(repo, task.branch, Path(task.worktree or repo.path))
        polls = int(step.args.get("polls", 0)) + 1
        self.store.update_step(step.id, args={**step.args, "polls": polls})
        if status == "pass":
            return Outcome("done", result="CI passed", note=f"CI passed; draft PR ready for review: {task.pr_url}")
        if status == "pending" or (status == "none" and polls < 6):
            return Outcome("wait", note=f"CI {status}", delay=float(self.s["ci_poll_seconds"]))
        if status == "none":
            return Outcome("owner", note=f"no CI checks reported on {task.pr_url}; review it by hand")
        self.store.append_steps(task.id, [
            {"kind": "code", "title": "Fix the CI failure", "args": {"prompt": f"CI failed on the pull request for: {task.title}. Reproduce with the repository's test entry point and fix the cause."}},
            {"kind": "test", "title": "Re-run the tests", "args": {}},
            {"kind": "pr", "title": "Push the fix", "args": {}},
            {"kind": "ci", "title": "Wait for CI", "args": {}},
        ], step.idx)
        return Outcome("fail", result="CI failed", note="CI failed; a fix step was added", supersede=True)

    # -- owner-facing ----------------------------------------------------------
    def needs_you(self) -> list[str]:
        items = []
        for step in self.store.waiting_steps():
            task = self.store.get(step.task_id)
            items.append(f"Approve {step.args.get('tool', '?')} for #{task.id} (ticket {step.ticket_id})")
        for task in self.store.tasks(["needs_owner"]):
            items.append(f"Needs you: #{task.id} {task.title[:60]}")
        return items

    def sync_objectives(self) -> None:
        items = self.needs_you()
        key = "\n".join(items)
        if key == self.store.flag("objectives"):
            return
        if self.game.set_objectives(items or ["autopilot: nothing needs you"]):
            self.store.set_flag("objectives", key)

    def maybe_digest(self, force: bool = False) -> Path | None:
        now = self.clock()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if not force and (time.localtime(now).tm_hour < int(self.s["digest_hour"]) or self.store.flag("digest_day") == today):
            return None
        since = float(self.store.flag("digest_ts", "0") or 0) or now - 86400
        path, headline = write_digest(self.store, self.s.digest_dir, since, now, self.needs_you(), self.redact, self.pool.status())
        self.store.set_flag("digest_day", today)
        self.store.set_flag("digest_ts", str(now))
        self.log(None, "digest", f"wrote {path}")
        if self.s["digest_toast"]:
            self.game.toast(headline)
        return path
