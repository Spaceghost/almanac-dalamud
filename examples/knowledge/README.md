---
title: How this knowledge base works
hosts: [any]
tags: [almanac, meta]
safety: read
updated: 2026-09-19
---
# Knowledge notes

Plain Markdown, one topic per file, in `hosts/`, `services/`, `projects/` and
`runbooks/`. Each file starts with a small front matter block:

```
---
title: One line saying what this is
hosts: [example-host]          # or [any]
tags: [incus, gpu]
safety: read                   # the most dangerous thing the note tells you to do: read | change | destructive
updated: 2026-01-01
sources: [where the facts came from]
---
```

Write for two readers: a human skimming, and a small local model that follows
instructions literally. Say where things live (exact paths and commands), how
to check them, the gotchas, and end with **Unknown / untested** when there is
anything you have not observed. Never paste secrets; name the file that holds
them instead.

Runbooks add `kind: runbook`, `tools: [...]` (the almanac tools the run may
use) and optionally `approve: [...]` (change tools the owner pre-approves for
unattended runs). Their body is step-by-step instructions plus what to report.
