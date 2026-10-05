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

With the log turned on (an empty file named "log" in the plugin's config
directory), each run appends what started it, the tabs that matter and what
it changed to tab_status.log in the state directory, shared by every session.

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
import traceback
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

CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR", "")
# The log is off unless an empty file named "log" is in the config directory.
LOG_ENABLED = bool(CONFIG_DIR) and os.path.exists(os.path.join(CONFIG_DIR, "log"))
LOG_FILE = os.path.join(STATE_DIR, "tab_status.log")
# Past this size the log moves to tab_status.log.1, replacing the older one.
LOG_MAX_BYTES = 1_000_000
# A session's socket sits in a directory named after the session.
SESSION_NAME = os.path.basename(os.path.dirname(SESSION)) or "?"
# Event fields worth logging, when the event has them.
EVENT_LOG_FIELDS = ("workspace_id", "tab_id", "pane_id", "agent", "agent_status")


def herdr(*args):
    """Run a Herdr CLI command and return its parsed JSON result."""
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    completed = subprocess.run(
        [binary, *args], capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)["result"]


def log(message):
    """Append a line to the log, if it is on. A failed write never stops a run."""
    if not LOG_ENABLED:
        return
    now = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    millis = int(now % 1 * 1000)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{stamp}.{millis:03d} [{SESSION_NAME} {os.getpid()}] {message}\n")
    except OSError:
        pass


def rotate_log():
    if not LOG_ENABLED:
        return
    try:
        if os.path.getsize(LOG_FILE) >= LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass


def describe_trigger(args):
    """What started this run, with the event's ids and agent status."""
    if "--after" in args:
        return "delayed run"
    action = os.environ.get("HERDR_PLUGIN_ACTION_ID")
    if action:
        return f"action {action}"
    event = os.environ.get("HERDR_PLUGIN_EVENT")
    if not event:
        return "startup"
    try:
        data = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}"))["data"]
        fields = [f"{key}={data[key]}" for key in EVENT_LOG_FIELDS if key in data]
    except (ValueError, KeyError, TypeError):
        fields = []
    return " ".join([event, *fields])


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


def log_tabs(tabs):
    """Log the tabs that matter: marked, with an active agent, or looked at.

    The focused space's active tab gets a `*`: Herdr counts it as looked at,
    at least while a window shows the session.
    """
    shown = [
        f'{tab["tab_id"]}{"*" if current else ""} {tab["agent_status"]} "{tab["label"]}"'
        for tab, _, current in tabs
        if current
        or tab["agent_status"] in MARKERS
        or strip_marker(tab["label"]) != tab["label"]
    ]
    log("  tabs: " + (", ".join(shown) or "none"))


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

    # (tab, position in its tab bar, whether it is the focused space's active tab)
    tabs = []
    for workspace in herdr("workspace", "list")["workspaces"]:
        # Herdr counts the focused space's active tab as looked at while a
        # window shows the session. Stock Herdr also does so when no window
        # shows it; Herdr doesn't tell the plugin which applies.
        current_tab = workspace["active_tab_id"] if workspace["focused"] else None
        workspace_tabs = herdr("tab", "list", "--workspace", workspace["workspace_id"])["tabs"]
        for position, tab in enumerate(workspace_tabs, start=1):
            tabs.append((tab, position, tab["tab_id"] == current_tab))
    log_tabs(tabs)

    for tab, position, _ in tabs:
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
                log(f"  {tab_id} seen: keeping its marker for {SEEN_DELAY_SECONDS}s")
            if now < remove_at:
                still_lingering[tab_id] = remove_at
                continue
        if label != tab["label"]:
            herdr("tab", "rename", tab["tab_id"], label)
            log(f'  rename {tab["tab_id"]}: "{tab["label"]}" -> "{label}"')

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
        rotate_log()
        log(describe_trigger(args))
        try:
            reconcile(clear="--clear" in args)
        except Exception:
            log("  failed:\n" + traceback.format_exc().rstrip())
            raise
