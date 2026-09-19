# Tool files

A tool is one `*.toml` file in a directory listed in `tools_dirs`. The file
name (without `.toml`) is the tool name unless `name` is set. Names are
lowercase `[a-z][a-z0-9_]*`.

## Top-level keys

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `description` | string | required | What the tool does and when to use it. The model reads this. |
| `safety` | `read` / `change` / `destructive` | required | `read` runs immediately; the others need approval (see README, Security model). |
| `run_on` | string | `"local"` | `"local"`, a host name from the config, or `"{host}"` to let the caller pick. |
| `hosts` | list | `["local"]` | Hosts the tool may run on. `"*"` means every configured host. With `run_on = "{host}"` a required `host` parameter is added, its enum being this list. |
| `timeout` | integer seconds | 60 | Budget for all commands together. |
| `max_output` | integer chars | 12000 | Longer output keeps its head and tail with a marker in between. |
| `cwd` | string | none | Working directory (local tools only). May use placeholders. |
| `[env]` | table | none | Extra environment variables (`env K=V` is prepended for ssh hosts). |

## Parameters: `[params.<name>]`

| Key | Meaning |
| --- | --- |
| `type` | `string` (default), `integer` or `boolean`. |
| `description` | Shown to the model. |
| `required` | `true` if the call must supply it. |
| `default` | Used when the call omits it. |
| `enum` | Allowed values. |
| `pattern` | Regex the whole string must match. Default: `[A-Za-z0-9][A-Za-z0-9._:/@+=-]{0,127}`. |
| `minimum`, `maximum` | Integer range. |
| `allow_dash` | Strings may start with `-` (only when the argv puts them after `-e` or `--`). |

Newlines and NUL are always rejected. Unknown arguments are rejected.

## Commands: `[[commands]]`

Each runs in order; with several commands their outputs are labelled.

| Key | Meaning |
| --- | --- |
| `argv` | The command as a list. Elements are strings with placeholders, or `{ when = "param", argv = [...] }` groups included only when that parameter is set (and true, for booleans). |
| `hosts` | Run this command only on these hosts. |
| `init` | Run this command only on hosts whose config `init` matches (`systemd` default, `openrc`). |
| `label` | Heading shown instead of the command line. |

Placeholders: `{param}` substitutes a validated argument into that element
(never splitting it or passing it through a shell); `{@home}`, `{@repo}` and
`{@python}` are the local home directory, the engine checkout and its Python;
`{{` and `}}` are literal braces (for tools like `curl -w '%{{http_code}}'`).
Placeholders are expanded on the machine running almanac.

A template naming an undeclared parameter is an error when the tool loads.

## Execution

- `transport = "local"` hosts run argv directly.
- `transport = "ssh"` hosts run `ssh -o BatchMode=yes -T <dest> -- <shlex-quoted argv>`;
  the remote login shell sees exactly the original words. Keys and host
  verification must already work non-interactively.
- Incus remotes need no special transport: run the local `incus` client with
  `<remote>:` in argv (see `examples/tools/incus_*.toml`).

## Checklist for a new tool

1. Pick the smallest `safety` that is honest. Anything that changes state is
   `change`; anything that loses data or interrupts a service is `destructive`.
2. Constrain every string with `enum` or a tight `pattern`.
3. Prefer read-only commands with bounded output (`-n`, `--lines`, `-m`).
4. `almanac tool <name> key=value` to try it; `almanac tools -v` to see its schema.
5. Mention it in a runbook's `tools:` if a runbook should use it.
