#!/usr/bin/env bash
# The Python engine's checks, one per call (.github/workflows/python.yml):
#
#   tools/ci/python-check.sh ruff | mypy | bandit | pip-audit | coverage
#
# ruff, mypy and bandit count their findings and hold the count to
# tests/quality-budget.txt: the check fails when a count rises, so a new
# finding is caught the day it appears while the old ones are worked down
# (`--update` rewrites the budget after fixing some). pip-audit has no budget:
# any finding fails. (ruff's formatter is not enforced: the tree predates it.) coverage fails under the floor
# in the same file.
# Needs: pip install -e ".[test]" ruff mypy bandit pip-audit pytest-cov
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1
BUDGET="$ROOT/tests/quality-budget.txt"
kind="${1:?ruff|mypy|bandit|pip-audit|coverage}"
update=0; [[ "${2:-}" == --update ]] && update=1
held() { # key count
  local have
  have="$(awk -v k="$1" '$1==k{print $2}' "$BUDGET")"
  if [[ $update == 1 ]]; then
    grep -v "^$1 " "$BUDGET" >"$BUDGET.tmp" || true
    echo "$1 $2" >>"$BUDGET.tmp"; sort -o "$BUDGET" "$BUDGET.tmp"; rm -f "$BUDGET.tmp"; have="$2"
  fi
  echo "$1: $2 findings (budget ${have:-none})"
  [[ -n "$have" && "$2" -le "$have" ]]
}
case "$kind" in
  ruff)
    ruff check src tests --output-format concise | tee /dev/stderr | grep -c ':[0-9]*:[0-9]*:' >/tmp/ruff.count
    held ruff "$(cat /tmp/ruff.count)" ;;
  mypy)
    mypy src --ignore-missing-imports | tee /dev/stderr | grep -c ': error:' >/tmp/mypy.count
    held mypy "$(cat /tmp/mypy.count)" ;;
  bandit)
    bandit -q -r src --severity-level medium || true
    held bandit "$(bandit -q -r src -f json | python3 -c 'import json,sys; print(sum(1 for r in json.load(sys.stdin)["results"] if r["issue_severity"] in ("MEDIUM","HIGH")))')" ;;
  pip-audit)
    pip-audit --skip-editable ;;
  coverage)
    floor="$(awk '$1=="coverage-floor"{print $2}' "$BUDGET")"
    pytest -q --cov=almanac --cov-report=term --cov-report=xml:build/coverage.xml "--cov-fail-under=${floor:-0}" ;;
  *) echo "unknown check: $kind" >&2; exit 2 ;;
esac
