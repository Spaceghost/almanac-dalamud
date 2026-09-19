"""The shipped examples must stay valid and free of secrets."""

from pathlib import Path

from almanac import frontmatter
from almanac.secrets_scan import find_secrets
from almanac.tools import load_tools

REPO = Path(__file__).resolve().parents[1]


def test_example_notes_are_valid_and_clean() -> None:
    tools = load_tools(REPO / "examples" / "tools", ["local", "example-host"])
    for path in (REPO / "examples" / "knowledge").rglob("*.md"):
        text = path.read_text()
        meta, _ = frontmatter.parse(text)
        assert frontmatter.validate(meta) == [], path
        assert find_secrets(text) == [], path
        if meta.get("kind") == "runbook":
            for name in frontmatter.as_list(meta.get("tools")):
                assert name in tools, f"{path}: unknown tool {name}"


def test_no_secrets_in_repo_text() -> None:
    for pattern in ("*.py", "*.toml", "*.md", "*.yaml", "*.service", "*.container", "*.sh"):
        for path in REPO.rglob(pattern):
            if ".venv" in path.parts or path.name == "test_kb.py":
                continue
            assert find_secrets(path.read_text()) == [], path
