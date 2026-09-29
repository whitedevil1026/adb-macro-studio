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
EXPORT_STEPS = [
    {"type": "tap_text", "text": "More options", "match": "contains", "timeout": 10, "delay": 0.6},
    {"type": "tap_text", "text": "More", "match": "exact", "timeout": 8, "delay": 0.6},
    {"type": "tap_text", "text": "Export chat", "match": "contains", "timeout": 8, "delay": 0.7},
    {"type": "tap_text", "text": "Include media", "match": "contains", "timeout": 8, "delay": 0.5},
    {"type": "if_text", "text": "Unable to export", "match": "contains", "timeout": 300,
     "or_text": "Quick Share", "delay": 0.3,
     "then": [
         {"type": "tap_text", "text": "OK", "match": "exact", "timeout": 8, "delay": 0.6},
         {"type": "tap_text", "text": "More options", "match": "contains", "timeout": 8, "delay": 0.6},
         {"type": "tap_text", "text": "More", "match": "exact", "timeout": 8, "delay": 0.6},
         {"type": "tap_text", "text": "Export chat", "match": "contains", "timeout": 8, "delay": 0.7},
         {"type": "tap_text", "text": "Without media", "match": "contains", "timeout": 8, "delay": 0.5},
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
    return core.find_node(_nodes(adb), LIST_MARKER, "contains") is not None


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


def _back_to_list(adb, tries=6):
    for _ in range(tries):
        if on_chat_list(adb):
            return True
        adb.key("BACK"); time.sleep(1.2)
    # last resort: bring WhatsApp back to the front
    try:
        adb.launch("com.whatsapp"); time.sleep(2.0)
    except core.AdbError:
        pass
    for _ in range(4):
        if on_chat_list(adb):
            return True
        adb.key("BACK"); time.sleep(1.2)
    return on_chat_list(adb)


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
    media = {"mode": ""}
    def _log(m):
        if "branch -> THEN" in m:
            media["mode"] = "without media"     # media failed -> fell back
        elif "branch -> ELSE" in m:
            media["mode"] = "with media"        # Include media succeeded
        emit_log(m)
    reason = _run_steps(adb, case, size, copy.deepcopy(EXPORT_STEPS), _log,
                        outer_stop=outer_stop, on_runner=on_runner,
                        limit=max(5, min(420, _left())))
    if outer_stop is not None and outer_stop.is_set():
        return "stopped", None, None, media["mode"]
    if _left() <= 0:
        emit_log("took too long to export - skipping (fail-timeout)")
        _back_to_list(adb); return "fail-timeout", None, None, media["mode"]
    if not reason.startswith("done"):
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

    # fail fast on a blind (unreliable) send so a mega-chat can't waste 10 min
    emit_log(f"sent to {pc_name}; waiting for the file to arrive...")
    res = bt.wait_for_new_file(save_dir, before, timeout=90 if blind else 600)
    _back_to_list(adb)
    if not res:
        return "fail-transfer", None, None, media["mode"]
    path, nbytes, digest = res
    try:
        case.record_file(Path(path), f"whatsapp export ({media['mode']}) via Quick Share")
    except Exception:
        pass
    emit_log(f"received {Path(path).name} ({nbytes} bytes, {media['mode']})")
    return "ok", path, digest, media["mode"]


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
        for _ in range(6):
            if on_chat_list(self.adb):
                return True
            self.adb.key("BACK"); time.sleep(1.2)
        return on_chat_list(self.adb)

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
                    node = self._scroll_to(name)
                    if not node:
                        self.emit("progress", i, total, name, "fail-notfound")
                        self.results.append((name, "fail-notfound")); continue
                    self.adb.tap(*core.node_center(node)); time.sleep(1.4)
                    status, path, digest, media = export_and_send(
                        self.adb, self.case, self.size,
                        emit_log=lambda m: self.emit("log", f"   {m}"),
                        outer_stop=self._stop, on_runner=self._set_runner,
                        pc_name=self.pc_name, save_dir=self.save_dir,
                        deadline=time.time() + MAX_SEND_SECONDS)
                    self.files[name] = (Path(path).name if path else "", digest or "",
                                        media if status == "ok" else "")
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
        for _ in range(6):
            if on_chat_list(self.adb):
                return True
            self.adb.key("BACK"); time.sleep(1.2)
        return on_chat_list(self.adb)

    def _swipe_down(self):
        w, h = self.size
        self.adb.swipe(w // 2, int(h * 0.78), w // 2, int(h * 0.33), 220); time.sleep(0.4)

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
                    wr.writerow([i, _csv_safe(nm), st, media,
                                 _csv_safe(fn), digest, self.times.get(nm, "")])
        except Exception as e:
            self.emit("log", f"csv write failed: {e}")

    def _export_one(self, name):
        node = _find_chat_node(self.adb, name)
        if not node:
            return "fail-notfound"
        self.adb.tap(*core.node_center(node)); time.sleep(1.4)
        status, path, digest, media = export_and_send(
            self.adb, self.case, self.size,
            emit_log=lambda m: self.emit("log", f"   {m}"),
            outer_stop=self._stop, on_runner=self._set_runner,
            pc_name=self.pc_name, save_dir=self.save_dir,
            deadline=time.time() + MAX_SEND_SECONDS)
        self.files[name] = (Path(path).name if path else "", digest or "",
                            media if status == "ok" else "")
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
