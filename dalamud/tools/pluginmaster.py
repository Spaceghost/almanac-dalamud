#!/usr/bin/env python3
"""Write a one-entry Dalamud plugin repository listing for this plugin.

Dalamud's plugin installer reads a "plugin master": a JSON array of manifests, each
carrying the fields a repository adds to the manifest that ships inside the zip
(download links, LastUpdate, the testing channel). This writes the array for one
plugin, from that plugin's own manifest, so each repository publishes its own entry as
a release asset and the site at spacegho.st/mods/ffxiv/plugins.json only has to
concatenate them.

  --channel stable   the entry for a tagged release: install and update point at
                     .../releases/latest/download/latest.zip, whose URL never changes
  --channel testing  the entry for a test build: the testing fields point at the
                     floating .../releases/download/testing/latest.zip prerelease, and
                     IsTestingExclusive says the plugin has no stable release yet
                     (the site clears that flag when a stable entry also exists)

No DownloadCount: nothing counts downloads from a repository hosted this way, and
Dalamud treats a missing count as zero rather than showing a wrong one.
"""
from __future__ import annotations

import argparse
import json
import sys

# Fields a manifest may carry into the listing; anything else in the manifest is passed
# through untouched, so a new Dalamud field needs no change here.
TESTING_ZIP = "https://github.com/{repo}/releases/download/testing/latest.zip"
STABLE_ZIP = "https://github.com/{repo}/releases/latest/download/latest.zip"

REQUIRED = ("Author", "Name", "InternalName", "AssemblyVersion", "Description", "DalamudApiLevel")


def build(manifest: dict, repo: str, channel: str, last_update: int, changelog: str | None) -> dict:
    missing = [f for f in REQUIRED if not manifest.get(f)]
    if missing:
        raise SystemExit("manifest is missing " + ", ".join(missing))
    entry = dict(manifest)
    entry.setdefault("ApplicableVersion", "any")
    entry.setdefault("RepoUrl", f"https://github.com/{repo}")
    entry["LastUpdate"] = int(last_update)
    stable, testing = STABLE_ZIP.format(repo=repo), TESTING_ZIP.format(repo=repo)
    if channel == "stable":
        entry["DownloadLinkInstall"] = stable
        entry["DownloadLinkUpdate"] = stable
        entry["DownloadLinkTesting"] = testing
        entry["IsTestingExclusive"] = False
        if changelog:
            entry["Changelog"] = changelog
    else:
        # A testing-only listing: install from the testing zip, and say so. When the site
        # merges this with a stable entry it keeps the stable install links.
        entry["DownloadLinkInstall"] = testing
        entry["DownloadLinkUpdate"] = testing
        entry["DownloadLinkTesting"] = testing
        entry["TestingAssemblyVersion"] = entry["AssemblyVersion"]
        entry["TestingDalamudApiLevel"] = entry["DalamudApiLevel"]
        entry["IsTestingExclusive"] = True
        if changelog:
            entry["TestingChangelog"] = changelog
    return entry


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, help="the plugin manifest that ships in the zip")
    ap.add_argument("--repo", required=True, help="owner/name on GitHub")
    ap.add_argument("--channel", choices=("stable", "testing"), default="stable")
    ap.add_argument("--last-update", type=int, default=0, help="unix time of the build")
    ap.add_argument("--changelog", default=None, help="what changed in this release")
    ap.add_argument("--out", required=True, help="where to write the listing")
    args = ap.parse_args(argv)

    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    entry = build(manifest, args.repo, args.channel, args.last_update, args.changelog)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump([entry], fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
