#!/usr/bin/env bash
# Fetch the Dalamud reference assemblies a plugin build needs, from goatcorp's public
# distribution. For CI and for a machine with no XIVLauncher install; a player's own
# machine already has them (Linux ~/.xlcore/dalamud/Hooks/dev/, Windows
# %APPDATA%\XIVLauncher\addon\Hooks\dev\) and needs none of this.
#
#   dalamud/tools/fetch-dalamud.sh            # -> ./.dalamud, prints the version
#   DALAMUD_HOME=... dotnet build ...         # then build against it
#
# Environment:
#   DALAMUD_HOME  where to unpack (default ./.dalamud in the checkout)
#   DALAMUD_TRACK latest (default) or a dalamud-distrib track like stg
#
# Exit codes: 0 done, 1 the download or the unpack failed, 127 a missing tool.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${DALAMUD_HOME:-$ROOT/.dalamud}"
TRACK="${DALAMUD_TRACK:-latest}"
BASE="https://goatcorp.github.io/dalamud-distrib"

for tool in curl unzip; do
  command -v "$tool" >/dev/null || { echo "error: $tool is required" >&2; exit 127; }
done

if [[ -f "$DEST/Dalamud.dll" ]]; then
  echo "Dalamud already at $DEST"
  exit 0
fi

zip="$(mktemp -t dalamud-XXXXXX.zip)"
trap 'rm -f "$zip"' EXIT
url="$BASE/$TRACK.zip"
[[ "$TRACK" == latest ]] || url="$BASE/$TRACK/latest.zip"
echo "fetching $url"
curl -fsSL --retry 3 --retry-delay 2 -o "$zip" "$url"
mkdir -p "$DEST"
unzip -q -o "$zip" -d "$DEST"
[[ -f "$DEST/Dalamud.dll" ]] || { echo "error: no Dalamud.dll in $url" >&2; exit 1; }
version="$(curl -fsSL "$BASE/version" 2>/dev/null || true)"
echo "Dalamud at $DEST ${version:+($version)}"
