#!/usr/bin/env python3
"""Mark Herdr tabs by their agents' status.

While a tab's agents are "done" (finished, not yet seen), "working" or
"blocked" (waiting for input or approval), the tab's name gets a marker in
front of it. The marker goes away once Herdr reports the agents seen and
idle, or gone. A "done" marker stays for SEEN_DELAY_SECONDS after you look
at its tab, so switching to a space still shows which tab had finished.

Herdr runs this script on agent status changes, tab and pane lifecycle
events, and at startup. Every run reconciles all tabs from Herdr's current
state instead of acting on the single event, so a missed or out-of-order
event is corrected by the next run.

Usage:
  tab_status.py              reconcile every tab's marker
  tab_status.py --after SEC  wait SEC seconds, then reconcile (used to
                             remove "done" markers once their delay is up)
  tab_status.py --clear      remove every marker (run before uninstalling)
"""
import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager

# Herdr's tab-level agent status -> marker shown in front of the tab name.
MARKERS = {
    "done": "🟢",
    "working": "⏳",
    "blocked": "🟠",
}
MARKER_PREFIXES = tuple(f"{marker} " for marker in MARKERS.values())
DONE_PREFIX = f"{MARKERS['done']} "

# How long a "done" marker stays after its tab has been seen.
SEEN_DELAY_SECONDS = 2

STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR", ".")
# Session socket -> tab id -> time its "done" marker comes off. Every session
# shares this state directory and tab ids repeat between sessions, so entries
# are kept per session.
SEEN_TABS_FILE = os.path.join(STATE_DIR, "seen_tabs.json")
SESSION = os.environ.get("HERDR_SOCKET_PATH", "")


def herdr(*args):
    """Run a Herdr CLI command and return its parsed JSON result."""
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    completed = subprocess.run(
        [binary, *args], capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)["result"]


def strip_marker(label):
    for prefix in MARKER_PREFIXES:
        if label.startswith(prefix):
            return label[len(prefix):]
    return label


def is_auto_named(tab, position, name):
    """Unnamed tabs show their position in the tab bar.

    Renaming one would freeze that number (Herdr has no way to return a tab
    to automatic naming), so those tabs are left alone. A tab's `number` is
    a stable id rather than its position, so both are checked.
    """
    return name in (str(position), str(tab["number"]))


def desired_label(tab, name, clear):
    marker = None if clear else MARKERS.get(tab["agent_status"])
    return f"{marker} {name}" if marker else name


def load_seen_tabs():
    try:
        with open(SEEN_TABS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_seen_tabs(seen_tabs):
    with open(SEEN_TABS_FILE, "w") as f:
        json.dump(seen_tabs, f, indent=2, sort_keys=True)


def reconcile_later(delay):
    """Run again after `delay` seconds, detached from this hook.

    Herdr waits for the hook's own process and its output pipes, so the
    delayed run gets its own session and no inherited stdio.
    """
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--after", f"{delay:.2f}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@contextmanager
def single_run():
    """Serialize runs; Herdr may start several hooks at once."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "reconcile.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def reconcile(clear=False):
    now = time.time()
    seen_tabs = load_seen_tabs()
    lingering = seen_tabs.get(SESSION, {})
    still_lingering = {}
    newly_seen = False

    for workspace in herdr("workspace", "list")["workspaces"]:
        tabs = herdr("tab", "list", "--workspace", workspace["workspace_id"])["tabs"]
        for position, tab in enumerate(tabs, start=1):
            name = strip_marker(tab["label"])
            if is_auto_named(tab, position, name):
                continue
            label = desired_label(tab, name, clear)
            # A "done" marker about to come off with nothing in its place:
            # its tab has just been seen, so keep the marker a little longer.
            if not clear and label == name and tab["label"].startswith(DONE_PREFIX):
                tab_id = tab["tab_id"]
                remove_at = lingering.get(tab_id)
                if remove_at is None:
                    remove_at = now + SEEN_DELAY_SECONDS
                    newly_seen = True
                if now < remove_at:
                    still_lingering[tab_id] = remove_at
                    continue
            if label != tab["label"]:
                herdr("tab", "rename", tab["tab_id"], label)

    if still_lingering:
        seen_tabs[SESSION] = still_lingering
    else:
        seen_tabs.pop(SESSION, None)
    save_seen_tabs(seen_tabs)
    if newly_seen:
        reconcile_later(SEEN_DELAY_SECONDS + 0.1)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--after" in args:
        time.sleep(float(args[args.index("--after") + 1]))
    with single_run():
        reconcile(clear="--clear" in args)
