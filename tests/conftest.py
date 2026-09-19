"""Shared fixtures. Tests never touch the network, the real config or real hosts."""

from __future__ import annotations

from pathlib import Path

import pytest

from almanac.config import DEFAULTS, Config, _merge

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    kb = tmp_path / "kb"
    (kb / "hosts").mkdir(parents=True)
    (kb / "runbooks").mkdir()
    (kb / "hosts" / "example-host.md").write_text(
        "---\ntitle: Example host\nhosts: [example-host]\ntags: [incus, gpu]\nsafety: read\nupdated: 2026-01-01\n---\n"
        "The example host runs Incus and has a GPU. Logs live in /var/log/example.\n"
    )
    (kb / "runbooks" / "check.md").write_text(
        "---\ntitle: Check things\nkind: runbook\nhosts: [any]\ntools: [echo_tool]\napprove: [touch_tool]\nsafety: read\n---\nRun echo_tool.\n"
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "echo_tool.toml").write_text(
        'description = "Echo a word."\nsafety = "read"\nrun_on = "local"\n'
        '[params.word]\ntype = "string"\nrequired = true\ndescription = "w"\n'
        '[params.count]\ntype = "integer"\nminimum = 1\nmaximum = 3\n'
        '[[commands]]\nargv = ["echo", "{word}", { when = "count", argv = ["x{count}"] }]\n'
    )
    (tools / "touch_tool.toml").write_text(
        'description = "Create a file."\nsafety = "change"\nrun_on = "local"\n'
        f'[params.name]\ntype = "string"\npattern = "[a-z]+"\nrequired = true\n'
        f'[[commands]]\nargv = ["touch", "{tmp_path}/{{name}}"]\n'
    )
    raw = _merge(
        DEFAULTS,
        {
            "knowledge_dirs": [str(kb)],
            "tools_dirs": [str(tools)],
            "state_dir": str(tmp_path / "state"),
            "token_file": str(tmp_path / "token"),
            "this_host": "testhost",
            "hosts": {"testhost": {"transport": "local"}, "remote-a": {"transport": "ssh", "ssh": "user@remote-a"}},
        },
    )
    cfg = Config(raw)
    cfg.ensure_token()
    return cfg
