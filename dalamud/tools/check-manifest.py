#!/usr/bin/env python3
"""Check the Dalamud manifest before it can ship.

Run by CI and by the release workflow, and worth running by hand after a version bump:

  dalamud/tools/check-manifest.py                  # fields, and the two versions agree
  dalamud/tools/check-manifest.py --tag v0.1.0     # ... and the tag is that version

It checks that
  * every field Dalamud's installer needs is present and of the right shape,
  * InternalName matches the assembly name (Dalamud loads <InternalName>.dll),
  * DalamudApiLevel is the level the current Dalamud loads (15),
  * Almanac.json's AssemblyVersion and Directory.Build.props' <Version> agree, so the
    listing never advertises a version the zip does not contain,
  * and, with --tag, that the release tag names that same version.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DALAMUD = HERE.parent
MANIFEST = DALAMUD / "src" / "Almanac.Plugin" / "Almanac.json"
PROPS = DALAMUD / "Directory.Build.props"
# The API level of the Dalamud that loads plugins today (Dalamud 15.x). A plugin built
# against a different level is refused by the installer, so this is checked, not guessed.
API_LEVEL = 15
ASSEMBLY_NAME = "Almanac"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
STRINGS = ("Author", "Name", "InternalName", "Punchline", "Description", "RepoUrl", "ApplicableVersion")


def check(manifest: dict, props_version: str | None, tag: str | None) -> list[str]:
    bad: list[str] = []
    for field in STRINGS:
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            bad.append(f"{field} must be a non-empty string")
    version = manifest.get("AssemblyVersion")
    if not isinstance(version, str) or not VERSION_RE.match(version or ""):
        bad.append("AssemblyVersion must be four numbers, like 0.1.0.0")
        version = None
    if manifest.get("InternalName") != ASSEMBLY_NAME:
        bad.append(f"InternalName must be {ASSEMBLY_NAME}: Dalamud loads <InternalName>.dll")
    if manifest.get("DalamudApiLevel") != API_LEVEL:
        bad.append(f"DalamudApiLevel must be {API_LEVEL}")
    tags = manifest.get("Tags")
    if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t for t in tags):
        bad.append("Tags must be a non-empty list of strings")
    icon = manifest.get("IconUrl")
    if not isinstance(icon, str) or not icon.startswith("https://"):
        bad.append("IconUrl must be an https URL to a 64x64..512x512 png")
    images = manifest.get("ImageUrls")
    if images is not None and (not isinstance(images, list) or not all(isinstance(i, str) and i.startswith("https://") for i in images)):
        bad.append("ImageUrls must be a list of https URLs")
    if version and props_version and version != props_version:
        bad.append(f"AssemblyVersion {version} does not match Directory.Build.props <Version> {props_version}")
    if version and tag:
        want = "v" + version.removesuffix(".0")
        if tag != want and not tag.startswith(want + "-"):
            bad.append(f"tag {tag} does not name AssemblyVersion {version} (expected {want} or {want}-<pre>)")
    return bad


def props_assembly_version(text: str) -> str | None:
    m = re.search(r"<Version>([0-9.]+)</Version>", text)
    if not m:
        return None
    parts = m.group(1).split(".")
    while len(parts) < 4:
        parts.append("0")
    return ".".join(parts[:4])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", default=None, help="the release tag being built, e.g. v0.1.0")
    args = ap.parse_args(argv)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = check(manifest, props_assembly_version(PROPS.read_text(encoding="utf-8")), args.tag)
    if bad:
        for line in bad:
            print(f"{MANIFEST.name}: {line}", file=sys.stderr)
        return 1
    print(f"{MANIFEST.name}: {manifest['InternalName']} {manifest['AssemblyVersion']}, API {manifest['DalamudApiLevel']}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
