---
title: Triage a Dalamud plugin crash or failed load (read-only)
kind: runbook
hosts: [example-host]
tags: [ffxiv, dalamud, crash]
tools: [dalamud_log_tail, dalamud_log_grep, xivmcp_ping]
safety: read
updated: 2026-09-19
sources: [example]
---
# Dalamud crash triage

1. `dalamud_log_grep pattern=Exception|Error|failed max=40` on `dalamud.log`;
   if the game restarted, also on `dalamud.old.log`.
2. For the plugin named in the first relevant hit, `dalamud_log_grep pattern=<PluginName>`
   to see its load sequence.
3. `dalamud_log_tail log=dalamud.boot.log lines=50` if the game failed to start.
4. `xivmcp_ping` to see whether the game is up right now.

## Report

The plugin involved, the first exception line and its type, whether the game
is currently running, and the log file to open for more.
