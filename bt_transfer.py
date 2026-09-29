"""
PC-side helper for Bluetooth (or any) file transfer: watch a destination folder and
detect when a new file has fully arrived (size stable), then hash it.

Runs on the PC (where the received files land). No adb here.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path


def snapshot(folder) -> dict:
    """Map current filename -> size for a folder (non-recursive)."""
    out = {}
    try:
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                try:
                    out[name] = os.path.getsize(p)
                except OSError:
                    pass
    except FileNotFoundError:
        pass
    return out


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def wait_for_new_file(folder, before: dict, timeout=120, stable_secs=2.0, poll=0.5):
    """Wait until a NEW file (not in `before`) appears in `folder` and its size stops
    growing for `stable_secs`. Returns (path, size, sha256) or None on timeout.

    `before` is a snapshot() taken just before the transfer was triggered.
    """
    folder = str(folder)
    deadline = time.time() + timeout
    stable_since = {}
    last_size = {}
    while time.time() < deadline:
        now = snapshot(folder)
        for name, size in now.items():
            if name in before and before[name] == size:
                continue                       # unchanged pre-existing file
            # a new or growing file
            if last_size.get(name) == size and size > 0:
                stable_since.setdefault(name, time.time())
                if time.time() - stable_since[name] >= stable_secs:
                    path = os.path.join(folder, name)
                    return path, size, sha256(path)
            else:
                stable_since.pop(name, None)   # size changed -> reset stability timer
            last_size[name] = size
        time.sleep(poll)
    return None


def default_bt_folders():
    """Likely Windows locations for received Bluetooth files (best-effort guesses)."""
    home = Path(os.path.expanduser("~"))
    return [str(home / "Documents"), str(home / "Downloads"), str(home)]


if __name__ == "__main__":
    # quick self-test: watch Downloads for 20s and report the first new stable file
    import sys
    folder = sys.argv[1] if len(sys.argv) > 1 else str(Path(os.path.expanduser("~")) / "Downloads")
    print("watching:", folder)
    base = snapshot(folder)
    res = wait_for_new_file(folder, base, timeout=20)
    print("result:", res)
