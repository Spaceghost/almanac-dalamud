"""Take a screenshot through xdg-desktop-portal without any dialog.

    python -m almanac.helpers.screenshot [OUTPUT_DIR]

Calls org.freedesktop.portal.Screenshot with interactive=false, waits for the
portal's Response signal, moves the resulting PNG into OUTPUT_DIR (default
~/.local/state/almanac/screenshots) and prints its path. Needs a desktop
session bus (DBUS_SESSION_BUS_ADDRESS); a desktop may still ask once for
permission the first time an unsandboxed program uses the portal.
"""

from __future__ import annotations

import secrets
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
from jeepney.io.blocking import open_dbus_connection

PORTAL = DBusAddress("/org/freedesktop/portal/desktop", bus_name="org.freedesktop.portal.Desktop", interface="org.freedesktop.portal.Screenshot")


def screenshot(output_dir: Path, timeout: float = 30.0) -> Path:
    token = "almanac" + secrets.token_hex(4)
    with open_dbus_connection(bus="SESSION") as conn:
        sender = conn.unique_name.lstrip(":").replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response", path=request_path)
        conn.send_and_get_reply(new_method_call(message_bus, "AddMatch", "s", (rule.serialise(),)))
        with conn.filter(rule) as queue:
            options = {"handle_token": ("s", token), "interactive": ("b", False)}
            conn.send_and_get_reply(new_method_call(PORTAL, "Screenshot", "sa{sv}", ("", options)))
            signal = conn.recv_until_filtered(queue, timeout=timeout)
    code, results = signal.body
    if code != 0:
        raise SystemExit(f"portal refused the screenshot (response {code}: {'cancelled' if code == 1 else 'error'})")
    source = Path(unquote(urlparse(results["uri"][1]).path))
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / time.strftime("screenshot-%Y%m%d-%H%M%S.png")
    shutil.move(source, target)
    return target


if __name__ == "__main__":
    out = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path("~/.local/state/almanac/screenshots").expanduser()
    print(screenshot(out))
