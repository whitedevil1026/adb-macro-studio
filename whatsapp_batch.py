"""
WhatsApp batch export: scan the chat list, then export selected chats one by one
by READING the screen (no fixed coordinates). Includes a robust scroll module that
uses the scanned chat order to scroll the right direction and recover from overshoot.

Drives adb_core primitives. The GUI (adb_gui.py) runs these in a thread and shows
live progress; nothing here touches Tkinter.
"""
from __future__ import annotations

import copy
import csv
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import adb_core as core
import bt_transfer as bt
try:
    import qs_accept
except Exception:
    qs_accept = None

# --- Quick Share transfer config (editable) ---
# Set these to your own values (or edit them in the batch window's fields at runtime):
PC_NAME = "YOUR-PC-NAME"                    # the PC's Quick Share device name (shown on the phone)
SAVE_DIR = str(Path.home() / "Downloads")  # folder where the PC's Quick Share saves received files

# If getting a chat to the "sent" point (export + reach share sheet + pick PC) takes longer
# than this, give up on it, mark it fail-timeout in the CSV, and move to the next chat.
# This is what stops a mega-chat (e.g. one that starves the screen-reader) from hanging the run.
MAX_SEND_SECONDS = 180                      # the actual file transfer keeps its own longer timeout

# Quick Share transfers (and, occasionally, the export menu) fail transiently - the transfer at
# the OS/Bluetooth/Wi-Fi-Direct layer (more often on large files), the menu when the phone is
# briefly too busy to render "Include media" in time. Neither is a logic bug, so we just retry
# the whole export+send a few times before giving up. A mega-chat 'fail-timeout' is NOT retried
# (retrying it would just time out again), and 'fail-notfound' is handled separately.
SHARE_RETRIES = 2                           # extra attempts after the first (so up to 3 total)
SHARE_RETRY_STATUSES = ("fail-transfer", "fail-sent", "fail-noshare", "fail-pcpick", "fail-export")

# Learned tap positions for "Quick Share" and the PC, per screen size. On a mega-chat the
# phone is too busy for uiautomator to read the share sheet, so we fall back to these.
_COORD_FILE = Path(__file__).resolve().parent / ".qs_coords.json"
def _load_coords():
    try:
        return json.loads(_COORD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
_COORDS = _load_coords()
def _save_coords():
    try:
        _COORD_FILE.write_text(json.dumps(_COORDS), encoding="utf-8")
    except Exception:
        pass

CONTACT_ID = "conversations_row_contact_name"     # WhatsApp chat-name node
LIST_MARKER = "Ask Meta AI or Search"             # present on the chat list
CHAT_MARKER = "More options"                       # present inside a chat

# The proven single-chat export flow (with-media, fallback to without-media).
# Timeouts are generous: on a busy phone the export menu / "Include media" dialog can take
# several seconds to render, and a too-short wait was causing false "fail-export" results.
EXPORT_STEPS = [
    {"type": "tap_text", "text": "More options", "match": "contains", "timeout": 15, "delay": 0.8},
    {"type": "tap_text", "text": "More", "match": "exact", "timeout": 12, "delay": 0.8},
    {"type": "tap_text", "text": "Export chat", "match": "contains", "timeout": 15, "delay": 1.0},
    {"type": "tap_text", "text": "Include media", "match": "contains", "timeout": 18, "delay": 0.7},
    {"type": "if_text", "text": "Unable to export", "match": "contains", "timeout": 300,
     "or_text": "Quick Share", "delay": 0.3,
     "then": [
         {"type": "tap_text", "text": "OK", "match": "exact", "timeout": 10, "delay": 0.8},
         {"type": "tap_text", "text": "More options", "match": "contains", "timeout": 15, "delay": 0.8},
         {"type": "tap_text", "text": "More", "match": "exact", "timeout": 12, "delay": 0.8},
         {"type": "tap_text", "text": "Export chat", "match": "contains", "timeout": 15, "delay": 1.0},
         {"type": "tap_text", "text": "Without media", "match": "contains", "timeout": 18, "delay": 0.7},
     ],
     "else": []},
]
BACKOUT = [
    {"type": "wait", "seconds": 8, "delay": 0.2, "label": "let export settle (no polling)"},
    {"type": "key", "key": "BACK", "delay": 1.0},
    {"type": "key", "key": "BACK", "delay": 1.2},
]


# ----------------------------------------------------------------- screen helpers
def _nodes(adb):
    try:
        return core.parse_ui_nodes(adb.ui_dump())
    except Exception:
        return []


# direction / formatting marks WhatsApp sprinkles into names; strip so the SAME chat always reads
# as the SAME name (otherwise a misread with a stray mark looks like a new/phantom chat).
_NAME_MARKS = "‎‏‪‫‬⁦⁧⁨⁩﻿"


def _norm_name(s):
    return (s or "").strip().strip(_NAME_MARKS).strip()


def _fingerprint(s):
    """Loose identity key for a chat name: lowercase letters+digits of ANY script, with
    whitespace/punctuation/emoji/direction-marks removed. Used to catch the SAME chat read with a
    slightly different name under load (dropped emoji, stray punctuation/space) so it isn't exported
    again in a loop.

    CRITICAL: keep Unicode word characters, not just ASCII. A name in a regional script
    (Telugu/Hindi/Arabic/CJK...) or an emoji-only name must NOT collapse to "" - otherwise the first
    such finished chat would poison the done-set and mass-skip every other non-Latin-named chat.
    Callers MUST treat a "" fingerprint as unique (never a duplicate)."""
    return re.sub(r"[\W_]+", "", _norm_name(s).lower(), flags=re.UNICODE)


def visible_chats(adb):
    """List of (y, name) for chat rows currently on screen, top -> bottom. Names are normalised
    (whitespace + invisible direction marks stripped) for stable de-duplication."""
    rows = []
    for n in _nodes(adb):
        if n["id"].endswith(CONTACT_ID):
            nm = _norm_name(n["text"])
            if nm:
                rows.append((n["bounds"][1], nm))
    return sorted(rows)


def _find_chat_node(adb, name):
    key = _norm_name(name).lower()
    for n in _nodes(adb):
        if n["id"].endswith(CONTACT_ID) and _norm_name(n["text"]).lower() == key:
            return n
    return None


def on_chat_list(adb):
    """True  = on the chat list,
    False = a readable screen that is NOT the list (safe to press BACK),
    None  = the screen could not be read - DO NOT press BACK (retry the read instead).
    Treating an unreadable dump as 'not on the list' was pressing BACK while actually on
    the list, which minimises/closes WhatsApp."""
    nodes = _nodes(adb)
    if not nodes:
        return None
    if core.find_node(nodes, LIST_MARKER, "contains") is not None:
        return True
    # robust fallback: several chat-name rows on screen means we're on the list, even if the
    # search-hint text differs by WhatsApp version/locale (an open chat shows no such rows).
    rows = [n for n in nodes if n["id"].endswith(CONTACT_ID) and n["text"].strip()]
    return len(rows) >= 2


def keep_awake(adb, on=True):
    """Prevent the screen from locking/sleeping mid-run. `svc power stayon` only holds while the
    device thinks it's charging and resets whenever USB briefly drops, so we ALSO push the
    screen-off timeout way up as a belt-and-braces measure."""
    try:
        adb.shell("svc", "power", "stayon", "true" if on else "false", quiet=True)
    except Exception:
        pass
    try:
        if on:
            adb.shell("settings", "put", "system", "screen_off_timeout", "1800000", quiet=True)  # 30 min
    except Exception:
        pass


_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


def pc_keep_awake(on=True):
    """Keep the PC (and its display) awake for the whole batch - the Windows equivalent of the
    phone's keep_awake. An unattended run can take hours, and if Windows sleeps, Quick Share stops
    receiving and pywinauto can't click Accept. Uses SetThreadExecutionState, which stays in effect
    on the calling thread until cleared (so call it from the batch thread and clear it in finally).
    No-op / best-effort off Windows."""
    try:
        import ctypes
        if on:
            ctypes.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED)
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception:
        pass


def screen_is_on(adb):
    """Best-effort display state: True = on, False = off, None = unknown."""
    try:
        out = adb.shell("dumpsys", "power", quiet=True)
    except Exception:
        return None
    for k in ("Display Power: state=ON", "mWakefulness=Awake", "mScreenOn=true"):
        if k in out:
            return True
    for k in ("Display Power: state=OFF", "mWakefulness=Asleep", "mWakefulness=Dozing", "mScreenOn=false"):
        if k in out:
            return False
    return None


def on_share_sheet(adb):
    """True if the Android system share sheet (chooser) is the foreground window. This is the
    RELIABLE way to know the export reached the share sheet, because `uiautomator dump` often
    returns NOTHING for the chooser (com.android.intentresolver / ChooserActivity), so the text
    'Quick Share' can't be read even though it's on screen."""
    try:
        out = adb.shell("dumpsys", "window", quiet=True)
    except Exception:
        return False
    for line in out.splitlines():
        if "mCurrentFocus" in line or "mFocusedApp" in line:
            if ("intentresolver" in line or "ChooserActivity" in line
                    or "ResolverActivity" in line or "com.android.internal.app.ChooserActivity" in line):
                return True
    return False


def dismiss_anr(adb):
    """Detect Android's "WhatsApp isn't responding" (ANR) dialog and tap WAIT to keep the app
    alive, instead of letting the run stall behind it (or the app get killed). WhatsApp ANRs when
    uiautomator reads hammer a busy/media-heavy phone. Returns True if an ANR dialog was handled.
    Best-effort and cheap - safe to call at the top of the main loop."""
    try:
        nodes = _nodes(adb)
    except Exception:
        return False
    hit = (core.find_node(nodes, "isn't responding", "contains")
           or core.find_node(nodes, "not responding", "contains")
           or core.find_node(nodes, "Close app", "contains"))
    if not hit:
        return False
    # prefer "Wait" (keep the app running); fall back to just dismissing the dialog.
    if not _tap_text(adb, "Wait", timeout=4, match="contains", delay=1.0):
        try:
            adb.key("BACK")
        except Exception:
            pass
    return True


def _device_wait_reason(adb_path):
    """A human-readable reason the batch is waiting for the device, so a long "initialisation" is
    never a mystery. Explains the ACTUAL adb state (unauthorized / offline / none)."""
    try:
        devs = core.Adb(adb_path).devices()
    except Exception as e:
        return f"waiting for device - cannot run adb ({e})"
    if not devs:
        return ("waiting for device - none detected. Check the USB cable/port, keep the phone "
                "UNLOCKED, and make sure File Transfer (MTP) + USB debugging are on.")
    states = ", ".join(f"{d.get('serial','?')}={d.get('state','?')}" for d in devs)
    if any(d.get("state") == "unauthorized" for d in devs):
        return (f"waiting for device - UNAUTHORIZED ({states}). Unlock the phone and tap "
                "'Allow'/'Always allow' on the USB-debugging prompt.")
    if any(d.get("state") == "offline" for d in devs):
        return (f"waiting for device - OFFLINE ({states}). Re-seat the USB cable (or toggle USB "
                "debugging); the phone is connected but not responding to adb yet.")
    return f"waiting for device to become ready ({states})"


def wake_unlock(adb, size=None):
    """Wake the screen and swipe up to dismiss a SIMPLE (swipe) keyguard. Cannot defeat a secure
    PIN/pattern/password lock - for those the caller must ask the user to unlock the phone."""
    try:
        adb.key("WAKEUP"); time.sleep(0.5)
        w, h = size or (720, 1600)
        adb.swipe(w // 2, int(h * 0.85), w // 2, int(h * 0.22), 300)   # swipe up on the keyguard
        time.sleep(0.5)
    except Exception:
        pass


# ----------------------------------------------------------------- scan
def scan_chats(adb, size, emit=None, max_scrolls=500, pause=None, stop=None):
    """Scroll from the top and return the ordered list of chat names.
    Uses a gentle, momentum-free swipe (no fling -> no skipped names) and treats the bottom as
    'the list stopped moving' (not 'no new names', which fires early on long lists).
    Pausable via `pause` (set = paused) and stoppable via `stop` (threading.Events)."""
    w, h = size
    cx, cy = w // 2, h // 2
    off = int(h * 0.06)                   # same tiny swing as the export scroll (no fling)

    def _wait_pause():
        while pause is not None and pause.is_set():
            if stop is not None and stop.is_set():
                return
            time.sleep(0.2)

    def swipe_up():                       # gentle, toward the top
        adb.swipe(cx, cy - off, cx, cy + off, 1200); time.sleep(0.7)

    def swipe_down():                     # gentle, toward the bottom
        adb.swipe(cx, cy + off, cx, cy - off, 1200); time.sleep(0.7)

    keep_awake(adb, True)
    # jump to the top first (stop when the view stops changing)
    last = None
    for _ in range(40):
        if stop and stop.is_set():
            return []
        _wait_pause()
        cur = [nm for _, nm in visible_chats(adb)]
        if cur and cur == last:
            break
        last = cur; swipe_up()

    ordered, seen = [], set()
    stale = 0
    for _ in range(max_scrolls):
        if stop and stop.is_set():
            break
        _wait_pause()
        before = [nm for _, nm in visible_chats(adb)]
        for nm in before:
            if nm not in seen:
                seen.add(nm); ordered.append(nm)
        if emit:
            emit("scan_progress", len(ordered))
        swipe_down()
        after = [nm for _, nm in visible_chats(adb)]
        if after and after == before:        # the list did not move -> bottom reached
            stale += 1
            if stale >= 3:
                break
        else:
            stale = 0
    return ordered


# ----------------------------------------------------------------- one export
def _run_steps(adb, case, size, steps, emit_log, outer_stop=None, on_runner=None, limit=420):
    done = threading.Event(); res = {}
    def em(kind, *a):
        if kind == "log":
            emit_log(a[0])
        elif kind == "finished":
            res["r"] = a[0]; done.set()
    r = core.MacroRunner(adb, case, steps, emit=em, cur_size=size,
                         loops=1, show_frames=False, stop_on_error=True)
    if on_runner:
        on_runner(r)                         # let the owner hold this runner (for Stop)
    r.start()
    start = time.time()
    while not done.wait(0.2):                # poll so an outer Stop interrupts promptly
        if outer_stop is not None and outer_stop.is_set():
            r.stop(); done.wait(5); break
        if time.time() - start > limit:
            r.stop(); done.wait(5); break
    if on_runner:
        on_runner(None)
    return res.get("r", "stopped")


# ----------------------------------------------------------------- send helpers
def _tap_text(adb, text, timeout=12, match="contains", delay=1.0, stop=None):
    end = time.time() + timeout
    while time.time() < end:
        if stop is not None and stop.is_set():
            return False
        n = core.find_node(_nodes(adb), text, match)
        if n:
            adb.tap(*core.node_center(n)); time.sleep(delay); return True
        time.sleep(1.0)
    return False


def _wait_text(adb, text, timeout=30, match="contains", stop=None):
    end = time.time() + timeout
    while time.time() < end:
        if stop is not None and stop.is_set():
            return False
        if core.find_node(_nodes(adb), text, match):
            return True
        time.sleep(1.0)
    return False


def _wait_node(adb, text, timeout=30, match="contains", stop=None):
    """Like _wait_text but returns the matching node (or None)."""
    end = time.time() + timeout
    while time.time() < end:
        if stop is not None and stop.is_set():
            return None
        n = core.find_node(_nodes(adb), text, match)
        if n:
            return n
        time.sleep(1.0)
    return None


def _wait_pc_node(adb, pc_name, timeout=30, stop=None):
    """Find the Quick Share device-picker tile for EXACTLY `pc_name`. STRICT on purpose: it only
    accepts a device whose name equals pc_name (optionally followed by a status like 'Available'),
    so it can NEVER tap a different nearby device whose name merely contains pc_name as a substring
    - critical because this selects the RECIPIENT. If two different device names would match, it
    refuses (returns None) rather than guess."""
    want = _norm_name(pc_name).lower()
    if not want:
        return None
    end = time.time() + timeout
    while time.time() < end:
        if stop is not None and stop.is_set():
            return None
        hits, seen = [], set()
        for n in _nodes(adb):
            for f in (n.get("text"), n.get("desc")):
                if not f:
                    continue
                fl = _norm_name(f).lower()
                if fl == want or fl.startswith(want + " ") or fl.startswith(want + "("):
                    hits.append(n); seen.add(fl); break
        if len(seen) == 1 and hits:          # exactly ONE device name matched -> safe to tap it
            return hits[0]
        if len(seen) > 1:                    # ambiguous (multiple device names match) -> refuse
            return None
        time.sleep(0.7)
    return None


def _wait_either(adb, text_a, text_b, timeout=60, match="contains", stop=None):
    """Return 'a' if text_a appears first, 'b' if text_b appears first, None on timeout/stop."""
    end = time.time() + timeout
    while time.time() < end:
        if stop is not None and stop.is_set():
            return None
        nodes = _nodes(adb)
        if core.find_node(nodes, text_a, match):
            return "a"
        if core.find_node(nodes, text_b, match):
            return "b"
        time.sleep(0.7)
    return None


def _wait_any(adb, labels, timeout=60, match="contains", stop=None):
    """labels: {key: text}. Return the key of the first text to appear on screen, or None."""
    end = time.time() + timeout
    while time.time() < end:
        if stop is not None and stop.is_set():
            return None
        nodes = _nodes(adb)
        for key, txt in labels.items():
            if core.find_node(nodes, txt, match):
                return key
        time.sleep(0.6)
    return None


def _wait_export_outcome(adb, timeout, stop=None):
    """After tapping Include/Without media, wait for the export outcome and return one of:
    'blocked' (Advanced chat privacy), 'unable' (media too big -> fall back), 'qs' (the share
    sheet was reached), or None (timeout/stopped). 'qs' is detected by the "Quick Share" text
    OR by the Android chooser being the foreground window - the chooser is frequently UNREADABLE
    by uiautomator, so waiting only for the text would hang forever."""
    end = time.time() + timeout
    while time.time() < end:
        if stop is not None and stop.is_set():
            return None
        nodes = _nodes(adb)
        if core.find_node(nodes, "Can't export", "contains"):
            return "blocked"
        if core.find_node(nodes, "Unable to export", "contains"):
            return "unable"
        if core.find_node(nodes, "Quick Share", "contains"):
            return "qs"
        if on_share_sheet(adb):            # chooser up but unreadable -> export reached the share sheet
            return "qs"
        time.sleep(0.7)
    return None


def _open_export_menu(adb, stop=None, left=lambda: 1e9):
    """Reliably navigate the chat overflow menu to reveal "Export chat".

    The single tap on the 3-dots ("More options") is frequently SWALLOWED by WhatsApp, so a
    blind one-shot tap often leaves the menu closed and "More"/"Export chat" is never found.
    Here we tap the 3-dots and CONFIRM the menu actually opened (a menu item is visible),
    re-tapping if it didn't, then tap "More" to reveal "Export chat". Returns True when
    "Export chat" is on screen."""
    def _stopped():
        return stop is not None and stop.is_set()
    for _ in range(4):                                  # whole-menu attempts
        if _stopped() or left() <= 0:
            return False
        if core.find_node(_nodes(adb), "Export chat", "contains"):
            return True
        # 1) open the overflow menu, verifying it actually opened
        opened = False
        for _ in range(3):                             # re-tap the 3-dots until the menu shows
            if _stopped() or left() <= 0:
                return False
            dots = _wait_node(adb, "More options", timeout=5, match="contains", stop=stop)
            if dots is None:
                break
            adb.tap(*core.node_center(dots)); time.sleep(1.4)
            nodes = _nodes(adb)
            if core.find_node(nodes, "Export chat", "contains"):
                return True                            # some layouts show Export chat directly
            if core.find_node(nodes, "More", "exact"):
                opened = True; break                   # menu is up (has the "More" item)
        if not opened:
            continue
        # 2) tap "More" -> reveals Export chat
        if _tap_text(adb, "More", timeout=6, match="exact", delay=1.0, stop=stop):
            if _wait_text(adb, "Export chat", timeout=6, match="contains", stop=stop):
                return True
    return False


def _do_export(adb, media, emit_log, outer_stop, left):
    """Export the open chat, WITH media first, falling back to WITHOUT media only if WhatsApp
    rejects it ("Unable to export"). Sets media['mode']. Returns 'done' / 'fail' / 'stopped' /
    'timeout'. On 'done' the Quick Share sheet is showing."""
    def _stopped():
        return outer_stop is not None and outer_stop.is_set()

    def _open_and_tap_export():
        if not _open_export_menu(adb, stop=outer_stop, left=left):
            return False
        return _tap_text(adb, "Export chat", timeout=12, match="contains", delay=1.2, stop=outer_stop)

    def _is_privacy_blocked():
        # newer WhatsApp: "Advanced chat privacy" blocks export -> a "Can't export chats" dialog
        n = _nodes(adb)
        return (core.find_node(n, "Can't export", "contains") is not None
                or core.find_node(n, "Advanced chat privacy", "contains") is not None)

    def _skip_blocked():
        emit_log("Advanced Chat Privacy is ON for this chat - cannot export; skipping")
        _tap_text(adb, "OK", timeout=6, match="exact", delay=0.6, stop=outer_stop)
        return "blocked"

    # --- attempt WITH media ---
    media["mode"] = "with media"
    if not _open_and_tap_export():
        return "stopped" if _stopped() else ("timeout" if left() <= 0 else "fail")
    # Some chats have "Advanced chat privacy" ON, which BLOCKS export: a "Can't export chats"
    # dialog shows instead of the media choice. Detect it right away so we don't hang for minutes
    # waiting for a share sheet that will never appear.
    if _wait_text(adb, "Can't export", timeout=4, match="contains", stop=outer_stop) or _is_privacy_blocked():
        return _skip_blocked()
    # WAIT for the media-choice dialog to appear after "Export chat", THEN tap "Include media".
    # (Don't assume it's instant - tapping too early / bailing early was leaving the export stuck.)
    if _wait_text(adb, "Include media", timeout=15, match="contains", stop=outer_stop):
        for _ in range(3):                              # re-tap if the choice dialog lingers
            if _stopped():
                return "stopped"
            if core.find_node(_nodes(adb), "Include media", "contains") is None:
                break                                   # tapped -> dialog gone, export proceeding
            _tap_text(adb, "Include media", timeout=8, match="contains", delay=1.2, stop=outer_stop)
    # branch: privacy-blocked | media-too-big | share sheet reached (by text OR foreground chooser)
    which = _wait_export_outcome(adb, timeout=max(5, min(300, left())), stop=outer_stop)
    if which == "qs":
        return "done"                                   # with media reached the share sheet
    if which == "blocked":
        return _skip_blocked()
    if which is None:
        return "stopped" if _stopped() else ("timeout" if left() <= 0 else "fail")

    # --- media rejected -> fall back to WITHOUT media ---
    media["mode"] = "without media"
    emit_log("media too big - falling back to Without media")
    _tap_text(adb, "OK", timeout=10, match="exact", delay=0.8, stop=outer_stop)
    if not _open_and_tap_export():
        return "stopped" if _stopped() else ("timeout" if left() <= 0 else "fail")
    if _wait_text(adb, "Without media", timeout=15, match="contains", stop=outer_stop):
        for _ in range(3):
            if _stopped():
                return "stopped"
            if core.find_node(_nodes(adb), "Without media", "contains") is None:
                break
            _tap_text(adb, "Without media", timeout=8, match="contains", delay=1.2, stop=outer_stop)
    # share sheet reached? (text OR the foreground chooser, since the chooser is often unreadable)
    if _wait_export_outcome(adb, timeout=max(5, min(240, left())), stop=outer_stop) == "qs":
        return "done"
    return "stopped" if _stopped() else ("timeout" if left() <= 0 else "fail")


def _back_to_list(adb, tries=14):
    """Get back to the chat list after an export. We are NOT on the list here (we're on the Quick
    Share picker / Android share sheet / a dialog / inside a chat), so a BACK is safe and needed -
    including on an UNREADABLE frame (the Android chooser returns NO nodes to uiautomator, so it
    reads as 'unreadable' - we must still BACK out of it). We explicitly detect the chooser by its
    foreground window and back out of it; otherwise we give a couple of unreadable frames a moment
    to settle, then press BACK regardless. This avoids the relaunch fallback (which resets the list
    scroll to the top and makes long runs re-traverse / stop early)."""
    none_streak = 0
    for _ in range(tries):
        if on_share_sheet(adb):            # unreadable Quick Share chooser - just BACK out of it
            adb.key("BACK"); time.sleep(1.2); none_streak = 0; continue
        st = on_chat_list(adb)
        if st:
            return True
        if st is None and none_streak < 2:
            none_streak += 1; time.sleep(1.0); continue   # let a transient frame settle
        none_streak = 0
        adb.key("BACK"); time.sleep(1.2)
    # last resort: bring WhatsApp back to the front (NOTE: this resets the list scroll)
    try:
        adb.launch("com.whatsapp"); time.sleep(2.0)
    except core.AdbError:
        pass
    for _ in range(5):
        st = on_chat_list(adb)
        if st:
            return True
        if st is None:
            time.sleep(1.0); continue
        adb.key("BACK"); time.sleep(1.2)
    return bool(on_chat_list(adb))


def export_and_send(adb, case, size, emit_log=lambda m: None, outer_stop=None,
                    on_runner=None, pc_name=PC_NAME, save_dir=SAVE_DIR, auto_accept=True,
                    deadline=None):
    """Open chat is assumed already. Export (media -> fallback), send via Quick Share to
    `pc_name`, wait for the file on the PC, hash it, then navigate back to the chat list.
    `deadline` (epoch seconds) caps the time to reach the 'sent' point; if it's blown we give
    up with 'fail-timeout' and move on (so a mega-chat can't hang the batch). The transfer
    itself keeps its own longer timeout.
    Returns (status, file_path_or_None, sha256_or_None, media_mode)."""
    def _left():
        return (deadline - time.time()) if deadline else 1e9
    # the flow always ATTEMPTS with-media first (it taps "Include media"), so default to that.
    # the branch only downgrades to "without media" if WhatsApp rejects the media export.
    # => the media column is filled for every chat that reaches the export screen, pass or fail.
    # the flow ATTEMPTS with-media first; _do_export downgrades media['mode'] to "without media"
    # only if WhatsApp rejects the media export.
    media = {"mode": "with media"}
    reason = _do_export(adb, media, emit_log, outer_stop, _left)
    if reason == "stopped":
        return "stopped", None, None, media["mode"]
    if reason == "timeout":
        emit_log("took too long to export - skipping (fail-timeout)")
        _back_to_list(adb); return "fail-timeout", None, None, media["mode"]
    if reason == "blocked":
        # "Advanced chat privacy" is ON for this chat - export is impossible until the user turns
        # it off, so don't retry; record the REASON clearly in the CSV and move on.
        _back_to_list(adb); return "blocked-privacy", None, None, media["mode"]
    if reason != "done":
        _back_to_list(adb)
        return "fail-export", None, None, media["mode"]
    key = f"{size[0]}x{size[1]}"
    cached = _COORDS.get(key, {})
    blind = False                              # did we fall back to a learned position?

    # --- find & tap "Quick Share" ---
    # The Android share chooser is frequently UNREADABLE by uiautomator (0 nodes). So: if the
    # chooser is the foreground window, don't waste time waiting for a "Quick Share" node that can
    # never be read - go straight to the learned position. Only try reading it when the chooser
    # isn't (yet) the recognised foreground.
    qs_node = None
    # If the chooser is foreground AND we already know where Quick Share is, blind-tap directly -
    # the chooser is unreadable so reading would only waste time. Otherwise TRY to read it: we need
    # a readable node to learn its position (and the chooser is only *sometimes* unreadable, so a
    # longer wait can catch a readable moment on the first run of a new device).
    if not (on_share_sheet(adb) and cached.get("qs")):
        qs_node = _wait_node(adb, "Quick Share",
                             max(2, min(40 if cached.get("qs") else 240, _left())), stop=outer_stop)
    before = bt.snapshot(save_dir)
    if auto_accept and qs_accept is not None:
        threading.Thread(target=lambda: qs_accept.accept_quickshare(timeout=180, log=emit_log),
                         daemon=True).start()
    if qs_node is not None:
        _COORDS.setdefault(key, {})["qs"] = list(core.node_center(qs_node)); _save_coords()
        adb.tap(*core.node_center(qs_node)); time.sleep(3.0)
    elif cached.get("qs"):
        emit_log("share sheet unreadable - tapping Quick Share by learned position")
        adb.tap(*cached["qs"]); time.sleep(3.0); blind = True
    else:
        _back_to_list(adb)
        return ("fail-timeout" if _left() <= 0 else "fail-noshare"), None, None, media["mode"]

    # --- find & tap the PC in the device picker ---
    # STRICT match on the exact PC name so we never send to a different nearby device whose name
    # merely contains the PC name (sending to the wrong recipient is unacceptable).
    cached = _COORDS.get(key, {})
    pc_node = _wait_pc_node(adb, pc_name, max(2, min(25 if cached.get("pc") else 60, _left())), stop=outer_stop)
    if pc_node is not None:
        _COORDS.setdefault(key, {})["pc"] = list(core.node_center(pc_node)); _save_coords()
        adb.tap(*core.node_center(pc_node)); time.sleep(2.0)
    else:
        # SAFETY: never blind-tap a learned position for the PC. The Quick Share device-picker
        # order is NOT fixed (other nearby devices come and go), so a blind tap could send the
        # export to the WRONG device. Fail instead; the retry will try again once the picker is
        # readable and the correct PC can be matched by name.
        _back_to_list(adb)
        return ("fail-timeout" if _left() <= 0 else "fail-pcpick"), None, None, media["mode"]

    # Wait for the file on the PC (the definitive success signal), reading the phone's Quick Share
    # screen as we go. KEY: a chat with a big attachment (a long PDF, a video) transfers SLOWLY, so
    # we must NOT give up on a fixed timeout - that was declaring a slow-but-fine transfer "failed"
    # and re-sending it (duplicate (1)(2)(3).zip). Instead we keep waiting as long as the transfer
    # is genuinely ALIVE (the phone shows Sending/progress, or already Completed), and only give up
    # when there's no file AND no visible progress for a while.
    emit_log(f"sent to {pc_name}; waiting for the file to arrive...")
    phone = {"next": 0.0, "sent": False, "failed": False, "completed": False,
             "active_at": 0.0, "completed_at": 0.0}

    def _abort():
        # returns "stop" (user stop -> bail now) or None. We DO NOT fast-fail on a phone 'Failed':
        # Quick Share writes the received file atomically at the END of the transfer, so no file is
        # visible mid-flight, and the phone's screen lists EVERY nearby device - an early/stray
        # 'Failed' (often from a DIFFERENT device) used to trip a retry that re-sent the chat and
        # created duplicate (1)(2)(3).zip files. The received file + 'Completed' + the activity-aware
        # wait are the verdict. We READ 'Completed'/'Done' (-> fast definitive success), track
        # in-progress signals (Sending/%/Receiving... -> active_at), and log 'Sent'/'Failed'.
        if outer_stop is not None and outer_stop.is_set():
            return "stop"
        now = time.time()
        if now < phone["next"]:
            return None
        phone["next"] = now + 3.0                       # throttle phone reads
        nodes = _nodes(adb)
        if not phone["completed"] and (core.find_node(nodes, "Completed", "contains")
                                       or core.find_node(nodes, "Done", "exact")):
            phone["completed"] = True
            phone["completed_at"] = now
            emit_log("phone shows Quick Share 'Completed'")
        # transfer-in-progress signals -> the transfer is ALIVE, keep waiting (big files are slow)
        for n in nodes:
            for f in (n.get("text"), n.get("desc")):
                if not f:
                    continue
                fl = f.lower()
                if ("sending" in fl or "receiving" in fl or "waiting for" in fl
                        or "connecting" in fl or "preparing" in fl or "transferring" in fl
                        or re.search(r"\d+\s*%", fl) or re.search(r"\d+(\.\d+)?\s*[mk]b/s", fl)):
                    phone["active_at"] = now
                    break
        if not phone["failed"] and core.find_node(nodes, "Failed", "contains"):
            phone["failed"] = True
            emit_log("phone screen shows a 'Failed' (ignored - the received file decides)")
        if not phone["sent"] and core.find_node(nodes, "Sent", "contains"):
            phone["sent"] = True
            emit_log("phone shows Quick Share 'Sent' - finalising on PC")
        return None

    # activity-aware wait: a minimum "floor" (in case the transfer screen is unreadable so we can't
    # see progress), extended while the phone keeps showing progress, up to a hard ceiling. The floor
    # is the latency of a GENUINE failure (file never comes, no progress), so keep it modest - a
    # successful transfer returns the instant the file lands, long before the floor.
    # BLIND (transfer screen unreadable): we can't see progress, so wait a longer minimum in case a
    # transfer is silently happening. READABLE: progress is visible, so the activity-extension below
    # covers any slow transfer and a short floor is enough - this keeps a genuine failure fast.
    floor_until = time.time() + (90 if blind else 45)
    hard_cap = time.time() + (600 if blind else 1200)
    res = None
    while time.time() < hard_cap:
        if outer_stop is not None and outer_stop.is_set():
            break
        res = bt.wait_for_new_file(save_dir, before, timeout=30,
                                   abort=_abort, confirm=lambda: phone["completed"])
        if res:
            break
        if phone["completed"]:
            # phone says done -> the file should land within seconds. Give it a short grace, then
            # STOP (don't loop to the 10-min cap): 'Completed' with no file = wrong Save folder.
            if time.time() - phone["completed_at"] < 45:
                continue
            break
        if time.time() - phone["active_at"] < 25:
            continue                                   # transfer still showing progress -> wait on
        if time.time() < floor_until:
            continue                                   # can't see progress yet -> honour the floor
        break                                          # no file, no progress -> genuinely done/failed
    _back_to_list(adb)
    if not res:
        # the file never arrived AND the transfer stopped showing progress: a real failure (PC not
        # receiving / asleep / wrong save folder / transfer dropped). Retried.
        if phone["completed"]:
            # the phone SAYS it completed but nothing landed in save_dir -> almost always the Save
            # folder isn't the real Quick Share destination. Retrying won't help; flag it loudly.
            emit_log(f"WARNING: phone shows 'Completed' but no file appeared in '{save_dir}'. "
                     "The Save folder is probably NOT where Quick Share saves - fix it to stop "
                     "every transfer being counted as failed.")
        return ("fail-sent" if phone["failed"] else "fail-transfer"), None, None, media["mode"]
    path, nbytes, digest = res
    # VERIFY the received .zip actually opens and read its real contents (authoritative over the
    # UI-flow guess). A big attachment whose transfer was cut short lands as a TRUNCATED/corrupt zip;
    # recording that as 'ok' would be bad evidence, so a zip that won't open is treated as a failure
    # and retried. A valid zip also makes the CSV 'media' column truthful and explains file sizes.
    label = media["mode"]
    try:
        info = bt.zip_media_info(path)
    except Exception as e:
        info = {"ok": False, "error": str(e)}
    if not info.get("ok"):
        emit_log(f"received file is not a valid/complete zip ({info.get('error')}) - treating as a "
                 "failed transfer and retrying")
        return "fail-transfer", None, None, media["mode"]
    label = info["label"]                               # "with media"/"without media" from the zip itself
    emit_log(f"verified contents: {info['files']} files, {info['media']} media, "
             f"{info['media_bytes']/1e6:.1f} MB media ({label})")
    if label != media["mode"]:
        emit_log(f"note: UI flow attempted '{media['mode']}' but the file is '{label}'")
    try:
        case.record_file(Path(path), f"whatsapp export ({label}) via Quick Share")
    except Exception:
        pass
    emit_log(f"received {Path(path).name} ({nbytes} bytes, {label})")
    return "ok", path, digest, label


def _late_arrived_file(save_dir, before, settle=2.0):
    """A transfer the timeout cut off can still land a moment later. Return (path, bytes, sha256)
    for a NEW, size-stable file that appeared since `before`, else None. This is the last guard
    against a duplicate: if the file is already here, we must NOT re-send the chat."""
    if not save_dir:
        return None
    try:
        now = bt.snapshot(save_dir)
        cands = [n for n, s in now.items() if s > 0 and (n not in before or before.get(n) != s)]
        if not cands:
            return None
        newest = max(cands, key=lambda n: os.path.getmtime(os.path.join(save_dir, n)))
        p = os.path.join(save_dir, newest)
        s1 = os.path.getsize(p)
        time.sleep(settle)
        if os.path.getsize(p) != s1:                       # still growing -> not finished
            return None
        if p.lower().endswith(".zip") and not bt.zip_media_info(p).get("ok"):
            return None                                    # partial/corrupt zip -> not a real success
        return p, s1, bt.sha256(p)
    except OSError:
        return None


def _await_late_file(adb, save_dir, before, emit_log, outer_stop, timeout=200):
    """The foolproof anti-duplicate guard. Before we ever RE-SEND a chat, make sure the previous
    transfer is not still completing: Quick Share writes the received file only when the transfer
    FINISHES, so a slow big-media transfer can be declared 'failed' and then land a moment later -
    re-sending it is what creates duplicate (1)(2)(3).zip files.

    We watch BOTH the save folder and the phone for up to `timeout`s:
      - a NEW, size-stable, VALID zip -> accept it (the transfer succeeded; do NOT re-send);
      - a file still GROWING, or the phone still showing Sending/%/progress -> keep waiting;
      - genuinely quiet (no file, no progress) for a while -> give up so a real failure is retried.
    Returns (path, bytes, sha256) or None."""
    if not save_dir:
        return None
    start = time.time()
    last_sizes = {}
    last_activity = time.time()
    next_phone = 0.0
    quiet_grace = 18.0
    while time.time() - start < timeout:
        if outer_stop is not None and outer_stop.is_set():
            return None
        try:
            now = bt.snapshot(save_dir)
        except Exception:
            now = {}
        growing = False
        for n, s in now.items():
            if s <= 0 or (n in before and before.get(n) == s):
                continue                                   # unchanged pre-existing file
            if last_sizes.get(n) != s:
                growing = True                             # size moved since last poll -> arriving
                last_sizes[n] = s
                last_activity = time.time()
                continue
            # stable since last poll -> is it a complete, valid export?
            p = os.path.join(save_dir, n)
            try:
                if (not n.lower().endswith(".zip")) or bt.zip_media_info(p).get("ok"):
                    time.sleep(0.6)
                    return p, os.path.getsize(p), bt.sha256(p)
            except OSError:
                pass
        if time.time() >= next_phone:                      # is the phone still transferring?
            next_phone = time.time() + 3.0
            try:
                for nd in _nodes(adb):
                    for f in (nd.get("text"), nd.get("desc")):
                        if not f:
                            continue
                        fl = f.lower()
                        if ("sending" in fl or "receiving" in fl or "transferring" in fl
                                or "waiting for" in fl or "connecting" in fl or "preparing" in fl
                                or re.search(r"\d+\s*%", fl)):
                            last_activity = time.time()
            except Exception:
                pass
        if not growing and (time.time() - last_activity) > quiet_grace:
            return None                                    # nothing arriving, phone idle -> real fail
        time.sleep(2.0)
    return None


def export_with_retry(adb, case, size, reopen, emit_log, outer_stop, on_runner,
                      pc_name, save_dir, ensure_list):
    """Run export_and_send, retrying the whole thing on a transient Quick Share failure
    (fail-transfer/noshare/pcpick). `reopen()` re-opens the chat from the list and returns
    True, or False if the chat row can't be found. Returns (status, path, digest, media).

    Before EVERY re-send we re-check the save folder for a file that arrived late: Quick Share
    writes the received file only when the transfer finishes, so a slow transfer can time out and
    then land - re-sending it would create a duplicate zip. If a new file is already here, we accept
    it instead of re-sending."""
    base_before = bt.snapshot(save_dir) if save_dir else {}
    status, path, digest, media = "fail-notfound", None, None, ""
    for attempt in range(1 + SHARE_RETRIES):
        if not reopen():
            if attempt == 0:
                return "fail-notfound", None, None, ""    # genuinely not on screen to begin with
            # couldn't re-find the chat for the retry (the list moved) - keep the REAL failure from
            # the previous attempt rather than masking it as 'fail-notfound' (which hides a real
            # chat and stops it being retried on resume).
            emit_log("could not re-open the chat for retry - keeping the previous result")
            return status, path, digest, media
        status, path, digest, media = export_and_send(
            adb, case, size, emit_log=emit_log, outer_stop=outer_stop, on_runner=on_runner,
            pc_name=pc_name, save_dir=save_dir, deadline=time.time() + MAX_SEND_SECONDS)
        if (status not in SHARE_RETRY_STATUSES
                or (outer_stop is not None and outer_stop.is_set())
                or attempt >= SHARE_RETRIES):
            return status, path, digest, media
        # FOOLPROOF anti-duplicate: never re-send while the previous (possibly slow) transfer could
        # still be completing. Wait and watch the folder + phone; if a valid file lands, accept it.
        emit_log("verifying the previous transfer really failed before re-sending (anti-duplicate)...")
        late = _await_late_file(adb, save_dir, base_before, emit_log, outer_stop)
        if late:
            lp, lb, ld = late
            label = media or "with media"
            try:
                info = bt.zip_media_info(lp)
                if info.get("ok"):
                    label = info["label"]
            except Exception:
                pass
            emit_log(f"received {Path(lp).name} ({lb} bytes, {label}) - arrived late; NOT re-sending (no duplicate)")
            try:
                case.record_file(Path(lp), f"whatsapp export ({label}) via Quick Share")
            except Exception:
                pass
            return "ok", lp, ld, label
        emit_log(f"{status} - retrying (attempt {attempt + 2}/{1 + SHARE_RETRIES})")
        ensure_list()
    return status, path, digest, media


# ----------------------------------------------------------------- batch thread
class WhatsAppBatch(threading.Thread):
    """emit(kind, *args):
        ("progress", index, total, name, status)   status: running/ok/fail-*/skipped
        ("log", msg)
        ("finished", {"last": <name or None>, "results": [(name, status), ...]})
    """

    def __init__(self, adb, case, size, names, order, emit, pause=None,
                 pc_name=PC_NAME, save_dir=SAVE_DIR):
        super().__init__(daemon=True)
        self.adb, self.case, self.size = adb, case, size
        self.names = names                    # chats to export (subset, in order)
        self.order = order                    # full scanned order (for scroll direction)
        self.emit = emit
        self._stop = threading.Event()
        self._pause = pause or threading.Event()   # set = paused
        self._runner = None                        # current inner export runner
        self.pc_name, self.save_dir = pc_name, save_dir
        self.files = {}                            # name -> (filename, sha256, media)
        self.times = {}                            # name -> ISO time the row became terminal
        self.csv_path = str(Path(case.dir) / "exported_chats.csv")
        self.last_done = None
        self.results = []

    def _write_csv(self):
        now = core.now_iso()
        rows = []
        for i, (nm, st) in enumerate(self.results, 1):
            self.times.setdefault(nm, now)   # stamp on first write = completion time
            fn, digest, media = self.files.get(nm, ("", "", ""))
            if not media and st.startswith("fail"):
                media = "not exported"       # chat never opened -> no media choice made
            rows.append([i, _csv_safe(nm), st, media, _csv_safe(fn), digest, self.times[nm]])
        write_csv_atomic(self.csv_path, rows, lambda m: self.emit("log", m))

    def stop(self):
        self._stop.set()
        r = self._runner
        if r:
            try:
                r.stop()
            except Exception:
                pass

    def _set_runner(self, r):
        self._runner = r

    def _wait_pause(self):
        was = self._pause.is_set()
        if was:
            self.emit("log", "paused")
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.2)
        if was and not self._stop.is_set():
            self.emit("log", "resumed")

    # --- device / list guards
    def _device_ready(self):
        try:
            return any(d["state"] == "device" for d in core.Adb(self.adb.path).devices())
        except Exception:
            return False

    def _wait_device(self, timeout=180):
        end = time.time() + timeout
        said = None
        while time.time() < end and not self._stop.is_set():
            if self._device_ready():
                if said is not None:
                    self.emit("log", "device is ready")
                return True
            # explain WHY we're waiting (so "stuck in initialisation" is never a mystery)
            msg = _device_wait_reason(self.adb.path)
            if msg != said:
                self.emit("log", msg)
                said = msg
            time.sleep(2)
        return False

    def _ensure_list(self):
        for _ in range(8):
            if dismiss_anr(self.adb):                 # clear an ANR dialog that blocks the list
                time.sleep(1.2)
            st = on_chat_list(self.adb)
            if st:
                return True
            if st is None:                # unreadable: retry, don't BACK out of WhatsApp
                time.sleep(1.0); continue
            self.adb.key("BACK"); time.sleep(1.2)
        return bool(on_chat_list(self.adb))

    def _swipe_down(self):
        w, h = self.size
        cx, cy = w // 2, h // 2
        off = int(h * 0.06)                    # gentle, no-fling drag (consistent with the rest)
        self.adb.swipe(cx, cy + off, cx, cy - off, 1200); time.sleep(0.7)

    def _swipe_up(self):
        w, h = self.size
        cx, cy = w // 2, h // 2
        off = int(h * 0.06)
        self.adb.swipe(cx, cy - off, cx, cy + off, 1200); time.sleep(0.7)

    def _scroll_to(self, name, max_steps=120):
        """Scroll toward `name` using the known order; recover from overshoot."""
        ti = self.order.index(name) if name in self.order else None
        for _ in range(max_steps):
            if self._stop.is_set():
                return None
            self._wait_pause()
            node = _find_chat_node(self.adb, name)
            if node:
                return node
            vis = [nm for _, nm in visible_chats(self.adb)]
            idxs = [self.order.index(nm) for nm in vis if nm in self.order]
            if ti is None or not idxs:
                self._swipe_down()                      # no info: scan downward
            elif max(idxs) < ti:
                self._swipe_down()                      # target is below the view
            elif min(idxs) > ti:
                self._swipe_up()                        # overshot: target is above
            else:
                self._swipe_down()                      # target within range but hidden: nudge
        return None

    def run(self):
        try:
            if not self._device_ready() and not self._wait_device():
                self.emit("finished", {"last": None, "results": []}); return
            keep_awake(self.adb, True)
            pc_keep_awake(True)                   # keep THIS PC awake for the whole run (like the phone)
            self.emit("log", "PC sleep/screen-off suppressed for the duration of the run")
            if not Path(self.save_dir).is_dir():
                self.emit("log", f"WARNING: save folder '{self.save_dir}' does not exist - "
                                 "received files won't be detected. Point Quick Share there.")
            self._ensure_list()
            total = len(self.names)
            for i, name in enumerate(self.names, 1):
                if self._stop.is_set():
                    self.emit("log", "stopped by user"); break
                self._wait_pause()
                if self._stop.is_set():
                    break
                self.emit("progress", i, total, name, "running")
                try:
                    if not self._device_ready():
                        self.emit("log", "device disconnected - waiting up to 180s")
                        if not self._wait_device():
                            self.emit("progress", i, total, name, "fail-device")
                            self.results.append((name, "fail-device")); break
                        time.sleep(2)
                    if not self._ensure_list():
                        self.emit("progress", i, total, name, "fail-nolist")
                        self.results.append((name, "fail-nolist")); continue
                    def reopen():
                        node = self._scroll_to(name)
                        if not node:
                            return False
                        self.adb.tap(*core.node_center(node)); time.sleep(1.4)
                        return True
                    status, path, digest, media = export_with_retry(
                        self.adb, self.case, self.size, reopen,
                        lambda m: self.emit("log", f"   {m}"),
                        self._stop, self._set_runner, self.pc_name, self.save_dir, self._ensure_list)
                    if status == "fail-notfound":
                        self.emit("progress", i, total, name, "fail-notfound")
                        self.results.append((name, "fail-notfound")); continue
                    # record the media mode that was chosen even if the send later failed
                    # (blank only if we never reached the with/without-media choice)
                    self.files[name] = (Path(path).name if path else "", digest or "", media)
                    if not self._stop.is_set():
                        _back_to_list(self.adb)      # guarantee we're off the share sheet/picker
                    if self._stop.is_set():
                        self.emit("progress", i, total, name, "stopped"); break
                    self.emit("progress", i, total, name, status)
                    self.results.append((name, status))
                    if status == "ok":
                        self.last_done = name
                    self._write_csv()
                except core.AdbError as e:
                    self.emit("log", f"adb error (likely disconnect): {e}")
                    self.emit("progress", i, total, name, "fail-adb")
                    self.results.append((name, "fail-adb"))
                    self._wait_device()
                    try:
                        self._ensure_list()
                    except core.AdbError:
                        pass
                except Exception as e:
                    # one chat's unexpected error must not abort the whole selected-export run
                    self.emit("log", f"unexpected error on {name!r} (continuing): {e!r}")
                    self.emit("progress", i, total, name, "fail-error")
                    self.results.append((name, "fail-error"))
                    try:
                        self._ensure_list()
                    except Exception:
                        pass
        finally:
            pc_keep_awake(False)       # release the PC sleep/display lock
            try:
                keep_awake(self.adb, False)
            except Exception:
                pass
            self._write_csv()          # guarantee the final state (incl. a last failure) is saved
            self.emit("finished", {"last": self.last_done, "results": self.results})


# ----------------------------------------------------------------- CSV helpers
def _csv_safe(v):
    """Neutralise spreadsheet formula injection from chat names/filenames."""
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return s


def _csv_unsafe(s):
    """Inverse of _csv_safe: strip the leading ' so a name read back from the CSV matches live."""
    if len(s) >= 2 and s[0] == "'" and s[1] in ("=", "+", "-", "@", "\t", "\r"):
        return s[1:]
    return s


CSV_HEADER = ["#", "chat", "status", "media", "saved_file", "sha256", "time"]


def write_csv_atomic(path, rows, log=lambda m: None):
    """Write the exported-chats CSV so it can NEVER be lost (it's evidence):
    - writes to a temp file then atomically replaces the target (a crash mid-write can't corrupt it);
    - makes the parent folder if missing;
    - REFUSES to overwrite an existing non-empty CSV with an EMPTY table (so a failed/empty run
      never erases prior progress)."""
    try:
        if not rows:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return                       # don't truncate real data to nothing
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(CSV_HEADER)
            for r in rows:
                wr.writerow(r)
        os.replace(tmp, path)                # atomic on the same filesystem
    except Exception as e:
        log(f"csv write failed: {e}")
        try:
            if os.path.exists(path + ".tmp"):
                os.remove(path + ".tmp")
        except Exception:
            pass


def latest_csv_for_serial(cases_dir, serial):
    """Find the most recent non-empty exported_chats.csv for THIS device (its serial is in the
    case folder name, '<timestamp>_<serial>'). Returns the path or None. This is how a restart on
    the SAME mobile continues the SAME CSV, while a DIFFERENT mobile (different serial) gets none
    and starts fresh."""
    if not serial:
        return None
    import glob
    pat = os.path.join(str(cases_dir), f"*_{serial}", "exported_chats.csv")
    found = [c for c in glob.glob(pat) if os.path.isfile(c) and os.path.getsize(c) > 0]
    if not found:
        return None
    return max(found, key=os.path.getmtime)


def load_progress(csv_path):
    """Read a prior exported_chats.csv so a run can RESUME. Returns (status, order, files, times)
    where status maps chat-name -> the recorded status. Callers keep chats already 'ok' (skip
    them) and re-queue everything else. Raises on a real read error (a locked/corrupt file) so the
    caller can ABORT instead of proceeding with empty data that might overwrite the evidence."""
    status, order, files, times = {}, [], {}, {}
    if not os.path.exists(csv_path):
        return status, order, files, times
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            nm = _csv_unsafe((row.get("chat") or "").strip())
            if not nm:
                continue
            if nm not in status:
                order.append(nm)
            status[nm] = (row.get("status") or "").strip()
            files[nm] = (_csv_unsafe(row.get("saved_file") or ""),
                         row.get("sha256") or "", row.get("media") or "")
            times[nm] = row.get("time") or ""
    return status, order, files, times


def save_names_csv(rows, path):
    """rows: list of (name, status). Writes a simple CSV; returns the path."""
    path = str(path)
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["#", "chat", "status", "time"])
        for i, (name, status) in enumerate(rows, 1):
            wr.writerow([i, _csv_safe(name), _csv_safe(status), core.now_iso()])
    return path


# ----------------------------------------------------------------- rolling scan+export
class RollingBatch(threading.Thread):
    """Page by page: read the chats on screen -> log them to CSV -> export the ones
    not done yet -> scroll down -> repeat to the bottom. No pre-scan, no scroll-to-find.

    emit(kind, *args):
        ("discover", name)          a newly seen chat (add a pending row)
        ("row", name, status)       running / ok / fail-*
        ("log", msg)
        ("finished", {"exported": n, "total": m, "csv": path})
    """

    def __init__(self, adb, case, size, emit, pause=None, csv_path=None,
                 pc_name=PC_NAME, save_dir=SAVE_DIR, resume_from=None, start_from=None):
        super().__init__(daemon=True)
        self.adb, self.case, self.size, self.emit = adb, case, size, emit
        self._stop = threading.Event()
        self._pause = pause or threading.Event()
        self._runner = None       # current inner export runner
        self.status = {}          # name -> status
        self.files = {}           # name -> (filename, sha256, media)
        self.times = {}           # name -> ISO time the row became terminal
        self.order = []           # discovery order
        self.pc_name, self.save_dir = pc_name, save_dir
        self.csv_path = str(csv_path) if csv_path else str(Path(case.dir) / "exported_chats.csv")
        self.resume_from = resume_from   # path to a prior CSV: skip its 'ok' chats, retry the rest
        self.start_from = start_from     # chat name to scroll to and begin at (skip everything above)
        self.only_names = None           # if set (a set of names), export ONLY these; skip others
        self.scan_order = None           # a pre-scanned ordered list of names (for directional seek)
        self._done_fps = set()           # fingerprints of finished chats (loop/duplicate guard)

    def stop(self):
        self._stop.set()
        r = self._runner
        if r:
            try:
                r.stop()
            except Exception:
                pass

    def _set_runner(self, r):
        self._runner = r

    def _wait_pause(self):
        was = self._pause.is_set()
        if was:
            self.emit("log", "paused")
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.2)
        if was and not self._stop.is_set():
            self.emit("log", "resumed")

    def _device_ready(self):
        try:
            return any(d["state"] == "device" for d in core.Adb(self.adb.path).devices())
        except Exception:
            return False

    def _wait_device(self, timeout=180):
        end = time.time() + timeout
        said = None
        while time.time() < end and not self._stop.is_set():
            if self._device_ready():
                if said is not None:
                    self.emit("log", "device is ready")
                return True
            # explain WHY we're waiting (so "stuck in initialisation" is never a mystery)
            msg = _device_wait_reason(self.adb.path)
            if msg != said:
                self.emit("log", msg)
                said = msg
            time.sleep(2)
        return False

    def _ensure_list(self):
        for _ in range(8):
            if dismiss_anr(self.adb):                 # clear an ANR dialog that blocks the list
                time.sleep(1.2)
            st = on_chat_list(self.adb)
            if st:
                return True
            if st is None:                # unreadable: retry, don't BACK out of WhatsApp
                time.sleep(1.0); continue
            self.adb.key("BACK"); time.sleep(1.2)
        return bool(on_chat_list(self.adb))

    def _recover_screen(self):
        """The chat list went unreadable. Could be a transient read, a SCREEN-OFF/sleep, or a
        LOCKED phone. Wake + re-assert stay-on + get back to the list. If the screen is off/locked
        we WAIT for the user to unlock (never give up or stop); if the screen is on but still
        unreadable it's a real glitch, so give up after ~1.5 min. Returns True once readable."""
        dead = 0
        while not self._stop.is_set():
            if not self._device_ready() and not self._wait_device():
                return False
            on = screen_is_on(self.adb)
            wake_unlock(self.adb, self.size)
            keep_awake(self.adb, True)
            self._ensure_list()
            if visible_chats(self.adb):
                return True
            if on is False:
                # screen off / secure lock -> wait for the user to unlock it; do NOT stop the run.
                self.emit("log", "phone is LOCKED or screen is OFF - please UNLOCK it "
                                 "(keep it plugged in); waiting...")
                time.sleep(5); dead = 0
            else:
                dead += 1
                if dead >= 30:            # screen on but unreadable for ~1.5 min -> real glitch
                    return False
                time.sleep(3)
        return False

    def _swipe_down(self):
        # Drag symmetrically about the EXACT centre (cx, cy+off -> cy-off), staying in the middle
        # band so it never hits the bottom "home" gesture area (which would close WhatsApp).
        # A SHORT swing over a SLOW stroke = a controlled drag with no fling momentum, so the list
        # advances only a little each time and keeps a big overlap between pages -> no chat is
        # skipped. (A faster/longer swipe flings past rows and was missing chats.)
        w, h = self.size                       # screen size, read from the device on connect
        cx, cy = w // 2, h // 2                # exact centre point
        # Tiny swing over a VERY slow stroke so the release velocity is far below Android's fling
        # threshold -> the list moves exactly the drag distance with NO momentum/overshoot, so no
        # chat is flung past unread. (0.06h over 1200ms: measured ratio ~1.0 = no overshoot.)
        off = int(h * 0.06)
        self.adb.swipe(cx, cy + off, cx, cy - off, 1200); time.sleep(0.7)

    def _swipe_up(self):
        w, h = self.size
        cx, cy = w // 2, h // 2
        off = int(h * 0.06)
        self.adb.swipe(cx, cy - off, cx, cy + off, 1200); time.sleep(0.7)

    def _seek_chat(self, target, max_scrolls=200):
        """Navigate to `target` and begin there. If a pre-scanned order is known, use it to decide
        DIRECTION (the target's index vs the chats currently on screen) and scroll up or down
        accordingly - much faster than always scrolling from the top. Every chat that comes BEFORE
        the target in the scan order is marked 'skipped' (we're deliberately starting later).
        Falls back to a top-down scan if no scan order is available. Returns True if found."""
        tgt = _norm_name(target).lower()
        order = [_norm_name(n) for n in (self.scan_order or [])]
        idx = {nm.lower(): i for i, nm in enumerate(order)}
        ti = idx.get(tgt)

        def _skip_before():
            # mark every chat that is before the target in the scan order as 'skipped'
            if ti is None:
                return
            for i in range(ti):
                orig = (self.scan_order or [])[i]
                if orig not in self.status:
                    self.status[orig] = "skipped"; self.order.append(orig)
                    self.emit("discover", orig); self.emit("row", orig, "skipped")
            self._write_csv()

        if ti is None:
            # no known position -> jump to the top, then scan down (old behaviour)
            for _ in range(30):
                if self._stop.is_set():
                    return False
                before = [nm for _, nm in visible_chats(self.adb)]
                self._swipe_up()
                after = [nm for _, nm in visible_chats(self.adb)]
                if after and after == before:
                    break

        for _ in range(max_scrolls):
            if self._stop.is_set():
                return False
            vis = [_norm_name(nm) for _, nm in visible_chats(self.adb)]
            vis_l = [nm.lower() for nm in vis]
            if tgt in vis_l:
                _skip_before()
                return True
            if ti is None:
                # unknown target position: scan downward, skipping what we pass
                for nm in vis:
                    if nm not in self.status:
                        self.status[nm] = "skipped"; self.order.append(nm)
                        self.emit("discover", nm); self.emit("row", nm, "skipped")
                self._write_csv()
                before = vis_l; self._swipe_down()
                after = [_norm_name(nm).lower() for _, nm in visible_chats(self.adb)]
                if after and after == before:
                    return False
                continue
            # directional: compare the target's index to what's on screen
            vis_idx = [idx[nm] for nm in vis_l if nm in idx]
            if not vis_idx:
                self._swipe_down(); continue
            if max(vis_idx) < ti:
                self._swipe_down()             # target is below the current view
            elif min(vis_idx) > ti:
                self._swipe_up()               # target is above the current view
            else:
                self._swipe_down()             # target is within range but just off-screen
        return False

    def _write_csv(self):
        now = core.now_iso()
        rows = []
        for i, nm in enumerate(self.order, 1):
            st = self.status.get(nm, "")
            if st and st not in ("pending", "running"):   # terminal -> stamp once
                self.times.setdefault(nm, now)
            fn, digest, media = self.files.get(nm, ("", "", ""))
            if not media and st.startswith("fail"):
                media = "not exported"       # chat never opened -> no media choice made
            rows.append([i, _csv_safe(nm), st, media, _csv_safe(fn), digest, self.times.get(nm, "")])
        write_csv_atomic(self.csv_path, rows, lambda m: self.emit("log", m))

    def _export_one(self, name):
        def reopen():
            node = _find_chat_node(self.adb, name)
            if not node:
                return False
            self.adb.tap(*core.node_center(node)); time.sleep(1.4)
            return True
        status, path, digest, media = export_with_retry(
            self.adb, self.case, self.size, reopen,
            lambda m: self.emit("log", f"   {m}"),
            self._stop, self._set_runner, self.pc_name, self.save_dir, self._ensure_list)
        # record the media mode that was chosen even if the send later failed
        # (blank only if we never reached the with/without-media choice)
        self.files[name] = (Path(path).name if path else "", digest or "", media)
        # GUARANTEE we are back on the chat list before the next chat - after exhausting the
        # retries the phone can be left on the (unreadable) Quick Share chooser/picker, which would
        # break the next chat. This robustly dismisses it (never relaunches unless truly stuck).
        if not self._stop.is_set():
            _back_to_list(self.adb)
        return status

    def run(self):
        exported = 0
        unexpected = 0                             # count of non-AdbError errors (safety bail at 30)
        try:
            if not self._device_ready() and not self._wait_device():
                self.emit("finished", {"exported": 0, "total": 0, "csv": self.csv_path}); return
            try:                                  # remember the phone's own screen-off timeout to restore later
                self._orig_sot = self.adb.shell("settings", "get", "system",
                                                "screen_off_timeout", quiet=True).strip()
            except Exception:
                self._orig_sot = None
            keep_awake(self.adb, True)
            pc_keep_awake(True)                   # keep THIS PC awake for the whole run (like the phone)
            self.emit("log", "PC sleep/screen-off suppressed for the duration of the run")
            if not Path(self.save_dir).is_dir():
                self.emit("log", f"WARNING: save folder '{self.save_dir}' does not exist - "
                                 "received files won't be detected. Point Quick Share there.")
            self._ensure_list()

            # RESUME: pre-load a prior run's CSV. Chats already 'ok' are kept (skipped); every
            # other chat is re-queued as pending so it gets retried.
            if self.resume_from:
                # BACK UP the source CSV first - it's evidence; never risk losing it on resume.
                try:
                    bak = self.resume_from + time.strftime(".%Y%m%d_%H%M%S.bak")
                    shutil.copy2(self.resume_from, bak)
                    self.emit("log", f"resume: backed up source CSV -> {os.path.basename(bak)}")
                except Exception as e:
                    self.emit("log", f"resume: could not back up source CSV ({e})")
                try:
                    st, od, fl, tm = load_progress(self.resume_from)
                except Exception as e:
                    # could NOT read the resume CSV (locked/corrupt). ABORT rather than proceed with
                    # empty data that might overwrite the evidence.
                    self.emit("log", f"resume FAILED to read '{self.resume_from}': {e} - aborting "
                                     f"(nothing overwritten)")
                    self.emit("finished", {"exported": 0, "total": 0, "csv": self.csv_path}); return
                for nm in od:
                    if nm not in self.order:
                        self.order.append(nm)
                    self.status[nm] = "ok" if st.get(nm) == "ok" else "pending"
                    self.files[nm] = fl.get(nm, ("", "", ""))
                    self.times[nm] = tm.get(nm, "")
                    if self.status[nm] == "ok":
                        fp = _fingerprint(nm)
                        if fp:
                            self._done_fps.add(fp)             # don't re-export a variant of a done chat
                    self.emit("discover", nm)
                    self.emit("row", nm, self.status[nm])
                done = sum(1 for nm in od if self.status.get(nm) == "ok")
                self.emit("log", f"resume: {len(od)} chats loaded, {done} already ok (skipping "
                                 f"those, retrying the rest)")
                if not self.scan_order:          # the prior run's order IS the scan list for seeking
                    self.scan_order = list(od)
                self._write_csv()

            # START-FROM: go (directionally, using the known scan order) to a named chat and begin
            # there - everything before it in the order is marked 'skipped'.
            if self.start_from:
                self.emit("log", f"seeking start chat: {self.start_from}")
                if self._seek_chat(self.start_from):
                    self.emit("log", f"found '{self.start_from}' - starting from here")
                else:
                    self.emit("log", f"'{self.start_from}' not found - starting from the top")

            self.emit("log", f"rolling export started - CSV: {self.csv_path}")
            stale = 0
            no_new = 0            # consecutive swipes that moved but revealed nothing new
            loops = 0
            while not self._stop.is_set():
                self._wait_pause()
                if self._stop.is_set():
                    break
                if not self._device_ready():
                    self.emit("log", "device disconnected - waiting up to 180s")
                    if not self._wait_device():
                        break
                    time.sleep(1)
                    keep_awake(self.adb, True); wake_unlock(self.adb, self.size)   # re-arm after reconnect
                    self._ensure_list()
                loops += 1
                if loops % 15 == 0:
                    keep_awake(self.adb, True)       # periodically re-assert (stay-on can lapse)
                try:
                    # if WhatsApp threw an "isn't responding" (ANR) dialog, tap WAIT and carry on
                    # instead of stalling behind it.
                    if dismiss_anr(self.adb):
                        self.emit("log", "handled a 'WhatsApp isn't responding' dialog (tapped Wait)")
                        time.sleep(1.5)
                    # a mega-chat's export can finish LATE and pop the Android share sheet after we
                    # already moved on; it's unreadable so visible_chats can't see it. Detect the
                    # chooser by its foreground window and BACK out of it before doing anything.
                    for _ in range(4):
                        if not on_share_sheet(self.adb):
                            break
                        self.adb.key("BACK"); time.sleep(1.0)
                    visible = [nm for _, nm in visible_chats(self.adb)]
                    if not visible:
                        # unreadable at the top of the loop -> often a LOCKED / screen-OFF phone.
                        # Wake it, and WAIT for the user to unlock a secure lock, before deciding.
                        if not self._recover_screen():
                            self.emit("log", "chat list unreadable / could not recover - stopping")
                            break
                        visible = [nm for _, nm in visible_chats(self.adb)]
                    for nm in visible:
                        if nm not in self.status:
                            # LOOP GUARD: if a chat we already FINISHED is read again with a slightly
                            # different name (happens under load), its fingerprint matches -> skip it
                            # as a duplicate instead of exporting it again. A "" fingerprint (an
                            # all-symbol/emoji name) is NEVER treated as a duplicate.
                            fp = _fingerprint(nm)
                            if fp and fp in self._done_fps:
                                self.status[nm] = "skipped"; self.order.append(nm)
                                self.emit("discover", nm); self.emit("row", nm, "skipped")
                                self.emit("log", f"duplicate of a finished chat - skipping: {nm!r}")
                                continue
                            # export only the selected chats when a whitelist is set; skip the rest
                            sel = self.only_names is None or nm in self.only_names
                            self.status[nm] = "pending" if sel else "skipped"
                            self.order.append(nm)
                            self.emit("discover", nm)
                            if not sel:
                                self.emit("row", nm, "skipped")
                    self._write_csv()
                    todo = [nm for nm in visible if self.status.get(nm) == "pending"]
                    if todo:
                        stale = 0
                        name = todo[0]
                        self.emit("row", name, "running")
                        st = self._export_one(name)
                        self.status[name] = st
                        fp = _fingerprint(name)
                        if fp:
                            self._done_fps.add(fp)               # finished -> never re-export a variant
                        self.emit("row", name, st)
                        if st == "ok":
                            exported += 1
                        self._write_csv()
                        continue
                    # whole page done -> scroll for more
                    before_swipe = visible
                    self._swipe_down()
                    after = [nm for _, nm in visible_chats(self.adb)]
                    if not after:                      # empty read -> wake/unlock and retry (not bottom)
                        if self._recover_screen():
                            continue
                        self.emit("log", "chat list unreadable / could not recover - stopping")
                        break
                    if any(nm not in self.status for nm in after):
                        stale = 0; no_new = 0          # found new chats -> keep going
                    elif after == before_swipe:
                        # the list did NOT move at all -> genuinely the bottom (only reliable signal)
                        stale += 1
                        if stale >= 3:
                            self.emit("log", "reached the bottom of the chat list")
                            break
                    else:
                        # list MOVED but revealed nothing new: we're re-traversing already-seen
                        # chats (e.g. the list jumped to the top after a relaunch). This is NOT the
                        # bottom - keep scrolling. Only an extremely long dry spell is a safety stop.
                        stale = 0
                        no_new += 1
                        if no_new >= 150:
                            self.emit("log", "no new chats after 150 scrolls - stopping (safety)")
                            break
                except core.AdbError as e:
                    self.emit("log", f"adb error (likely disconnect): {e}")
                    self._wait_device()
                    try:
                        self._ensure_list()
                    except core.AdbError:
                        pass
                except Exception as e:
                    # a single unexpected error on one chat must NEVER kill the whole evidence run:
                    # log it, try to get back to the list, and carry on. Bail only if they pile up.
                    unexpected += 1
                    self.emit("log", f"unexpected error (continuing, {unexpected}/30): {e!r}")
                    if unexpected >= 30:
                        self.emit("log", "too many unexpected errors - stopping (safety)")
                        break
                    try:
                        self._ensure_list()
                    except Exception:
                        pass
                    time.sleep(1.0)
        finally:
            pc_keep_awake(False)                  # release the PC sleep/display lock
            keep_awake(self.adb, False)
            try:                                  # restore the phone's original screen-off timeout
                if getattr(self, "_orig_sot", None) and self._orig_sot.isdigit():
                    self.adb.shell("settings", "put", "system", "screen_off_timeout",
                                   self._orig_sot, quiet=True)
            except Exception:
                pass
            self._write_csv()
            self.emit("finished", {"exported": exported, "total": len(self.order), "csv": self.csv_path})
