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


def visible_chats(adb):
    """List of (y, name) for chat rows currently on screen, top -> bottom."""
    rows = [(n["bounds"][1], n["text"]) for n in _nodes(adb)
            if n["id"].endswith(CONTACT_ID) and n["text"].strip()]
    return sorted(rows)


def _find_chat_node(adb, name):
    key = name.strip().lower()
    for n in _nodes(adb):
        if n["id"].endswith(CONTACT_ID) and n["text"].strip().lower() == key:
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
    """Keep the screen on while plugged in (prevents lock/sleep mid-run)."""
    try:
        adb.shell("svc", "power", "stayon", "true" if on else "false", quiet=True)
    except Exception:
        pass


def wake_unlock(adb):
    """Best-effort: wake the screen and swipe up if it went to a simple lock."""
    try:
        adb.key("WAKEUP")
        time.sleep(0.4)
        w, h = 720, 1600
        adb.swipe(w, int(h * 1.4), w, int(h * 0.4), 250)   # swipe up on keyguard
        time.sleep(0.4)
    except Exception:
        pass


# ----------------------------------------------------------------- scan
def scan_chats(adb, size, emit=None, max_scrolls=60, pause=None, stop=None):
    """Scroll from the top and return the ordered list of chat names.
    Pausable via `pause` (set = paused) and stoppable via `stop` (threading.Events)."""
    w, h = size
    def _wait_pause():
        while pause is not None and pause.is_set():
            if stop is not None and stop.is_set():
                return
            time.sleep(0.2)
    def swipe_up():                       # toward the top (big, fast)
        adb.swipe(w // 2, int(h * 0.28), w // 2, int(h * 0.82), 220); time.sleep(0.3)
    def swipe_down():
        adb.swipe(w // 2, int(h * 0.78), w // 2, int(h * 0.33), 220); time.sleep(0.32)

    keep_awake(adb, True)
    # jump to the top first
    last = None
    for _ in range(10):
        if stop and stop.is_set():
            return []
        _wait_pause()
        cur = [nm for _, nm in visible_chats(adb)]
        if cur and cur == last:
            break
        last = cur; swipe_up()

    ordered, seen = [], set()
    stable = 0
    for _ in range(max_scrolls):
        if stop and stop.is_set():
            break
        _wait_pause()
        added = 0
        for _, nm in visible_chats(adb):
            if nm not in seen:
                seen.add(nm); ordered.append(nm); added += 1
        if emit:
            emit("scan_progress", len(ordered))
        stable = stable + 1 if added == 0 else 0
        if stable >= 2:                      # bottom reached (no new names twice)
            break
        swipe_down()
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

    # --- attempt WITH media ---
    media["mode"] = "with media"
    if not _open_and_tap_export():
        return "stopped" if _stopped() else ("timeout" if left() <= 0 else "fail")
    # WAIT for the media-choice dialog to appear after "Export chat", THEN tap "Include media".
    # (Don't assume it's instant - tapping too early / bailing early was leaving the export stuck.)
    if _wait_text(adb, "Include media", timeout=15, match="contains", stop=outer_stop):
        for _ in range(3):                              # re-tap if the choice dialog lingers
            if _stopped():
                return "stopped"
            if core.find_node(_nodes(adb), "Include media", "contains") is None:
                break                                   # tapped -> dialog gone, export proceeding
            _tap_text(adb, "Include media", timeout=8, match="contains", delay=1.2, stop=outer_stop)
    # branch: "Unable to export" (too big) -> fall back; else the Quick Share sheet appears
    which = _wait_either(adb, "Unable to export", "Quick Share",
                         timeout=max(5, min(300, left())), stop=outer_stop)
    if which == "b":
        return "done"                                   # with media reached Quick Share
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


def _back_to_list(adb, tries=8):
    for _ in range(tries):
        st = on_chat_list(adb)
        if st:
            return True
        if st is None:                    # unreadable screen: retry the read, never BACK blindly
            time.sleep(1.0); continue
        adb.key("BACK"); time.sleep(1.2)
    # last resort: bring WhatsApp back to the front
    try:
        adb.launch("com.whatsapp"); time.sleep(2.0)
    except core.AdbError:
        pass
    for _ in range(4):
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
    elif cached.get("pc"):
        emit_log(f"tapping {pc_name} by learned position")
        adb.tap(*cached["pc"]); time.sleep(2.0); blind = True
    else:
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
            return "fail-notfound", None, None, ""
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
        self.adb.swipe(w // 2, int(h * 0.72), w // 2, int(h * 0.34), 400); time.sleep(0.8)

    def _swipe_up(self):
        w, h = self.size
        self.adb.swipe(w // 2, int(h * 0.34), w // 2, int(h * 0.72), 400); time.sleep(0.8)

    def _scroll_to(self, name, max_steps=45):
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
                 pc_name=PC_NAME, save_dir=SAVE_DIR):
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

    def _swipe_down(self):
        # Work from the EXACT centre of the screen (computed from the live device size), and
        # drag symmetrically about it: from (cx, cy+off) down-to-up to (cx, cy-off). Centring on
        # the true midpoint keeps the whole gesture in the middle band, far from the bottom
        # "home" gesture area (which minimises/closes WhatsApp) and the top status bar.
        w, h = self.size                       # screen size, read from the device on connect
        cx, cy = w // 2, h // 2                # exact centre point
        off = int(h * 0.12)                    # symmetric offset above/below centre (~24% span)
        self.adb.swipe(cx, cy + off, cx, cy - off, 600); time.sleep(0.6)

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
            keep_awake(self.adb, True)
            if not Path(self.save_dir).is_dir():
                self.emit("log", f"WARNING: save folder '{self.save_dir}' does not exist - "
                                 "received files won't be detected. Point Quick Share there.")
            self._ensure_list()
            self.emit("log", f"rolling export started - CSV: {self.csv_path}")
            stale = 0
            empty_reads = 0
            while not self._stop.is_set():
                self._wait_pause()
                if self._stop.is_set():
                    break
                if not self._device_ready():
                    self.emit("log", "device disconnected - waiting up to 180s")
                    if not self._wait_device():
                        break
                    time.sleep(1); self._ensure_list()
                try:
                    visible = [nm for _, nm in visible_chats(self.adb)]
                    for nm in visible:
                        if nm not in self.status:
                            self.status[nm] = "pending"; self.order.append(nm)
                            self.emit("discover", nm)
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
                    self._swipe_down()
                    after = [nm for _, nm in visible_chats(self.adb)]
                    if not after:                      # empty read = screen-reader error, NOT the bottom
                        empty_reads += 1
                        if empty_reads >= 8:
                            self.emit("log", "chat list unreadable repeatedly - stopping")
                            break
                        continue
                    empty_reads = 0
                    if not any(nm not in self.status for nm in after):
                        stale += 1
                        if stale >= 2:                 # nothing new twice = bottom
                            self.emit("log", "reached the bottom of the chat list")
                            break
                    else:
                        stale = 0
                except core.AdbError as e:
                    self.emit("log", f"adb error (likely disconnect): {e}")
                    self._wait_device()
                    try:
                        self._ensure_list()
                    except core.AdbError:
                        pass
        finally:
            keep_awake(self.adb, False)
            self._write_csv()
            self.emit("finished", {"exported": exported, "total": len(self.order), "csv": self.csv_path})
