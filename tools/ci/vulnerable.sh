#!/usr/bin/env bash
# Fail when a project resolves a NuGet package with a known vulnerability,
# direct or transitive.   tools/ci/vulnerable.sh path/to/A.csproj ...
set -uo pipefail
bad=0
for p in "$@"; do
  out="$(dotnet list "$p" package --vulnerable --include-transitive 2>&1)" || { echo "$out"; exit 2; }
  echo "$out"
  if grep -q 'has the following vulnerable packages' <<<"$out"; then bad=1; fi
done
exit "$bad"
