#!/usr/bin/env python3
"""List the licence of every NuGet package the given projects resolve (after
`dotnet restore`), and fail on a copyleft one.

    tools/ci/licences.py path/to/A.csproj path/to/B.csproj

Reads each project's project.assets.json and the .nuspec of each
package in the NuGet package folder. A package that only gives a licence URL
is listed as such; it does not fail the check.
"""
import json
import pathlib
import re
import subprocess
import sys

FORBIDDEN = re.compile(r"\b(A?GPL|LGPL|SSPL|EUPL|CC-BY-NC)", re.I)


def licence(nuspec: pathlib.Path) -> str:
    text = nuspec.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"<license[^>]*>([^<]+)</license>", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"<licenseUrl>([^<]+)</licenseUrl>", text)
    return f"url: {m.group(1).strip()}" if m else "not stated"


def main(projects: list[str]) -> int:
    rows: dict[str, str] = {}
    for project in projects:
        # the projects may put obj/ outside the checkout, so ask MSBuild where it is
        asked = subprocess.run(["dotnet", "msbuild", project, "-getProperty:ProjectAssetsFile"],
                               capture_output=True, text=True, check=False).stdout.strip()
        assets = pathlib.Path(asked) if asked else pathlib.Path(project).parent / "obj" / "project.assets.json"
        if not assets.is_file():
            print(f"licences: no {assets}; run dotnet restore first", file=sys.stderr)
            return 2
        data = json.loads(assets.read_text(encoding="utf-8"))
        folders = [pathlib.Path(p) for p in data.get("packageFolders", {})]
        for key, lib in data.get("libraries", {}).items():
            if lib.get("type") != "package":
                continue
            found = "nuspec not found"
            for folder in folders:
                specs = list((folder / lib["path"]).glob("*.nuspec"))
                if specs:
                    found = licence(specs[0])
                    break
            rows[key] = found
    if not rows:
        print("licences: no packages found", file=sys.stderr)
        return 2
    bad = 0
    print("| Package | Licence |\n| --- | --- |")
    for key in sorted(rows):
        flag = ""
        if FORBIDDEN.search(rows[key]):
            bad += 1
            flag = "  <-- not allowed"
        print(f"| {key} | {rows[key]}{flag} |")
    print(f"\n{len(rows)} packages, {bad} with a licence that is not allowed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
