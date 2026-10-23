#!/usr/bin/env python3
"""
ADB Macro Studio - GUI front-end
================================
A Tkinter desktop UI (stdlib only) driving the engine in adb_core.py.

What it gives you:
  * LIVE phone screen in the window (periodic screencap - read-only, nothing
    is written to the phone).
  * Click on the live screen to drop a TAP at the exact coordinate (no need to
    read the pointer-location overlay).
  * AUTO-RECORD real gestures from the phone (tap / long-press / swipe) via
    getevent.
  * Build a step list, edit / reorder, save it as a NAMED macro (JSON).
  * SIMULATE one step once with a live preview overlay, or RUN the whole macro
    once / N times / continuously / until the screen stops changing.
  * Cross-model replay: macros remember the resolution they were recorded at and
    are auto-scaled to whatever phone is connected now.
  * Every adb command + every pulled/saved file (with SHA-256) is logged to a
    per-device case folder for chain of custody.

Run:  python adb_gui.py
Needs: adb on PATH (or set the ADB_PATH environment variable), Python 3 with tkinter.
"""
from __future__ import annotations

import base64
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from pathlib import Path

import adb_core as core
from adb_core import (
    Adb, CaseLog, TouchRecorder, MacroRunner,
    STEP_FIELDS, COMMON_FIELDS, KEYCODES, COORD_TYPES,
    new_step, describe_step, save_macro, load_macro, list_macros,
    find_adb, now_iso, AdbError,
)
import queue as _queue
import whatsapp_batch as wb
import contacts_extract as ce


def _load_contacts_map(contacts_dir):
    """number(digits) -> display name, from the contacts CSVs extract_contacts wrote. Used to put
    saved contact names on reconstructed chats."""
    import csv as _csv
    import re as _re
    out = {}
    for fn in ("whatsapp_contacts.csv", "contacts.csv"):
        p = os.path.join(contacts_dir, fn)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for row in _csv.DictReader(f):
                    num = _re.sub(r"\D", "", row.get("number") or row.get("normalized_number") or "")
                    name = (row.get("name") or "").strip()
                    if num and name and num not in out:
                        out[num] = name
        except Exception:
            pass
    return out

BASE = Path(__file__).resolve().parent
MACRO_DIR = BASE / "macros"
CASE_DIR = BASE / "cases"
LOG_DIR = BASE / "logs"
DEBUG_LOG = LOG_DIR / "gui_session.log"
BATCH_LOG = LOG_DIR / "batch_session.log"
CANVAS_W, CANVAS_H = 290, 500
LIVE_INTERVAL_MS = 800
LIVE_BUSY_MS = 6000      # live view during a run: a full-resolution screenshot every 6 s, not 2.4 s -
#                          screenshots compete with the run's screen reads and heat a long run's phone
LOG_MAX_LINES = 3000     # on-screen log only (the log FILES keep everything): an unbounded text box
#                          slowed the window down over a long run


ACCENT = "#128c7e"            # WhatsApp teal
ACCENT_DARK = "#0b6b60"
BG = "#f4f6f8"


def apply_theme(root):
    """A clean, consistent look on every window (plain ttk, no extra packages)."""
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        return
    base = ("Segoe UI", 10)
    for cls in ("Text", "Listbox", "Entry"):          # plain tk widgets (ttk ones use the styles)
        root.option_add(f"*{cls}.Font", base)
    root.configure(background=BG)
    st.configure(".", background=BG, font=base)
    st.configure("TFrame", background=BG)
    st.configure("TLabel", background=BG, foreground="#1f2328")
    st.configure("Header.TLabel", font=("Segoe UI Semibold", 15), foreground="#0f172a")
    st.configure("Sub.TLabel", foreground="#5f6b7a")
    st.configure("TButton", padding=(10, 5), background="#ffffff", bordercolor="#d0d7de")
    st.map("TButton", background=[("active", "#eef2f6")])
    st.configure("Accent.TButton", padding=(14, 7), background=ACCENT, foreground="#ffffff",
                 bordercolor=ACCENT_DARK, font=("Segoe UI Semibold", 10))
    st.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#9bbfb9")])
    st.configure("Danger.TButton", foreground="#b42318")
    st.configure("Card.TLabelframe", background=BG, bordercolor="#d0d7de")
    st.configure("Card.TLabelframe.Label", background=BG, foreground="#334155",
                 font=("Segoe UI Semibold", 10))
    st.configure("TLabelframe", background=BG, bordercolor="#d0d7de")
    st.configure("TLabelframe.Label", background=BG, foreground="#334155", font=("Segoe UI Semibold", 10))
    st.configure("Treeview", rowheight=26, background="#ffffff", fieldbackground="#ffffff",
                 bordercolor="#d0d7de")
    st.configure("Treeview.Heading", font=("Segoe UI Semibold", 10), background="#e9eef3")
    st.map("Treeview", background=[("selected", "#cfe8e4")], foreground=[("selected", "#0f172a")])
    st.configure("TCheckbutton", background=BG)
    st.configure("TRadiobutton", background=BG)


def _px(widget, n):
    """`n` design pixels (at 100% display scaling) in real pixels on this display."""
    try:
        return int(n * widget.winfo_fpixels("1i") / 96.0)
    except Exception:
        return n


def _fit(widget, w, h):
    """A geometry of w x h design pixels, scaled to the display and kept inside the screen."""
    sw, sh = widget.winfo_screenwidth(), widget.winfo_screenheight()
    return f"{min(_px(widget, w), sw - 40)}x{min(_px(widget, h), sh - 110)}"


def _append_capped(widget, line):
    widget.config(state="normal")
    widget.insert("end", line + "\n")
    extra = int(widget.index("end-1c").split(".")[0]) - LOG_MAX_LINES
    if extra > 0:
        widget.delete("1.0", f"{extra + 1}.0")
    widget.see("end")
    widget.config(state="disabled")


# ------------------------------------------------------------------ step dialog
class StepDialog(tk.Toplevel):
    """Modal form to create or edit one step. Returns dict via self.result."""

    def __init__(self, master, step_type=None, step=None, app=None):
        super().__init__(master)
        self.title("Edit step" if step else "Add step")
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        self._vars = {}
        self.app = app                 # gives access to the connected device
        self._caprec = None
        self._capq = _queue.Queue()

        self.type_var = tk.StringVar(value=step["type"] if step else (step_type or "tap"))
        row = 0
        ttk.Label(self, text="Type").grid(row=row, column=0, sticky="e", padx=6, pady=4)
        type_box = ttk.Combobox(self, textvariable=self.type_var,
                                values=list(STEP_FIELDS.keys()), state="readonly", width=18)
        type_box.grid(row=row, column=1, sticky="w", padx=6, pady=4)
        if step:  # can't change type while editing
            type_box.configure(state="disabled")
        type_box.bind("<<ComboboxSelected>>", lambda e: self._build_fields())

        self.fields_frame = ttk.Frame(self)
        self.fields_frame.grid(row=1, column=0, columnspan=2, sticky="ew")
        self._existing = step
        self._build_fields()

        btns = ttk.Frame(self)
        btns.grid(row=2, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="left", padx=4)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # center on the parent window and keep fully on-screen
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = master.winfo_rootx(), master.winfo_rooty()
        pw, ph = master.winfo_width(), master.winfo_height()
        x = max(0, min(px + (pw - w) // 2, self.winfo_screenwidth() - w))
        y = max(0, min(py + (ph - h) // 3, self.winfo_screenheight() - h))
        self.geometry(f"+{x}+{y}")
        self.lift()
        self.focus_force()

        self.grab_set()
        self.wait_window(self)

    def _build_fields(self):
        for w in self.fields_frame.winfo_children():
            w.destroy()
        self._vars = {}
        t = self.type_var.get()
        specs = STEP_FIELDS.get(t, []) + COMMON_FIELDS
        for i, (key, label, default) in enumerate(specs):
            val = (self._existing or {}).get(key, default)
            ttk.Label(self.fields_frame, text=label).grid(row=i, column=0, sticky="e", padx=6, pady=3)
            var = tk.StringVar(value=str(val))
            ttk.Entry(self.fields_frame, textvariable=var, width=32).grid(
                row=i, column=1, sticky="w", padx=6, pady=3)
            self._vars[key] = (var, default)

        # for tap/long_press/swipe, offer to fill X/Y by tapping the phone
        if t in COORD_TYPES and self.app and getattr(self.app, "adb", None):
            r = len(specs)
            self.cap_btn = ttk.Button(self.fields_frame,
                                      text="📍  Tap the phone to fill X/Y",
                                      command=self._capture)
            self.cap_btn.grid(row=r, column=0, columnspan=2, pady=(8, 2), padx=6, sticky="ew")
            self.cap_status = ttk.Label(self.fields_frame, text="", foreground="#0a7")
            self.cap_status.grid(row=r + 1, column=0, columnspan=2, padx=6)

    def _capture(self):
        """Start listening for one phone tap; fill X/Y (and X2/Y2) when it lands."""
        if not (self.app and getattr(self.app, "adb", None)):
            return
        w, h = self.app.cur_size
        self.cap_btn.config(text="Tap the phone now...", state="disabled")
        self.cap_status.config(text="waiting for a tap on the phone", foreground="#c60")
        try:
            self._caprec = TouchRecorder(
                self.app.adb, w, h,
                on_gesture=lambda g: self._capq.put(g),
                on_error=lambda m: self._capq.put({"__err__": m}))
            self._caprec.start()
        except AdbError as e:
            self._caprec = None
            self.cap_btn.config(text="📍  Tap the phone to fill X/Y", state="normal")
            self.cap_status.config(text=f"error: {e}", foreground="#c00")
            return
        self._poll_cap()

    def _poll_cap(self):
        try:
            g = self._capq.get_nowait()
        except _queue.Empty:
            self.after(100, self._poll_cap)
            return
        self._stop_cap()
        self.cap_btn.config(text="📍  Tap the phone to fill X/Y", state="normal")
        if "__err__" in g:
            self.cap_status.config(text=f"error: {g['__err__']}", foreground="#c00")
            return
        for key in ("x", "y", "x2", "y2"):
            if key in self._vars and g.get(key) is not None:
                self._vars[key][0].set(str(g[key]))
        self.cap_status.config(text=f"captured ({g.get('x')}, {g.get('y')})", foreground="#0a7")

    def _stop_cap(self):
        if self._caprec:
            try:
                self._caprec.stop()
            except Exception:
                pass
            self._caprec = None

    def _cancel(self):
        self._stop_cap()
        self.destroy()

    def _ok(self):
        self._stop_cap()
        t = self.type_var.get()
        values = {}
        for key, (var, default) in self._vars.items():
            raw = var.get().strip()
            try:
                if isinstance(default, bool):
                    values[key] = raw.lower() in ("1", "true", "yes", "y")
                elif isinstance(default, int):
                    values[key] = int(float(raw)) if raw else default
                elif isinstance(default, float):
                    values[key] = float(raw) if raw else default
                else:
                    values[key] = raw
            except ValueError:
                messagebox.showerror("Bad value", f"'{key}' must be a number.", parent=self)
                return
        try:
            self.result = new_step(t, **values)
        except ValueError as e:
            messagebox.showerror("Invalid step", str(e), parent=self)
            return
        self.destroy()


# ------------------------------------------------------------------ sequence dialog
class SequenceDialog(tk.Toplevel):
    """Pick saved actions (macros) in an order and chain them into one sequence macro."""

    def __init__(self, master, app):
        super().__init__(master)
        self.title("Build sequence from actions")
        self.transient(master)
        self.resizable(False, False)
        self.app = app
        self.result = None
        self._actions = list_macros(MACRO_DIR)          # [(path, macro), ...]
        self._chosen_names = []

        top = ttk.Frame(self, padding=8)
        top.pack(fill="both", expand=True)
        ttk.Label(top, text="Saved actions").grid(row=0, column=0, padx=4)
        ttk.Label(top, text="Sequence (runs top → bottom)").grid(row=0, column=2, padx=4)

        self.avail = tk.Listbox(top, height=12, width=28, exportselection=False)
        for _, m in self._actions:
            tag = " [seq]" if m.get("is_sequence") else ""
            self.avail.insert("end", f"{m['name']}  ({len(m.get('steps', []))} steps){tag}")
        self.avail.grid(row=1, column=0, rowspan=6, padx=4)
        self.avail.bind("<Double-Button-1>", lambda e: self._add())

        midb = ttk.Frame(top)
        midb.grid(row=1, column=1, rowspan=6, padx=4)
        ttk.Button(midb, text="Add →", width=10, command=self._add).pack(pady=3)
        ttk.Button(midb, text="← Remove", width=10, command=self._remove).pack(pady=3)
        ttk.Button(midb, text="Up", width=10, command=lambda: self._move(-1)).pack(pady=3)
        ttk.Button(midb, text="Down", width=10, command=lambda: self._move(1)).pack(pady=3)

        self.chosen = tk.Listbox(top, height=12, width=28, exportselection=False)
        self.chosen.grid(row=1, column=2, rowspan=6, padx=4)

        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")
        ttk.Label(bottom, text="Sequence name").pack(side="left")
        self.name_var = tk.StringVar(value="sequence1")
        ttk.Entry(bottom, textvariable=self.name_var, width=20).pack(side="left", padx=6)
        ttk.Button(bottom, text="Save sequence", command=self._save).pack(side="left", padx=4)
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="left")

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = master.winfo_rootx(), master.winfo_rooty()
        pw, ph = master.winfo_width(), master.winfo_height()
        x = max(0, min(px + (pw - w) // 2, self.winfo_screenwidth() - w))
        y = max(0, min(py + (ph - h) // 3, self.winfo_screenheight() - h))
        self.geometry(f"+{x}+{y}")
        self.lift()
        self.grab_set()
        if not self._actions:
            messagebox.showinfo(
                "No saved actions yet",
                "First record some taps, type a name (e.g. action1), and click 'Save action'.\n"
                "Do that for each action, then come back here to chain them into a sequence.",
                parent=self)
        self.wait_window(self)

    def _add(self):
        sel = self.avail.curselection()
        if not sel:
            return
        name = self._actions[sel[0]][1]["name"]
        self._chosen_names.append(name)
        self.chosen.insert("end", name)

    def _remove(self):
        sel = self.chosen.curselection()
        if not sel:
            return
        self.chosen.delete(sel[0])
        self._chosen_names.pop(sel[0])

    def _move(self, d):
        sel = self.chosen.curselection()
        if not sel:
            return
        i = sel[0]
        j = i + d
        if 0 <= j < len(self._chosen_names):
            self._chosen_names[i], self._chosen_names[j] = self._chosen_names[j], self._chosen_names[i]
            self.chosen.delete(0, "end")
            for n in self._chosen_names:
                self.chosen.insert("end", n)
            self.chosen.selection_set(j)

    def _save(self):
        if not self._chosen_names:
            messagebox.showinfo("Empty", "Add at least one action to the sequence.", parent=self)
            return
        by_name = {m["name"]: m for _, m in self._actions}
        steps, ref = [], None
        for n in self._chosen_names:
            m = by_name.get(n)
            if not m:
                continue
            ref = ref or m.get("ref_size")
            steps.extend(m.get("steps", []))
        seq = {
            "name": self.name_var.get().strip() or "sequence1",
            "created": now_iso(),
            "is_sequence": True,
            "components": list(self._chosen_names),
            "ref_size": ref or (list(self.app.cur_size) if self.app.cur_size != (0, 0) else None),
            "steps": steps,
        }
        save_macro(MACRO_DIR, seq)
        self.result = seq
        messagebox.showinfo(
            "Sequence saved",
            f"'{seq['name']}' saved: {len(steps)} steps from "
            f"{len(self._chosen_names)} action(s).\nIt's now loaded - press 'Run macro' to play it.",
            parent=self)
        self.destroy()


# ------------------------------------------------------------------ batch export window
class BatchExportWindow(tk.Toplevel):
    """Scan the WhatsApp chat list, pick chats, export them one by one with live progress."""

    STATUS_TAGS = {"ok": "#1b873f", "running": "#1565c0", "pending": "#7a7f87",
                   "skipped": "#9aa0a6", "blocked-privacy": "#b26a00"}

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("WhatsApp export")
        self.geometry(_fit(self, 820, 800))
        self.minsize(min(_px(self, 720), self.winfo_screenwidth() - 40),
                     min(_px(self, 600), self.winfo_screenheight() - 110))
        self.q = _queue.Queue()
        self.order = []           # full scanned chat order (top -> bottom)
        self.row_of = {}          # chat name -> tree item id
        self.batch = None
        self.scanning = False
        self.pause_evt = threading.Event()      # set = paused (shared by scan + batch)
        self._running_name = None               # chat currently being exported (for status display)
        self.scan_stop = threading.Event()

        head = ttk.Frame(self, padding=(12, 10, 12, 4))
        head.pack(fill="x")
        ttk.Label(head, text="WhatsApp export", style="Header.TLabel").pack(side="left")
        self.status_lbl = ttk.Label(head, text="ready - open WhatsApp on its chat list",
                                    style="Sub.TLabel")
        self.status_lbl.pack(side="left", padx=12)

        # the main action first: scan and export in one pass (a separate scan is optional)
        acts = ttk.Frame(self, padding=(12, 4))
        acts.pack(fill="x")
        ttk.Button(acts, text="Scan + Export all chats", style="Accent.TButton",
                   command=self.rolling_start).pack(side="left")
        self.start_btn = ttk.Button(acts, text="Export selected", command=self.start)
        self.start_btn.pack(side="left", padx=(8, 0))
        ttk.Button(acts, text="Export one chat...", command=self.export_one).pack(side="left", padx=(8, 0))
        self.scan_btn = ttk.Button(acts, text="Scan only (list chats)", command=self.scan)
        self.scan_btn.pack(side="right")
        ttk.Button(acts, text="Select all",
                   command=lambda: self.tree.selection_set(self.tree.get_children())).pack(side="right", padx=8)

        cfg = ttk.LabelFrame(self, text="Settings", padding=(10, 6), style="Card.TLabelframe")
        cfg.pack(fill="x", padx=12, pady=(8, 4))
        ttk.Label(cfg, text="PC (Quick Share) name").grid(row=0, column=0, sticky="w")
        self.pc_var = tk.StringVar(value=wb.PC_NAME)
        ttk.Entry(cfg, textvariable=self.pc_var, width=20).grid(row=0, column=1, sticky="w", padx=(6, 18))
        ttk.Label(cfg, text="App").grid(row=0, column=2, sticky="w")
        # which WhatsApp to export (only the installed ones are offered)
        apps = wb.installed_whatsapp(self.app.adb) if self.app.adb else []
        self._app_pkgs = {wb.WA_PACKAGES[pk]: pk for pk in (apps or ["com.whatsapp"])}
        self.app_var = tk.StringVar(value=next(iter(self._app_pkgs)))
        ttk.Combobox(cfg, textvariable=self.app_var, values=list(self._app_pkgs), state="readonly",
                     width=18).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(cfg, text="Quick Share save folder").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.dir_var = tk.StringVar(value=wb.SAVE_DIR)
        ttk.Entry(cfg, textvariable=self.dir_var, width=58).grid(row=1, column=1, columnspan=3, sticky="we",
                                                                 padx=6, pady=(6, 0))
        cfg.columnconfigure(3, weight=1)
        wb.label_asker = self._ask_label     # 'Export chat' in a language not seen before

        # resume / start-from controls (recover after a lock/crash without redoing work)
        cfg2 = ttk.LabelFrame(self, text="Resume", padding=(10, 6), style="Card.TLabelframe")
        cfg2.pack(fill="x", padx=12, pady=4)
        ttk.Label(cfg2, text="Start from chat").pack(side="left")
        self.startfrom_var = tk.StringVar(value="")
        ttk.Entry(cfg2, textvariable=self.startfrom_var, width=20).pack(side="left", padx=6)
        ttk.Button(cfg2, text="Use selected", command=self.start_from_selected).pack(side="left")
        self.resume_path = None
        ttk.Button(cfg2, text="Continue from a CSV...", command=self._pick_resume).pack(side="left", padx=(14, 0))
        self.resume_lbl = ttk.Label(cfg2, text="(automatic: this phone's last session)", style="Sub.TLabel")
        self.resume_lbl.pack(side="left", padx=6)
        ttk.Button(cfg2, text="clear", command=self._clear_resume).pack(side="left")

        mid = ttk.Frame(self, padding=(12, 6))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=("chat", "status"), show="headings",
                                 selectmode="extended", height=14)
        self.tree.heading("chat", text="Chat")
        self.tree.heading("status", text="Status")
        self.tree.column("chat", width=470)
        self.tree.column("status", width=150, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)
        tsb = ttk.Scrollbar(mid, command=self.tree.yview)
        tsb.pack(side="left", fill="y")
        self.tree.config(yscrollcommand=tsb.set)
        for st, col in self.STATUS_TAGS.items():
            self.tree.tag_configure(st, foreground=col)
        self.tree.tag_configure("fail", foreground="#c62828")

        pf = ttk.Frame(self, padding=(12, 4))
        pf.pack(fill="x")
        self.progress_lbl = ttk.Label(pf, text="idle", style="Sub.TLabel")
        self.progress_lbl.pack(side="left")
        ttk.Button(pf, text="Stop", style="Danger.TButton", command=self.stop).pack(side="right")
        self.pause_btn = ttk.Button(pf, text="Pause", command=self.toggle_pause)
        self.pause_btn.pack(side="right", padx=6)
        self.resume_btn = ttk.Button(pf, text="Retry pending / failed", command=self.resume_pending)
        self.resume_btn.pack(side="right")
        ttk.Button(pf, text="Save list to CSV...", command=self.save_csv).pack(side="right", padx=6)

        lf = ttk.LabelFrame(self, text="Progress log", padding=4, style="Card.TLabelframe")
        lf.pack(fill="both", expand=False, padx=12, pady=(4, 12))
        self.log = tk.Text(lf, height=6, wrap="word", relief="flat", font=("Consolas", 9),
                           background="#fbfbfc")
        self.log.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(lf, command=self.log.yview)
        lsb.pack(side="left", fill="y")
        self.log.config(yscrollcommand=lsb.set, state="disabled")

        if not self.app.adb:
            messagebox.showinfo("No device", "Connect a device in the main window first.", parent=self)
        self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _log(self, m):
        try:
            with open(BATCH_LOG, "a", encoding="utf-8") as f:
                f.write(f"{now_iso()}  {m}\n")
        except Exception:
            pass
        _append_capped(self.log, m)

    def _emit(self, kind, *a):          # called from worker threads
        self.q.put((kind, a))

    def _package(self):
        return self._app_pkgs.get(self.app_var.get(), "com.whatsapp")

    def _ask_label(self, key, options):
        """Called from the batch thread when 'Export chat' isn't known in the phone's language:
        ask the examiner (on the UI thread) and wait for the answer. None = not answered."""
        box = {"done": threading.Event(), "answer": None}
        self._label_box = box                # released by _close if the window goes away
        self.q.put(("ask_label", (key, options, box)))
        box["done"].wait()
        return box["answer"]

    def _show_label_question(self, key, options, box):
        dlg = tk.Toplevel(self)
        dlg.title("Teach the tool this language")
        dlg.transient(self)
        ttk.Label(dlg, padding=10, wraplength=420, justify="left",
                  text="The phone shows WhatsApp's menu in a language this tool hasn't seen yet.\n\n"
                       "Which of these items means 'Export chat'? (Look at the phone if unsure.) "
                       "It is remembered for this phone language, so you're only asked once.").pack()
        lb = tk.Listbox(dlg, height=min(10, len(options)), width=48, exportselection=False)
        for o in options:
            lb.insert("end", o)
        lb.pack(padx=10)

        def finish(answer):
            box["answer"] = answer
            box["done"].set()
            dlg.destroy()

        def use():
            sel = lb.curselection()
            if sel:
                finish(options[sel[0]])
        btns = ttk.Frame(dlg, padding=10)
        btns.pack(fill="x")
        ttk.Button(btns, text="This is 'Export chat'", command=use).pack(side="right")
        ttk.Button(btns, text="None of these - skip", command=lambda: finish(None)).pack(side="right", padx=6)
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(None))
        self._log("asking which menu item means 'Export chat' (phone language not seen before)")

    def _tag(self, status):
        return status if status in self.STATUS_TAGS else ("fail" if status.startswith("fail") else "")

    def scan(self):
        if not self.app.adb:
            messagebox.showinfo("No device", "Connect a device first.", parent=self)
            return
        if self.scanning:
            return
        self.scanning = True
        self.scan_btn.config(state="disabled")
        self.status_lbl.config(text="scanning the chat list...")
        self._log("scanning the chat list...")
        self.tree.delete(*self.tree.get_children())
        self.row_of.clear()
        self.scan_stop.clear()
        self.pause_evt.clear()
        self.pause_btn.config(text="Pause")

        if not self.app.case:
            self.app.case = CaseLog(CASE_DIR, self.app.serial or "unknown")
        case = self.app.case

        def work():
            try:
                names = wb.scan_chats(self.app.adb, self.app.cur_size, emit=self._emit,
                                      pause=self.pause_evt, stop=self.scan_stop)
                if names:                    # the scanned list is evidence too: keep it in the case
                    out = wb.save_names_csv([(n, "scanned") for n in names],
                                            Path(case.dir) / f"scanned_chats_{core.now_stamp()}.csv")
                    case.record_file(Path(out), "chat list scan")
                    self._emit("log", f"scan saved in the case folder: {Path(out).name}")
                self._emit("scan_done", names)
            except Exception as e:
                self._emit("log", f"scan error: {e}")
                self._emit("scan_done", [])
        threading.Thread(target=work, daemon=True).start()

    def toggle_pause(self):
        if self.pause_evt.is_set():
            self.pause_evt.clear()
            self.pause_btn.config(text="Pause")
            run = getattr(self, "_running_name", None)
            self.status_lbl.config(text=(f"▶ RUNNING: {run}" if run else "▶ running"),
                                   foreground="#06c")
            self._log("RESUMED")
        else:
            self.pause_evt.set()
            self.pause_btn.config(text="Resume")
            run = getattr(self, "_running_name", None)
            self.status_lbl.config(
                text=(f"⏸ PAUSED (holding at '{run}')" if run else "⏸ PAUSED"),
                foreground="#c60")
            self._log("PAUSE pressed - holding after the current step "
                      "(a transfer in flight finishes first)")

    def resume_pending(self):
        pend = [iid for iid in self.tree.get_children()
                if self.tree.set(iid, "status") == "pending"
                or self.tree.set(iid, "status").startswith("fail")]
        if not pend:
            messagebox.showinfo("Nothing pending", "No pending or failed chats to resume.", parent=self)
            return
        self.tree.selection_set(pend)
        self.start()

    def _pick_resume(self):
        from tkinter import filedialog
        init = ""
        try:
            init = str(Path(self.app.case.dir).parent) if self.app.case else ""
        except Exception:
            pass
        p = filedialog.askopenfilename(parent=self, title="Pick a prior exported_chats.csv to resume from",
                                       initialdir=init, filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not p:
            return
        # LOAD the CSV now so its results are visible in the list and a start-chat can be selected.
        # This works for a CSV from ANOTHER instance/PC too - we load whatever chats+statuses it has.
        try:
            st, od, fl, tm = wb.load_progress(p)
        except Exception as e:
            messagebox.showerror("Resume load failed", f"Could not read:\n{p}\n\n{e}", parent=self)
            return
        self.resume_path = p
        self.resume_lbl.config(text="resume: " + Path(p).name, foreground="#0a7")
        self.tree.delete(*self.tree.get_children())
        self.row_of.clear()
        self.order = []
        for nm in od:
            status = st.get(nm) or "pending"
            iid = self.tree.insert("", "end", values=(nm, status), tags=(self._tag(status),))
            self.row_of[nm] = iid
            self.order.append(nm)
        done = sum(1 for nm in od if st.get(nm) == "ok")
        self.status_lbl.config(
            text=f"loaded {len(od)} from CSV ({done} already ok) - select a chat, then "
                 "'Start from selected'")
        self._log(f"resume CSV loaded: {len(od)} chats, {done} already ok  <-  {p}")

    def _clear_resume(self):
        self.resume_path = None
        self.resume_lbl.config(text="(automatic: this phone's last session)", foreground="#5f6b7a")

    def rolling_start(self, only=None):
        """Auto page-by-page: scan a screenful, export the undone ones, scroll, repeat. Writes CSV."""
        if not self.app.adb:
            messagebox.showinfo("No device", "Connect a device first.", parent=self)
            return
        if self.batch and self.batch.is_alive():
            messagebox.showinfo("Busy", "A run is already in progress.", parent=self)
            return
        if not self.app.case:
            self.app.case = CaseLog(CASE_DIR, self.app.serial or "unknown")
        # capture the scan state (order + selection) BEFORE clearing the tree
        scan_order = [self.tree.item(iid, "values")[0] for iid in self.tree.get_children()]
        sel_names = set(only) if only else {self.tree.item(iid, "values")[0] for iid in self.tree.selection()}
        startf = None if only else (self.startfrom_var.get().strip() or None)
        # SAME-DEVICE CONTINUITY: the picked CSV, or THIS device's most recent one, is READ so its
        # finished chats are skipped - but progress is always written into the CURRENT case folder
        # (next to this session's contacts and exports); the earlier CSV is left untouched.
        resume, csv_out = wb.plan_csv(self.app.case.dir, str(CASE_DIR), self.app.serial,
                                      self._package(), picked=self.resume_path)
        if resume and Path(resume) != Path(csv_out):
            self._log(f"continuing this device's earlier progress from: {resume}")
        self._log(f"CSV for this session: {csv_out}")
        self.tree.delete(*self.tree.get_children())
        self.row_of.clear()
        self.order = []
        self.pause_evt.clear()
        self.pause_btn.config(text="Pause")
        if getattr(self.app, "_live_pause", None):
            self.app._live_pause.set()
        self.start_btn.config(state="disabled")
        self.batch = wb.RollingBatch(self.app.adb, self.app.case, self.app.cur_size,
                                     self._emit, pause=self.pause_evt, csv_path=csv_out,
                                     pc_name=self.pc_var.get().strip() or wb.PC_NAME,
                                     save_dir=self.dir_var.get().strip() or wb.SAVE_DIR,
                                     resume_from=resume,
                                     start_from=startf, package=self._package())
        self.batch.scan_order = scan_order or None     # lets start-from go directionally (up/down)
        # start-from takes precedence; otherwise a selection means "export only these"
        if startf:
            self._log(f"AUTO: starting from '{startf}' (scrolling to it, exporting from there down)")
        elif sel_names:
            self.batch.only_names = sel_names
            self._log(f"AUTO: exporting ONLY the {len(sel_names)} selected chat(s)")
        else:
            self._log("AUTO scan + export starting (page by page)")
        self._log(f"CSV file: {self.batch.csv_path}")
        self.batch.start()

    def start_from_selected(self):
        """Take the chat selected in the list and run Auto starting from it (directional scroll)."""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select a chat", "Scan first, then select the chat to start from.",
                                parent=self)
            return
        self.startfrom_var.set(self.tree.item(sel[0], "values")[0])
        self.rolling_start()

    def save_csv(self):
        rows = [(self.tree.item(iid, "values")[0], self.tree.item(iid, "values")[1])
                for iid in self.tree.get_children()]
        if not rows:
            messagebox.showinfo("Empty", "Scan the chat list first (nothing to save).", parent=self)
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
                                            filetypes=[("CSV files", "*.csv")],
                                            initialfile="whatsapp_chats.csv")
        if not path:
            return
        try:
            wb.save_names_csv(rows, path)
            self._log(f"saved chat list ({len(rows)} rows) -> {path}")
            messagebox.showinfo("Saved", f"Chat list saved to:\n{path}", parent=self)
        except Exception as e:
            messagebox.showerror("Save failed", str(e), parent=self)

    def start(self):
        """Export the selected chats. Same engine as Auto (verified scrolling, Archived chats, the
        end-of-run retry), limited to the selection."""
        if not self.tree.selection():
            messagebox.showinfo("Pick chats", "Select one or more chats first (Ctrl/Shift-click, or Select all).", parent=self)
            return
        self.rolling_start()

    def export_one(self):
        """Export ONE chat by name (e.g. one that failed): finds it in the chat list or the Archived
        chats, exports it, and stops."""
        sel = self.tree.selection()
        guess = self.tree.item(sel[0], "values")[0] if len(sel) == 1 else ""
        name = simpledialog.askstring("Export one chat", "Chat name exactly as WhatsApp shows it:",
                                      initialvalue=guess, parent=self)
        if name and name.strip():
            self.rolling_start(only={name.strip()})

    def stop(self):
        self.scan_stop.set()                 # stop a running scan
        if self.batch:
            self.batch.stop()                # stop the batch AND its current export
        self._log("STOP pressed - halting now...")

    def _pump(self):
        try:
            while True:
                kind, a = self.q.get_nowait()
                if kind == "ask_label":
                    self._show_label_question(*a)
                elif kind == "scan_progress":
                    self.status_lbl.config(text=f"scanning... {a[0]} chats so far")
                elif kind == "scan_done":
                    self.order = a[0]
                    for nm in self.order:
                        iid = self.tree.insert("", "end", values=(nm, "pending"), tags=("pending",))
                        self.row_of[nm] = iid
                    self.status_lbl.config(text=f"{len(self.order)} chats - select some and click Export selected")
                    self._log(f"scan done: {len(self.order)} chats found")
                    self.scanning = False
                    self.scan_btn.config(state="normal")
                elif kind == "discover":
                    name = a[0]
                    if name not in self.row_of:
                        iid = self.tree.insert("", "end", values=(name, "pending"), tags=("pending",))
                        self.row_of[name] = iid
                        self.order.append(name)
                        self.tree.see(iid)
                elif kind == "row":
                    name, status = a
                    iid = self.row_of.get(name)
                    if iid:
                        self.tree.set(iid, "status", status)
                        self.tree.item(iid, tags=(self._tag(status),))
                        self.tree.see(iid)
                    self.progress_lbl.config(text=f"{name}  ->  {status}")
                    if status == "running":
                        self._running_name = name
                        if not self.pause_evt.is_set():
                            self.status_lbl.config(text=f"▶ RUNNING: {name}", foreground="#06c")
                elif kind == "progress":
                    i, total, name, status = a
                    self.progress_lbl.config(text=f"{i}/{total}: {name}  ->  {status}")
                    iid = self.row_of.get(name)
                    if iid:
                        self.tree.set(iid, "status", status)
                        self.tree.item(iid, tags=(self._tag(status),))
                        self.tree.see(iid)
                elif kind == "log":
                    self._log(a[0])
                elif kind == "finished":
                    summ = a[0]
                    if "csv" in summ:                      # rolling auto run
                        self.progress_lbl.config(text=f"done - {summ['exported']}/{summ['total']} exported")
                        if summ.get("stopped"):
                            where = summ.get("last") or "?"
                            self.status_lbl.config(text=f"■ STOPPED at '{where}' "
                                                   f"({summ.get('pending', 0)} still pending)",
                                                   foreground="#c60")
                            self._log(f"STOPPED by user at '{where}'. {summ['exported']} exported, "
                                      f"{summ.get('pending', 0)} still pending. CSV: {summ['csv']}")
                        else:
                            self.status_lbl.config(text=f"✓ DONE - {summ['exported']}/{summ['total']} "
                                                   "exported", foreground="#0a7")
                            self._log(f"FINISHED (auto): {summ['exported']}/{summ['total']} exported. "
                                      f"CSV: {summ['csv']}")
                    else:
                        results = summ.get("results", [])
                        ok = sum(1 for _, s in results if s == "ok")
                        self.progress_lbl.config(text=f"done - {ok}/{len(results)} ok")
                        self._log(f"FINISHED: {ok}/{len(results)} exported OK. Last exported: {summ.get('last')}")
                    self.start_btn.config(state="normal")
                    if getattr(self.app, "_live_pause", None):
                        self.app._live_pause.clear()
        except _queue.Empty:
            pass
        except Exception as _e:                 # one bad event must not freeze the UI
            try:
                self._log(f"pump error (skipped): {_e}")
            except Exception:
                pass
        self.after(100, self._pump)

    def _close(self):
        if wb.label_asker == self._ask_label:
            wb.label_asker = None
        box = getattr(self, "_label_box", None)
        if box:
            box["done"].set()                # never leave the batch thread waiting on a closed window
        if self.batch:
            self.batch.stop()
        self.scan_stop.set()
        try:
            wb.keep_awake(self.app.adb, False)      # restore normal screen timeout
        except Exception:
            pass
        if getattr(self.app, "_live_pause", None):
            self.app._live_pause.clear()
        self.destroy()


# ------------------------------------------------------------------ main app
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("ADB Macro Studio")
        root.geometry(_fit(root, 1120, 760))
        root.minsize(min(_px(root, 940), root.winfo_screenwidth() - 40),
                     min(_px(root, 600), root.winfo_screenheight() - 110))

        self.q: queue.Queue = queue.Queue()
        self.adb_path = find_adb()
        self.adb: Adb | None = None
        self.serial: str | None = None
        self.cur_size = (0, 0)          # this device's resolution
        self.macro_ref_size = None      # resolution the loaded macro was recorded at
        self.case: CaseLog | None = None
        self.steps: list[dict] = []
        self.recorder: TouchRecorder | None = None
        self.runner: MacroRunner | None = None
        self._armed = False                       # one-tap capture armed?
        self._oneshot_rec: TouchRecorder | None = None

        self._base_img = None           # keep refs so Tk doesn't GC images
        self._disp_img = None
        self.view = None                # {f, x0, y0, w, h} for click->device mapping
        self.macro_paths = {}           # display name -> Path

        self._build_ui()
        if not self.adb_path:
            self._log("!! adb not found. Add platform-tools to PATH.")
        else:
            self._log(f"adb: {self.adb_path}")
            self.refresh_devices()
        self._live_stop = threading.Event()
        self._live_pause = threading.Event()  # set = paused
        self.root.after(80, self._pump)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI construction -------------------------------------------------
    def _build_ui(self):
        head = ttk.Frame(self.root, padding=(12, 10, 12, 0))
        head.pack(side="top", fill="x")
        ttk.Label(head, text="ADB Macro Studio", style="Header.TLabel").pack(side="left")
        ttk.Label(head, text="offline WhatsApp evidence collection", style="Sub.TLabel").pack(side="left", padx=10)
        ttk.Button(head, text="WhatsApp export...", style="Accent.TButton",
                   command=self.open_batch_export).pack(side="right")
        top = ttk.Frame(self.root, padding=(12, 8))
        top.pack(side="top", fill="x")
        ttk.Label(top, text="Device").pack(side="left")
        self.dev_var = tk.StringVar()
        self.dev_box = ttk.Combobox(top, textvariable=self.dev_var, width=34, state="readonly")
        self.dev_box.pack(side="left", padx=4)
        ttk.Button(top, text="Refresh", command=self.refresh_devices).pack(side="left")
        ttk.Button(top, text="Connect", command=self.connect).pack(side="left", padx=4)
        ttk.Button(top, text="Contacts + WhatsApp JIDs → case folder",
                   command=self.extract_contacts_to_folder).pack(side="left", padx=4)
        ttk.Button(top, text="Reconstruct from backup (read-only)...",
                   command=self.reconstruct_backup).pack(side="left", padx=4)
        self.info_lbl = ttk.Label(top, text="not connected")
        self.info_lbl.pack(side="left", padx=10)
        self.live_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Live view", variable=self.live_var).pack(side="right")

        main = ttk.Panedwindow(self.root, orient="horizontal")

        # left: live canvas
        left = ttk.Frame(main, padding=6)
        self.canvas = tk.Canvas(left, width=CANVAS_W, height=CANVAS_H, bg="black",
                                highlightthickness=1, highlightbackground="#444")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.click_mode = tk.StringVar(value="off")
        cm = ttk.Frame(left)
        cm.pack(pady=4)
        ttk.Label(cm, text="Click live image to add:").pack(side="left")
        ttk.Radiobutton(cm, text="off", value="off", variable=self.click_mode).pack(side="left")
        ttk.Radiobutton(cm, text="add tap", value="tap", variable=self.click_mode).pack(side="left")
        ttk.Radiobutton(cm, text="pick element", value="pick", variable=self.click_mode).pack(side="left")
        main.add(left, weight=0)

        # right: controls
        right = ttk.Frame(main, padding=6)
        main.add(right, weight=1)

        stepf = ttk.LabelFrame(right, text="Steps  (Ctrl/Shift-click to select several, then 'Save as action')",
                               padding=6)
        lower = ttk.Frame(right)                  # record/actions/playback keep their full size;
        lower.pack(side="bottom", fill="x")       # the step list takes whatever height is left
        stepf.pack(fill="both", expand=True)
        self.step_list = tk.Listbox(stepf, height=8, activestyle="dotbox", selectmode="extended")
        self.step_list.pack(side="left", fill="both", expand=True)
        self.step_list.bind("<Double-Button-1>", lambda e: self.edit_step())
        sb = ttk.Scrollbar(stepf, command=self.step_list.yview)
        sb.pack(side="left", fill="y")
        self.step_list.config(yscrollcommand=sb.set)
        sbtn = ttk.Frame(stepf)
        sbtn.pack(side="left", fill="y", padx=4)
        ttk.Button(sbtn, text="Save as action", command=self.save_action).grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(2, 8))
        for i, (txt, cmd) in enumerate((("Add...", self.add_step), ("Edit", self.edit_step),
                                        ("Delete", self.del_step), ("Clear", self.clear_steps),
                                        ("Up", lambda: self.move_step(-1)),
                                        ("Down", lambda: self.move_step(1)))):
            ttk.Button(sbtn, text=txt, width=8, command=cmd).grid(row=1 + i // 2, column=i % 2,
                                                                  padx=2, pady=2, sticky="ew")

        recf = ttk.LabelFrame(lower, text="Record from phone", padding=6)
        recf.pack(fill="x", pady=4)
        self.cap_btn = ttk.Button(recf, text="Capture one tap", command=self.arm_capture)
        self.cap_btn.pack(side="left")
        self.rec_btn = ttk.Button(recf, text="Auto capture (until Stop)", command=self.toggle_record)
        self.rec_btn.pack(side="left", padx=4)
        self.rec_lbl = ttk.Label(recf, text="idle")
        self.rec_lbl.pack(side="left", padx=8)

        macf = ttk.LabelFrame(lower, text="Actions / Sequences", padding=6)
        macf.pack(fill="x", pady=4)
        ttk.Label(macf, text="Name").grid(row=0, column=0, sticky="e")
        self.name_var = tk.StringVar(value="action1")
        ttk.Entry(macf, textvariable=self.name_var, width=22).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Button(macf, text="Save action", command=self.save).grid(row=0, column=2, padx=4)
        self.macro_box = ttk.Combobox(macf, width=22, state="readonly")
        self.macro_box.grid(row=1, column=1, sticky="w", padx=4, pady=4)
        ttk.Button(macf, text="Load", command=self.load).grid(row=1, column=2, padx=4)
        ttk.Button(macf, text="List", command=self.refresh_macros).grid(row=1, column=0, padx=4)
        ttk.Button(macf, text="Build sequence from actions...",
                   command=self.build_sequence).grid(row=2, column=0, columnspan=3,
                                                      pady=(6, 0), sticky="ew")
        ttk.Button(macf, text="WhatsApp batch export...",
                   command=self.open_batch_export).grid(row=3, column=0, columnspan=3,
                                                        pady=(4, 0), sticky="ew")

        playf = ttk.LabelFrame(lower, text="Playback", padding=6)
        playf.pack(fill="x", pady=4)
        ttk.Label(playf, text="Loops").grid(row=0, column=0, sticky="e")
        self.loops_var = tk.IntVar(value=1)
        ttk.Spinbox(playf, from_=1, to=9999, textvariable=self.loops_var, width=6).grid(row=0, column=1, sticky="w")
        self.cont_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(playf, text="continuous", variable=self.cont_var).grid(row=0, column=2, padx=6)
        self.unchanged_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(playf, text="stop when screen unchanged",
                        variable=self.unchanged_var).grid(row=0, column=3, padx=6)
        ttk.Button(playf, text="Simulate selected once",
                   command=self.simulate_selected).grid(row=1, column=0, columnspan=2, pady=6, sticky="w")
        ttk.Button(playf, text="Run macro", command=self.run_all).grid(row=1, column=2, pady=6)
        ttk.Button(playf, text="Stop", command=self.stop_run).grid(row=1, column=3, pady=6)

        logf = ttk.LabelFrame(self.root, text="Log", padding=4)
        logf.pack(side="bottom", fill="x", padx=12, pady=(0, 10))   # always visible
        main.pack(fill="both", expand=True)
        self.log_txt = tk.Text(logf, height=4, wrap="word", relief="flat", font=("Consolas", 9),
                               background="#fbfbfc")
        self.log_txt.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(logf, command=self.log_txt.yview)
        lsb.pack(side="left", fill="y")
        self.log_txt.config(yscrollcommand=lsb.set, state="disabled")

    # ---- logging ---------------------------------------------------------
    def _log(self, msg: str):
        line = f"{now_iso()}  {msg}"
        try:
            with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        _append_capped(self.log_txt, line)

    def _adblog(self, msg):          # called from any thread
        self.q.put(("log", msg))

    # ---- devices ---------------------------------------------------------
    def refresh_devices(self):
        if not self.adb_path:
            return
        try:
            devs = Adb(self.adb_path).devices()
        except AdbError as e:
            self._log(f"!! {e}")
            return
        labels = [core.device_label(d) for d in devs]
        self.dev_box["values"] = labels
        self._devs = devs
        if labels and not self.dev_var.get():
            self.dev_box.current(0)
        self._log(f"{len(devs)} device(s) found")

    def connect(self):
        idx = self.dev_box.current()
        if idx < 0:
            messagebox.showinfo("No device", "Refresh and pick a device first.")
            return
        d = self._devs[idx]
        if d["state"] != "device":
            messagebox.showwarning("Not ready", f"Device state is '{d['state']}'. "
                                   "Accept the USB-debugging prompt on the phone.")
            return
        self.serial = d["serial"]
        self.adb = Adb(self.adb_path, self.serial, log=self._adblog)
        try:
            info = self.adb.device_info()
        except AdbError as e:
            self._log(f"!! {e}")
            return
        self.cur_size = (info["width"], info["height"])
        self.case = CaseLog(CASE_DIR, self.serial)
        self.case.write(f"connected {info}")
        self.info_lbl.config(
            text=f"{info['manufacturer']} {info['model']} | Android {info['android']} "
                 f"| {info['width']}x{info['height']} | {info['device']}")
        self._log(f"connected: {info['model']} ({info['width']}x{info['height']})")
        self._log(f"case folder: {self.case.dir}")
        self.refresh_macros()
        self._start_live()
        self._extract_contacts_async()        # once ADB is ready: pull contacts + WhatsApp JIDs

    def extract_contacts_to_folder(self):
        """Button: extract contacts.csv + whatsapp_contacts.csv (names with WhatsApp JIDs) into THIS
        session's case folder, hashed in its hashes.csv - next to the chat CSV and exports."""
        if not self.adb:
            messagebox.showinfo("No device", "Connect a device first.")
            return
        if not self.case:
            self.case = CaseLog(CASE_DIR, self.serial or "unknown")
        adb, case = self.adb, self.case
        folder = str(case.dir)

        def work():
            try:
                self._adblog(f"contacts: extracting into the case folder {folder} ...")
                res = ce.extract_contacts(adb, folder, log=self._adblog, case=case)
                names = ", ".join(os.path.basename(f) for f in res["files"])
                self._adblog(f"contacts: done - {res['contacts']} phone contacts, "
                             f"{res['whatsapp']} WhatsApp contacts -> {names or '(no files)'}")
            except Exception as e:
                self._adblog(f"contacts: extraction error: {e!r}")

        threading.Thread(target=work, daemon=True).start()

    def reconstruct_backup(self):
        """Read-only full extraction: pull/decrypt WhatsApp's own backup and rebuild every chat as a
        WhatsApp-export .zip on the PC. Never writes to the phone. Works from the live phone, or from
        a .crypt15 (+ key) / already-decrypted msgstore.db you already have."""
        import wa_backup as wab
        key = None
        if self.adb and messagebox.askyesno(
                "Pull from phone?",
                "Pull WhatsApp's backup from the connected phone now?\n\n"
                "This is READ-ONLY - it only copies the backup off the phone and writes nothing to "
                "it.\n\nChoose 'No' to instead pick a .crypt15 or already-decrypted msgstore.db you "
                "already have."):
            src = "device"
        else:
            src = filedialog.askopenfilename(
                title="Pick a WhatsApp backup (.crypt15) or a decrypted msgstore.db",
                filetypes=[("WhatsApp backup / db", "*.crypt15 *.crypt14 *.db"), ("All files", "*.*")])
            if not src:
                return
            if src.endswith((".crypt15", ".crypt14", ".crypt12")):
                key = filedialog.askopenfilename(
                    title="Pick the 64-character backup key file (e.g. Wakey.txt)",
                    filetypes=[("Key file", "*.txt *.key"), ("All files", "*.*")])
                if not key:
                    messagebox.showinfo("Key needed", "A .crypt15 backup needs its backup key.")
                    return
        include_media = messagebox.askyesno(
            "Include media?", "Include media (photos, videos, documents) in the reconstructed "
            "chats?\n\nYes = self-contained evidence but larger and slower.\nNo = text only (fast).")
        out = filedialog.askdirectory(title="Choose an output folder for the reconstructed chats",
                                      initialdir=str(self.case.dir) if self.case else None)
        if not out:
            return

        adb = self.adb
        media_pick = None
        if include_media and src != "device":
            media_pick = filedialog.askdirectory(
                title="Pick the WhatsApp 'Media' folder (or its parent) - Cancel to skip media") or None
        package = "com.whatsapp"
        if src == "device":
            apps = wb.installed_whatsapp(adb)
            if apps == ["com.whatsapp.w4b"] or (len(apps) > 1 and messagebox.askyesno(
                    "Which WhatsApp?", "Both WhatsApp and WhatsApp Business are installed.\n\n"
                    "Yes = WhatsApp Business backup\nNo = WhatsApp backup")):
                package = "com.whatsapp.w4b"

        def work():
            try:
                db_path = None
                media_root = media_pick
                contacts = None
                if src == "device":
                    try:
                        cdir = os.path.join(out, "_contacts")
                        ce.extract_contacts(adb, cdir, log=self._adblog)
                        contacts = _load_contacts_map(cdir)
                    except Exception as e:
                        self._adblog(f"backup: contacts step skipped ({e!r})")
                    crypt, _dg = wab.pull_backup(adb, out, emit=self._adblog, package=package)
                    if not crypt:
                        return
                    if include_media:
                        media_root = wab.pull_media(adb, out, emit=self._adblog, package=package)
                    db_path = os.path.join(out, "msgstore.db")
                    self._adblog("backup: pick the 64-char backup key file for THIS device...")
                    kpick = filedialog.askopenfilename(title="Backup key file (e.g. Wakey.txt)")
                    if not kpick or not wab.decrypt_backup(crypt, kpick, db_path, emit=self._adblog):
                        return
                elif src.endswith((".crypt15", ".crypt14", ".crypt12")):
                    db_path = os.path.join(out, "msgstore.db")
                    if not wab.decrypt_backup(src, key, db_path, emit=self._adblog):
                        return
                else:
                    db_path = src                       # already a decrypted msgstore.db

                self._adblog("backup: reconstructing chats (read-only)...")
                mans = wab.reconstruct(db_path, media_root, out, contacts=contacts,
                                       copy_media=include_media, emit=self._adblog)
                self._adblog(f"backup: DONE - {len(mans)} chats reconstructed into {out}")
                self._adblog('backup: view with  python chat_viewer.py "%s"' % out)
            except Exception as e:
                self._adblog(f"backup: error: {e!r}")

        threading.Thread(target=work, daemon=True).start()

    def _extract_contacts_async(self):
        """On connect, pull the phone's contacts and the WhatsApp contacts (with JID) into the case
        folder, in the background so it never blocks the UI. Non-fatal."""
        adb, case = self.adb, self.case
        if not adb or not case:
            return

        def work():
            try:
                # _adblog is the thread-safe logger (queues to the UI pump); never touch widgets here
                self._adblog("contacts: starting extraction (phone contacts + WhatsApp JIDs)...")
                res = ce.extract_contacts(adb, case.dir, log=self._adblog, case=case)
                self._adblog(f"contacts: done - {res['contacts']} phone contacts, "
                             f"{res['whatsapp']} WhatsApp contacts. Saved in the case folder.")
            except Exception as e:
                self._adblog(f"contacts: extraction error (non-fatal): {e!r}")

        threading.Thread(target=work, daemon=True).start()

    # ---- live view -------------------------------------------------------
    def _start_live(self):
        self._live_stop = threading.Event()
        self._live_pause = threading.Event()
        t = threading.Thread(target=self._live_loop, daemon=True)
        t.start()

    def _live_loop(self):
        streak = core.FailStreak()      # a disconnected phone logs once, not every frame
        while not self._live_stop.is_set():
            if self.live_var.get() and self.adb:
                # Keep MIRRORING even while a batch/scan is running (so you can watch what adb is
                # doing live) - just at a slower cadence then, to limit contention with the batch's
                # own adb traffic. 'Live view' unticked still turns it off entirely.
                busy = self._live_pause.is_set()
                try:
                    png = self.adb.screencap()
                    self.q.put(("liveframe", png))
                    msg = streak.ok("live view resumed")
                    if msg:
                        self.q.put(("log", msg))
                except Exception as e:
                    msg = streak.fail(f"live view paused - the phone is not answering ({e}). If it was "
                                      "unplugged: reconnect the USB cable, unlock it, press Refresh "
                                      "and Connect. Retrying quietly.")
                    if msg:
                        self.q.put(("log", msg))
                    self._live_stop.wait(2.0)
                self._live_stop.wait((LIVE_BUSY_MS if busy else LIVE_INTERVAL_MS) / 1000)
                continue
            self._live_stop.wait(LIVE_INTERVAL_MS / 1000)

    def _render_png(self, png: bytes):
        try:
            base = tk.PhotoImage(data=base64.b64encode(png))
        except Exception as e:
            self._log(f"render error: {e}")
            return
        w, h = base.width(), base.height()
        f = max(1, -(-w // CANVAS_W), -(-h // CANVAS_H))
        disp = base.subsample(f, f) if f > 1 else base
        self._base_img, self._disp_img = base, disp
        dw, dh = disp.width(), disp.height()
        x0, y0 = (CANVAS_W - dw) // 2, (CANVAS_H - dh) // 2
        self.view = {"f": f, "x0": x0, "y0": y0, "w": w, "h": h}
        self.canvas.delete("all")
        self.canvas.create_image(x0, y0, anchor="nw", image=disp)
        if not getattr(self, "_frame_logged", False):
            self._frame_logged = True
            self._log(f"live frame rendered {w}x{h} (subsample {f})")

    def _draw_preview(self, kind, points):
        if not self.view:
            return
        f, x0, y0 = self.view["f"], self.view["x0"], self.view["y0"]
        self.canvas.delete("ov")
        pts = [(x0 + px / f, y0 + py / f) for px, py in points]
        for cx, cy in pts:
            self.canvas.create_oval(cx - 10, cy - 10, cx + 10, cy + 10,
                                    outline="#00e5ff", width=3, tags="ov")
        if kind == "swipe" and len(pts) == 2:
            self.canvas.create_line(*pts[0], *pts[1], fill="#00e5ff", width=3, arrow="last", tags="ov")

    def _on_canvas_click(self, event):
        if not self.view:
            return
        f, x0, y0, w, h = (self.view[k] for k in ("f", "x0", "y0", "w", "h"))
        dx, dy = int((event.x - x0) * f), int((event.y - y0) * f)
        self._log(f"canvas click ({dx},{dy}) armed={self._armed} mode={self.click_mode.get()}")
        if not (0 <= dx < w and 0 <= dy < h):
            return
        if self._armed:                       # one-tap capture: this click is the point
            self._finish_oneshot(new_step("tap", x=dx, y=dy, label="tap"))
            return
        mode = self.click_mode.get()
        if mode == "off":
            return
        if mode == "tap":
            self.steps.append(new_step("tap", x=dx, y=dy, label="tap"))
            self._refresh_steps()
            self._log(f"added tap ({dx}, {dy}) from click")
        elif mode == "pick":
            if not self.adb:
                return
            self._log(f"reading UI element under ({dx}, {dy})...")
            self._draw_preview("tap", [(dx, dy)])
            threading.Thread(target=self._pick_element, args=(dx, dy), daemon=True).start()

    def _pick_element(self, dx, dy):
        """Dump the UI, find the element under (dx, dy), build a tap_text step."""
        try:
            nodes = core.parse_ui_nodes(self.adb.ui_dump())
        except Exception as e:
            self.q.put(("log", f"element pick failed: {e}"))
            return
        n = core.node_at(nodes, dx, dy)
        if not n:
            self.q.put(("log", f"no labelled UI element under ({dx}, {dy}) - use 'add tap' instead"))
            return
        label = core.node_label(n)
        match = "exact" if (n["text"] or n["desc"]) else "contains"
        step = new_step("tap_text", text=label, match=match, label=n.get("cls", "") or "element")
        self.q.put(("addstep", step))
        self.q.put(("log", f"picked element: \"{label}\" [{match}]  ({n.get('cls', '')})"))

    # ---- step list -------------------------------------------------------
    def _refresh_steps(self):
        self.step_list.delete(0, "end")
        for i, s in enumerate(self.steps, 1):
            lbl = f"  {s['label']}" if s.get("label") else ""
            self.step_list.insert("end", f"{i:>2}. {s['type']:<11} {describe_step(s)}{lbl}")

    def _sel(self):
        s = self.step_list.curselection()
        return s[0] if s else None

    def add_step(self):
        dlg = StepDialog(self.root, app=self)
        if dlg.result:
            i = self._sel()
            if i is None:
                self.steps.append(dlg.result)
            else:
                self.steps.insert(i + 1, dlg.result)
            self._refresh_steps()

    def edit_step(self):
        i = self._sel()
        if i is None:
            return
        dlg = StepDialog(self.root, step=self.steps[i], app=self)
        if dlg.result:
            self.steps[i] = dlg.result
            self._refresh_steps()
            self.step_list.selection_set(i)

    def del_step(self):
        i = self._sel()
        if i is not None:
            self.steps.pop(i)
            self._refresh_steps()

    def move_step(self, d):
        i = self._sel()
        if i is None:
            return
        j = i + d
        if 0 <= j < len(self.steps):
            self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
            self._refresh_steps()
            self.step_list.selection_set(j)

    def clear_steps(self):
        if self.steps and messagebox.askyesno("Clear", "Remove all steps?"):
            self.steps = []
            self._refresh_steps()

    # ---- one-tap capture -------------------------------------------------
    def arm_capture(self):
        """Arm a single capture: the next phone tap OR live-screen click is
        recorded as one step, then it disarms automatically."""
        if self._armed:                       # pressing again = cancel
            self._disarm()
            self._log("capture cancelled")
            return
        if not self.adb:
            messagebox.showinfo("No device", "Connect a device first.")
            return
        self._armed = True
        self.cap_btn.config(text="Cancel capture")
        self.rec_lbl.config(text="TAP THE PHONE  (or click the live screen)")
        self._log("armed - tap the phone now, or click the live screen")
        try:                                  # also listen for a real finger tap
            self._oneshot_rec = TouchRecorder(
                self.adb, self.cur_size[0], self.cur_size[1],
                on_gesture=lambda g: self.q.put(("oneshot", g)),
                on_error=lambda m: self.q.put(("log", m)))
            self._oneshot_rec.start()
        except AdbError as e:
            self._oneshot_rec = None
            self._log(f"(phone-tap listen off: {e}) - click the live screen instead")

    def _disarm(self):
        self._armed = False
        if self._oneshot_rec:
            try:
                self._oneshot_rec.stop()
            except Exception:
                pass
            self._oneshot_rec = None
        self.cap_btn.config(text="Capture one tap")
        self.rec_lbl.config(text="idle")

    def _finish_oneshot(self, step):
        if not self._armed:
            return
        self._disarm()
        self.steps.append(step)
        self._refresh_steps()
        self._log("captured " + describe_step(step))

    # ---- recording -------------------------------------------------------
    def toggle_record(self):
        if self.recorder:
            self.recorder.stop()
            self.recorder = None
            self.rec_btn.config(text="Auto capture (until Stop)")
            self.rec_lbl.config(text="idle")
            self._log("auto capture stopped")
            return
        if not self.adb:
            messagebox.showinfo("No device", "Connect a device first.")
            return
        try:
            self.recorder = TouchRecorder(
                self.adb, self.cur_size[0], self.cur_size[1],
                on_gesture=lambda g: self.q.put(("gesture", g)),
                on_error=lambda m: self.q.put(("log", m)))
            dev = self.recorder.start()
        except AdbError as e:
            self.recorder = None
            messagebox.showerror("Record failed", str(e))
            return
        self.rec_btn.config(text="Stop capture")
        self.rec_lbl.config(text=f"capturing ({dev.get('name','touch')}) - tap freely, press Stop when done")
        self._log("auto capture started - every tap/swipe is recorded until Stop")

    # ---- macros ----------------------------------------------------------
    def refresh_macros(self):
        self.macro_paths = {}
        names = []
        for p, m in list_macros(MACRO_DIR):
            names.append(m["name"])
            self.macro_paths[m["name"]] = p
        self.macro_box["values"] = names
        if names:
            self.macro_box.current(0)

    def save(self):
        if not self.steps:
            messagebox.showinfo("Nothing to save", "Add or record some steps first.")
            return
        name = self.name_var.get().strip() or "macro"
        macro = {
            "name": name,
            "created": now_iso(),
            "device": {"tag": core.device_tag(self.serial), "size": list(self.cur_size)},
            "ref_size": list(self.cur_size) if self.cur_size != (0, 0) else None,
            "steps": self.steps,
        }
        path = save_macro(MACRO_DIR, macro)
        self._log(f"saved macro '{name}' -> {path}")
        self.refresh_macros()

    def save_action(self):
        """Save the SELECTED steps (or all, if none selected) as a named action."""
        sel = self.step_list.curselection()
        steps = [self.steps[i] for i in sel] if sel else list(self.steps)
        if not steps:
            messagebox.showinfo("Nothing to save", "Record or select some taps first.")
            return
        default = self.name_var.get().strip() or "action1"
        name = simpledialog.askstring(
            "Save as action",
            f"Name for this action ({len(steps)} step{'s' if len(steps) != 1 else ''} "
            f"{'selected' if sel else '- all'}):",
            initialvalue=default, parent=self.root)
        if not name:
            return
        macro = {
            "name": name.strip(),
            "created": now_iso(),
            "device": {"tag": core.device_tag(self.serial), "size": list(self.cur_size)},
            "ref_size": list(self.cur_size) if self.cur_size != (0, 0) else None,
            "steps": steps,
        }
        save_macro(MACRO_DIR, macro)
        self.name_var.set(name.strip())
        self._log(f"saved action '{name.strip()}' ({len(steps)} steps)")
        self.refresh_macros()

    def load(self):
        name = self.macro_box.get()
        if not name or name not in self.macro_paths:
            return
        try:
            macro = load_macro(self.macro_paths[name])
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            return
        self.steps = macro["steps"]
        self.name_var.set(macro["name"])
        self.macro_ref_size = macro.get("ref_size")
        self._refresh_steps()
        note = ""
        if self.macro_ref_size and tuple(self.macro_ref_size) != self.cur_size and self.cur_size != (0, 0):
            note = f" (will scale {self.macro_ref_size} -> {list(self.cur_size)})"
        self._log(f"loaded '{macro['name']}' with {len(self.steps)} steps{note}")

    def open_batch_export(self):
        if not self.adb:
            messagebox.showinfo("No device", "Connect a device first.")
            return
        BatchExportWindow(self)

    def build_sequence(self):
        dlg = SequenceDialog(self.root, self)
        self.refresh_macros()
        if dlg.result:
            self.steps = dlg.result["steps"]
            self.name_var.set(dlg.result["name"])
            self.macro_ref_size = dlg.result.get("ref_size")
            self._refresh_steps()
            self._log(f"built sequence '{dlg.result['name']}' ({len(self.steps)} steps) from: "
                      f"{', '.join(dlg.result['components'])}")

    # ---- playback --------------------------------------------------------
    def _start_runner(self, steps, rows, loops):
        if not self.adb:
            messagebox.showinfo("No device", "Connect a device first.")
            return
        if self.runner and self.runner.is_alive():
            messagebox.showinfo("Busy", "A run is already in progress. Stop it first.")
            return
        if not self.case:
            self.case = CaseLog(CASE_DIR, self.serial or "unknown")
        self._live_pause.set()  # let the runner own the screen
        ref = self.macro_ref_size or list(self.cur_size)
        self.runner = MacroRunner(
            self.adb, self.case, steps,
            emit=lambda *a: self.q.put(a),
            rows=rows, ref_size=ref, cur_size=list(self.cur_size),
            loops=loops, stop_when_unchanged=self.unchanged_var.get(),
            preview=0.7, show_frames=True, stop_on_error=True)
        self.runner.start()

    def run_all(self):
        if not self.steps:
            messagebox.showinfo("Empty", "No steps to run.")
            return
        loops = 0 if self.cont_var.get() else max(1, self.loops_var.get())
        self._log(f"running {len(self.steps)} steps x {'continuous' if loops == 0 else loops}")
        self._start_runner(self.steps, list(range(len(self.steps))), loops)

    def simulate_selected(self):
        i = self._sel()
        if i is None:
            messagebox.showinfo("Pick a step", "Select a step in the list first.")
            return
        self._log(f"simulate step {i + 1}: {self.steps[i]['type']}")
        self._start_runner([self.steps[i]], [i], 1)

    def stop_run(self):
        if self.runner:
            self.runner.stop()
            self._log("stop requested")

    # ---- event pump (marshals worker-thread events to the UI thread) -----
    def _pump(self):
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind in ("liveframe", "frame"):
                    self._render_png(ev[1])
                elif kind == "preview":
                    self._draw_preview(ev[1], ev[2])
                elif kind == "log":
                    self._log(ev[1])
                elif kind == "loop":
                    self._log(f"--- loop {ev[1]}" + (f"/{ev[2]}" if ev[2] else "") + " ---")
                elif kind == "step":
                    row = ev[1]
                    self.step_list.selection_clear(0, "end")
                    self.step_list.selection_set(row)
                    self.step_list.see(row)
                elif kind == "oneshot":
                    g = ev[1]
                    if self._armed:
                        step = new_step(g["type"], **{k: g[k] for k in g
                                                      if k in ("x", "y", "x2", "y2", "duration")})
                        self._finish_oneshot(step)
                elif kind == "addstep":
                    self.steps.append(ev[1])
                    self._refresh_steps()
                elif kind == "gesture":
                    g = ev[1]
                    self.steps.append(new_step(g["type"], **{k: g[k] for k in g
                                                             if k in ("x", "y", "x2", "y2", "duration")}))
                    self._refresh_steps()
                    self._log(f"recorded {describe_step(self.steps[-1])}")
                elif kind == "finished":
                    self._log(f"run finished: {ev[1]}")
                    self._live_pause.clear()
                    self.runner = None
        except queue.Empty:
            pass
        except Exception as _e:                 # one bad event must not freeze the UI
            try:
                self._log(f"pump error (skipped): {_e}")
            except Exception:
                pass
        self.root.after(80, self._pump)

    def _on_close(self):
        try:
            if self.recorder:
                self.recorder.stop()
            if self._oneshot_rec:
                self._oneshot_rec.stop()
            if self.runner:
                self.runner.stop()
            self._live_stop.set()
        finally:
            self.root.destroy()


def main():
    MACRO_DIR.mkdir(parents=True, exist_ok=True)
    CASE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:                                   # crisp text on scaled (125%/150%) Windows displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    apply_theme(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
