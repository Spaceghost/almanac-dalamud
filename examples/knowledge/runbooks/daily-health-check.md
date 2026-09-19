---
title: Daily health check (read-only)
kind: runbook
hosts: [any]
tags: [health, daily]
tools: [host_status, gpu_status, ollama_ps]
safety: read
updated: 2026-09-19
sources: [example]
---
# Daily health check

1. For every host in the config, run `host_status`. Note load, available
   memory, any filesystem over 85 %, and failed services.
2. Run `gpu_status` on hosts with GPUs. Note which processes use which GPU.
3. Run `ollama_ps`. A model loaded for hours means idle unload is not working.

## Report

One line per host: `ok` or the problem, then the exact output lines that show it.
