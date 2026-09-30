# ADB Macro Studio

An **offline** desktop tool to **record, save, and replay repetitive taps/swipes on an
Android phone over ADB** — without rooting, and without installing anything on the phone.
Built for workflows where you repeat the same on-screen steps across many devices
(QA, kiosk setup, bulk configuration, forensic *logical* extraction, etc.).

- 🖥️ **Live phone screen** inside the app (read-only screenshots — nothing is written to the phone).
- 👆 **Capture taps** by tapping the phone, clicking the live image, or via **auto-capture** (records everything until you press Stop).
- 🧩 **Actions & Sequences** — group taps into named *actions*, then chain actions into a *sequence* and run it.
- 🔁 **Replay** once, N times, continuously, or until the screen stops changing.
- 📐 **Cross-device** — macros remember the resolution they were recorded at and are auto-scaled to the connected phone.
- 🧾 **Audit trail** — every adb command and every pulled/saved file (with SHA-256) is logged to a per-device case folder.
- 🐍 **Zero dependencies** — pure Python standard library + the `adb` executable.

There is also a **headless CLI** (`adb_cli.py`) for machines with no display.

> ⚠️ **Use responsibly.** Only use this on devices you own or are explicitly authorized to
> access. Enabling USB debugging and sending input events modifies device state; in a
> forensic context, document what you do. This project is provided as-is under the MIT license.

---

## 1. Requirements

| Need | Details |
|------|---------|
| **Python 3.8+** | With **tkinter** (bundled on Windows/macOS; on Linux run `sudo apt install python3-tk`). |
| **Android platform-tools (`adb`)** | Download from Google (see below). |
| **A phone with USB debugging** | Any Android device, connected by a **data** USB cable. |

### Install `adb` (Android platform-tools)
1. Download **SDK Platform-Tools** for your OS from Google:
   <https://developer.android.com/tools/releases/platform-tools>
2. Unzip it somewhere, e.g. `C:\platform-tools`.
3. Make `adb` findable in **one** of these ways:
   - **Add it to your PATH** (recommended), **or**
   - Set an environment variable `ADB_PATH` to the full path of `adb`, e.g.
     `C:\platform-tools\adb.exe`, **or**
   - Drop it in a common location (`C:\platform-tools\`, the Android SDK, etc.) — the app auto-detects these.

Verify:
```bash
adb version
```

### Already have `adb` in another folder? (point the tool at it)
You don't have to move it. Tell the tool where it is with the **`ADB_PATH`** variable —
which must be the **full path to the `adb.exe` file**, not the folder.

Example: adb lives at `D:\tools\platform-tools\adb.exe`, app extracted to `C:\adb-macro-studio`.

**A. Just for this run (Command Prompt):**
```bat
cd /d C:\adb-macro-studio
set ADB_PATH=D:\tools\platform-tools\adb.exe
python adb_gui.py
```

**B. Or add the folder to PATH for this run** (here you point at the *folder*):
```bat
set PATH=%PATH%;D:\tools\platform-tools
python adb_gui.py
```
> **What that line means:** `PATH` is the list of folders Windows searches when you type a
> command like `adb`. Entries are separated by semicolons (`;`). `%PATH%` means "everything
> already in PATH", so the line reads: *new PATH = (everything already there) + `;` + the adb
> folder* — it **appends** the folder while keeping the rest.
> ⚠️ Always include `%PATH%;`. Writing just `set PATH=D:\tools\platform-tools` would **erase**
> the existing list for that window and break other commands. (This change is temporary — it
> only affects that one Command Prompt window.)

**C. Set it once, permanently** (`setx` — then open a NEW window):
```bat
setx ADB_PATH "D:\tools\platform-tools\adb.exe"
```

PowerShell equivalent of A: `$env:ADB_PATH = "D:\tools\platform-tools\adb.exe"`.
macOS/Linux: `export ADB_PATH=/path/to/adb`.

When the app starts, the **Log box** shows the adb it picked (e.g. `adb: D:\tools\platform-tools\adb.exe`) so you can confirm.

---

## 2. Get the code

Download the ZIP from GitHub (green **Code → Download ZIP**) and extract it, **or** clone:
```bash
git clone https://github.com/whitedevil1026/adb-macro-studio.git
cd adb-macro-studio
```

---

## 3. Run

### Windows — Command Prompt (cmd)
```bat
cd C:\path\to\adb-macro-studio
python adb_gui.py
```
If `python` isn't recognised, use the launcher:
```bat
py adb_gui.py
```
To point at a specific adb for this run:
```bat
set ADB_PATH=C:\platform-tools\adb.exe
python adb_gui.py
```

### Windows — PowerShell
```powershell
cd C:\path\to\adb-macro-studio
$env:ADB_PATH = "C:\platform-tools\adb.exe"   # optional
python adb_gui.py
```

### macOS / Linux
```bash
cd /path/to/adb-macro-studio
export ADB_PATH=/path/to/adb        # optional if adb is on PATH
python3 adb_gui.py
```

### Headless / no display (CLI)
```bash
python adb_cli.py
```

---

## 4. Connect the phone

1. On the phone: **Settings → About phone → tap "Build number" 7 times** to enable Developer options.
2. **Settings → Developer options → turn on USB debugging.**
3. Plug the phone into the PC with a **data** USB cable; set the USB mode to **File transfer (MTP)**.
4. Run `adb devices`. On the phone, tap **"Allow USB debugging"** and tick **"Always allow from this computer"**.
   You want the device to show up as `device` (not `unauthorized` or `offline`).

```bash
adb devices
```

---

## 5. Using the GUI

1. **Refresh → Connect** — the live screen appears and the top bar shows the model/resolution.
2. **Capture taps** (any of):
   - **Capture one tap** → then tap the phone (or click the live image) → one step recorded.
   - **Auto capture (until Stop)** → tap/swipe freely on the phone → **Stop**. Everything is recorded.
   - **Add…** → a dialog where you can type X/Y, or click **📍 Tap the phone to fill X/Y**.
   - **Click live image to add:** `add tap` / `pick element` — click the on-screen image to add a tap or an element-based (`tap_text`) step.
3. **Group into actions** — select steps in the list (Ctrl/Shift-click for several) → **Save as action**, give it a name (e.g. `action1`). Repeat for `action2`, `action3`…
4. **Build a sequence** — **Build sequence from actions…** → add saved actions in order → **Save sequence**. It auto-loads.
5. **Run** — **Simulate selected once** (one step, live) or **Run macro** (Loops / continuous / stop-when-screen-unchanged).
6. **Pull data off the phone** — add a `pull` step (**Add… → type `pull`**) with a phone path (e.g. `/sdcard/DCIM`); files land in `cases/<timestamp>_<serial>/pulled/` with SHA-256 hashes logged.

### Step types available
`tap`, `long_press`, `swipe`, `text`, `key`, `wait`, `launch` (open an app), `tap_text` /
`wait_text` (find & tap a UI element by text/id — resolution-independent), `if_text`
(conditional branch — see below), `screenshot`, `ui_dump`, `pull`.

### Conditional branching (`if_text`)
`if_text` reads the screen and runs one set of steps if a word/phrase appears, another set if
it doesn't — so a macro can react to popups and different screens. Fields: `text` (what to look
for), `match` (`contains`/`exact`), `timeout` (how long to watch), and `or_text` (as soon as
this appears, take the *else* path — avoids waiting the full timeout on the success screen).
The step carries two nested step lists, `then` and `else`. Example (retry an export "without
media" only if the "unable to export" popup shows):

```json
{
  "type": "if_text", "text": "Unable to export", "match": "contains",
  "timeout": 120, "or_text": "Quick Share",
  "then": [
    {"type": "tap_text", "text": "OK", "delay": 1},
    {"type": "tap_text", "text": "Export chat", "delay": 1.5},
    {"type": "tap_text", "text": "Without media", "delay": 1}
  ],
  "else": []
}
```

Because it taps by **text** (via `uiautomator`), the same macro works across screen sizes and
UI variations without any fixed coordinates.

---

## 6. Presets (ready-made macros)
The [`presets/`](presets/) folder ships example macros. Notably
[`whatsapp_export.json`](presets/whatsapp_export.json) exports a WhatsApp chat/group by
reading the screen (no coordinates; works for chats and groups), trying with media and
falling back to without-media if the export popup appears. To use one, copy it into `macros/`
and **Load** it in the app. See [presets/README.md](presets/README.md) for details.

## 7. Batch export WhatsApp chats to the PC (Quick Share)

For bulk logical extraction, the app can walk **every** WhatsApp chat and save each export
straight to this PC over **Quick Share** — no cloud, no cable copy, and it works fully
**offline** (Quick Share uses Bluetooth + Wi-Fi Direct, so it runs in airplane mode as long as
Bluetooth and Wi-Fi are switched on). Every saved file is SHA-256 hashed and logged to a CSV.

### One-time setup
1. Install **Quick Share for Windows** on the PC and sign in; set it to **receive** and choose a
   **save folder** (e.g. `Downloads` or a case folder).
2. On the phone, make sure **Quick Share** is available in WhatsApp's share sheet and that the PC
   shows up as a nearby device (Bluetooth **and** Wi-Fi on, on both).
3. In the app's batch window, set two fields to match your setup:
   - **PC name** — exactly as the phone sees this PC in the Quick Share device list.
   - **Save folder** — the same folder Quick Share for Windows saves incoming files to.

> **Hands-off Accept (optional):** the app can auto-click the Windows Quick Share **Accept**
> button for you, but that needs the third-party **`pywinauto`** package:
> ```bash
> pip install pywinauto
> ```
> **Without it the tool still works** — the export and Quick Share send run normally and the file
> is still received and hashed — you just click **Accept** yourself on each transfer. If your
> Windows is not in English, adjust the accept label in `qs_accept.py` (`ACCEPT_LABELS`).

### How the rolling export works
It does **not** pre-scan the whole list. Instead it reads the chats currently on screen, exports
the ones not done yet, scrolls down, and repeats to the bottom — so it starts working immediately
and survives lists of hundreds of chats. Per chat it:

1. Opens the chat and exports it, **trying "with media" first** and automatically falling back to
   **"without media"** if WhatsApp says the export is too large.
2. Shares the resulting file to this PC via **Quick Share**, waits for it to land in the save
   folder, and records its **SHA-256**.
3. Writes a row to the CSV, then moves to the next chat.

**Buttons:** *Scan* (see what's on screen), *Auto* (rolling scan+export), *Pause/Resume*,
*Resume pending* (retry rows still pending or failed), *Save CSV*, and *Stop* (halts within a few
seconds — it also interrupts an in-progress export).

### Skip chats that take too long
Some very large chats make the phone too busy for the screen-reader (Android can even kill the
UI-dump helper under memory pressure). To stop one chat from stalling the whole run, each chat has
a time budget — **`MAX_SEND_SECONDS`** in `whatsapp_batch.py` (default **180 s**) — to reach the
"sent" point. If it's exceeded, the chat is marked **`fail-timeout`** in the CSV and the batch
moves on. (The file transfer itself keeps a separate, longer timeout.)

### The CSV (audit log)
Written to `cases/<timestamp>_<serial>/exported_chats.csv` and **rewritten after every chat**, so
it's always current — even mid-run and even for failures. Columns:

| Column | Meaning |
|--------|---------|
| `#` | Row number in discovery order |
| `chat` | Chat/group name (sanitised against spreadsheet formula injection) |
| `status` | `ok`, or a `fail-*` reason (see below), `pending`, `running`, `stopped` |
| `media` | `with media` / `without media` (the path taken during export, recorded even if the send later failed), or `not exported` when the chat never opened. Blank only while a row is still `pending`/`running`. |
| `saved_file` | Filename received on the PC |
| `sha256` | Hash of the received file |
| `time` | When **that** chat finished (frozen per row, not the last-write time) |

Common `status` values: `ok`, `fail-export` (couldn't produce the file), `fail-timeout` (over the
per-chat budget), `fail-sent` (the phone's Quick Share showed **"Failed"** - the transfer dropped,
usually Bluetooth/Wi-Fi), `fail-transfer` (the file never arrived within the timeout - PC not
receiving, asleep, or wrong save folder), `fail-noshare`/`fail-pcpick` (couldn't find Quick Share
or this PC in the picker), `fail-notfound` (chat row not found), `fail-device` (phone disconnected
too long). The phone's Quick Share page is read live during the send, so a **"Failed"** is caught
in seconds (fast retry) and a **"Sent"** is logged as progress.

> **Limitation:** chats are tracked by display name. Two chats with the **identical** name are
> de-duplicated — only the first is exported. Rename one on the phone if you need both.

### Viewing the exported chats — offline WhatsApp-Web-style viewer
[`chat_viewer.py`](chat_viewer.py) turns a folder of export `.zip`s into **one self-contained,
offline HTML page** that reads like WhatsApp Web — a searchable chat list on the left, message
bubbles with inline images/video/audio on the right, and global + in-chat search.

**For a full export set (all chats + all media), use folder mode** — one `index.html` plus a
`media/` folder beside it. You open `index.html` and every chat + every image/video/audio is
there, at any size, nothing skipped:
```bash
python chat_viewer.py "E:\quickshare" --media folder
# -> writes E:\quickshare\index.html  +  E:\quickshare\media\   (open index.html; keep media\ beside it)
```

Or a **single portable file** (good for one/a few smaller chats), media embedded as base64:
```bash
python chat_viewer.py "E:\quickshare"          # -> chat_viewer.html (one file)
```

Options:
- `--media {embed,folder}` — `embed` = one self-contained `.html` (default; media as base64, capped
  by `--max-embed-mb`). `folder` = `index.html` + a `media/` folder holding **all** media at any
  size (recommended for a whole export set). A truly single file with *all* media can be many GB and
  won't open — that's why big sets use folder mode.
- `-o path.html` — output path.
- `--max-embed-mb N` — embed mode only: embed media up to N MB each (default **20**); larger shows a
  labelled placeholder. `0` = embed everything.
- `--owner "Your Name"` — right-align your own messages as "you" (auto-detected otherwise; WhatsApp
  writes the owner's own messages as "You").

It's pure standard library + vanilla JS, fully offline, and read-only (nothing is written back to
the exports). Media is embedded as base64 so the single `.html` is portable.

---

## 8. Where things are saved
| Folder | Contents | Published? |
|--------|----------|------------|
| `macros/` | Your saved actions & sequences (JSON) | No (git-ignored) |
| `cases/`  | Per-device case folder: `session.log`, `hashes.csv`, `exported_chats.csv`, pulled files, screenshots | No |
| Quick Share save folder | The exported chat `.zip` files received on the PC | No (outside the repo) |
| `logs/`   | GUI session/debug log | No |

---

## 9. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `adb` not found | Add platform-tools to PATH, or set `ADB_PATH`. |
| Device `unauthorized` / prompt keeps reappearing | Unlock the phone; on the popup tick **Always allow** → Allow. If stuck: Developer options → **Revoke USB debugging authorizations**, toggle USB debugging off/on, then `adb kill-server && adb devices`. |
| Device keeps disconnecting (state flips to "no devices") | Use a proper **data** cable, plug **directly** into the PC (no hub), set USB mode to **File transfer**, and disable USB selective suspend in Windows power settings. |
| Live screen is black | Make sure the phone is **unlocked** and the connection is stable. |
| Auto-capture finds no touchscreen | Ensure the device is connected/authorized; the app auto-selects the real touchscreen node. |
| Taps land in the wrong place on another phone | Re-record, or rely on the auto-scaling (macros store the recording resolution). For maximum robustness use `tap_text` (element-based) steps. |
| Batch export: files never arrive on the PC | Check **PC name** and **save folder** match Quick Share for Windows; confirm Bluetooth **and** Wi-Fi are on for both devices and the PC appears in the phone's Quick Share list. |
| Batch export: a chat shows `fail-timeout` | The chat was too large to reach "sent" within `MAX_SEND_SECONDS` (default 180). Increase it in `whatsapp_batch.py`, or export that one chat manually. |
| Batch export: Accept isn't auto-clicked | Non-English Windows — set the button label in `qs_accept.py` (`ACCEPT_LABELS`). |

---

## 10. License
MIT — see [LICENSE](LICENSE). No warranty. You are responsible for how you use it.
