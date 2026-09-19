---
title: example-host, a GPU workstation that also games
hosts: [example-host]
tags: [host, gpu, workstation]
safety: read
updated: 2026-09-19
sources: [example]
---
# example-host

- OS: an atomic desktop (rpm-ostree). Install nothing system-wide; use
  `~/.local`, containers or toolbox.
- GPUs: GPU 0 drives the display and games; GPU 1 has no display and is the
  only one used for inference (see [local inference](../services/local-inference.md)).
- RAM: 16 GB. Games come first: inference loads on demand and unloads when idle.

## How to check

- `host_status host=example-host` — uptime, memory, disk, failed units.
- `gpu_status host=example-host` — which GPU holds what.

## Unknown / untested

- Replace this note with your own hosts; it is an example.
