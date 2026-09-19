# Almanac benchmark

A fixed, versioned set of FFXIV tasks that shows how well a local model uses
XivMcp's tools, and what it costs your machine to run it. The same suite runs
from the Dalamud plugin (**Almanac → Benchmark**) and headlessly from the
Python engine (`almanac bench`). Results can be submitted, opt-in and
anonymously, to the community leaderboard at
<https://spacegho.st/mods/ffxiv/almanac/>, which turns them into the model
recommendations the setup wizard shows.

- Suite: [`suites/ffxiv-core.json`](suites/ffxiv-core.json) (id `ffxiv-core`)
- Result format: [`schema/results.schema.json`](schema/results.schema.json)
- Recommendations format: [`schema/recommendations.schema.json`](schema/recommendations.schema.json)

Both clients implement the rules below. When they disagree, this document is
right and the client has a bug.

## Modes

- **mock** (default headless, and for CI): tool calls are answered from the
  suite's `fixtures`. No game, no XivMcp. Fully deterministic apart from the
  model itself.
- **live**: tool calls go to XivMcp in the running game. Only the suite's
  tools are offered, and all of them are Read or Ui tier (they read game
  state, place a map flag, or post on the agent board); nothing asks for
  in-game approval. Answer checks that depend on fixture data are skipped
  (`"live": {"answer": "skip"}`); `*_from_tool` checks use whatever XivMcp
  actually returned. The leaderboard ranks mock and live separately.

## The suite file

The suite hash is the SHA-256 of the file's bytes, exactly as shipped (the
file is marked `-text` in `.gitattributes`, so checkouts never rewrite line
endings). Any change to the file is a new `version`.

- `system_prompt`: the only system message.
- `defaults`: `max_steps` (model turns per task), `temperature`,
  `max_tokens` (per model turn), `weights` (`tools` + `answer`, sum 1).
- `tools`: MCP-style tool definitions (`name`, `description`,
  `inputSchema`). A task offers only the tools named in its `tools` list.
- `fixtures`: tool name → result. Either one result object for any
  arguments, or a list of `{when, result}`; the first `when` whose matchers
  all match the call's arguments wins (`{}` matches anything). The result is
  returned to the model as the tool message content, serialised as compact
  JSON.
- `tasks[]`: `id`, `category`, `prompt` (one user message), `tools`,
  optional `max_steps`, `expect`, optional `live`.

### Argument matchers

Used by fixture `when` and by `expect.calls[].args`. Each key names an
argument; a missing argument never matches. The value is a matcher object
with exactly one form, or a bare JSON value meaning `eq`:

| Matcher | Matches when |
| --- | --- |
| `{"eq": v}` | the argument equals `v` (numbers compare numerically, so `135` = `135.0`) |
| `{"approx": n, "tolerance": t}` | the argument is a number with `abs(arg - n) <= t` |
| `{"icontains_any": [s, ...]}` | the argument is a string that contains any `s`, case-insensitively |

### Paths

`*_from_tool` checks read a value from the **first** result that tool
returned during this task: dotted keys, `[n]` indexes, and `[key~text]`,
which selects the first array element whose `key` is a string containing
`text` case-insensitively. Example: `aetherytes[name~Moraby].gilCost`.

## Running a task

1. Messages: `system_prompt`, then the task `prompt` as the user.
2. Send a streaming Chat Completions request (`stream: true`,
   `stream_options.include_usage: true`, `temperature`, `max_tokens`, and the
   offered tools as OpenAI `tools` of type `function` with `parameters` =
   `inputSchema`; omit `tools` when the task offers none).
3. If the reply has tool calls, validate and execute each (below), append the
   assistant message and one `tool` message per call, and go to 2.
4. The task ends when a reply has no tool calls (its content is the **final
   answer**) or after `max_steps` model turns (no final answer).

A call is **valid** when its name is one of the task's offered tools, its
arguments parse as a JSON object, and they satisfy the tool's `inputSchema`
(`type` object/string/integer/number/boolean, `required`,
`additionalProperties: false`, `enum`, `minimum`/`maximum`, `maxLength`). An
invalid call is not executed; the model gets a tool message
`{"error": "invalid call: <reason>"}` and may retry. In mock mode a valid
call without a matching fixture gets `{"error": "no data"}`.

### Tool-calling capability

Every run records the model's `tool_calling`:

- `native`: the backend returned OpenAI `tool_calls`.
- `prompted`: the model is run with the fallback below because the backend
  rejected `tools` or the model is known not to emit native calls. The
  fallback appends to the system prompt:

  ```
  You can call these tools. To call one, reply with only a JSON object on one line:
  {"tool": "<name>", "arguments": {...}}
  After a call you will get its result as the next user message, starting "Tool result:". When you have the answer, reply normally without JSON.
  <one line per tool: name: description Arguments (JSON Schema): <compact inputSchema>>
  ```

  A reply whose trimmed content (after removing one surrounding ```` ``` ````
  or ```` ```json ```` fence) parses as an object with a string `tool` is a
  tool call; its result goes back as a user message
  `Tool result: <compact JSON>`.
- `none`: a run where the model made no tool call in any task that expects
  one.

## Scoring

**Tool score** (per task):

- `expect.calls` empty: 1, except with `forbid_any_call` where any call
  (valid or not) makes it 0.
- Otherwise each expected call is matched, in order, by the first valid call
  not yet used with the same name whose arguments match `args`;
  `tool_score = matched / expected`.
- Then, if the task made any calls, `tool_score *= valid_calls / all_calls`.

**Answer score** (per task): the fraction of checks that pass, 1 when the
task has none. Matching is case-insensitive.

| Check | Counts as | Passes when the final answer |
| --- | --- | --- |
| `contains_all` | one check per string | contains it |
| `contains_any` | one check | contains at least one |
| `not_contains` | one check per string | does not contain it |
| `contains_from_tool` | one check per entry | contains the string at `path` (fails if the tool was never called) |
| `numbers_from_tool` | one check per entry | contains a number within `tolerance` of the number at `path` |
| `exact` | one check | equals it after normalisation |
| `max_chars` | one check | is at most that many characters |

Numbers are read from the answer with `-?\d+(?:\.\d+)?` after removing
thousands separators (`1,234` → `1234`). `exact` normalisation: trim, strip
one surrounding code fence and any backticks or quotes around the whole
answer, collapse whitespace, drop one trailing `.`. No final answer scores 0.
In live mode, `"live": {"answer": "skip"}` removes the answer score and the
task score is the tool score.

**Task score** = `weights.tools * tool_score + weights.answer * answer_score`.
A task **succeeds** when its score is 1.

`error` (first that applies): `timeout`, `http` (backend error), `no_answer`
(no final answer), `bad_tool_call` (any invalid call), `wrong_answer`
(answer score < 1), otherwise `null`.

**Run metrics**:

- `score` = 100 × mean task score, one decimal.
- `success_rate` = succeeded / tasks.
- `tool_call_validity` = valid calls / all calls over the run (1 with no calls).
- `quality` = mean answer score over tasks where it was computed.
- `ttft_ms`: per task, from sending the first request to the first streamed
  delta (content, reasoning or tool call); the run value is the median.
- `tokens_per_s`: per task, output tokens (usage `completion_tokens`, else
  one per streamed delta) divided by the time from each turn's first delta to
  its end, summed over turns; the run value is the median.
- `peak_vram_mb`: the highest GPU memory reading taken before, during
  (every second) and after the run: Ollama's `/api/ps` `size_vram` when the
  backend is Ollama, otherwise `nvidia-smi` total used memory, otherwise
  `null`.
- `total_s`: wall time of the run, excluding a warm-up request ("Reply with
  OK") that is sent first so model loading does not count as TTFT.

## Submitting

Submission is off unless the player (or `almanac bench --submit`) asks for
it, and shows the exact JSON first. It is posted to
`POST https://spacegho.st/mods/ffxiv/almanac/api/results` and contains only
what the schema allows: GPU name and VRAM (rounded down to 256 MB), rounded
system RAM, OS family, backend kind/version, model id, quantisation, context,
tool-calling capability, the scores and per-task results. No player or
character names, no paths, hostnames, IP addresses or free text.
