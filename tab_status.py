#!/usr/bin/env python3
"""Mark Herdr tabs by their agents' status.

While a tab's agents are "done" (finished, not yet seen), "working" or
"blocked" (waiting for input or approval), the tab's name gets a marker in
front of it. The marker goes away once Herdr reports the agents seen and
idle, or gone. A "done" marker stays for SEEN_DELAY_SECONDS after you look
at its tab, so switching to a space still shows which tab had finished.

A tab can also be flagged, to come back to later: FLAG goes in front of its
name, after any status marker, and stays until the tab is unflagged.

Herdr runs this script on agent status changes, tab and pane lifecycle
events, and at startup. Every run reconciles all tabs from Herdr's current
state instead of acting on the single event, so a missed or out-of-order
event is corrected by the next run.

Settings go in config.toml in the plugin's config directory (see
load_settings). "mark_unnamed" puts markers on unnamed tabs too (see
is_auto_named). "log" makes each run append what started it, the tabs that
matter and what it changed to tab_status.log in the state directory, shared
by every session.

Usage:
  tab_status.py              reconcile every tab's marker
  tab_status.py --after SEC  wait SEC seconds, then reconcile (used to
                             remove "done" markers once their delay is up)
  tab_status.py --clear      remove every marker, and keep this session's
                             tabs without markers until --refresh (run
                             before uninstalling)
  tab_status.py --refresh    turn markers back on after --clear, then
                             reconcile
  tab_status.py --flag       flag the focused tab, or unflag it, then
                             reconcile
"""
import configparser
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

# Put in front of a flagged tab's name. It counts as part of the name, so
# status markers go in front of it and leave it alone.
FLAG = "🚩 "

# How long a "done" marker stays after its tab has been seen.
SEEN_DELAY_SECONDS = 2

STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR", ".")
# Session socket -> tab id -> time its "done" marker comes off. Every session
# shares this state directory and tab ids repeat between sessions, so entries
# are kept per session.
SEEN_TABS_FILE = os.path.join(STATE_DIR, "seen_tabs.json")
SESSION = os.environ.get("HERDR_SOCKET_PATH", "")
# Session sockets whose markers were removed with --clear. Every run there
# keeps markers off until --refresh, so neither the runs started by the
# clear's own renames nor later agent changes put them back before the
# plugin is uninstalled.
CLEARED_FILE = os.path.join(STATE_DIR, "cleared_sessions.json")

CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR", "")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.toml") if CONFIG_DIR else ""
# Every setting is true or false, and off unless config.toml turns it on.
SETTINGS = ("mark_unnamed", "log")
# The section header parse_flat_toml adds. A TOML table name can't contain
# a space unless quoted, so no valid config.toml has a table by this name.
FLAT_SECTION = "top level"


def warn(message):
    """Herdr keeps a run's stderr: `herdr plugin log list` shows it."""
    print(f"tab-status: {message}", file=sys.stderr)


def parse_flat_toml(text):
    """The `name = value` lines of a TOML file, read as INI.

    For Pythons before 3.11, which have no tomllib. It follows TOML where
    these settings need it: names keep their case, `#` starts a comment
    anywhere, indentation means nothing, and only `true` and `false` are
    booleans. Other values stay text, and a [table] (even [DEFAULT]) reads as
    a setting of that name, so both are reported as wrong.
    """
    # INI reads an indented line as more of the value above it, and `#` as a
    # comment only after a space. Stripping both keeps every line's number.
    lines = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    # The added header names INI's default section, so a [DEFAULT] in the
    # file is an ordinary table instead of settings for every section.
    parser = configparser.ConfigParser(
        delimiters=("=",), default_section=FLAT_SECTION, interpolation=None
    )
    parser.optionxform = str
    try:
        parser.read_string("\n".join([f"[{FLAT_SECTION}]", *lines]))
    # Their line numbers count the header line added above.
    except configparser.ParsingError as err:
        numbers = [str(lineno - 1) for lineno, _ in err.errors]
        where = f"line{'s' if len(numbers) > 1 else ''} {', '.join(numbers)}"
        raise ValueError(f"expected name = value on {where}") from None
    except configparser.DuplicateOptionError as err:
        raise ValueError(
            f"{err.option!r} is set twice, the second time on line {err.lineno - 1}"
        ) from None
    except configparser.DuplicateSectionError as err:
        raise ValueError(
            f"[{err.section}] appears twice, the second time on line {err.lineno - 1}"
        ) from None
    booleans = {"true": True, "false": False}
    data = {name: booleans.get(value, value) for name, value in parser.defaults().items()}
    data.update((table, {}) for table in parser.sections())
    return data


def load_settings():
    """{setting: bool} from config.toml.

    A missing file leaves every setting off. So does a file that can't be
    read, with a warning. Pythons before 3.11 read it with parse_flat_toml.
    """
    settings = dict.fromkeys(SETTINGS, False)
    if not CONFIG_FILE or not os.path.exists(CONFIG_FILE):
        return settings
    try:
        with open(CONFIG_FILE, "rb") as f:
            try:
                import tomllib
            except ImportError:
                data = parse_flat_toml(f.read().decode("utf-8"))
            else:
                data = tomllib.load(f)
    # TOMLDecodeError and UnicodeDecodeError are ValueErrors. tomllib runs
    # out of stack on a file nested hundreds of levels deep.
    except (OSError, ValueError, RecursionError, configparser.Error) as err:
        warn(f"ignoring {CONFIG_FILE}: {err}")
        return settings
    for name, value in data.items():
        if name not in settings:
            warn(f"{CONFIG_FILE}: unknown setting {name!r}")
        elif not isinstance(value, bool):
            warn(f"{CONFIG_FILE}: {name} must be true or false")
        else:
            settings[name] = value
    return settings


# Set from config.toml once a run holds the lock (see __main__), so a
# delayed run reads the settings as they are after its wait.
LOG_ENABLED = False
MARK_UNNAMED = False
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


def toggle_flag(name):
    return name[len(FLAG):] if name.startswith(FLAG) else FLAG + name


def is_auto_named(tab, position, name):
    """Unnamed tabs show their position in the tab bar.

    Renaming one would freeze that number (Herdr has no way to return a tab
    to automatic naming), so those tabs are left alone unless MARK_UNNAMED
    is on. A tab's `number` is a stable id rather than its position, so both
    are checked.
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


def markers_off(clear, refresh):
    """Whether this session's tabs stay without markers, after this run's
    --clear or --refresh."""
    try:
        with open(CLEARED_FILE) as f:
            cleared = set(json.load(f))
    except (OSError, ValueError, TypeError):
        cleared = set()
    was_off = SESSION in cleared
    off = (was_off or clear) and not refresh
    if off != was_off:
        if off:
            cleared.add(SESSION)
        else:
            cleared.discard(SESSION)
        with open(CLEARED_FILE, "w") as f:
            json.dump(sorted(cleared), f, indent=2)
        log("  markers back on" if was_off else "  markers off until refresh")
    elif off:
        log("  markers off since clear; refresh turns them back on")
    return off


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


def reconcile(clear=False, flag_tab_id=None):
    """Bring every tab's marker up to date.

    With `flag_tab_id`, that tab's flag is also toggled, in the same rename
    as its marker.
    """
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
    if flag_tab_id and all(tab["tab_id"] != flag_tab_id for tab, _, _ in tabs):
        log(f"  flag: no tab {flag_tab_id}")

    for tab, position, _ in tabs:
        name = strip_marker(tab["label"])
        if tab["tab_id"] == flag_tab_id:
            name = toggle_flag(name)
        if not MARK_UNNAMED and is_auto_named(tab, position, name):
            # Unnamed tabs get no marker. This removes the one left on a tab
            # that was just unflagged back to its number.
            label = name
        else:
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
                label = DONE_PREFIX + name
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
        settings = load_settings()
        LOG_ENABLED, MARK_UNNAMED = settings["log"], settings["mark_unnamed"]
        rotate_log()
        log(describe_trigger(args))
        try:
            # Herdr passes the tab in front of you: the focused space's active tab.
            flag_tab_id = os.environ.get("HERDR_TAB_ID") if "--flag" in args else None
            off = markers_off(clear="--clear" in args, refresh="--refresh" in args)
            reconcile(clear=off, flag_tab_id=flag_tab_id)
        except Exception:
            log("  failed:\n" + traceback.format_exc().rstrip())
            raise
