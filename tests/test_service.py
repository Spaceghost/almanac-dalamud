import json

from almanac.service import Almanac


def test_read_tool_runs_and_is_audited(config) -> None:
    alm = Almanac(config)
    out = alm.call("echo_tool", {"word": "hi"}, caller="test")
    assert not out.is_error and "hi" in out.text
    record = json.loads((config.state_dir / "audit.jsonl").read_text().splitlines()[-1])
    assert record["event"] == "ran" and record["tool"] == "echo_tool"


def test_change_needs_confirmation_token(config, tmp_path) -> None:
    alm = Almanac(config)
    first = alm.call("touch_tool", {"name": "made"}, caller="test")
    assert first.needs_confirmation and not (tmp_path / "made").exists()
    assert "touch" in first.plan
    wrong = alm.call("touch_tool", {"name": "made", "confirm": "0000"}, caller="test")
    assert wrong.needs_confirmation and not (tmp_path / "made").exists()
    other_args = alm.call("touch_tool", {"name": "other", "confirm": first.token}, caller="test")
    assert other_args.needs_confirmation and not (tmp_path / "other").exists()
    done = alm.call("touch_tool", {"name": "made", "confirm": first.token}, caller="test")
    assert not done.is_error and (tmp_path / "made").exists()
    events = [json.loads(l)["event"] for l in (config.state_dir / "audit.jsonl").read_text().splitlines()]
    assert events == ["planned", "planned", "planned", "approved", "ran"]


def test_kb_note_is_a_change_with_diff_plan(config) -> None:
    alm = Almanac(config)
    first = alm.call("kb_note", {"path": "services/a.md", "title": "A", "body": "alpha"}, caller="test")
    assert first.needs_confirmation and "+alpha" in first.plan
    done = alm.call("kb_note", {"path": "services/a.md", "title": "A", "body": "alpha"}, caller="test", approved=True)
    assert done.text.startswith("written")


def test_catalogue_filters_by_safety(config) -> None:
    alm = Almanac(config)
    names = {t["name"] for t in alm.catalogue("read")}
    assert "echo_tool" in names and "touch_tool" not in names and "kb_note" not in names
    change = {t["name"]: t for t in alm.catalogue("change")}
    assert "confirm" in change["touch_tool"]["input_schema"]["properties"]


def test_invalid_arguments_are_errors(config) -> None:
    out = Almanac(config).call("echo_tool", {"word": "; rm -rf /"}, caller="test")
    assert out.is_error
