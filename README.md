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
`wait_text` (find & tap a UI element by text/id — resolution-independent), `screenshot`,
`ui_dump`, `pull`.

---

## 6. Where things are saved
| Folder | Contents | Published? |
|--------|----------|------------|
| `macros/` | Your saved actions & sequences (JSON) | No (git-ignored) |
| `cases/`  | Per-device case folder: `session.log`, `hashes.csv`, pulled files, screenshots | No |
| `logs/`   | GUI session/debug log | No |

---

## 7. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `adb` not found | Add platform-tools to PATH, or set `ADB_PATH`. |
| Device `unauthorized` / prompt keeps reappearing | Unlock the phone; on the popup tick **Always allow** → Allow. If stuck: Developer options → **Revoke USB debugging authorizations**, toggle USB debugging off/on, then `adb kill-server && adb devices`. |
| Device keeps disconnecting (state flips to "no devices") | Use a proper **data** cable, plug **directly** into the PC (no hub), set USB mode to **File transfer**, and disable USB selective suspend in Windows power settings. |
| Live screen is black | Make sure the phone is **unlocked** and the connection is stable. |
| Auto-capture finds no touchscreen | Ensure the device is connected/authorized; the app auto-selects the real touchscreen node. |
| Taps land in the wrong place on another phone | Re-record, or rely on the auto-scaling (macros store the recording resolution). For maximum robustness use `tap_text` (element-based) steps. |

---

## 8. License
MIT — see [LICENSE](LICENSE). No warranty. You are responsible for how you use it.
