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
import re
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
    # branch: privacy-blocked | media-too-big | Quick Share sheet
    which = _wait_any(adb, {"blocked": "Can't export", "unable": "Unable to export",
                            "qs": "Quick Share"},
                      timeout=max(5, min(300, left())), stop=outer_stop)
    if which == "qs":
        return "done"                                   # with media reached Quick Share
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
    if _wait_text(adb, "Quick Share", timeout=max(5, min(240, left())), stop=outer_stop):
        return "done"
    return "stopped" if _stopped() else ("timeout" if left() <= 0 else "fail")


def _back_to_list(adb, tries=10):
    """Get back to the chat list after an export. We are NOT on the list here (we're on a share
    sheet / dialog / inside a chat), so a BACK is safe and needed - including on an UNREADABLE
    frame, which is usually a stuck dialog, not the list. We give the first couple of unreadable
    frames a moment to settle, then press BACK regardless. This avoids the relaunch fallback
    (which resets the list scroll to the top and makes long runs re-traverse / stop early)."""
    none_streak = 0
    for _ in range(tries):
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

    # --- find & tap "Quick Share" (via screen-reader, else the learned position) ---
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
        emit_log("phone too busy to read the screen - tapping Quick Share by learned position")
        adb.tap(*cached["qs"]); time.sleep(3.0); blind = True
    else:
        _back_to_list(adb)
        return ("fail-timeout" if _left() <= 0 else "fail-noshare"), None, None, media["mode"]

    # --- find & tap the PC in the device picker (screen-reader, else learned position) ---
    cached = _COORDS.get(key, {})
    pc_node = _wait_node(adb, pc_name, max(2, min(25 if cached.get("pc") else 60, _left())), stop=outer_stop)
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

    # Wait for the file on the PC (definitive success), while ALSO reading the phone's Quick Share
    # status: if it shows "Failed" we bail immediately (fast retry) instead of waiting the whole
    # timeout; "Sent" is logged as progress. Fail fast on a blind send so a mega-chat can't waste
    # 10 min.
    emit_log(f"sent to {pc_name}; waiting for the file to arrive...")
    phone = {"next": 0.0, "sent": False, "failed": False}

    def _abort():
        if outer_stop is not None and outer_stop.is_set():
            return True
        now = time.time()
        if now < phone["next"]:
            return False
        phone["next"] = now + 4.0                       # throttle phone reads
        nodes = _nodes(adb)
        if core.find_node(nodes, "Failed", "contains"):
            phone["failed"] = True
            emit_log("phone shows Quick Share 'Failed'")
            return True
        if not phone["sent"] and core.find_node(nodes, "Sent", "contains"):
            phone["sent"] = True
            emit_log("phone shows Quick Share 'Sent' - finalising on PC")
        return False

    res = bt.wait_for_new_file(save_dir, before, timeout=90 if blind else 600, abort=_abort)
    _back_to_list(adb)
    if not res:
        # distinct reasons: the phone actively reported the send "Failed" (transfer dropped -
        # Bluetooth/Wi-Fi) vs the file simply never arrived within the timeout (PC not receiving,
        # asleep, wrong save folder). Both are retried.
        if phone["failed"]:
            return "fail-sent", None, None, media["mode"]
        return "fail-transfer", None, None, media["mode"]
    path, nbytes, digest = res
    try:
        case.record_file(Path(path), f"whatsapp export ({media['mode']}) via Quick Share")
    except Exception:
        pass
    emit_log(f"received {Path(path).name} ({nbytes} bytes, {media['mode']})")
    return "ok", path, digest, media["mode"]


def export_with_retry(adb, case, size, reopen, emit_log, outer_stop, on_runner,
                      pc_name, save_dir, ensure_list):
    """Run export_and_send, retrying the whole thing on a transient Quick Share failure
    (fail-transfer/noshare/pcpick). `reopen()` re-opens the chat from the list and returns
    True, or False if the chat row can't be found. Returns (status, path, digest, media)."""
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
        try:
            now = core.now_iso()
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                wr = csv.writer(f)
                wr.writerow(["#", "chat", "status", "media", "saved_file", "sha256", "time"])
                for i, (nm, st) in enumerate(self.results, 1):
                    self.times.setdefault(nm, now)   # stamp on first write = completion time
                    fn, digest, media = self.files.get(nm, ("", "", ""))
                    if not media and st.startswith("fail"):
                        media = "not exported"       # chat never opened -> no media choice made
                    wr.writerow([i, _csv_safe(nm), st, media, _csv_safe(fn), digest, self.times[nm]])
        except Exception as e:
            self.emit("log", f"csv write failed: {e}")

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
        while time.time() < end and not self._stop.is_set():
            if self._device_ready():
                return True
            time.sleep(2)
        return False

    def _ensure_list(self):
        for _ in range(8):
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
        finally:
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


def load_progress(csv_path):
    """Read a prior exported_chats.csv so a run can RESUME. Returns (status, order, files, times)
    where status maps chat-name -> the recorded status. Callers keep chats already 'ok' (skip
    them) and re-queue everything else."""
    status, order, files, times = {}, [], {}, {}
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
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
    except FileNotFoundError:
        pass
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
        while time.time() < end and not self._stop.is_set():
            if self._device_ready():
                return True
            time.sleep(2)
        return False

    def _ensure_list(self):
        for _ in range(8):
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

    def _seek_chat(self, target, max_scrolls=120):
        """Scroll from the top until `target` is on screen; mark every chat passed on the way as
        'skipped' (we're deliberately starting later). Returns True if found."""
        tgt = (target or "").strip().lower()
        # jump to the very top first so "start from here" is deterministic
        for _ in range(30):
            if self._stop.is_set():
                return False
            before = [nm for _, nm in visible_chats(self.adb)]
            self._swipe_up()
            after = [nm for _, nm in visible_chats(self.adb)]
            if after and after == before:
                break                          # at the top
        for _ in range(max_scrolls):
            if self._stop.is_set():
                return False
            visible = [nm for _, nm in visible_chats(self.adb)]
            if any(nm.strip().lower() == tgt for nm in visible):
                return True
            for nm in visible:                 # everything above the target is intentionally skipped
                if nm not in self.status:
                    self.status[nm] = "skipped"; self.order.append(nm)
                    self.emit("discover", nm); self.emit("row", nm, "skipped")
            self._write_csv()
            before = visible
            self._swipe_down()
            after = [nm for _, nm in visible_chats(self.adb)]
            if after and after == before:      # reached the bottom without finding it
                return False
        return False

    def _write_csv(self):
        try:
            now = core.now_iso()
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                wr = csv.writer(f)
                wr.writerow(["#", "chat", "status", "media", "saved_file", "sha256", "time"])
                for i, nm in enumerate(self.order, 1):
                    st = self.status.get(nm, "")
                    if st and st not in ("pending", "running"):   # terminal -> stamp once
                        self.times.setdefault(nm, now)
                    fn, digest, media = self.files.get(nm, ("", "", ""))
                    if not media and st.startswith("fail"):
                        media = "not exported"       # chat never opened -> no media choice made
                    wr.writerow([i, _csv_safe(nm), st, media,
                                 _csv_safe(fn), digest, self.times.get(nm, "")])
        except Exception as e:
            self.emit("log", f"csv write failed: {e}")

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
        return status

    def run(self):
        exported = 0
        try:
            if not self._device_ready() and not self._wait_device():
                self.emit("finished", {"exported": 0, "total": 0, "csv": self.csv_path}); return
            try:                                  # remember the phone's own screen-off timeout to restore later
                self._orig_sot = self.adb.shell("settings", "get", "system",
                                                "screen_off_timeout", quiet=True).strip()
            except Exception:
                self._orig_sot = None
            keep_awake(self.adb, True)
            if not Path(self.save_dir).is_dir():
                self.emit("log", f"WARNING: save folder '{self.save_dir}' does not exist - "
                                 "received files won't be detected. Point Quick Share there.")
            self._ensure_list()

            # RESUME: pre-load a prior run's CSV. Chats already 'ok' are kept (skipped); every
            # other chat is re-queued as pending so it gets retried.
            if self.resume_from:
                st, od, fl, tm = load_progress(self.resume_from)
                for nm in od:
                    if nm not in self.order:
                        self.order.append(nm)
                    self.status[nm] = "ok" if st.get(nm) == "ok" else "pending"
                    self.files[nm] = fl.get(nm, ("", "", ""))
                    self.times[nm] = tm.get(nm, "")
                    self.emit("discover", nm)
                    self.emit("row", nm, self.status[nm])
                done = sum(1 for nm in od if self.status.get(nm) == "ok")
                self.emit("log", f"resume: {len(od)} chats loaded, {done} already ok (skipping "
                                 f"those, retrying the rest)")
                self._write_csv()

            # START-FROM: scroll to a named chat and begin there (mark everything above as skipped).
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
        finally:
            keep_awake(self.adb, False)
            try:                                  # restore the phone's original screen-off timeout
                if getattr(self, "_orig_sot", None) and self._orig_sot.isdigit():
                    self.adb.shell("settings", "put", "system", "screen_off_timeout",
                                   self._orig_sot, quiet=True)
            except Exception:
                pass
            self._write_csv()
            self.emit("finished", {"exported": exported, "total": len(self.order), "csv": self.csv_path})
