---
title: XivMcp, the MCP server inside FFXIV
hosts: [example-host]
tags: [ffxiv, dalamud, mcp, game]
safety: change
updated: 2026-09-19
sources: [XivMcp README]
---
# XivMcp

A Dalamud plugin that serves MCP at `http://127.0.0.1:41800/mcp` while the
game runs. Its bearer token lives in the plugin's own config
(`~/.xlcore/pluginConfigs/XivMcp.json`, key `BearerToken` under
XIVLauncher.Core); almanac reads it at call time and never copies it.

almanac uses it as a companion server (`[upstreams.xivmcp]`): its tools
appear to the local model as `xivmcp__<tool>`.

- Tiers: **read** and **ui** tools are always offered. **action** (targeting,
  gearsets, teleport, slash commands) and **chat** (text other players see)
  are offered only with `--allow-game-actions` or `/actions on`, and the game
  still shows its own confirmation window for each call.
- Progress: almanac posts `started`, `step n: <tool>` and `done`/`failed` to
  the in-game agent board with `post_status`.

## How to check

- `xivmcp_ping` — `401` means up and enforcing auth; a connection error means
  the game or the plugin is not running.

## Unknown / untested

- Tool names and tiers change with plugin versions; `almanac chat` then
  `/tools` shows what is offered right now.
