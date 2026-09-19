---
title: Dalamud plugin development loop
hosts: [example-host]
tags: [ffxiv, dalamud, dotnet, plugin]
safety: change
updated: 2026-09-19
sources: [example]
---
# Building and loading a dev plugin

1. Build: `dalamud_plugin_build` (`dotnet build -c Release` in the checkout).
2. Stage: `dalamud_plugin_install_dev plugin=<Name>` copies `bin/Release` to
   `~/dev-plugins/<Name>`.
3. Once, in game: `/xlsettings` → Experimental → Dev Plugin Locations, add the
   DLL path; then `/xlplugins` → Dev Tools → enable it. Dalamud reloads the
   plugin when the DLL changes (each managed reload can leak memory; restart
   the game after many reloads).
4. Watch: `dalamud_log_grep pattern=<Name>|Exception`.

## Unknown / untested

- Paths are examples; copy the two tools and point them at your checkout.
