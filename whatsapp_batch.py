"""
WhatsApp batch export: scan the chat list, then export selected chats one by one
by READING the screen (no fixed coordinates). Includes a robust scroll module that
uses the scanned chat order to scroll the right direction and recover from overshoot.

Drives adb_core primitives. The GUI (adb_gui.py) runs these in a thread and shows
live progress; nothing here touches Tkinter.
"""
from __future__ import annotations

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

accept_watcher = None     # a run's single qs_accept.AcceptWatcher (set while RollingBatch runs)

# --- which WhatsApp ------------------------------------------------------------------------------
WA_PACKAGES = {"com.whatsapp": "WhatsApp", "com.whatsapp.w4b": "WhatsApp Business"}


def installed_whatsapp(adb):
    """The WhatsApp apps installed on the phone (regular first), e.g. ['com.whatsapp.w4b']."""
    try:
        have = set(adb.packages("com.whatsapp"))
    except Exception:
        return []
    return [p for p in WA_PACKAGES if p in have]


def wa_package(adb):
    """The WhatsApp app this run drives (set by the batch; regular WhatsApp by default)."""
    return getattr(adb, "wa_package", None) or "com.whatsapp"


def csv_name(package=None):
    """Each app keeps its own progress CSV, so resuming one never skips the other's chats."""
    return "exported_chats_business.csv" if package == "com.whatsapp.w4b" else "exported_chats.csv"


# --- on-screen words that have no language-free id -----------------------------------------------
# Every other step finds its button by resource-id (identical in every language, recorded from a real
# phone). "Export chat" is the exception: it sits next to "Clear chat" in the same menu with the same
# id and a position that differs per chat type, so it is matched by its text - English built in, any
# other language learned once from the examiner (label_asker) and remembered per phone language.
# WhatsApp's own translations can't be read off the phone (they ship in a private compressed format).
DEFAULT_LABELS = {"export_chat": ("Export chat",)}
LABEL_FILE = str(Path(__file__).resolve().parent / ".ui_labels.json")
label_asker = None        # GUI hook: fn(key, options) -> the option meaning `key`, or None


def phone_locale(adb):
    loc = getattr(adb, "_wb_locale", None)
    if loc is None:
        loc = ""
        for prop in ("persist.sys.locale", "ro.product.locale"):
            try:
                loc = (adb.getprop(prop) or "").strip()
            except Exception:
                loc = ""
            if loc:
                break
        try:
            adb._wb_locale = loc
        except Exception:
            pass
    return loc or "unknown"


def _learned_labels():
    try:
        with open(LABEL_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def labels(adb, key):
    """Texts that mean `key` on this phone: built-in English + what was learned for its language."""
    learned = _learned_labels().get(key, {}).get(phone_locale(adb), [])
    return list(DEFAULT_LABELS.get(key, ())) + [t for t in learned if isinstance(t, str) and t]


def learn_label(adb, key, text):
    data = _learned_labels()
    have = data.setdefault(key, {}).setdefault(phone_locale(adb), [])
    if text not in have:
        have.append(text)
    tmp = LABEL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LABEL_FILE)


def _find_label(nodes, adb, key):
    for t in labels(adb, key):
        hit = core.find_node(nodes, t, "exact") or core.find_node(nodes, t, "contains")
        if hit:
            return hit
    return None


def _coord_key(adb, size):
    """Learned tap positions belong to one phone MODEL and screen size: another brand with the same
    resolution has a different share sheet, so a blind tap there could hit the wrong target."""
    model = getattr(adb, "_wb_model", None)
    if model is None:
        try:
            model = (adb.getprop("ro.product.model") or "").strip()
        except Exception:
            model = ""
        try:
            adb._wb_model = model
        except Exception:
            pass
    return f"{model or 'unknown'}|{size[0]}x{size[1]}"


def _learned_coords(adb, size):
    """This phone's learned positions. Positions saved before they were kept per model (key = screen
    size only) move to the first model that uses them, so a second model never inherits them."""
    key, legacy = _coord_key(adb, size), f"{size[0]}x{size[1]}"
    if key not in _COORDS and legacy in _COORDS:
        _COORDS[key] = _COORDS.pop(legacy)
        _save_coords()
    return _COORDS.get(key, {})


CONTACT_ID = "conversations_row_contact_name"     # WhatsApp chat-name node
LIST_MARKER = "Ask Meta AI or Search"             # present on the chat list (English)
LIST_IDS = ("my_search_bar", "conversations_row_contact_name")   # the chat list, in any language
CHAT_ID = "conversation_root_layout"              # an open chat, in any language
OVERFLOW_ID = "menuitem_overflow"                 # the 3-dots button (list AND chat toolbar)
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
_NODE_CACHE = {"adb": None, "gen": None, "t": 0.0, "nodes": None}
_NODE_TTL = 0.35          # reuse a dump only within this window AND only if no input happened since


def _nodes(adb):
    """Read the on-screen UI nodes (one `uiautomator dump`). A tiny cache collapses REDUNDANT
    back-to-back reads: it returns the last dump only when (a) no input event happened since (the
    adb input_gen is unchanged) AND (b) it is < _NODE_TTL old. Poll-and-wait loops sleep >= 0.6s
    between reads, so they always exceed the TTL and get a FRESH dump - the cache never hides a
    screen change they are waiting for; it only saves the duplicate dumps that used to happen within
    a single step. uiautomator dump is the tool's #1 cost (~1-5s each), so this is a safe speedup."""
    try:
        gen = getattr(adb, "input_gen", None)
        now = time.time()
        c = _NODE_CACHE
        if (adb is not None and c["adb"] is adb and c["gen"] == gen
                and c["nodes"] is not None and (now - c["t"]) < _NODE_TTL):
            return c["nodes"]
        nodes = core.parse_ui_nodes(adb.ui_dump())
        c.update(adb=adb, gen=gen, t=now, nodes=nodes)
        return nodes
    except Exception:
        return []


# direction / formatting marks WhatsApp sprinkles into names; strip so the SAME chat always reads
# as the SAME name (otherwise a misread with a stray mark looks like a new/phantom chat).
_NAME_MARKS = "‎‏‪‫‬⁦⁧⁨⁩﻿"


_NAME_MARK_RE = re.compile("[​‎‏‪-‮⁦-⁩﻿]")
_WS_RE = re.compile(r"\s+", re.UNICODE)


def _norm_name(s):
    """Stable identity for a chat name across screen reads. Removes invisible direction/format marks
    ANYWHERE in the name (not just at the ends), turns every kind of whitespace (incl. NBSP) into a
    single space, and Unicode-normalizes (NFC). ZWJ/ZWNJ (U+200D/U+200C) are KEPT - Indic scripts and
    emoji need them. Without this the same chat could read as two different names between dumps."""
    import unicodedata
    s = unicodedata.normalize("NFC", s or "")
    s = _NAME_MARK_RE.sub("", s)
    return _WS_RE.sub(" ", s).strip()


def duplicate_names(names):
    """Names that appear more than once in `names` (F-10: same-display-name chats are tracked as one
    because status is keyed by name; surfacing co-visible duplicates lets the operator disambiguate)."""
    return {n for n in names if names.count(n) > 1}


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


def _row_name(n):
    """A chat row's name: its text, or its content-desc when the text is empty (some WhatsApp
    builds/rows expose the name only there - those chats used to be invisible to the tool)."""
    return _norm_name(n.get("text") or n.get("desc") or "")


def _chat_rows(nodes):
    """Chat-name nodes on screen as dicts {name, node, full}, top -> bottom. `full` is False for a
    row CLIPPED at the top/bottom edge (uiautomator reports clipped, visible bounds): tapping the
    centre of a half-hidden row can land on the bottom navigation bar instead of the chat."""
    rows = [n for n in nodes if n["id"].endswith(CONTACT_ID) and _row_name(n)]
    if not rows:
        return []
    hmax = max(n["bounds"][3] - n["bounds"][1] for n in rows)
    out = [{"name": _row_name(n), "node": n,
            "full": (n["bounds"][3] - n["bounds"][1]) >= 0.75 * hmax} for n in rows]
    return sorted(out, key=lambda r: r["node"]["bounds"][1])


def visible_chats(adb):
    """List of (y, name) for chat rows currently on screen, top -> bottom. Names are normalised
    (whitespace + invisible direction marks stripped) for stable de-duplication."""
    return [(r["node"]["bounds"][1], r["name"]) for r in _chat_rows(_nodes(adb))]


def fully_visible_chats(adb):
    """Names of the rows that are NOT clipped at a screen edge - the only ones safe to tap."""
    return {r["name"] for r in _chat_rows(_nodes(adb)) if r["full"]}


def _chat_node_in(nodes, name):
    """The node for chat `name` among `nodes`, preferring a fully visible (un-clipped) row."""
    key = _norm_name(name).lower()
    hits = [r for r in _chat_rows(nodes) if r["name"].lower() == key]
    if not hits:
        return None
    hits.sort(key=lambda r: not r["full"])          # full rows first
    return hits[0]["node"]


def _find_chat_node(adb, name):
    """The on-screen node for `name`, preferring a fully visible (un-clipped) row."""
    return _chat_node_in(_nodes(adb), name)


def on_chat_list(adb):
    """True  = on the chat list,
    False = a readable screen that is NOT the list (safe to press BACK),
    None  = the screen could not be read - DO NOT press BACK (retry the read instead).
    Treating an unreadable dump as 'not on the list' was pressing BACK while actually on
    the list, which minimises/closes WhatsApp."""
    nodes = _nodes(adb)
    if not nodes:
        return None
    if any(n["id"].endswith("/" + CHAT_ID) for n in nodes):
        return False
    if core.find_node(nodes, LIST_MARKER, "contains") is not None:
        return True
    if any(n["id"].endswith("/" + LIST_IDS[0]) for n in nodes) and _chat_rows(nodes):
        return True
    # robust fallback: several chat-name rows on screen means we're on the list, even if the
    # search-hint text differs by WhatsApp version/locale (an open chat shows no such rows).
    return len(_chat_rows(nodes)) >= 2


# ----------------------------------------------------------------- list navigation (any screen)
# ONE scroller for every feature (scan, rolling export, export-selected, seek). It measures the
# list on screen - its box and the row pitch - instead of using fixed fractions of the screen, so it
# is the same on any screen size, density, font size or maker. Every page move is VERIFIED: the
# last chat seen must still be on screen afterwards (a fling that overshoots is walked back), so no
# chat can be skipped; and an end of the list is only accepted after swiping the other way and back
# proves swipes are working (an ignored swipe looks exactly like the end of the list).
ARCHIVED_ID = "archived_row"
ALL_FILTER_ID = "conversations_filter_debug_view_id_all"


def _list_geometry(nodes):
    """(rows top->bottom, list top, list bottom, row pitch, list centre x), or None when no chat rows
    are readable."""
    rows = _chat_rows(nodes)
    if not rows:
        return None
    tops = [r["node"]["bounds"][1] for r in rows]
    lists = [n for n in nodes if n["id"].endswith("/list") and core._area(n) > 0]
    if lists:
        b = max(lists, key=core._area)["bounds"]
        top, bottom, cx = b[1], b[3], (b[0] + b[2]) // 2
    else:
        top, bottom = tops[0], rows[-1]["node"]["bounds"][3]
        cx = core.node_center(rows[0]["node"])[0]
    gaps = sorted(b - a for a, b in zip(tops, tops[1:]) if b - a > 0)
    hmax = max(r["node"]["bounds"][3] - r["node"]["bounds"][1] for r in rows)
    pitch = gaps[len(gaps) // 2] if gaps else hmax * 3
    return rows, top, bottom, max(pitch, 20), cx


def _drag(adb, top, bottom, pitch, cx, dist, direction, fast=False):
    """Drag the list by `dist` px inside its own box (never onto the toolbar / nav bar).
    direction 'down' reveals later chats. Slow enough (~2 rows/s) not to fling unless `fast`."""
    margin = max(8, pitch // 3)
    room = (bottom - margin) - (top + margin)
    dist = max(1, min(int(dist), room))
    ms = 250 if fast else max(300, int(dist / (2.0 * pitch) * 1000))
    if direction == "down":
        y1 = bottom - margin
        adb.swipe(cx, y1, cx, y1 - dist, ms)
    else:
        y1 = top + margin
        adb.swipe(cx, y1, cx, y1 + dist, ms)
    time.sleep(1.0 if fast else 0.6)
    try:
        adb._wb_moved = True             # the next tap must wait for the list to settle
    except Exception:
        pass


def scroll_list(adb, direction="down", rows=None, fast=False):
    """Move the chat list one page (or `rows` rows) and verify nothing was skipped.
    Returns ('moved' | 'still' | 'unreadable', names now on screen)."""
    geo = _list_geometry(_nodes(adb))
    if geo is None:
        return "unreadable", []
    rows_now, top, bottom, pitch, cx = geo
    before = [r["name"] for r in rows_now]
    full = {r["name"] for r in rows_now if r["full"]}
    if direction == "down":
        anchor = next((n for n in reversed(before) if n in full), before[-1])
    else:
        anchor = next((n for n in before if n in full), before[0])
    dist = (rows * pitch) if rows else (bottom - top) - 1.5 * pitch     # a page, keeping overlap
    _drag(adb, top, bottom, pitch, cx, max(dist, pitch * 0.8), direction, fast=fast)
    after = [nm for _, nm in visible_chats(adb)]
    if not after:
        return "unreadable", []
    # moved = a chat we had not seen appeared. Comparing whole views is wrong on Android 12+: at an
    # end the list STRETCHES (more for a fast swipe), so edge rows come and go without any scrolling
    # - that made the end undetectable and the scroll never stop.
    if not any(n not in before for n in after):
        return "still", after
    if fast:
        return "moved", after
    back = "up" if direction == "down" else "down"
    for _ in range(12):                      # overshoot: walk back until the anchor is on screen
        if anchor in after or any(n in before for n in after):
            break
        _drag(adb, top, bottom, pitch, cx, pitch, back)
        after = [nm for _, nm in visible_chats(adb)]
    return "moved", after


def _settled_node(adb, finder, tries=5):
    """The node `finder(nodes)` picks, read from a screen that has stopped moving. Right after a
    scroll Android 12+ still shows the list stretched / settling, so positions from that read are
    wrong and a tap there can open a DIFFERENT chat. After a scroll we re-read until the node is in
    the same place twice."""
    if not getattr(adb, "_wb_moved", False):
        return finder(_nodes(adb))
    prev = None
    for i in range(tries):
        if i:
            time.sleep(0.4)                   # > the screen-read cache TTL: a fresh read
        n = finder(_nodes(adb))
        if n is not None and prev is not None and n["bounds"] == prev["bounds"]:
            break
        prev = n
    else:
        n = prev
    try:
        adb._wb_moved = False
    except Exception:
        pass
    return n


def opened_chat_matches(nodes, name):
    """Is the open chat the one we meant? True / False, or None when no title is shown.
    A wrong chat must never be exported under another chat's name."""
    title = core.find_node(nodes, "conversation_contact_name", "exact")
    if title is None or not title["text"]:
        return None
    got, want = _norm_name(title["text"]).lower(), _norm_name(name).lower()
    if got.endswith(("…", "...")):                 # a long name shortened in the header
        return want.startswith(got.rstrip(".…").rstrip())
    return got == want


def scroll_to_end(adb, direction="up", stop=None, max_swipes=600):
    """Scroll to the very top ('up') or bottom ('down') of the list, and VERIFY it: two unchanged
    reads may just be ignored swipes, so swipe the other way and back - only if that returns to the
    same view is it really the end. Returns True when the end is confirmed."""
    other = "down" if direction == "up" else "up"
    stills = 0
    for _ in range(max_swipes):
        if stop is not None and stop.is_set():
            return False
        res, view = scroll_list(adb, direction, fast=(direction == "up"))
        if res == "unreadable":
            time.sleep(1.0)
            continue
        if res == "moved":
            stills = 0
            continue
        stills += 1
        if stills < 2:
            continue
        if _end_confirmed(adb, direction, other, view, stop):
            return True
        stills = 0
    return False


def _end_confirmed(adb, direction, other, view, stop):
    for _ in range(3):                       # the probe swipe itself may be ignored: retry it
        if stop is not None and stop.is_set():
            return False
        res, _probe = scroll_list(adb, other, rows=1.5)
        if res == "moved":
            break
        time.sleep(1.5)
    else:
        return True                          # nothing moves either way: the list fits one screen
    edge = 0 if direction == "up" else -1    # the first chat at the top / the last at the bottom
    for _ in range(6):                       # come back; the same end again = the real end
        res, back = scroll_list(adb, direction, rows=2)
        if res == "still":
            return bool(back) and bool(view) and back[edge] == view[edge]
    return False


def show_all_chats(adb):
    """Tap the 'All' filter tab (by id, any language) so a left-on Unread/Groups/... tab can't hide
    chats. Harmless when All is already selected. Returns True if the tab was there."""
    chip = core.find_node(_nodes(adb), ALL_FILTER_ID, "exact")
    if chip:
        adb.tap(*core.node_center(chip)); time.sleep(0.8)
    return chip is not None


def in_archived(adb):
    """True when the Archived chats list is showing (its own activity / no main search bar)."""
    line = foreground_focus(adb)
    if line:
        return "archived" in line
    nodes = _nodes(adb)
    return bool(_chat_rows(nodes)) and core.find_node(nodes, LIST_IDS[0], "exact") is None


def open_archived(adb, stop=None):
    """From the main chat list: go to its top and open 'Archived'. False if there is none."""
    if in_archived(adb):
        return True
    scroll_to_end(adb, "up", stop=stop)
    row = _settled_node(adb, lambda ns: core.find_node(ns, ARCHIVED_ID, "exact"))
    if row is None:
        return False
    adb.tap(*core.node_center(row)); time.sleep(1.5)
    return in_archived(adb)


def leave_archived(adb):
    for _ in range(3):
        if not in_archived(adb):
            return True
        adb.key("BACK"); time.sleep(1.2)
    return not in_archived(adb)


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


def foreground_focus(adb):
    """The current foreground-window line (mCurrentFocus) in lowercase, or '' if unreadable."""
    try:
        out = adb.shell("dumpsys", "window", quiet=True)
    except Exception:
        return ""
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            return line.lower()
    return ""


def on_whatsapp_foreground(adb):
    """True = WhatsApp is the foreground app, False = a DIFFERENT app is (e.g. the Quick Share
    completion screen, which lives in its own app and is NOT the Android chooser), None = unreadable.
    Lets us BACK out of ANY post-send Quick Share screen - not only the share chooser - so the run
    reliably returns to the chat list instead of falling back to a scroll-resetting relaunch."""
    line = foreground_focus(adb)
    if not line:
        return None
    if f" {wa_package(adb)}/" in line:
        return True
    if any(f" {p}/" in line for p in WA_PACKAGES):
        return False           # the OTHER WhatsApp app is in front (e.g. regular during a Business run)
    # a known non-WhatsApp surface we must leave (Quick Share / Nearby / Samsung share / chooser)
    for marker in ("quickshare", "quick_share", "nearby", "sharesheet", "sharelive", "intentresolver",
                   "chooseractivity", "resolveractivity", "com.google.android.gms",
                   "com.samsung.android.app.sharelive", "mtp", "directshare"):
        if marker in line:
            return False
    return None            # some other/unknown readable window - don't force BACK blindly


def dismiss_anr(adb):
    """Detect Android's "WhatsApp isn't responding" (ANR) dialog and tap WAIT to keep the app
    alive, instead of letting the run stall behind it (or the app get killed). WhatsApp ANRs when
    uiautomator reads hammer a busy/media-heavy phone. Returns True if an ANR dialog was handled.
    Best-effort and cheap - safe to call at the top of the main loop."""
    try:
        nodes = _nodes(adb)
    except Exception:
        return False
    wait = core.find_node(nodes, "android:id/aerr_wait", "exact")   # Android's own ids: any language
    if wait:
        adb.tap(*core.node_center(wait)); time.sleep(1.0)
        return True
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
    states = ", ".join(f"{core.device_tag(d.get('serial'))}={d.get('state','?')}" for d in devs)
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
    """Every chat name, in order: the main chat list from its very top, then the Archived chats.
    Uses the shared verified scroller (no chat skipped on any screen) and the All filter tab.
    Pausable via `pause` (set = paused) and stoppable via `stop` (threading.Events)."""
    def _wait_pause():
        while pause is not None and pause.is_set():
            if stop is not None and stop.is_set():
                return
            time.sleep(0.2)

    keep_awake(adb, True)
    ordered, seen = [], set()

    def collect(names):
        for nm in names:
            if nm not in seen:
                seen.add(nm); ordered.append(nm)
        if emit:
            emit("scan_progress", len(ordered))

    leave_archived(adb)
    for section in ("chats", "archived"):
        if stop is not None and stop.is_set():
            break
        if section == "archived":
            if not open_archived(adb, stop=stop):
                break
        scroll_to_end(adb, "up", stop=stop)
        if section == "chats":
            show_all_chats(adb)
        collect([nm for _, nm in visible_chats(adb)])
        stills = 0
        for _ in range(max_scrolls):
            if stop is not None and stop.is_set():
                break
            _wait_pause()
            res, view = scroll_list(adb, "down")
            collect(view)
            if res == "moved":
                stills = 0
                continue
            if res == "unreadable":
                time.sleep(1.0)
                continue
            stills += 1
            if stills >= 2:
                if _end_confirmed(adb, "down", "up", view, stop):
                    break
                stills = 0
    leave_archived(adb)
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


SHARE_TARGETS = ("Quick Share", "Nearby Share")   # Google renamed Nearby Share in 2024; older
#                                                  phones and older Play services still show it


def _share_target(nodes):
    for label in SHARE_TARGETS:
        n = core.find_node(nodes, label, "contains")
        if n:
            return n
    return None


def _wait_share_target(adb, timeout=30, stop=None):
    end = time.time() + timeout
    while time.time() < end:
        if stop is not None and stop.is_set():
            return None
        n = _share_target(_nodes(adb))
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
        if core.find_node(nodes, "Unable to export", "contains") or _notice_dialog(nodes):
            return "unable"                # (a one-button notice after the media choice, any language)
        if _share_target(nodes):
            return "qs"
        if on_share_sheet(adb):            # chooser up but unreadable -> export reached the share sheet
            return "qs"
        time.sleep(0.7)
    return None


def _by_id(nodes, rid):
    return core.find_node(nodes, rid, "exact")


def _media_dialog(nodes):
    """The export's with/without-media question: (include, without) buttons, or None. It is a
    standard Android dialog - 'Include media' is button1 and 'Without media' button3 (recorded on a
    real phone), so it is found in any language."""
    inc, without = _by_id(nodes, "android:id/button1"), _by_id(nodes, "android:id/button3")
    if inc and without and _by_id(nodes, "android:id/message"):
        return inc, without
    return None


def _notice_dialog(nodes):
    """A one-button WhatsApp notice (message + OK only), e.g. 'Unable to export' / 'Can't export':
    returns its OK button, or None."""
    ok = _by_id(nodes, "android:id/button1")
    if (ok and _by_id(nodes, "android:id/message")
            and not _by_id(nodes, "android:id/button2") and not _by_id(nodes, "android:id/button3")):
        return ok
    return None


def _more_item(nodes):
    """The overflow menu's 'More' entry: the only item with a submenu arrow (any language)."""
    arrow = core.find_node(nodes, "submenuarrow", "exact")
    if arrow:
        cy = core.node_center(arrow)[1]
        rows = [n for n in nodes if n["clickable"] and n["bounds"][1] <= cy < n["bounds"][3]]
        if rows:
            return min(rows, key=core._area)
    return core.find_node(nodes, "More", "exact")


def _submenu_options(nodes):
    """Item texts of an open submenu, top to bottom - what the examiner picks from."""
    if core.find_node(nodes, "submenuarrow", "exact"):
        return []                                       # still the first menu, not the submenu
    items = sorted((n for n in nodes if n["id"].endswith("/title") and n["text"]),
                   key=lambda n: n["bounds"][1])
    return list(dict.fromkeys(n["text"] for n in items))


def _ask_export_label(adb, nodes):
    """'Export chat' isn't known in this phone's language: show the examiner the submenu's items and
    learn the one they pick. Without an answer nothing is tapped - a guess could hit 'Clear chat'."""
    options = _submenu_options(nodes)
    if not options or label_asker is None:
        return False
    choice = label_asker("export_chat", options)
    if choice not in options:
        return False
    learn_label(adb, "export_chat", choice)
    return True


def _open_export_menu(adb, stop=None, left=lambda: 1e9):
    """Reliably navigate the chat overflow menu to reveal "Export chat".

    The single tap on the 3-dots is frequently SWALLOWED by WhatsApp, so a blind one-shot tap often
    leaves the menu closed and "More"/"Export chat" is never found. Here we tap the 3-dots and
    CONFIRM the menu actually opened (its "More" item is visible), re-tapping if it didn't, then tap
    "More" to reveal "Export chat". Buttons are found by resource-id, so this works in any language.
    Returns True when "Export chat" is on screen."""
    def _stopped():
        return stop is not None and stop.is_set()
    for _ in range(4):                                  # whole-menu attempts
        if _stopped() or left() <= 0:
            return False
        nodes = _nodes(adb)
        if _find_label(nodes, adb, "export_chat"):
            return True
        # 1) open the overflow menu, verifying it actually opened
        opened = False
        for _ in range(3):                             # re-tap the 3-dots until the menu shows
            if _stopped() or left() <= 0:
                return False
            dots = (_wait_node(adb, OVERFLOW_ID, timeout=5, match="exact", stop=stop)
                    or _wait_node(adb, CHAT_MARKER, timeout=1, match="contains", stop=stop))
            if dots is None:
                break
            adb.tap(*core.node_center(dots)); time.sleep(1.4)
            nodes = _nodes(adb)
            if _find_label(nodes, adb, "export_chat"):
                return True                            # some layouts show Export chat directly
            if _more_item(nodes):
                opened = True; break                   # menu is up (has the "More" item)
        if not opened:
            continue
        # 2) tap "More" -> reveals Export chat
        adb.tap(*core.node_center(_more_item(nodes))); time.sleep(1.0)
        end = time.time() + 6
        while time.time() < end and not _stopped():
            nodes = _nodes(adb)
            if _find_label(nodes, adb, "export_chat"):
                return True
            time.sleep(1.0)
        return _ask_export_label(adb, nodes)           # submenu up, label unknown in this language
    return False


def _do_export(adb, media, emit_log, outer_stop, left):
    """Export the open chat, WITH media first, falling back to WITHOUT media only if WhatsApp
    rejects it ("Unable to export"). Sets media['mode']. Returns 'done' / 'fail' / 'stopped' /
    'timeout' / 'blocked'. On 'done' the Quick Share sheet is showing."""
    def _stopped():
        return outer_stop is not None and outer_stop.is_set()

    def _open_and_tap_export():
        if not _open_export_menu(adb, stop=outer_stop, left=left):
            return False
        end = time.time() + 12
        while time.time() < end and not _stopped():
            hit = _find_label(_nodes(adb), adb, "export_chat")
            if hit:
                adb.tap(*core.node_center(hit)); time.sleep(1.2)
                return True
            time.sleep(1.0)
        return False

    def _is_privacy_blocked(n):
        # newer WhatsApp: "Advanced chat privacy" blocks export -> a "Can't export chats" dialog
        return (core.find_node(n, "Can't export", "contains") is not None
                or core.find_node(n, "Advanced chat privacy", "contains") is not None)

    def _tap_ok():
        ok = _notice_dialog(_nodes(adb))
        if ok:
            adb.tap(*core.node_center(ok)); time.sleep(0.8)
        else:
            _tap_text(adb, "OK", timeout=6, match="exact", delay=0.6, stop=outer_stop)

    def _skip_blocked():
        emit_log("Advanced Chat Privacy is ON for this chat - cannot export; skipping")
        _tap_ok()
        return "blocked"

    def _media_choice(timeout=15):
        """After 'Export chat': 'media' (the with/without question is up), 'blocked', or None."""
        end = time.time() + timeout
        while time.time() < end and not _stopped():
            n = _nodes(adb)
            if _is_privacy_blocked(n):
                return "blocked"
            if _media_dialog(n):
                return "media"
            if _notice_dialog(n):
                return "blocked"            # a one-button notice instead of the media question
            time.sleep(0.8)
        return None

    def _choose(which):
        """Tap Include (0) / Without (1) media, re-tapping while the dialog lingers."""
        for _ in range(3):
            if _stopped():
                return False
            d = _media_dialog(_nodes(adb))
            if d is None:
                break                                   # tapped -> dialog gone, export proceeding
            adb.tap(*core.node_center(d[which])); time.sleep(1.2)
        return not _stopped()

    # --- attempt WITH media ---
    media["mode"] = "with media"
    if not _open_and_tap_export():
        return "stopped" if _stopped() else ("timeout" if left() <= 0 else "fail")
    # Some chats have "Advanced chat privacy" ON, which BLOCKS export: a "Can't export chats"
    # dialog shows instead of the media choice. Detect it right away so we don't hang for minutes
    # waiting for a share sheet that will never appear.
    first = _media_choice()
    if first == "blocked":
        return _skip_blocked()
    if first == "media" and not _choose(0):
        return "stopped"
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
    _tap_ok()
    if not _open_and_tap_export():
        return "stopped" if _stopped() else ("timeout" if left() <= 0 else "fail")
    if _media_choice() == "media" and not _choose(1):
        return "stopped"
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
        # If a NON-WhatsApp app is foreground (the Quick Share picker OR its 'Completed' screen,
        # Nearby, Samsung share, the chooser, ...), BACK straight out of it - this is what returns us
        # to WhatsApp after a send, instead of waiting out unreadable frames.
        if on_share_sheet(adb) or on_whatsapp_foreground(adb) is False:
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
        adb.launch(wa_package(adb)); time.sleep(2.0)
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


def _pause_wait(pause, stop=None):
    """Block while the pause event is set. Returns the seconds spent paused, so a caller can extend
    a transfer deadline by that much (paused time must not count against the transfer). Returns
    immediately when not paused, or bails if a stop is requested."""
    if pause is None or not pause.is_set():
        return 0.0
    t0 = time.time()
    while pause.is_set():
        if stop is not None and stop.is_set():
            break
        time.sleep(0.2)
    return time.time() - t0


def export_and_send(adb, case, size, emit_log=lambda m: None, outer_stop=None,
                    on_runner=None, pc_name=PC_NAME, save_dir=SAVE_DIR, auto_accept=True,
                    deadline=None, pause=None):
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
    _pause_wait(pause, outer_stop)                   # honour Pause before touching the phone
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
    key = _coord_key(adb, size)
    cached = _learned_coords(adb, size)
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
        qs_node = _wait_share_target(adb, max(2, min(40 if cached.get("qs") else 240, _left())),
                                     stop=outer_stop)
    before = bt.snapshot(save_dir)
    if auto_accept and qs_accept is not None:
        if accept_watcher is not None:
            accept_watcher.arm(180)            # the run's one watcher (no new thread per chat)
        else:
            threading.Thread(target=lambda: qs_accept.accept_quickshare(timeout=180, log=emit_log),
                             daemon=True).start()
    if qs_node is not None:
        _COORDS.setdefault(key, {})["qs"] = list(core.node_center(qs_node)); _save_coords()
        adb.tap(*core.node_center(qs_node)); time.sleep(2.0)  # picker then polls (_wait_pc_node)
    elif cached.get("qs"):
        emit_log("share sheet unreadable - tapping Quick Share by learned position")
        adb.tap(*cached["qs"]); time.sleep(2.0); blind = True
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
        adb.tap(*core.node_center(pc_node)); time.sleep(1.5)  # transfer wait polls next
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
             "active_at": 0.0, "final_at": 0.0}

    def _abort():
        # returns "stop" (user stop -> bail now) or None. We DO NOT fast-fail on a phone 'Failed':
        # the phone's screen lists EVERY nearby device, so a stray 'Failed' (often from a DIFFERENT
        # device) must never cancel/retry a transfer that actually succeeded. The received file is
        # the final verdict; the phone screen tells us WHEN to keep waiting for it:
        #   - 'Sent' / 'Completed' / 'Done' (-> final_at): the phone FINISHED the transfer, so the
        #     file is imminent - wait for it, and NEVER re-send (that was the duplicate bug).
        #   - 'Sending' / '%' / 'x MB/s' (-> active_at): still in flight, keep waiting (big files).
        if outer_stop is not None and outer_stop.is_set():
            return "stop"
        now = time.time()
        if now < phone["next"]:
            return None
        phone["next"] = now + 6.0                       # throttle phone reads (less uiautomator load
        #                                                 during the transfer -> Quick Share runs faster
        #                                                 and WhatsApp ANRs less; the PC file is the
        #                                                 real signal and is polled far more often)
        nodes = _nodes(adb)
        if not phone["completed"] and (core.find_node(nodes, "Completed", "contains")
                                       or core.find_node(nodes, "Done", "exact")):
            phone["completed"] = True
            if not phone["final_at"]:
                phone["final_at"] = now
            emit_log("phone shows Quick Share 'Completed' - waiting for the file")
        if not phone["sent"] and core.find_node(nodes, "Sent", "contains"):
            phone["sent"] = True
            if not phone["final_at"]:
                phone["final_at"] = now
            emit_log("phone shows Quick Share 'Sent' - the file is on its way, waiting for it")
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
        return None

    # activity-aware wait: a minimum "floor" (in case the transfer screen is unreadable so we can't
    # see progress), extended while the phone keeps showing progress, up to a hard ceiling. The floor
    # is the latency of a GENUINE failure (file never comes, no progress), so keep it modest - a
    # successful transfer returns the instant the file lands, long before the floor.
    # BLIND (transfer screen unreadable): we can't see progress, so wait a longer minimum in case a
    # transfer is silently happening. READABLE: progress is visible, so the activity-extension below
    # covers any slow transfer and a short floor is enough - this keeps a genuine failure fast.
    floor_until = time.time() + (120 if blind else 75)
    hard_cap = time.time() + (900 if blind else 1200)
    res = None
    while time.time() < hard_cap:
        paused = _pause_wait(pause, outer_stop)      # Pause mid-transfer: freeze polling, don't fail
        if paused:
            floor_until += paused
            hard_cap += paused                       # paused time doesn't count against the transfer
        if outer_stop is not None and outer_stop.is_set():
            break
        res = bt.wait_for_new_file(save_dir, before, timeout=30,
                                   abort=_abort, confirm=lambda: phone["completed"])
        if res:
            break
        if phone["final_at"]:
            # the phone FINISHED the transfer (Sent/Completed) -> the file is imminent; wait a
            # generous grace for it to land, then STOP (don't loop to the cap): finished-but-no-file
            # means the Save folder isn't where Quick Share saves.
            if time.time() - phone["final_at"] < 120:
                continue
            break
        if time.time() - phone["active_at"] < 30:
            continue                                   # transfer still showing progress -> wait on
        if time.time() < floor_until:
            continue                                   # can't see progress yet -> honour the floor
        break                                          # no file, no progress -> genuinely done/failed
    if accept_watcher is not None:
        accept_watcher.disarm()                # transfer over: never accept someone else's file
    if not res:
        # LAST-CHANCE catch: the file may have landed right at the wait boundary (or while we were
        # navigating). Grab a new, stable, VALID zip from the folder before declaring failure - so a
        # good transfer is validated and recorded instead of wrongly failed/duplicated.
        late = _late_arrived_file(save_dir, before)
        if late:
            res = late
    _back_to_list(adb)
    if not res:
        # the file never arrived AND the transfer stopped showing progress: a real failure (PC not
        # receiving / asleep / wrong save folder / transfer dropped). Retried.
        if phone["final_at"]:
            # the phone SAYS it finished (Sent/Completed) but nothing landed in save_dir -> almost
            # always the Save folder isn't the real Quick Share destination. Retrying won't help.
            emit_log(f"WARNING: phone reported the transfer finished but no file appeared in "
                     f"'{save_dir}'. The Save folder is probably NOT where Quick Share saves - fix "
                     "it to stop every transfer being counted as failed.")
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
    _record_export(case, path, digest, label, emit_log)
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
    quiet_grace = 30.0
    finished_at = 0.0           # when the phone showed Sent/Completed (file imminent -> wait on)
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
        if time.time() >= next_phone:                      # read the phone's transfer screen
            next_phone = time.time() + 6.0                 # light phone polling -> faster transfer
            try:
                for nd in _nodes(adb):
                    for f in (nd.get("text"), nd.get("desc")):
                        if not f:
                            continue
                        fl = f.lower()
                        # 'Sent'/'Completed'/'Done' = the phone FINISHED -> file imminent, wait on
                        if "sent" in fl or "completed" in fl or fl.strip() == "done":
                            last_activity = time.time()
                            if not finished_at:
                                finished_at = time.time()
                        elif ("sending" in fl or "receiving" in fl or "transferring" in fl
                                or "waiting for" in fl or "connecting" in fl or "preparing" in fl
                                or re.search(r"\d+\s*%", fl)):
                            last_activity = time.time()
            except Exception:
                pass
        # once the phone has reported finished, keep waiting the full window for the file to land
        if finished_at:
            time.sleep(2.0)
            continue
        if not growing and (time.time() - last_activity) > quiet_grace:
            return None                                    # nothing arriving, phone idle -> real fail
        time.sleep(2.0)
    return None



def keep_in_case(case, path, digest, note):
    """Copy a received export into <case>/exports/ so the whole session is in ONE folder, verify the
    copy has the received file's SHA-256, and log it in hashes.csv. An earlier export of the same
    chat is never overwritten. Returns the copy's path, or None if the copy didn't verify."""
    src = Path(path)
    dest_dir = Path(case.dir) / "exports"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest, n = dest_dir / src.name, 1
    while dest.exists():
        dest = dest_dir / f"{src.stem} ({n}){src.suffix}"
        n += 1
    shutil.copy2(src, dest)
    if core.sha256_file(dest) != digest:
        dest.unlink()
        return None
    case.record_file(dest, f"{note} (copy of {src})")
    return dest


def _record_export(case, path, digest, label, emit_log):
    """Chain of custody for a received export: hash the received file where Quick Share saved it,
    and keep a verified copy in the case folder."""
    note = f"whatsapp export ({label}) via Quick Share"
    try:
        case.record_file(Path(path), note)
    except Exception as e:
        emit_log(f"WARNING: could not record {Path(path).name} in the case log ({e})")
    try:
        if keep_in_case(case, path, digest, note) is None:
            emit_log(f"WARNING: the copy of {Path(path).name} in the case folder did not match its "
                     "hash - not kept (the received file is unchanged)")
    except Exception as e:
        emit_log(f"WARNING: could not copy {Path(path).name} into the case folder ({e})")


def export_with_retry(adb, case, size, reopen, emit_log, outer_stop, on_runner,
                      pc_name, save_dir, ensure_list, pause=None):
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
        _pause_wait(pause, outer_stop)                    # honour Pause between attempts
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
            pc_name=pc_name, save_dir=save_dir, deadline=time.time() + MAX_SEND_SECONDS,
            pause=pause)
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
            _record_export(case, lp, ld, label, emit_log)
            return "ok", lp, ld, label
        emit_log(f"{status} - retrying (attempt {attempt + 2}/{1 + SHARE_RETRIES})")
        ensure_list()
    return status, path, digest, media


# ----------------------------------------------------------------- evidence CSV
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


CSV_HEADER = ["#", "chat", "status", "media", "saved_file", "sha256", "time", "list"]
_SECTION_NAMES = {"chats": "chat list", "archived": "Archived chats"}


_CSV_FALLBACK = {}        # target csv -> side file used while the target is locked (this run)
_REPLACE_TRIES = 5        # a briefly locked CSV (antivirus/indexer) is retried before falling back


def _unsaved_path(path):
    """A new side-file name next to `path`: exported_chats.unsaved-<stamp>.csv."""
    base = path[:-4] if path.lower().endswith(".csv") else path
    cand = f"{base}.unsaved-{time.strftime('%Y%m%d_%H%M%S')}.csv"
    n = 1
    while os.path.exists(cand):
        cand = f"{base}.unsaved-{time.strftime('%Y%m%d_%H%M%S')}-{n}.csv"
        n += 1
    return cand


def write_csv_atomic(path, rows, log=lambda m: None):
    """Write the exported-chats CSV so it can NEVER be lost (it's evidence):
    - writes to a temp file then atomically replaces the target (a crash mid-write can't corrupt it);
    - makes the parent folder if missing;
    - REFUSES to overwrite an existing non-empty CSV with an EMPTY table (so a failed/empty run
      never erases prior progress);
    - if the target is LOCKED (Windows refuses the replace while the CSV is open, e.g. in Excel),
      retries briefly, then saves the update to a side file 'exported_chats.unsaved-<stamp>.csv'.
      The update used to be discarded here, losing that progress."""
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
        tries = 1 if path in _CSV_FALLBACK else _REPLACE_TRIES   # known-locked: don't stall each chat
        for attempt in range(tries):
            try:
                os.replace(tmp, path)        # atomic on the same filesystem
                _CSV_FALLBACK.pop(path, None)
                return
            except PermissionError:
                if attempt + 1 < tries:
                    time.sleep(0.4 * (attempt + 1))
        side = _CSV_FALLBACK.get(path) or _unsaved_path(path)
        try:
            os.replace(tmp, side)
        except PermissionError:              # the side file itself got opened: start a new one
            side = _unsaved_path(path)
            os.replace(tmp, side)
        if path not in _CSV_FALLBACK:
            log(f"CSV LOCKED: '{os.path.basename(path)}' is open in another program (Excel?) - "
                f"progress saved to '{os.path.basename(side)}'. Close the CSV and it is updated "
                "again on the next write.")
        _CSV_FALLBACK[path] = side
    except Exception as e:
        log(f"csv write failed: {e}")
        try:
            if os.path.exists(path + ".tmp"):
                os.remove(path + ".tmp")
        except Exception:
            pass


def plan_csv(case_dir, cases_dir, serial, package=None, picked=None):
    """(resume_from, csv_path) for a run. Progress is ALWAYS written into the current case folder,
    so one session's CSV, contacts, exports and hashes stay together. A CSV picked by the examiner,
    or this device's newest earlier one, is only READ (its chats already ok are skipped) and left
    untouched as the earlier session's evidence."""
    out = Path(case_dir) / csv_name(package)
    resume = picked or latest_csv_for_serial(cases_dir, serial, package)
    return (str(resume) if resume else None), str(out)


def latest_csv_for_serial(cases_dir, serial, package=None):
    """Find the most recent non-empty exported-chats CSV for THIS device. Case folders are named
    '<timestamp>_<device tag>' (older ones '<timestamp>_<serial>'). Side files saved while the CSV
    was locked ('exported_chats.unsaved-<stamp>.csv') count too, so the newest progress wins.
    Returns the path or None. This is how a restart on the SAME mobile continues the SAME CSV,
    while a DIFFERENT mobile gets none and starts fresh. WhatsApp Business progress lives in its own
    CSV (csv_name), so each app resumes only its own."""
    if not serial:
        return None
    import glob
    found = []
    for folder in {core.device_tag(serial), core.safe_name(serial)}:
        base = csv_name(package)[:-4]
        for name in (base + ".csv", base + ".unsaved-*.csv"):
            pat = os.path.join(glob.escape(str(cases_dir)), f"*_{glob.escape(folder)}", name)
            found += [c for c in glob.glob(pat)
                      if os.path.isfile(c) and os.path.getsize(c) > 0]
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
            nm = _norm_name(_csv_unsafe(row.get("chat") or ""))   # same identity as live reads
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
                 pc_name=PC_NAME, save_dir=SAVE_DIR, resume_from=None, start_from=None, package=None):
        super().__init__(daemon=True)
        self.adb, self.case, self.size, self.emit = adb, case, size, emit
        if package:
            adb.wa_package = package          # the WhatsApp app this run drives
        self._stop = threading.Event()
        self._pause = pause or threading.Event()
        self._runner = None       # current inner export runner
        self._last_running = None  # the chat we last started exporting (for "stopped where" status)
        self.status = {}          # name -> status
        self.files = {}           # name -> (filename, sha256, media)
        self.times = {}           # name -> ISO time the row became terminal
        self.order = []           # discovery order
        self.pc_name, self.save_dir = pc_name, save_dir
        self.csv_path = str(csv_path) if csv_path else str(Path(case.dir) / csv_name(package))
        self.resume_from = resume_from   # path to a prior CSV: skip its 'ok' chats, retry the rest
        self.start_from = start_from     # chat name to scroll to and begin at (skip everything above)
        self.only_names = None           # if set (a set of names), export ONLY these; skip others
        self.scan_order = None           # a pre-scanned ordered list of names (for directional seek)
        self._done_fps = set()           # fingerprints of finished chats (loop/duplicate guard)
        self._dup_warned = set()         # names already flagged as ambiguous duplicates (F-10)
        self.section_of = {}             # chat name -> 'chats' (main list) or 'archived'

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
        scroll_list(self.adb, "down")          # one verified page (see scroll_list)

    def _swipe_up(self):
        scroll_list(self.adb, "up")

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

    # ------------------------------------------------------------- walking the lists
    def _wanted(self, nm):
        """Is `nm` one of the chats this run should export? (all, or only the chosen ones)"""
        if self.only_names is None:
            return True
        keys = {_norm_name(n).lower() for n in self.only_names}
        return _norm_name(nm).lower() in keys

    def _targets_done(self):
        """With a chosen set of chats: True once every one of them has a final status."""
        if not self.only_names:
            return False
        keys = {_norm_name(n).lower() for n in self.only_names}
        done = {_norm_name(n).lower() for n, s in self.status.items() if s not in ("pending", "running")}
        return keys <= done

    def _enter_section(self, section):
        """Show the main chat list ('chats') or the Archived list, with the All filter on."""
        self._ensure_list()
        if section == "archived":
            return open_archived(self.adb, stop=self._stop)
        leave_archived(self.adb)
        show_all_chats(self.adb)
        return True

    def _in_section(self, section):
        return in_archived(self.adb) == (section == "archived")

    def _walk(self, section, from_top=True, retry=None):
        """Export every pending chat of one list, top to bottom. `retry` = a set of chat names to
        export again (the end-of-run sweep); otherwise every newly seen chat is queued.
        Returns the number of chats exported OK."""
        exported = 0
        unexpected = 0                             # non-AdbError errors (safety bail at 30)
        try:
            if not self._enter_section(section):
                return 0
            if from_top:
                scroll_to_end(self.adb, "up", stop=self._stop)
        except Exception as e:                     # never let one bad read end the evidence run
            unexpected += 1
            self.emit("log", f"unexpected error (continuing, {unexpected}/30): {e!r}")
        stills = 0
        unreadable = 0        # consecutive pages with no readable chat rows
        clip_waits = 0        # nudges spent bringing a clipped (half-visible) chat fully on screen
        loops = 0
        while not self._stop.is_set() and loops < 20000:
            loops += 1
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
            if loops % 15 == 0:
                keep_awake(self.adb, True)       # periodically re-assert (stay-on can lapse)
            try:
                # if WhatsApp threw an "isn't responding" (ANR) dialog, tap WAIT and carry on
                if dismiss_anr(self.adb):
                    self.emit("log", "handled a 'WhatsApp isn't responding' dialog (tapped Wait)")
                    time.sleep(1.5)
                # a mega-chat's export can finish LATE and pop the Android share sheet after we
                # already moved on; it's unreadable, so detect it by its foreground window.
                for _ in range(4):
                    if not on_share_sheet(self.adb):
                        break
                    self.adb.key("BACK"); time.sleep(1.0)
                rows = _chat_rows(_nodes(self.adb))
                if not rows:
                    # unreadable -> often a LOCKED / screen-OFF phone: wake it and WAIT for an unlock
                    unreadable += 1
                    if unreadable > 30 or not self._recover_screen():
                        self.emit("log", "chat list unreadable / could not recover - stopping")
                        break
                    continue
                unreadable = 0
                if not self._in_section(section):
                    # a relaunch (last-resort recovery) lands on the main list: go back to this list
                    if not self._enter_section(section):
                        break
                    scroll_to_end(self.adb, "up", stop=self._stop)
                    continue
                visible = [r["name"] for r in rows]
                if retry is None:
                    self._register(visible, section)
                self._write_csv()
                want = retry if retry is not None else None
                pend = [r for r in rows if self.status.get(r["name"]) == "pending"
                        and (want is None or r["name"] in want)]
                todo = [r["name"] for r in pend if r["full"]]
                if pend and not todo:
                    # only TAP chats fully on screen (a clipped row can put the tap on the nav bar)
                    clip_waits += 1
                    if clip_waits <= 2:
                        y = core.node_center(pend[0]["node"])[1]
                        scroll_list(self.adb, "up" if y < self.size[1] // 2 else "down", rows=1)
                        continue
                    todo = [pend[0]["name"]]           # fallback: never get stuck on it
                if todo:
                    stills = 0
                    clip_waits = 0
                    name = todo[0]
                    self._last_running = name
                    self.emit("row", name, "running")
                    st = self._export_one(name)
                    self.status[name] = st
                    self.section_of.setdefault(name, section)
                    fp = _fingerprint(name)
                    if fp:
                        self._done_fps.add(fp)               # finished -> never re-export a variant
                    self.emit("row", name, st)
                    if st == "ok":
                        exported += 1
                    self._write_csv()
                    continue
                if self._targets_done() or (retry is not None and not any(
                        self.status.get(n) == "pending" for n in retry)):
                    break                                    # everything asked for is done
                res, view = scroll_list(self.adb, "down")
                if res == "unreadable":
                    if not self._recover_screen():
                        self.emit("log", "chat list unreadable / could not recover - stopping")
                        break
                    continue
                if res == "moved":
                    stills = 0
                    continue
                stills += 1
                if stills >= 2:
                    if _end_confirmed(self.adb, "down", "up", view, self._stop):
                        self.emit("log", f"reached the bottom of the {_SECTION_NAMES[section]}")
                        break
                    stills = 0
            except core.AdbError as e:
                self.emit("log", f"adb error (likely disconnect): {e}")
                self._wait_device()
                try:
                    self._ensure_list()
                except core.AdbError:
                    pass
            except Exception as e:
                # a single unexpected error on one chat must NEVER kill the whole evidence run
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
        return exported

    def _register(self, visible, section):
        """Queue newly seen chats (or mark them skipped when only some chats were chosen)."""
        for nm in visible:
            self.section_of.setdefault(nm, section)
            if nm in self.status:
                continue
            # LOOP GUARD: a chat we already FINISHED read again with a slightly different name
            # (happens under load) has the same fingerprint -> skip it as a duplicate.
            # A "" fingerprint (an all-symbol/emoji name) is NEVER treated as a duplicate.
            fp = _fingerprint(nm)
            if fp and fp in self._done_fps:
                self.status[nm] = "skipped"; self.order.append(nm)
                self.emit("discover", nm); self.emit("row", nm, "skipped")
                self.emit("log", f"duplicate of a finished chat - skipping: {nm!r}")
                continue
            sel = self._wanted(nm)
            self.status[nm] = "pending" if sel else "skipped"
            self.order.append(nm)
            self.emit("discover", nm)
            if not sel:
                self.emit("row", nm, "skipped")
        # F-10: two chats with the SAME display name are tracked as one (status is keyed by name):
        # surface it so the operator renames one, instead of a silently-incomplete CSV.
        for dup in duplicate_names(visible):
            if dup not in self._dup_warned:
                self._dup_warned.add(dup)
                self.emit("log", f"WARNING ambiguous-duplicate: {dup!r} appears more than "
                          "once on screen; only one is tracked - rename one on the phone "
                          "to export both")

    def _retry_failed(self):
        """End-of-run sweep: every chat that still failed (transfer, picker, export, not found...)
        gets one more full attempt, in the list it lives in. Privacy-blocked chats can't be exported
        and are not retried. Returns the number recovered."""
        failed = [n for n, s in self.status.items() if s.startswith("fail")]
        if not failed or self._stop.is_set():
            return 0
        self.emit("log", f"retrying {len(failed)} chat(s) that failed earlier: "
                         + ", ".join(repr(n) for n in failed[:10]) + (" ..." if len(failed) > 10 else ""))
        before = {n: self.status[n] for n in failed}
        for n in failed:
            self.status[n] = "pending"
            self.emit("row", n, "pending")
        got = 0
        for section in ("chats", "archived"):
            names = {n for n in failed if self.section_of.get(n, "chats") == section}
            if names and not self._stop.is_set():
                got += self._walk(section, from_top=True, retry=names)
        for n in failed:                          # not reached again -> keep its real failure
            if self.status.get(n) in ("pending", "running"):
                self.status[n] = before[n]
                self.emit("row", n, before[n])
        self._write_csv()
        return got

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
            rows.append([i, _csv_safe(nm), st, media, _csv_safe(fn), digest, self.times.get(nm, ""),
                         self.section_of.get(nm, "")])
        write_csv_atomic(self.csv_path, rows, lambda m: self.emit("log", m))

    def _export_one(self, name):
        def reopen():
            node = _settled_node(self.adb, lambda nodes: _chat_node_in(nodes, name))
            if not node:
                return False
            self.adb.tap(*core.node_center(node)); time.sleep(1.4)
            if opened_chat_matches(_nodes(self.adb), name) is False:
                self.emit("log", f"   opened a different chat than {name!r} - going back (not exported)")
                _back_to_list(self.adb)
                return False
            return True
        status, path, digest, media = export_with_retry(
            self.adb, self.case, self.size, reopen,
            lambda m: self.emit("log", f"   {m}"),
            self._stop, self._set_runner, self.pc_name, self.save_dir, self._ensure_list,
            pause=self._pause)
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
            global accept_watcher
            if qs_accept is not None:
                accept_watcher = qs_accept.AcceptWatcher(log=lambda m: self.emit("log", f"   {m}"))
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
                    if self.status[nm] == "ok" and self.only_names and self._wanted(nm):
                        self.status[nm] = "pending"     # explicitly chosen -> export it again
                        self.emit("log", f"resume: {nm!r} was already ok - exporting again (chosen)")
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
            # main list from the top (a START-FROM seek already placed us: walk on from there),
            # then the Archived chats, then one more attempt for anything that failed.
            exported += self._walk("chats", from_top=not self.start_from)
            if not self._stop.is_set() and not self._targets_done():
                if open_archived(self.adb, stop=self._stop):
                    self.emit("log", "now exporting the Archived chats")
                    exported += self._walk("archived", from_top=True)
                else:
                    self.emit("log", "no Archived chats")
            exported += self._retry_failed()
            leave_archived(self.adb)
        finally:
            if accept_watcher is not None:
                accept_watcher.stop()
                accept_watcher = None
            pc_keep_awake(False)                  # release the PC sleep/display lock
            keep_awake(self.adb, False)
            try:                                  # restore the phone's original screen-off timeout
                if getattr(self, "_orig_sot", None) and self._orig_sot.isdigit():
                    self.adb.shell("settings", "put", "system", "screen_off_timeout",
                                   self._orig_sot, quiet=True)
            except Exception:
                pass
            self._write_csv()
            pending_left = sum(1 for s in self.status.values() if s in ("pending", "running"))
            self.emit("finished", {"exported": exported, "total": len(self.order),
                                   "csv": self.csv_path, "stopped": self._stop.is_set(),
                                   "last": self._last_running, "pending": pending_left})
