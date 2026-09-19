"""Refuse to store anything that looks like a credential.

Used on every knowledge write (kb_note) and by the test suite over the whole
knowledge/ tree. It is a tripwire, not a guarantee: it catches the common
shapes (private keys, provider tokens, `token = <long value>` assignments).
"""

from __future__ import annotations

import re

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("OpenAI/Anthropic key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{24,}\b")),
    ("Slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Tailscale key", re.compile(r"\btskey-[a-z]+-[A-Za-z0-9-]{20,}\b")),
    ("bearer header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{24,}")),
    (
        "secret assignment",
        re.compile(
            r"(?i)\b(token|secret|password|passwd|api[_-]?key|auth)\b[\"']?\s*[:=]\s*[\"']?(?!\$|<|\{|\.\.\.)[A-Za-z0-9._~+/=-]{20,}"
        ),
    ),
]


def find_secrets(text: str) -> list[str]:
    """Return human-readable descriptions of suspected secrets (never the values)."""
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in PATTERNS:
            if pattern.search(line):
                hits.append(f"line {lineno}: looks like a {label}")
    return hits
