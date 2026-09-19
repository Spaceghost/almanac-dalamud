"""CHANGELOG.md is generated from changelog.json; this fails when they drift.

The renderer is tools/changelog.py, the same one CI and `tools/changelog.py`
by hand use, so there is only ever one way the file is written.
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_changelog_md_matches_changelog_json():
    done = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "changelog.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
