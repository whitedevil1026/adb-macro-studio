#!/usr/bin/env python3
"""
ADB Macro Studio - headless CLI
===============================
A terminal menu front-end driving the same engine in adb_core.py, for when you
have no display (SSH, remote workstation) or just prefer the keyboard.

Same macros, same case folders as the GUI (adb_gui.py) - they interoperate.

Run:  python adb_cli.py
Needs: adb on PATH (or set the ADB_PATH environment variable), Python 3.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import adb_core as core
from adb_core import (
    Adb, CaseLog, TouchRecorder, MacroRunner,
    STEP_FIELDS, COMMON_FIELDS, KEYCODES,
    new_step, describe_step, save_macro, load_macro, list_macros,
    find_adb, find_scrcpy, AdbError,
)

BASE = Path(__file__).resolve().parent
MACRO_DIR = BASE / "macros"
CASE_DIR = BASE / "cases"


class Cli:
    def __init__(self):
        self.adb_path = find_adb()
        self.adb: Adb | None = None
        self.serial: str | None = None
        self.cur_size = (0, 0)
        self.ref_size = None            # resolution a loaded macro was recorded at
        self.case: CaseLog | None = None
        self.steps: list[dict] = []
        self.name = "macro1"

    # -- helpers ----------------------------------------------------------
    def log(self, msg):
        print("  " + str(msg))

    def need_device(self) -> bool:
        if self.adb and self.serial:
            return True
        print("  No device connected (use option 1).")
        return False

    def _ask(self, prompt, default=None):
        s = input(f"    {prompt}" + (f" [{default}]" if default is not None else "") + ": ").strip()
        return s if s else ("" if default is None else str(default))

    # -- device -----------------------------------------------------------
    def select_device(self):
        if not self.adb_path:
            print("  adb not found on PATH."); return
        try:
            devs = Adb(self.adb_path).devices()
        except AdbError as e:
            print("  " + str(e)); return
        if not devs:
            print("  No devices. Plug in the phone, enable USB debugging, accept the RSA prompt.")
            return
        for i, d in enumerate(devs, 1):
            print(f"   {i}. {d['serial']}  [{d['state']}]  {d.get('model','')}")
        sel = self._ask("Pick #", 1)
        if not (sel.isdigit() and 1 <= int(sel) <= len(devs)):
            return
        d = devs[int(sel) - 1]
        if d["state"] != "device":
            print(f"  Device state is '{d['state']}' - accept the prompt on the phone and retry.")
            return
        self.serial = d["serial"]
        self.adb = Adb(self.adb_path, self.serial, log=self.log)
        info = self.adb.device_info()
        self.cur_size = (info["width"], info["height"])
        self.case = CaseLog(CASE_DIR, self.serial)
        self.case.write(f"connected {info}")
        print(f"  Connected: {info['manufacturer']} {info['model']} | Android {info['android']} "
              f"| {info['width']}x{info['height']}")
        print(f"  Case folder: {self.case.dir}")

    # -- recording --------------------------------------------------------
    def record(self):
        if not self.need_device():
            return
        got = []

        def on_gesture(g):
            step = new_step(g["type"], **{k: g[k] for k in g
                                          if k in ("x", "y", "x2", "y2", "duration")})
            got.append(step)
            print(f"    + {describe_step(step)}")

        try:
            rec = TouchRecorder(self.adb, self.cur_size[0], self.cur_size[1],
                                on_gesture=on_gesture, on_error=lambda m: print("  " + m))
            dev = rec.start()
        except AdbError as e:
            print("  record failed: " + str(e)); return
        print(f"  Recording from {dev.get('name','touchscreen')}. Tap/swipe on the phone.")
        input("  >>> press Enter here to STOP recording <<<\n")
        rec.stop()
        if got and self._ask(f"Add these {len(got)} step(s) to the macro? (y/n)", "y").lower().startswith("y"):
            self.steps.extend(got)
        print(f"  Sequence now has {len(self.steps)} step(s).")

    # -- manual step ------------------------------------------------------
    def add_step(self):
        types = list(STEP_FIELDS.keys())
        print("  Step types:")
        for i, t in enumerate(types, 1):
            print(f"   {i:>2}. {t}")
        sel = self._ask("Type #")
        if not (sel.isdigit() and 1 <= int(sel) <= len(types)):
            return
        t = types[int(sel) - 1]
        if t == "key":
            print("  keys: " + ", ".join(KEYCODES))
        values = {}
        for key, label, default in STEP_FIELDS[t] + COMMON_FIELDS:
            raw = self._ask(label, default)
            try:
                if isinstance(default, bool):
                    values[key] = raw.lower() in ("1", "true", "yes", "y")
                elif isinstance(default, int):
                    values[key] = int(float(raw)) if raw != "" else default
                elif isinstance(default, float):
                    values[key] = float(raw) if raw != "" else default
                else:
                    values[key] = raw
            except ValueError:
                print(f"  '{key}' must be a number - step cancelled."); return
        try:
            self.steps.append(new_step(t, **values))
        except ValueError as e:
            print("  " + str(e)); return
        print(f"  Added. {len(self.steps)} step(s).")

    def view_steps(self):
        if not self.steps:
            print("  (no steps yet)"); return
        for i, s in enumerate(self.steps, 1):
            lbl = f"  '{s['label']}'" if s.get("label") else ""
            print(f"   {i:>2}. {s['type']:<11} {describe_step(s)}{lbl}")
        print("  d<N> delete | m<N> move up | Enter back")
        cmd = input("  > ").strip().lower()
        if cmd.startswith("d") and cmd[1:].isdigit():
            i = int(cmd[1:]) - 1
            if 0 <= i < len(self.steps):
                print("  removed:", describe_step(self.steps.pop(i)))
        elif cmd.startswith("m") and cmd[1:].isdigit():
            i = int(cmd[1:]) - 1
            if 0 < i < len(self.steps):
                self.steps[i - 1], self.steps[i] = self.steps[i], self.steps[i - 1]
                print("  moved up.")

    # -- macros -----------------------------------------------------------
    def save(self):
        if not self.steps:
            print("  Nothing to save."); return
        self.name = self._ask("Macro name", self.name) or self.name
        macro = {
            "name": self.name,
            "created": core.now_iso(),
            "device": {"serial": self.serial, "size": list(self.cur_size)},
            "ref_size": list(self.cur_size) if self.cur_size != (0, 0) else None,
            "steps": self.steps,
        }
        path = save_macro(MACRO_DIR, macro)
        print(f"  Saved -> {path}")

    def load(self):
        macros = list_macros(MACRO_DIR)
        if not macros:
            print("  No saved macros."); return
        for i, (p, m) in enumerate(macros, 1):
            print(f"   {i:>2}. {m['name']}  ({len(m.get('steps', []))} steps)")
        sel = self._ask("Load #")
        if not (sel.isdigit() and 1 <= int(sel) <= len(macros)):
            return
        _, macro = macros[int(sel) - 1]
        self.steps = macro["steps"]
        self.name = macro["name"]
        self.ref_size = macro.get("ref_size")
        note = ""
        if self.ref_size and tuple(self.ref_size) != self.cur_size and self.cur_size != (0, 0):
            note = f" (will scale {self.ref_size} -> {list(self.cur_size)})"
        print(f"  Loaded '{self.name}' with {len(self.steps)} steps{note}")

    # -- playback ---------------------------------------------------------
    def _run(self, steps, rows, loops):
        if not self.need_device():
            return
        if not self.case:
            self.case = CaseLog(CASE_DIR, self.serial or "unknown")
        ref = self.ref_size or list(self.cur_size)
        done = threading.Event()

        def emit(kind, *a):
            if kind == "step":
                print(f"    step {a[0] + 1}: {describe_step(a[1])}")
            elif kind == "loop":
                print(f"  --- loop {a[0]}" + (f"/{a[1]}" if a[1] else "") + " ---")
            elif kind == "log":
                print("    " + str(a[0]))
            elif kind == "finished":
                print("  finished: " + str(a[0]))
                done.set()

        runner = MacroRunner(self.adb, self.case, steps, emit=emit, rows=rows,
                             ref_size=ref, cur_size=list(self.cur_size), loops=loops,
                             stop_when_unchanged=getattr(self, "_unchanged", False),
                             show_frames=False, stop_on_error=True)
        runner.start()
        try:
            while not done.wait(0.2):
                pass
        except KeyboardInterrupt:
            print("\n  stopping...")
            runner.stop()
            done.wait(5)

    def run_all(self):
        if not self.steps:
            print("  No steps."); return
        print("  Repeat: [1] once  [N] number  [c] continuous (Ctrl+C stops)")
        mode = self._ask("Choice", "1").lower()
        self._unchanged = self._ask("Stop when screen stops changing? (y/n)", "n").lower().startswith("y")
        loops = 0 if mode == "c" else (int(mode) if mode.isdigit() else 1)
        self._run(self.steps, list(range(len(self.steps))), loops)

    def simulate_one(self):
        if not self.steps:
            print("  No steps."); return
        self.view_steps_plain()
        sel = self._ask("Simulate which step #")
        if sel.isdigit() and 1 <= int(sel) <= len(self.steps):
            i = int(sel) - 1
            self._unchanged = False
            self._run([self.steps[i]], [i], 1)

    def view_steps_plain(self):
        for i, s in enumerate(self.steps, 1):
            print(f"   {i:>2}. {s['type']:<11} {describe_step(s)}")

    # -- utilities --------------------------------------------------------
    def packages(self):
        if not self.need_device():
            return
        kw = self._ask("filter keyword (blank = all)", "")
        for p in self.adb.packages(kw):
            print("   " + p)

    def quick_pull(self):
        if not self.need_device():
            return
        remote = self._ask("Phone path", "/sdcard/DCIM")
        local = self._ask("Folder name under case/pulled", "pull")
        step = new_step("pull", remote=remote, local=local)
        self._run([step], [0], 1)

    # -- main loop --------------------------------------------------------
    def menu(self):
        print(r"""
  =========================================
     ADB MACRO STUDIO  -  headless CLI
  =========================================""")
        if not self.adb_path:
            print("  !! adb not found. Add platform-tools to PATH.")
        else:
            print(f"  adb: {self.adb_path}")
            if not find_scrcpy(self.adb_path):
                print("  (tip: install scrcpy next to adb for a live mirror while you record)")
        while True:
            dev = self.serial or "none"
            print(f"""
  Device: {dev} | Size: {self.cur_size[0]}x{self.cur_size[1]} | Steps: {len(self.steps)} | Macro: {self.name}
  ----------------------------------------------------------------
   1. Select / connect device        6. Load macro
   2. Record gestures (getevent)     7. Run macro (once/N/continuous)
   3. Add step manually              8. Simulate a single step once
   4. View / edit steps              9. Pull files (quick)
   5. Save macro                     u. List app packages
                                     0. Exit""")
            c = input("  select > ").strip().lower()
            try:
                if c == "1": self.select_device()
                elif c == "2": self.record()
                elif c == "3": self.add_step()
                elif c == "4": self.view_steps()
                elif c == "5": self.save()
                elif c == "6": self.load()
                elif c == "7": self.run_all()
                elif c == "8": self.simulate_one()
                elif c == "9": self.quick_pull()
                elif c == "u": self.packages()
                elif c == "0":
                    print("  bye."); break
                else:
                    print("  ?")
            except AdbError as e:
                print("  adb error: " + str(e))
            except KeyboardInterrupt:
                print("\n  (cancelled)")


def main():
    MACRO_DIR.mkdir(parents=True, exist_ok=True)
    CASE_DIR.mkdir(parents=True, exist_ok=True)
    Cli().menu()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  interrupted. bye.")
