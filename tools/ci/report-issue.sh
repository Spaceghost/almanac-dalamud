#!/usr/bin/env bash
# A check reports itself: one row in the job summary, and one issue per kind of
# check, updated rather than duplicated.
#
#   tools/ci/report-issue.sh <kind> <pass|fail> [details-file]
#
# fail: comments on the open issue labelled quality-<kind>, or opens it.
# pass: closes that issue with a note when one is open.
# Issues are only touched on push, schedule and workflow_dispatch runs with
# GH_TOKEN set: a pull request (a fork's above all) only gets the summary row.
# Always exits 0; the step that ran the check carries the failure.
set -uo pipefail
kind="${1:?kind}" status="${2:?pass|fail}" details="${3:-}"
label="quality-$kind"
run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
excerpt=""
[[ -n "$details" && -s "$details" ]] && excerpt="$(tail -c 6000 "$details")"

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  header=""
  [[ -s "$GITHUB_STEP_SUMMARY" ]] || header='| Check | Result |\n| --- | --- |\n'
  printf "$header"'| %s | %s |\n' "$kind" "$status" >>"$GITHUB_STEP_SUMMARY"
fi

case "${GITHUB_EVENT_NAME:-}" in push | schedule | workflow_dispatch) ;; *) exit 0 ;; esac
[[ -n "${GH_TOKEN:-}" && -n "${GITHUB_REPOSITORY:-}" ]] || exit 0
command -v gh >/dev/null || exit 0

n="$(gh issue list --repo "$GITHUB_REPOSITORY" --label "$label" --state open --json number --jq '.[0].number' 2>/dev/null || true)"
if [[ "$status" == pass ]]; then
  [[ -n "$n" ]] && gh issue close "$n" --repo "$GITHUB_REPOSITORY" --comment "Passing again in $run_url (${GITHUB_SHA:-})." >/dev/null
  exit 0
fi
# shellcheck disable=SC2016 # the backticks are Markdown
body="$(printf 'The `%s` check failed in %s (commit %s). Artifacts are attached to the run.\n\n```\n%s\n```\n' "$kind" "$run_url" "${GITHUB_SHA:-}" "$excerpt")"
gh label create "$label" --color B60205 --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1 || true
if [[ -n "$n" ]]; then gh issue comment "$n" --repo "$GITHUB_REPOSITORY" --body "$body" >/dev/null
else gh issue create --repo "$GITHUB_REPOSITORY" --label "$label" --title "Quality check failing: $kind" --body "$body" >/dev/null; fi
exit 0
