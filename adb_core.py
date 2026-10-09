"""ADB plumbing for the macro recorder: device calls, touch recording, UI lookup,
macro playback and evidence logging.

Stdlib + the adb binary only, so it runs on an offline workstation.
Every adb command goes through Adb.run(), which is the single place adb is invoked.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

KEYCODES = {
    "HOME": 3, "BACK": 4, "CALL": 5, "ENDCALL": 6, "DPAD_UP": 19, "DPAD_DOWN": 20,
    "DPAD_LEFT": 21, "DPAD_RIGHT": 22, "DPAD_CENTER": 23, "VOLUME_UP": 24,
    "VOLUME_DOWN": 25, "POWER": 26, "CAMERA": 27, "TAB": 61, "ENTER": 66, "DEL": 67,
    "MENU": 82, "SEARCH": 84, "PAGE_UP": 92, "PAGE_DOWN": 93, "ESCAPE": 111,
    "MOVE_HOME": 122, "MOVE_END": 123, "APP_SWITCH": 187, "SLEEP": 223, "WAKEUP": 224,
}

# Field specs for every step type: (key, label, default). The default's Python type
# is the type the value is coerced to when edited.
STEP_FIELDS = {
    "tap":        [("x", "X", 0), ("y", "Y", 0)],
    "long_press": [("x", "X", 0), ("y", "Y", 0), ("duration", "Hold (ms)", 800)],
    "swipe":      [("x", "X start", 0), ("y", "Y start", 0), ("x2", "X end", 0),
                   ("y2", "Y end", 0), ("duration", "Duration (ms)", 300)],
    "text":       [("text", "Text to type (ASCII)", "")],
    "key":        [("key", "Key name or code", "BACK")],
    "wait":       [("seconds", "Seconds", 1.0)],
    "launch":     [("package", "App package", "com.whatsapp")],
    "tap_text":   [("text", "Text / description / resource-id", ""),
                   ("match", "Match: contains | exact", "contains"), ("timeout", "Timeout (s)", 10.0)],
    "wait_text":  [("text", "Text / description / resource-id", ""),
                   ("match", "Match: contains | exact", "contains"), ("timeout", "Timeout (s)", 10.0)],
    "if_text":    [("text", "Text to detect on screen", ""),
                   ("match", "Match: contains | exact", "contains"),
                   ("timeout", "Max wait (s)", 60.0),
                   ("or_text", "Else as soon as this text appears", "")],
    "screenshot": [("name", "File label", "screen")],
    "ui_dump":    [("name", "File label", "ui")],
    "pull":       [("remote", "Path on phone", "/sdcard/Android/media/com.whatsapp/WhatsApp"),
                   ("local", "Folder name on PC", "whatsapp")],
}
COMMON_FIELDS = [("delay", "Wait after (s)", 1.0), ("label", "Label", "")]
COORD_TYPES = ("tap", "long_press", "swipe")


class AdbError(RuntimeError):
    pass


# ---------------------------------------------------------------- locating tools

def find_adb() -> str | None:
    """Locate the adb executable: $ADB_PATH, then PATH, then common install dirs."""
    env = os.environ.get("ADB_PATH")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("adb")
    if found:
        return found
    for p in (
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        os.path.expandvars(r"%ProgramFiles%\platform-tools\adb.exe"),
        r"C:\platform-tools\adb.exe",
        r"C:\Android\platform-tools\adb.exe",
        os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
        "/usr/local/bin/adb", "/usr/bin/adb",
    ):
        if p and os.path.isfile(p):
            return p
    return None


def find_scrcpy(adb_path: str | None) -> str | None:
    found = shutil.which("scrcpy")
    if found:
        return found
    if adb_path:
        sib = Path(adb_path).with_name("scrcpy.exe")
        if sib.is_file():
            return str(sib)
    return None


# ---------------------------------------------------------------- pure parsers

def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s)).strip("_") or "item"


def parse_devices(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("*", "List of")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        d = {"serial": parts[0], "state": parts[1]}
        for p in parts[2:]:
            if ":" in p:
                k, v = p.split(":", 1)
                d[k] = v
        out.append(d)
    return out


def parse_wm_size(text: str) -> tuple[int, int] | None:
    phys = over = None
    for m in re.finditer(r"(Physical|Override) size:\s*(\d+)x(\d+)", text):
        wh = (int(m[2]), int(m[3]))
        if m[1] == "Override":
            over = wh
        else:
            phys = wh
    return over or phys


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        head = data[:120].decode("utf-8", "replace").strip()
        raise AdbError(f"screencap did not return a PNG (got: {head!r})")
    return struct.unpack(">II", data[16:24])


_SHELL_SPECIAL = set("\\'\"`$&|;<>()*?~!#[]{}")


def escape_input_text(s: str) -> str:
    """Escape text for `adb shell input text` (runs through the phone's sh)."""
    out = []
    for ch in s:
        if ch == " ":
            out.append("%s")
        elif ch in _SHELL_SPECIAL:
            out.append("\\" + ch)
        elif ord(ch) < 32 or ord(ch) > 126:
            raise ValueError(f"'input text' can only type plain ASCII; cannot type {ch!r}")
        else:
            out.append(ch)
    return "".join(out)


def key_code(k) -> int:
    s = str(k).strip().upper()
    if s.isdigit():
        return int(s)
    s = s.removeprefix("KEYCODE_")
    if s not in KEYCODES:
        raise ValueError(f"unknown key {k!r}; use a number or one of {', '.join(KEYCODES)}")
    return KEYCODES[s]


_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


# Characters illegal in XML 1.0 (control chars, lone surrogates, U+FFFE/U+FFFF) and numeric
# references to them. ONE contact name containing such a character used to make ET.fromstring()
# throw, so the WHOLE screen read as empty (every chat on that page vanished).
_BAD_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_BAD_XML_REFS = re.compile(
    r"&#(?:[xX]0*(?:[0-8bBcCeEfF]|1[0-9a-fA-F])|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));")
_NODE_TAG = re.compile(r"<node\b([^>]*?)/?>", re.S)
_ATTR = re.compile(r'([\w:-]+)="([^"]*)"')


def _complete_dump(text: str) -> bool:
    """True only for a WHOLE uiautomator dump (opening AND closing tag). A dump cut short when the
    phone kills uiautomator must be retried - not parsed as an empty screen."""
    return "<hierarchy" in text and "</hierarchy>" in text


def _node_dict(get):
    m = _BOUNDS.search(get("bounds", "") or "")
    if not m:
        return None
    return {"text": get("text", "") or "", "desc": get("content-desc", "") or "",
            "id": get("resource-id", "") or "", "cls": get("class", "") or "",
            "pkg": get("package", "") or "", "clickable": get("clickable") == "true",
            "bounds": tuple(map(int, m.groups()))}


def parse_ui_nodes(xml_text: str) -> list[dict]:
    start, end = xml_text.find("<hierarchy"), xml_text.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise AdbError("uiautomator returned no UI hierarchy: " + xml_text[:200].strip())
    body = xml_text[start:end + len("</hierarchy>")]
    body = _BAD_XML_REFS.sub("", _BAD_XML_CHARS.sub("", body))
    try:
        nodes = [_node_dict(el.get) for el in ET.fromstring(body).iter("node")]
    except ET.ParseError:
        # still malformed (odd escaping in some name): pull node attributes out by regex rather
        # than losing every row on the screen.
        import html
        nodes = []
        for m in _NODE_TAG.finditer(body):
            attrs = {k: html.unescape(v) for k, v in _ATTR.findall(m.group(1))}
            nodes.append(_node_dict(attrs.get))
    return [n for n in nodes if n]


def _area(n) -> int:
    x1, y1, x2, y2 = n["bounds"]
    return max(0, x2 - x1) * max(0, y2 - y1)


def node_center(n) -> tuple[int, int]:
    x1, y1, x2, y2 = n["bounds"]
    return (x1 + x2) // 2, (y1 + y2) // 2


def node_label(n) -> str:
    return n["text"] or n["desc"] or n["id"]


def find_node(nodes: list[dict], query: str, match: str = "contains") -> dict | None:
    """First visible node whose text, content-desc or resource-id matches query."""
    q = query.strip().lower()
    if not q:
        return None
    visible = [n for n in nodes if _area(n) > 0]

    def fields(n):
        return [f.lower() for f in (n["text"], n["desc"], n["id"]) if f]

    exact = [n for n in visible if q in fields(n) or n["id"].lower().endswith("/" + q)]
    if exact or match == "exact":
        return exact[0] if exact else None
    loose = [n for n in visible if any(q in f for f in fields(n))]
    return loose[0] if loose else None


def node_at(nodes: list[dict], x: int, y: int) -> dict | None:
    """Smallest labelled node under the point (what a tap there would hit)."""
    hits = [n for n in nodes if (n["text"] or n["desc"]) and _area(n) > 0
            and n["bounds"][0] <= x < n["bounds"][2] and n["bounds"][1] <= y < n["bounds"][3]]
    return min(hits, key=_area) if hits else None


def visible_texts(nodes: list[dict]) -> list[str]:
    out = []
    for n in nodes:
        parts = [p for p in (n["text"], n["desc"]) if p]
        if parts and _area(n) > 0:
            out.append(" | ".join(dict.fromkeys(parts)))
    return out


def parse_getevent_caps(text: str) -> list[dict]:
    """Parse `getevent -lp` into input devices with their touch X/Y ranges."""
    devs, cur = [], None
    for line in text.splitlines():
        m = re.match(r"\s*add device \d+:\s*(\S+)", line)
        if m:
            cur = {"path": m[1], "name": "", "max_x": None, "max_y": None, "has_mt": False}
            devs.append(cur)
            continue
        if cur is None:
            continue
        m = re.search(r'name:\s*"(.*)"', line)
        if m:
            cur["name"] = m[1]
            continue
        m = re.search(r"\b(ABS_MT_POSITION_X|ABS_MT_POSITION_Y|ABS_X|ABS_Y)\s*:.*?\bmax\s+(-?\d+)", line)
        if m:
            key = "max_x" if m[1].endswith("X") else "max_y"
            if m[1].startswith("ABS_MT"):
                cur["has_mt"] = True
            if m[1].startswith("ABS_MT") or cur[key] is None:
                cur[key] = int(m[2])
    return devs


def score_touch_device(d: dict) -> int:
    """Higher = more likely to be the real finger touchscreen."""
    n = d["name"].lower()
    s = 0
    if "touchscreen" in n:
        s += 100
    elif "touch" in n and "pad" not in n:
        s += 50
    if any(w in n for w in ("pen", "stylus")):
        s -= 80
    if "pad" in n:
        s -= 40
    if d.get("has_mt"):          # multi-touch = a finger panel, not a stylus/button
        s += 20
    return s


def pick_touch_device(devs: list[dict]) -> dict | None:
    # Need a multi-touch panel; fall back to any device with an X/Y range.
    cands = [d for d in devs if d["max_x"] and d["max_y"] and d.get("has_mt")]
    if not cands:
        cands = [d for d in devs if d["max_x"] and d["max_y"]]
    if not cands:
        return None
    cands.sort(key=score_touch_device, reverse=True)
    return cands[0]


_EV = re.compile(r"^\[\s*(\d+\.\d+)\]\s+(?:/dev/\S+:\s+)?(EV_\w+)\s+(\w+)\s+(\S+)")


class GestureParser:
    """Turns `getevent -lt` lines from a touchscreen into tap/long_press/swipe steps.

    Only the first finger (slot 0) is followed; raw sensor units are scaled to
    screen pixels using the device's max X/Y and the natural (portrait) screen size.
    """

    def __init__(self, max_x, max_y, width, height, move_px=30, long_ms=500):
        self.max_x, self.max_y, self.width, self.height = max_x, max_y, width, height
        self.move_px, self.long_ms = move_px, long_ms
        self.slot = 0
        self.cx = self.cy = None
        self.down = self.pend_down = self.pend_up = False
        self.t0 = 0.0
        self.x0 = self.y0 = self.x1 = self.y1 = 0

    def _px(self, rx, ry):
        return (round(rx * self.width / (self.max_x + 1)), round(ry * self.height / (self.max_y + 1)))

    def feed(self, line: str) -> dict | None:
        m = _EV.match(line.strip())
        if not m:
            return None
        t, typ, code, val = float(m[1]), m[2], m[3], m[4]
        if typ == "EV_ABS":
            if code == "ABS_MT_SLOT":
                self.slot = int(val, 16)
            elif self.slot != 0:
                pass
            elif code in ("ABS_MT_POSITION_X", "ABS_X"):
                self.cx = int(val, 16)
            elif code in ("ABS_MT_POSITION_Y", "ABS_Y"):
                self.cy = int(val, 16)
            elif code == "ABS_MT_TRACKING_ID":
                if val.lower() == "ffffffff":
                    self.pend_up = True
                else:
                    self.pend_down = True
        elif typ == "EV_KEY" and code == "BTN_TOUCH":
            if val == "DOWN":
                self.pend_down = True
            elif val == "UP":
                self.pend_up = True
        elif typ == "EV_SYN" and code == "SYN_REPORT":
            return self._sync(t)
        return None

    def _sync(self, t):
        gesture = None
        if self.pend_down and not self.down and self.cx is not None and self.cy is not None:
            self.down, self.t0 = True, t
            self.x0, self.y0 = self.cx, self.cy
        if self.down and self.cx is not None:
            self.x1, self.y1 = self.cx, self.cy
        if self.pend_up and self.down:
            self.down = False
            gesture = self._classify(self.t0, t)
        self.pend_down = self.pend_up = False
        return gesture

    def _classify(self, t0, t1):
        x0, y0 = self._px(self.x0, self.y0)
        x1, y1 = self._px(self.x1, self.y1)
        ms = int(round((t1 - t0) * 1000))
        base = {"t_down": t0, "t_up": t1}
        if math.hypot(x1 - x0, y1 - y0) >= self.move_px:
            return {"type": "swipe", "x": x0, "y": y0, "x2": x1, "y2": y1,
                    "duration": max(50, min(ms, 5000)), **base}
        if ms >= self.long_ms:
            return {"type": "long_press", "x": x0, "y": y0, "duration": ms, **base}
        return {"type": "tap", "x": x0, "y": y0, **base}


def scaler(ref, cur):
    """Map coordinates recorded at resolution ref=(w,h) onto resolution cur=(w,h)."""
    if not ref or not cur or not all(ref) or not all(cur) or tuple(ref) == tuple(cur):
        return lambda x, y: (int(x), int(y))
    rw, rh = ref
    cw, ch = cur
    return lambda x, y: (round(x * cw / rw), round(y * ch / rh))


def describe_step(s: dict) -> str:
    t = s.get("type")
    if t == "tap":
        return f"({s['x']}, {s['y']})"
    if t == "long_press":
        return f"({s['x']}, {s['y']}) hold {s.get('duration', 800)} ms"
    if t == "swipe":
        return f"({s['x']}, {s['y']}) -> ({s['x2']}, {s['y2']}) in {s.get('duration', 300)} ms"
    if t == "text":
        return repr(s.get("text", ""))
    if t == "key":
        return str(s.get("key"))
    if t == "wait":
        return f"{s.get('seconds')} s"
    if t == "launch":
        return s.get("package", "")
    if t in ("tap_text", "wait_text"):
        return f"\"{s.get('text')}\" ({s.get('match', 'contains')}, up to {s.get('timeout', 10)} s)"
    if t == "if_text":
        return (f"if screen shows \"{s.get('text')}\" -> then {len(s.get('then', []))} step(s), "
                f"else {len(s.get('else', []))} step(s)")
    if t in ("screenshot", "ui_dump"):
        return f"save as '{s.get('name')}'"
    if t == "pull":
        return f"{s.get('remote')} -> pulled/{s.get('local')}"
    return json.dumps(s)


def new_step(type_: str, **values) -> dict:
    if type_ not in STEP_FIELDS:
        raise ValueError(f"unknown step type {type_!r}")
    step = {"type": type_}
    for key, _label, default in STEP_FIELDS[type_] + COMMON_FIELDS:
        step[key] = values.get(key, default)
    return step


# ---------------------------------------------------------------- macros on disk

def save_macro(folder: Path, macro: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_name(macro['name'])}.json"
    path.write_text(json.dumps(macro, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_macro(path: Path) -> dict:
    macro = json.loads(Path(path).read_text(encoding="utf-8"))
    steps = macro.get("steps")
    if not isinstance(steps, list):
        raise ValueError(f"{path}: 'steps' must be a list")
    for i, s in enumerate(steps, 1):
        if s.get("type") not in STEP_FIELDS:
            raise ValueError(f"{path}: step {i} has unknown type {s.get('type')!r}")
    return macro


def list_macros(folder: Path) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(Path(folder).glob("*.json")):
        try:
            out.append((p, load_macro(p)))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- evidence log

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class CaseLog:
    """cases/<stamp>_<serial>/ with session.log (every adb command) and hashes.csv."""

    def __init__(self, root: Path, serial: str):
        self.dir = Path(root) / f"{now_stamp()}_{safe_name(serial)}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "session.log"
        self.hash_path = self.dir / "hashes.csv"
        self._lock = threading.Lock()
        self._n = 0
        if not self.hash_path.exists():
            with open(self.hash_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["time", "file", "bytes", "sha256", "note"])

    def write(self, msg: str):
        with self._lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()}  {msg}\n")

    def stem(self, label: str) -> str:
        with self._lock:
            self._n += 1
            return f"{self._n:04d}_{safe_name(label)}_{now_stamp()}"

    def save_bytes(self, sub: str, filename: str, data: bytes, note: str = "") -> Path:
        path = self.dir / sub / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.record_file(path, note)
        return path

    def record_file(self, path: Path, note: str = "") -> str:
        digest = sha256_file(path)
        rel = Path(path).relative_to(self.dir).as_posix()
        with self._lock, open(self.hash_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([now_iso(), rel, Path(path).stat().st_size, digest, note])
        self.write(f"saved {rel} sha256={digest}")
        return digest

    def record_tree(self, folder: Path, note: str = "") -> int:
        n = 0
        for p in sorted(Path(folder).rglob("*")):
            if p.is_file():
                self.record_file(p, note)
                n += 1
        return n


# ---------------------------------------------------------------- adb wrapper

class Adb:
    def __init__(self, path: str, serial: str | None = None, log=None):
        self.path, self.serial = path, serial
        self.log = log or (lambda msg: None)
        self.input_gen = 0        # bumped on every input event; lets a UI-dump cache know the
        #                           screen may have changed (see whatsapp_batch._nodes)

    def cmd(self, *args) -> list[str]:
        base = [self.path] + (["-s", self.serial] if self.serial else [])
        return base + [str(a) for a in args]

    def run(self, *args, timeout=60, quiet=False) -> bytes:
        c = self.cmd(*args)
        if not quiet:
            self.log("adb " + " ".join(c[1:]))
        try:
            p = subprocess.run(c, capture_output=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise AdbError(f"adb {' '.join(c[1:])} timed out after {timeout}s") from None
        if p.returncode != 0:
            msg = (p.stderr or p.stdout).decode("utf-8", "replace").strip()
            raise AdbError(f"adb {' '.join(c[1:])} failed (exit {p.returncode}): {msg}")
        return p.stdout

    def shell(self, *args, timeout=60, quiet=False) -> str:
        return self.run("shell", *args, timeout=timeout, quiet=quiet).decode("utf-8", "replace")

    # --- device facts
    def devices(self) -> list[dict]:
        return parse_devices(Adb(self.path).run("devices", "-l", quiet=True).decode("utf-8", "replace"))

    def version(self) -> str:
        return Adb(self.path).run("version", quiet=True).decode("utf-8", "replace").strip()

    def getprop(self, key: str) -> str:
        return self.shell("getprop", key, quiet=True).strip()

    def device_info(self) -> dict:
        info = {k: self.getprop(p) for k, p in (
            ("manufacturer", "ro.product.manufacturer"), ("model", "ro.product.model"),
            ("android", "ro.build.version.release"), ("sdk", "ro.build.version.sdk"),
            ("fingerprint", "ro.build.fingerprint"))}
        info["serial"] = self.serial
        info["width"], info["height"] = parse_wm_size(self.shell("wm", "size", quiet=True)) or (0, 0)
        dens = re.findall(r"density:\s*(\d+)", self.shell("wm", "density", quiet=True))
        info["density"] = int(dens[-1]) if dens else 0
        return info

    def packages(self, contains: str = "") -> list[str]:
        out = self.shell("pm", "list", "packages", quiet=True)
        names = sorted(l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:"))
        return [n for n in names if contains.lower() in n.lower()]

    # --- screen
    def screencap(self, quiet=True) -> bytes:
        data = self.run("exec-out", "screencap", "-p", timeout=30, quiet=quiet)
        png_size(data)
        return data

    def ui_dump(self, retries=3) -> str:
        """UI hierarchy XML. Tries stdout first (writes nothing on the phone). Retries with
        backoff because uiautomator can be SIGKILLed (exit 137) when the phone is busy
        (e.g. preparing a media export)."""
        last = ""
        for attempt in range(retries):
            try:                                  # primary: stream to stdout
                out = self.run("exec-out", "uiautomator", "dump", "/dev/tty", timeout=30, quiet=True)
                text = out.decode("utf-8", "replace")
                if _complete_dump(text):          # a dump cut short (process killed) is retried
                    return text
            except AdbError as e:
                last = str(e)
            try:                                  # fallback: dump to a file, read it back
                remote = "/sdcard/window_dump.xml"
                self.shell("uiautomator", "dump", remote, timeout=30, quiet=True)
                text = self.run("exec-out", "cat", remote, quiet=True).decode("utf-8", "replace")
                self.shell("rm", "-f", remote, quiet=True)
                if _complete_dump(text):
                    return text
            except AdbError as e:
                last = str(e)
            time.sleep(1.0 + attempt)             # back off (phone busy) before retrying
        raise AdbError("uiautomator dump failed after retries: " + last)

    # --- input  (each input bumps input_gen so a UI-dump cache knows the screen may have changed)
    def tap(self, x, y):
        self.input_gen += 1
        self.shell("input", "tap", int(x), int(y))

    def swipe(self, x, y, x2, y2, ms=300):
        self.input_gen += 1
        self.shell("input", "swipe", int(x), int(y), int(x2), int(y2), int(ms))

    def long_press(self, x, y, ms=800):
        self.swipe(x, y, x, y, ms)                     # swipe() already bumps input_gen

    def type_text(self, s: str):
        self.input_gen += 1
        self.shell("input", "text", escape_input_text(s))

    def key(self, k):
        self.input_gen += 1
        self.shell("input", "keyevent", key_code(k))

    def launch(self, package: str):
        self.input_gen += 1
        out = self.shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        if "No activities found" in out or "monkey aborted" in out:
            raise AdbError(f"cannot launch {package}: {out.strip()}")

    # --- settings / files
    def get_setting(self, namespace: str, key: str) -> str:
        return self.shell("settings", "get", namespace, key, quiet=True).strip()

    def put_setting(self, namespace: str, key: str, value):
        self.shell("settings", "put", namespace, key, value)

    def pull(self, remote: str, local_dir: Path):
        self.run("pull", "-a", remote, str(local_dir), timeout=None)


# ---------------------------------------------------------------- recording from the phone

class TouchRecorder:
    """Streams `getevent -lt` from the touchscreen and reports finished gestures."""

    def __init__(self, adb: Adb, width: int, height: int, on_gesture, on_error):
        self.adb, self.width, self.height = adb, width, height
        self.on_gesture, self.on_error = on_gesture, on_error
        self.proc = None
        self.stopping = False
        self.device = None

    def start(self) -> dict:
        dev = None
        for attempt in range(3):                 # getevent -lp can return empty right after reconnect
            caps = self.adb.shell("getevent", "-lp", timeout=15)
            dev = pick_touch_device(parse_getevent_caps(caps))
            if dev:
                break
            time.sleep(0.6)
        if not dev:
            raise AdbError("no touchscreen found in 'getevent -lp' output (is the device connected?)")
        self.device = dev
        parser = GestureParser(dev["max_x"], dev["max_y"], self.width, self.height)
        # -tt gives getevent a pty: output is line-buffered and it dies when we disconnect.
        cmd = self.adb.cmd("shell", "-tt", "getevent", "-lt", dev["path"])
        self.adb.log("adb " + " ".join(cmd[1:]))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.PIPE, creationflags=NO_WINDOW)
        threading.Thread(target=self._read, args=(parser,), daemon=True).start()
        return dev

    def _read(self, parser):
        for raw in self.proc.stdout:
            g = parser.feed(raw.decode("utf-8", "replace"))
            if g:
                self.on_gesture(g)
        if not self.stopping:
            self.on_error("touch recording ended (getevent exited)")

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ---------------------------------------------------------------- playback

class MacroRunner(threading.Thread):
    """Plays steps on the phone in a background thread.

    emit(kind, *args) is called with: ("loop", n, total), ("step", row, step),
    ("preview", type, points), ("frame", png_bytes), ("log", msg), ("finished", reason).
    """

    def __init__(self, adb: Adb, case: CaseLog, steps: list[dict], emit, *, rows=None,
                 ref_size=None, cur_size=None, loops=1, stop_when_unchanged=False,
                 preview=0.6, show_frames=True, stop_on_error=True):
        super().__init__(daemon=True)
        self.adb, self.case, self.steps, self.emit = adb, case, steps, emit
        self.rows = rows or list(range(len(steps)))
        self.scale = scaler(ref_size, cur_size)
        self.loops, self.stop_when_unchanged = loops, stop_when_unchanged
        self.preview, self.show_frames, self.stop_on_error = preview, show_frames, stop_on_error
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def sleep(self, seconds) -> bool:
        """Interruptible sleep; True if Stop was pressed."""
        return self._stop.wait(max(0.0, float(seconds or 0)))

    def run(self):
        try:
            reason = self._run()
        except Exception as e:
            reason = f"error: {e}"
        self.emit("finished", reason)

    def _run(self) -> str:
        n, last = 0, None
        while True:
            n += 1
            self.emit("loop", n, self.loops)
            for row, step in zip(self.rows, self.steps):
                if self._stop.is_set():
                    return "stopped"
                self.emit("step", row, step)
                try:
                    self._exec(step)
                except Exception as e:
                    self.emit("log", f"step {row + 1} ({step['type']}) failed: {e}")
                    if self.stop_on_error:
                        return f"error at step {row + 1}"
                if self.sleep(step.get("delay", 0)):
                    return "stopped"
                if self.show_frames and step["type"] != "screenshot":
                    self.emit("frame", self.adb.screencap())
            if self.loops and n >= self.loops:
                return f"done ({n} run{'s' if n > 1 else ''})"
            if self.stop_when_unchanged:
                digest = hashlib.sha256(self.adb.screencap()).hexdigest()
                if digest == last:
                    return f"screen stopped changing after {n} runs"
                last = digest

    def _show(self, kind, points):
        self.emit("preview", kind, points)
        self.sleep(self.preview)

    def _exec(self, s: dict):
        t = s["type"]
        if t in COORD_TYPES:
            x, y = self.scale(s["x"], s["y"])
            if t == "swipe":
                x2, y2 = self.scale(s["x2"], s["y2"])
                self._show(t, [(x, y), (x2, y2)])
                self.adb.swipe(x, y, x2, y2, s.get("duration", 300))
            else:
                self._show(t, [(x, y)])
                if t == "tap":
                    self.adb.tap(x, y)
                else:
                    self.adb.long_press(x, y, s.get("duration", 800))
        elif t == "text":
            self.adb.type_text(s["text"])
        elif t == "key":
            self.adb.key(s["key"])
        elif t == "wait":
            self.sleep(s.get("seconds", 1))
        elif t == "launch":
            self.adb.launch(s["package"])
        elif t in ("tap_text", "wait_text"):
            node = self._wait_for(s["text"], s.get("match", "contains"), float(s.get("timeout", 10)))
            if t == "tap_text":
                x, y = node_center(node)
                self._show("tap", [(x, y)])
                self.adb.tap(x, y)
        elif t == "screenshot":
            data = self.adb.screencap(quiet=False)
            self.case.save_bytes("screens", self.case.stem(s.get("name", "screen")) + ".png", data, "screenshot")
            self.emit("frame", data)
        elif t == "ui_dump":
            self.adb.log("adb exec-out uiautomator dump /dev/tty")
            xml = self.adb.ui_dump()
            stem = self.case.stem(s.get("name", "ui"))
            self.case.save_bytes("ui_dumps", stem + ".xml", xml.encode("utf-8"), "UI hierarchy")
            text = "\n".join(visible_texts(parse_ui_nodes(xml)))
            self.case.save_bytes("ui_dumps", stem + ".txt", text.encode("utf-8"), "visible text")
        elif t == "pull":
            dest = self.case.dir / "pulled" / safe_name(s.get("local") or "pull")
            dest.mkdir(parents=True, exist_ok=True)
            self.adb.pull(s["remote"], dest)
            n = self.case.record_tree(dest, f"pulled from {s['remote']}")
            self.emit("log", f"pulled {n} files into {dest}")
        elif t == "if_text":
            query = s.get("text", "")
            match = s.get("match", "contains")
            or_text = s.get("or_text", "")
            timeout = float(s.get("timeout", 60))
            self.emit("log", f"branch: watching for \"{query}\""
                             + (f"  (else as soon as \"{or_text}\" appears)" if or_text else ""))
            found = self._branch_wait(query, match, or_text, timeout)
            branch = s.get("then", []) if found else s.get("else", [])
            self.emit("log", f"branch -> {'THEN' if found else 'ELSE'} ({len(branch)} step(s))")
            for sub in branch:
                if self._stop.is_set():
                    return
                self._exec(sub)
                if self.sleep(sub.get("delay", 0)):
                    return
        else:
            raise ValueError(f"unknown step type {t!r}")

    def _branch_wait(self, query, match, or_text, timeout) -> bool:
        """Poll the screen: True as soon as `query` appears, False if `or_text`
        appears first or the timeout elapses."""
        deadline = timeout
        while True:
            try:
                nodes = parse_ui_nodes(self.adb.ui_dump())
                if query and find_node(nodes, query, match):
                    return True
                if or_text and find_node(nodes, or_text, "contains"):
                    return False
            except AdbError:
                pass
            if deadline <= 0 or self.sleep(1.0):
                return False
            deadline -= 1.0 + 1.2

    def _wait_for(self, query, match, timeout):
        self.adb.log(f"looking for \"{query}\" on screen (uiautomator)")
        deadline = timeout
        while True:
            try:
                node = find_node(parse_ui_nodes(self.adb.ui_dump()), query, match)
                if node:
                    return node
            except AdbError as e:  # "could not get idle state" while the screen animates
                self.emit("log", f"UI dump retry: {e}")
            if deadline <= 0 or self.sleep(1.0):
                raise TimeoutError(f"\"{query}\" not found on screen within {timeout:g} s")
            deadline -= 1.0 + 1.2  # sleep + typical dump time
