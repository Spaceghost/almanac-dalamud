#!/usr/bin/env bash
# Cut a release. The only command a release needs; the same in every one of these mods.
#
#   tools/release.sh test              the next testing build, from master as it is
#   tools/release.sh stable X.Y.Z      the stable release X.Y.Z
#   tools/release.sh test -n           (or stable X.Y.Z -n) a dry run: every check, the
#                                      plan and the release notes; changes nothing
#   tools/release.sh verify vX.Y.Z     check a published release again, end to end
#
# It refuses unless the tree is clean, on master, level with origin, and CI is green
# for that commit. Then it writes the version everywhere it lives, turns the changelog's
# unreleased section into the release (stable only), commits, tags, pushes, waits for
# the Release workflow, and checks what came out: the release and its files, the
# checksums, the download links, and the live listing at
# https://spacegho.st/mods/ffxiv/plugins.json. See docs/RELEASING.md.
#
# Needs git, python3 and an authenticated gh. Exit 0 only when the release is live.
set -euo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/releasekit.py" "$@"
