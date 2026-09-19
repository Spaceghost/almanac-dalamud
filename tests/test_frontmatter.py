from almanac import frontmatter


def test_roundtrip() -> None:
    meta = {"title": "T", "hosts": ["a", "b"], "safety": "read", "updated": "2026-01-01"}
    text = frontmatter.render(meta, "Body\n")
    parsed, body = frontmatter.parse(text)
    assert parsed == meta
    assert body == "Body\n"


def test_no_front_matter() -> None:
    assert frontmatter.parse("# plain\n") == ({}, "# plain\n")


def test_validate() -> None:
    assert frontmatter.validate({"title": "x", "safety": "nuke"}) == ["safety must be one of read, change, destructive"]
    assert "missing title" in frontmatter.validate({})
