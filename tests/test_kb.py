import pytest

from almanac.kb import KnowledgeBase, KnowledgeError


def kb_for(config):
    return KnowledgeBase(config.knowledge_dirs, config.state_dir / "index.sqlite")


def test_search_and_filters(config) -> None:
    kb = kb_for(config)
    hits = kb.search("incus gpu")
    assert hits[0]["path"] == "hosts/example-host.md"
    assert kb.search("incus", host="other-host") == []
    assert kb.search("runbook echo", kind="runbook")[0]["path"] == "runbooks/check.md"


def test_reindex_after_edit(config) -> None:
    kb = kb_for(config)
    kb.refresh()
    path = config.knowledge_dirs[0] / "hosts" / "example-host.md"
    path.write_text(path.read_text() + "\nzebrafish appear here\n")
    import os

    os.utime(path, (1, 1))
    assert kb.search("zebrafish")[0]["path"] == "hosts/example-host.md"
    path.unlink()
    assert kb.search("zebrafish") == []


def test_note_create_update_and_no_silent_overwrite(config) -> None:
    kb = kb_for(config)
    preview = kb.note("services/new-thing.md", "New thing", "It exists.", hosts=["example-host"])
    assert preview["action"] == "create" and not preview["written"]
    assert "+It exists." in preview["diff"]
    assert not (config.knowledge_dirs[0] / "services" / "new-thing.md").exists()
    written = kb.note("services/new-thing.md", "New thing", "It exists.", write=True)
    assert written["written"]
    with pytest.raises(KnowledgeError, match="base_sha256"):
        kb.note("services/new-thing.md", "New thing", "Changed.", write=True)
    current = kb.load("services/new-thing.md")
    update = kb.note("services/new-thing.md", "New thing", "Changed.", base_sha256=current.sha256, write=True)
    assert "-It exists." in update["diff"] and "+Changed." in update["diff"]


def test_note_rejects_secrets_and_bad_paths(config) -> None:
    kb = kb_for(config)
    with pytest.raises(KnowledgeError, match="secret"):
        kb.note("services/x.md", "X", "token = abcdefghijklmnopqrstuvwxyz0123456789")
    for bad in ("../escape.md", "/etc/passwd", "Hosts/X.md", "a/b.txt"):
        with pytest.raises(KnowledgeError):
            kb.note(bad, "X", "y")


def test_multiple_roots_first_wins(config, tmp_path) -> None:
    second = tmp_path / "second"
    (second / "hosts").mkdir(parents=True)
    (second / "hosts" / "example-host.md").write_text("---\ntitle: Shadowed\n---\nshadowed\n")
    (second / "hosts" / "other.md").write_text("---\ntitle: Other\n---\nonly in second\n")
    kb = KnowledgeBase([*config.knowledge_dirs, second], config.state_dir / "i2.sqlite")
    assert kb.load("hosts/example-host.md").title == "Example host"
    assert kb.load("hosts/other.md").title == "Other"
