import shlex
from pathlib import Path

import pytest

from almanac.tools import Param, ToolError, execute, load_tool, load_tools, truncate, wrap_for_host

REPO = Path(__file__).resolve().parents[1]


def test_param_validation() -> None:
    p = Param(name="n", type="string", required=True)
    assert p.check("web-01") == "web-01"
    for bad in ("-rf", "a b", "a;b", "$(x)", "a\nb", "`x`", ""):
        with pytest.raises(ToolError):
            p.check(bad)
    i = Param(name="i", type="integer", minimum=1, maximum=5)
    assert i.check("3") == 3
    with pytest.raises(ToolError):
        i.check(9)
    with pytest.raises(ToolError):
        i.check(True)
    e = Param(name="e", enum=["a", "b"])
    with pytest.raises(ToolError):
        e.check("c")


def test_render_when_groups_and_literal_braces(tmp_path) -> None:
    f = tmp_path / "tt.toml"
    f.write_text(
        'description = "d"\nsafety = "read"\n[params.p]\ntype = "string"\n[params.flag]\ntype = "boolean"\ndefault = false\n'
        '[[commands]]\nargv = ["x", "%{{code}}", { when = "p", argv = ["--p", "{p}"] }, { when = "flag", argv = ["--flag"] }]\n'
    )
    tool = load_tool(f, [])
    assert tool.render(tool.validate({}))[1][0][1] == ["x", "%{code}"]
    assert tool.render(tool.validate({"p": "v", "flag": True}))[1][0][1] == ["x", "%{code}", "--p", "v", "--flag"]


def test_undeclared_placeholder_rejected(tmp_path) -> None:
    f = tmp_path / "tt.toml"
    f.write_text('description = "d"\nsafety = "read"\n[[commands]]\nargv = ["echo", "{nope}"]\n')
    with pytest.raises(ToolError, match="undeclared"):
        load_tool(f, [])


def test_host_wildcard_and_init_filter(tmp_path) -> None:
    tool = load_tools(REPO / "examples" / "tools", ["alpha", "beta"])["host_status"]
    assert tool.params["host"].enum == ["alpha", "beta"]
    _, systemd = tool.render({"host": "alpha"}, "systemd")
    _, openrc = tool.render({"host": "alpha"}, "openrc")
    assert any(argv[0] == "systemctl" for _, argv in systemd)
    assert any(argv[0] == "rc-status" for _, argv in openrc)
    assert not any(argv[0] == "systemctl" for _, argv in openrc)


def test_ssh_wrapping_quotes_each_element() -> None:
    argv = ["grep", "-e", "a b; rm -rf /", "/tmp/x"]
    wrapped = wrap_for_host(argv, {"transport": "ssh", "ssh": "user@h"}, {})
    assert wrapped[:2] == ["ssh", "-o"]
    assert shlex.split(wrapped[-1]) == argv


def test_execute_local_and_truncate(config) -> None:
    tool = load_tools(config.tools_dirs[0], list(config.hosts))["echo_tool"]
    result = execute(tool, tool.validate({"word": "hello", "count": 2}), config.hosts, config.this_host)
    assert result.ok and result.output.strip() == "hello x2"
    text, cut = truncate("a" * 100, 30)
    assert cut and "truncated" in text


def test_all_example_tools_load() -> None:
    tools = load_tools(REPO / "examples" / "tools", ["local"])
    assert {"host_status", "dalamud_log_grep", "incus_stop"} <= set(tools)
    assert tools["incus_stop"].safety == "destructive"
