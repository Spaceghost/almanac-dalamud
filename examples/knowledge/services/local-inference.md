---
title: Local inference with Ollama on a dedicated GPU
hosts: [example-host]
tags: [ollama, gpu, inference, policy]
safety: read
updated: 2026-09-19
sources: [example]
---
# Local inference

Ollama serves `127.0.0.1:11434`, pinned to the inference GPU with
`CUDA_VISIBLE_DEVICES=<GPU UUID>` (or, in a container, CDI
`nvidia.com/gpu=<GPU UUID>`), `OLLAMA_KEEP_ALIVE=5m`,
`OLLAMA_MAX_LOADED_MODELS=1`.

almanac's gateway sits in front of it and refuses to load a model when host
memory or the GPU's free VRAM is low (`[guard]` in the config); scheduled
runbooks then exit 75 and try again next time.

## How to check

- `ollama_ps` — what is loaded, how much VRAM, when it expires.
- `almanac model status` / `almanac model unload`.

## Unknown / untested

- Example values; tune `[guard]` for your machine.
