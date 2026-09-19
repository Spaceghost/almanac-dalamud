#!/usr/bin/env bash
# Package a built Almanac plugin for Dalamud, and write the one-entry plugin
# repository listing that spacegho.st/mods/ffxiv/plugins.json is assembled from.
# Publishes nothing: the release workflow attaches what this writes.
#
#   dotnet build dalamud/Almanac.Dalamud.slnx -c Release && dalamud/tools/package.sh
#
# Output, all under $OUT (default ../almanac-dalamud-build/release):
#   latest.zip                  the plugin folder as Dalamud installs it (Almanac.json,
#                               Almanac.dll, Almanac.Core.dll, its dependencies and
#                               runtimes/, no .pdb). The name is stable on purpose, so
#                               .../releases/latest/download/latest.zip never changes.
#   Almanac-<version>.zip       the same bytes under a self-describing name
#   pluginmaster.json           stable-channel listing (one entry, a JSON array)
#   pluginmaster-testing.json   testing-channel listing, when TESTING=1
#
# Environment:
#   BIN                the build output to package (default
#                      $ALMANAC_ARTIFACTS/bin/Almanac.Plugin/release, else
#                      ../almanac-dalamud-build/artifacts/bin/Almanac.Plugin/release)
#   OUT                where to write (default ../almanac-dalamud-build/release)
#   REPO               owner/name on GitHub (default $GITHUB_REPOSITORY, else
#                      Spaceghost/almanac-dalamud)
#   TESTING            1 to write pluginmaster-testing.json instead of pluginmaster.json
#   SOURCE_DATE_EPOCH  timestamp for the zip entries and LastUpdate (default: the last
#                      commit's time, else 0), so the same build packs to the same bytes
#
# Exit codes: 0 done, 1 nothing to package or a missing file, 127 a missing tool.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

command -v zip >/dev/null || { echo "error: zip is required" >&2; exit 127; }
command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 127; }

ARTIFACTS="${ALMANAC_ARTIFACTS:-$ROOT/../almanac-dalamud-build/artifacts}"
BIN="${BIN:-$ARTIFACTS/bin/Almanac.Plugin/release}"
OUT="${OUT:-$ROOT/../almanac-dalamud-build/release}"
REPO="${REPO:-${GITHUB_REPOSITORY:-Spaceghost/almanac-dalamud}}"
MANIFEST="$ROOT/dalamud/src/Almanac.Plugin/Almanac.json"

[[ -f "$BIN/Almanac.dll" ]] || {
  echo "error: $BIN/Almanac.dll missing; run: dotnet build dalamud/Almanac.Dalamud.slnx -c Release" >&2
  exit 1
}
[[ -f "$MANIFEST" ]] || { echo "error: $MANIFEST missing" >&2; exit 1; }

VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["AssemblyVersion"])' "$MANIFEST")"
[[ -n "$VERSION" ]] || { echo "error: no AssemblyVersion in $MANIFEST" >&2; exit 1; }
if [[ -z "${SOURCE_DATE_EPOCH:-}" ]]; then
  SOURCE_DATE_EPOCH="$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || echo 0)"
fi

STAGE="${TMPDIR:-/tmp}/almanac-package-$$"
trap 'rm -rf "$STAGE"' EXIT
rm -rf "$STAGE"
mkdir -p "$STAGE/Almanac" "$OUT"

# Everything the built plugin needs, minus debug symbols and the reference assemblies
# Dalamud itself provides (the SDK already keeps those out of the output).
( cd "$BIN" && find . -type f ! -name '*.pdb' -print0 | while IFS= read -r -d '' f; do
    mkdir -p "$STAGE/Almanac/$(dirname "$f")"
    cp "$f" "$STAGE/Almanac/$f"
  done )
cp "$MANIFEST" "$STAGE/Almanac/Almanac.json"

# Dalamud unpacks the zip straight into the plugin folder, so the files sit at the root.
find "$STAGE/Almanac" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
rm -f "$OUT/latest.zip" "$OUT/Almanac-$VERSION.zip"
( cd "$STAGE/Almanac" && find . -type f | LC_ALL=C sort | sed 's|^\./||' |
    TZ=UTC zip -X -D -q "$OUT/latest.zip" -@ )
cp "$OUT/latest.zip" "$OUT/Almanac-$VERSION.zip"
echo "== $OUT/latest.zip"
unzip -l "$OUT/latest.zip" | tail -n 3

# The listing: the shipped manifest plus the fields a plugin repository adds.
CHANNEL=stable
LISTING="$OUT/pluginmaster.json"
if [[ "${TESTING:-0}" == 1 ]]; then CHANNEL=testing; LISTING="$OUT/pluginmaster-testing.json"; fi
python3 "$ROOT/dalamud/tools/pluginmaster.py" \
  --manifest "$MANIFEST" --repo "$REPO" --channel "$CHANNEL" \
  --last-update "$SOURCE_DATE_EPOCH" --out "$LISTING"
echo "== $LISTING"
cat "$LISTING"
