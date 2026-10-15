#!/usr/bin/env python3
"""Report whether CameraDeck can be stopped for an update without interrupting work.

Exit status: 0 = safe (service idle or stopped), 1 = busy, 2 = not safe to
decide (restarting, failed, unreachable, or an instance without /api/activity).
Run it immediately before stopping, e.g.
    python3 scripts/check-idle.py && sudo systemctl stop cameradeck
"""

import argparse
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def service_state(unit):
    try:
        output = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState,SubState,LoadState"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def manual_instance():
    try:
        output = subprocess.run(
            ["pgrep", "-af", r"python[0-9.]* \S*app\.py"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True  # Cannot rule one out.
    return bool(output.strip())


def password():
    if "CAMERADECK_PASSWORD" in os.environ:
        return os.environ["CAMERADECK_PASSWORD"]
    try:
        lines = (ROOT / ".env").read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        key, _, value = line.strip().partition("=")
        if key.strip() == "CAMERADECK_PASSWORD":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    return ""


def activity(url):
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    secret = password()
    if secret:
        login = urllib.request.Request(
            url + "/api/login",
            data=json.dumps({"password": secret}).encode(),
            headers={"Content-Type": "application/json"},
        )
        opener.open(login, timeout=10).close()
    with opener.open(url + "/api/activity", timeout=10) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:80")
    parser.add_argument("--unit", default="cameradeck")
    args = parser.parse_args()
    url = args.url.rstrip("/")

    state = service_state(args.unit)
    active = state.get("ActiveState", "unknown")
    if active == "inactive":
        if not manual_instance():
            print("stopped: CameraDeck is not running; safe to update.")
            return 0
        print("service inactive but an app.py process is running; checking it.")
    elif active == "failed":
        print(
            f"failed ({state.get('SubState')}): not safe to update; investigate first."
        )
        return 2
    elif active != "active":
        print(
            f"{active} ({state.get('SubState')}): restarting or changing state; check again later."
        )
        return 2

    try:
        result = activity(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(
                "unknown: this instance predates /api/activity; check the UI manually."
            )
        else:
            print(f"unknown: {url} answered HTTP {exc.code}.")
        return 2
    except (OSError, ValueError) as exc:
        print(f"unknown: could not query {url}: {exc}")
        return 2
    if result.get("idle") is True:
        print("idle: nothing would be interrupted; safe to update.")
        return 0
    operations = result.get("operations") or {}
    print("busy: " + ", ".join(f"{k} ({v})" for k, v in operations.items()))
    return 1


if __name__ == "__main__":
    sys.exit(main())
