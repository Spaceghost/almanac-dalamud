"""autopilot: an autonomous coding + FFXIV companion loop.

Pieces (each small, each testable with fakes):

* ``settings``  the ``[autopilot]`` config section and its defaults
* ``store``     the SQLite task queue: tasks, plan steps, event log, budget, flags
* ``sources``   where tasks come from (GitHub issues, inbox file, vote tallies,
                failing CI runs, ``almanac autopilot add``)
* ``planner``   the local model (via the gateway's backend) turns a task into steps
* ``budget``    per-run and daily caps for cloud coding sessions
* ``coder``     Claude Code (``claude -p``) / Codex (``codex exec``) sessions in a worktree
* ``git``       worktrees, branch push, draft PRs, CI status (never main)
* ``game``      XivMcp: agent board, toasts, tracker objectives, approval tickets
* ``runner``    the loop that ties them together
* ``digest``    the morning summary

Nothing in here automates gameplay. Game actions only ever go through
XivMcp's approval tickets, which the player approves or denies in game.
"""
