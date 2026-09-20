# Releasing Almanac

One command cuts a release, and it is the same command in every one of these mods.

```sh
tools/release.sh test            # the next testing build, from master as it is
tools/release.sh stable X.Y.Z    # the stable release X.Y.Z
```

Add `-n` for a dry run: every check, the plan, and the release notes, with nothing
changed. `tools/release.sh verify vX.Y.Z` checks a published release again.

It needs `git`, `python3` and an authenticated `gh`. Nothing else is done by hand.

## What it does

1. Refuses unless the working tree is clean, on `master`, level with `origin/master`,
   and CI and Python are green for that commit (it waits while CI is still running).
2. Works out the version. A tag is `vX.Y.Z` (stable) or `vX.Y.Z-test.N` (testing).
   Dalamud compares the four-part `AssemblyVersion`, so the fourth number counts builds
   of X.Y.Z: test N is `X.Y.Z.N`, and the stable release is one past the last test
   (`X.Y.Z.0` when there was none). Every build is newer than the one before it, and a
   tester on `X.Y.Z-test.N` is offered the stable `X.Y.Z`.
3. Writes that version everywhere it lives (the manifest, `dalamud/Directory.Build.props`, `pyproject.toml` and `src/almanac/__init__.py`), and for a stable release turns the
   changelog's unreleased section into `X.Y.Z` with today's date. Entries keep their
   status: **BETA** stays BETA until it has been seen working in game, and entries that
   are still being built stay in the unreleased section.
4. Commits `Release <tag>`, tags it, and pushes `master` and the tag together.
5. Waits for the [Release workflow](../.github/workflows/release.yml), then checks the
   result: the release exists on the right channel, `latest.zip`, the versioned zip, the
   pluginmaster JSON and `SHA256SUMS` are attached, the downloads match their checksums,
   and <https://spacegho.st/mods/ffxiv/plugins.json> shows the new version with every
   link answering. The listing is cached for some minutes, so this last step waits.

If the workflow fails, fix it on `master` and re-run that workflow; the tag stays put.

## What a release looks like

The title is `Almanac vX.Y.Z`, or `Almanac vX.Y.Z-test.N (testing)`. The notes are
generated from the changelog (`changelog.json`, the one players read in game): the banner, an honest line about what has not
been verified in game, the entries grouped as *New*, *Fixed* and *In this build, not yet
verified in game*, how to install, the checksums, and links to the site and the
changelog. The same entries, shortened, go into the listing for Dalamud's installer.

## Channels

* **Stable** is the newest full release. The listing serves
  `releases/latest/download/latest.zip`, a URL that never changes.
* **Testing** is one floating prerelease called `testing`, which always carries the
  newest test build, at `releases/download/testing/latest.zip`. Players opt in with
  *Get plugin testing builds*. A testing build never touches a stable release's files.

## What the workflow will not do

It runs only in this repository (never a fork), only on GitHub-hosted runners, with
actions pinned by commit and a token that can write releases and nothing else. It
refuses a tag that is not on `master`, and a tag that does not name the version in the
manifest. Zips are packed with sorted names and fixed timestamps, so the same build
packs to the same bytes.

## What a release does not prove

That the plugin works in the game. The workflow builds and tests without the game;
only playing it verifies it, and the changelog's statuses say which is which.
