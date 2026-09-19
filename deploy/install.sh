#!/usr/bin/env bash
# Install almanac for the current user. Rootless; touches only:
#   ~/.local/share/almanac/venv       Python venv with the pinned dependencies
#   ~/.local/share/almanac/README.md  symlink to this checkout's README
#   ~/.config/almanac/                config.toml (if missing) and the 0600 token
#   ~/.config/systemd/user/           almanac-mcp, almanac-gateway, almanac-run@,
#                                     almanac-autopilot (installed, never enabled here)
#   ~/.local/bin/almanac              symlink to the venv's entry point
#
#   deploy/install.sh            install + enable and start the two services
#   deploy/install.sh --no-start install only
#
# It never prints the token, never enables lingering and never uses sudo.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
share="$HOME/.local/share/almanac"
units="$HOME/.config/systemd/user"
start=1
[[ "${1:-}" == "--no-start" ]] && start=0

python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
python3 -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")' \
  || { echo "this Python's sqlite3 lacks FTS5" >&2; exit 1; }

mkdir -p "$share" "$units" "$HOME/.local/bin"
[[ -d "$share/venv" ]] || python3 -m venv "$share/venv"
"$share/venv/bin/pip" install --quiet --disable-pip-version-check -e "$repo"
ln -sfn "$repo/README.md" "$share/README.md"
ln -sfn "$share/venv/bin/almanac" "$HOME/.local/bin/almanac"

"$share/venv/bin/almanac" init

install -m 0644 "$repo"/deploy/systemd/almanac-{mcp,gateway,autopilot}.service "$repo/deploy/systemd/almanac-run@.service" "$units/"
systemctl --user daemon-reload
if (( start )); then
  systemctl --user enable --now almanac-mcp.service almanac-gateway.service
  systemctl --user --no-pager --lines=3 status almanac-mcp.service almanac-gateway.service || true
fi

cat <<MSG

almanac installed. Next:
  almanac doctor                         # check config, knowledge, backend
  edit ~/.config/almanac/config.toml     # knowledge_dirs, tools_dirs, hosts, listen
The autopilot unit is installed but not enabled; see the README's "Autopilot"
section before: systemctl --user enable --now almanac-autopilot
To keep the services running while you are logged out (headless), the
machine owner must enable lingering once:  loginctl enable-linger $USER
MSG
