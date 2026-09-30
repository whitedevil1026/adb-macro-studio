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


_TEXT_EXTS = (".txt",)


def zip_media_info(path) -> dict:
    """Inspect a received WhatsApp export .zip to VERIFY its media content (authoritative over
    the UI-flow inference) and summarise what's inside. A "with media" export bundles media files
    next to _chat.txt; a "without media" export is just the _chat.txt.
    Returns {ok, files, txt, media, has_media, label, media_bytes, chat_txt} or {ok: False, error}."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
    except Exception as e:
        return {"ok": False, "error": str(e)}
    txt = [i for i in infos if i.filename.lower().endswith(_TEXT_EXTS)]
    media = [i for i in infos if not i.filename.lower().endswith(_TEXT_EXTS)]
    return {
        "ok": True,
        "files": len(infos),
        "txt": len(txt),
        "media": len(media),
        "has_media": bool(media),
        "label": "with media" if media else "without media",
        "media_bytes": sum(i.file_size for i in media),
        "chat_txt": txt[0].filename if txt else None,
    }


def default_bt_folders():
    """Likely Windows locations for received Bluetooth files (best-effort guesses)."""
    home = Path(os.path.expanduser("~"))
    return [str(home / "Documents"), str(home / "Downloads"), str(home)]


def _inspect_cli(target):
    """Print the media summary for one .zip, or every .zip in a folder."""
    import glob
    try:
        import sys as _sys
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    zips = [target] if target.lower().endswith(".zip") else sorted(glob.glob(os.path.join(target, "*.zip")))
    if not zips:
        print("no .zip files found at:", target); return
    print(f"{'file':50} {'label':13} {'media':>6} {'MB':>8}")
    print("-" * 82)
    for z in zips:
        info = zip_media_info(z)
        name = os.path.basename(z)[:48]
        if not info.get("ok"):
            print(f"{name:50} ERROR: {info.get('error')}"); continue
        print(f"{name:50} {info['label']:13} {info['media']:>6} {info['media_bytes']/1e6:>8.1f}")


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    # inspect mode: a .zip file, or a folder that contains .zip exports
    if arg.lower().endswith(".zip") or (arg and os.path.isdir(arg) and
                                        __import__("glob").glob(os.path.join(arg, "*.zip"))):
        _inspect_cli(arg)
    else:
        # default: watch a folder for 20s and report the first new stable file
        folder = arg or str(Path(os.path.expanduser("~")) / "Downloads")
        print("watching:", folder)
        base = snapshot(folder)
        res = wait_for_new_file(folder, base, timeout=20)
        print("result:", res)
