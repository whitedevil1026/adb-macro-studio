# -*- coding: utf-8 -*-
"""Regression tests for issues found in the 23-Oct-2026 field run logs.

Each test names the field failure it reproduces. No device, no network; the Quick Share window is a
fake that mirrors the real one observed on the receiving PC (title 'Quick Share', process
nearby_share.exe, window class 'Google.Windows.NearbyShare').
"""
import glob
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import adb_core as core          # noqa: E402
import qs_accept                 # noqa: E402
import whatsapp_batch as wb      # noqa: E402

SERIAL = "SER123"
TAG = "dev-3e10639645"           # hand-derived: "dev-" + sha256(b"SER123").hexdigest()[:10]


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeCtrl:
    def __init__(self, text, control_type="Button"):
        self.text = text
        self.element_info = types.SimpleNamespace(control_type=control_type, name=text)
        self.clicked = False

    def window_text(self):
        return self.text

    def invoke(self):
        self.clicked = True

    def click_input(self):
        self.clicked = True


class FakeWindow:
    def __init__(self, title, class_name, controls=()):
        self.title = title
        self.controls = list(controls)
        self.element_info = types.SimpleNamespace(class_name=class_name, process_id=4242,
                                                  control_type="Window", name=title)

    def window_text(self):
        return self.title

    def descendants(self, control_type=None):
        if control_type:
            return [c for c in self.controls if c.element_info.control_type == control_type]
        return list(self.controls)


def fake_pywinauto(windows):
    mod = types.ModuleType("pywinauto")

    class Desktop:
        def __init__(self, backend=None):
            pass

        def windows(self):
            return windows
    mod.Desktop = Desktop
    return mod


def quick_share_window(*controls):
    return FakeWindow("Quick Share", "Google.Windows.NearbyShare", controls)


def run_accept(windows, timeout=10, exe_paths=(), launched=None):
    """Run accept_quickshare against fake windows. No real program is ever started: the Quick Share
    executable list is replaced (empty by default) and process launches are recorded instead."""
    logs = []
    record = launched if launched is not None else []
    with mock.patch.dict(sys.modules, {"pywinauto": fake_pywinauto(windows)}), \
            mock.patch.object(qs_accept, "time", FakeClock()), \
            mock.patch.object(qs_accept, "QS_EXE_PATHS", tuple(exe_paths)), \
            mock.patch.object(qs_accept.subprocess, "Popen",
                              lambda args, **k: record.append(list(args))):
        clicked = qs_accept.accept_quickshare(timeout=timeout, poll=0.5, log=logs.append)
    return clicked, logs


# ----------------------------------------------------------------- nothing written to the phone
class TestScreenReadsWriteNothingOnThePhone(unittest.TestCase):
    """A failed screen read used to fall back to `uiautomator dump /sdcard/window_dump.xml` - a file
    written on the (evidence) phone. Retrying the stdout read is enough and writes nothing."""

    def test_a_failed_read_is_retried_without_writing_a_file_on_the_phone(self):
        xml = b"<?xml version='1.0' ?><hierarchy rotation='0'><node text='' bounds='[0,0][1,1]'/></hierarchy>"
        calls = []
        replies = [core.AdbError("killed (exit 137)"), b"<hierarchy", xml]   # killed, cut short, ok

        def run(self, *args, **k):
            calls.append(args)
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(core.Adb, "run", run), mock.patch.object(core.time, "sleep", lambda s: None):
            out = core.Adb("adb", SERIAL).ui_dump()
        self.assertIn("</hierarchy>", out)
        self.assertFalse([c for c in calls if any("/sdcard" in str(a) for a in c)], calls)


# ----------------------------------------------------------------- one Accept watcher per run
class TestAcceptWatcher(unittest.TestCase):
    """A long run used to start a NEW 180-second UI-polling thread for every chat attempt; on failures
    and retries several ran at once. One watcher serves the whole run, armed only while a transfer
    of ours is expected."""

    def setUp(self):
        self.win = quick_share_window()
        for p in (mock.patch.dict(sys.modules, {"pywinauto": fake_pywinauto([self.win])}),
                  mock.patch.object(qs_accept, "time", FakeClock()),
                  mock.patch.object(qs_accept, "QS_EXE_PATHS", ())):
            p.start()
            self.addCleanup(p.stop)

    def test_accept_is_clicked_only_while_armed(self):
        w = qs_accept.AcceptWatcher(log=lambda m: None)
        accept = FakeCtrl("Accept")
        self.win.controls = [accept]
        w.poll_once()
        self.assertFalse(accept.clicked)             # not ours: nobody asked for a transfer
        w.arm(60)
        w.poll_once()
        self.assertTrue(accept.clicked)
        self.assertFalse(w.armed())                  # one click per armed transfer

    def test_arming_again_reuses_the_one_thread(self):
        started = []
        with mock.patch.object(qs_accept.threading, "Thread",
                               lambda *a, **k: started.append(1) or mock.Mock()):
            w = qs_accept.AcceptWatcher(log=lambda m: None)
            for _ in range(70):
                w.arm(180)
        self.assertEqual(len(started), 1)

    def test_an_armed_window_expires(self):
        w = qs_accept.AcceptWatcher(log=lambda m: None)
        w.arm(10)
        qs_accept.time.sleep(11)
        accept = FakeCtrl("Accept")
        self.win.controls = [accept]
        w.poll_once()
        self.assertFalse(accept.clicked)


# ----------------------------------------------------------------- Quick Share auto-Accept
class TestQuickShareAccept(unittest.TestCase):
    def test_does_not_click_inside_a_browser_tab_whose_title_mentions_quickshare(self):
        # field log 20:34:03: "clicked Accept on 'comparing main...fix/quickshare-backtoli'"
        browser_text = FakeCtrl("broader auto-Accept matching", "Text")
        browser_btn = FakeCtrl("Accept cookies")
        browser = FakeWindow("Comparing main...fix/quickshare-backtolist - Brave",
                             "Chrome_WidgetWin_1", [browser_text, browser_btn])
        accept = FakeCtrl("Accept")
        clicked, _ = run_accept([browser, quick_share_window(accept)])
        self.assertTrue(clicked)
        self.assertTrue(accept.clicked)
        self.assertFalse(browser_text.clicked or browser_btn.clicked)

    def test_no_click_when_only_a_lookalike_window_exists(self):
        lookalike_btn = FakeCtrl("Accept all")
        lookalike = FakeWindow("Quick Share - Google Search - Brave", "Chrome_WidgetWin_1",
                               [lookalike_btn])
        clicked, _ = run_accept([lookalike])
        self.assertFalse(clicked)
        self.assertFalse(lookalike_btn.clicked)

    def test_never_clicks_a_quick_share_settings_control(self):
        # 'Receive from: Everyone' is the visibility setting; clicking it changes Quick Share config
        setting = FakeCtrl("Receive from: Everyone")
        accept = FakeCtrl("Accept")
        clicked, _ = run_accept([quick_share_window(setting, accept)])
        self.assertTrue(clicked)
        self.assertTrue(accept.clicked)
        self.assertFalse(setting.clicked)

    def test_exactly_titled_quick_share_window_of_another_vendor_still_works(self):
        accept = FakeCtrl("Accept")
        clicked, _ = run_accept([FakeWindow("Quick Share", "SamsungQuickShareWnd", [accept])])
        self.assertTrue(clicked)
        self.assertTrue(accept.clicked)

    def test_log_never_records_other_applications_window_titles(self):
        teams = FakeWindow("Join a Microsoft Teams meeting by ID | Microsoft Teams",
                           "TeamsWebView", [])
        clicked, logs = run_accept([teams, quick_share_window()], timeout=30)
        self.assertFalse(clicked)
        self.assertFalse(any("teams" in m.lower() for m in logs), logs)

    def _run_with_exe(self, windows):
        exe = os.path.join(tempfile.mkdtemp(), "nearby_share.exe")
        open(exe, "wb").close()
        launched = []
        run_accept(windows, timeout=2, exe_paths=(exe,), launched=launched)
        return exe, launched

    def test_opens_the_quick_share_window_when_it_is_only_in_the_tray(self):
        # observed: nearby_share.exe running with no window -> nothing to click
        exe, launched = self._run_with_exe([])
        self.assertEqual(launched, [[exe]])

    def test_does_not_relaunch_when_the_quick_share_window_is_already_open(self):
        _exe, launched = self._run_with_exe([quick_share_window()])
        self.assertEqual(launched, [])

    def test_tells_the_user_to_restart_when_pywinauto_cannot_load(self):
        # field log: "pywinauto missing: No module named 'win32api'" after installing mid-run
        logs = []
        with mock.patch.dict(sys.modules, {"pywinauto": None}):
            self.assertFalse(qs_accept.accept_quickshare(timeout=1, log=logs.append))
        self.assertTrue(any("restart" in m.lower() for m in logs), logs)


# ----------------------------------------------------------------- evidence CSV under a file lock
@unittest.skipUnless(os.name == "nt", "the lock that broke the field run is Windows behavior")
class TestCsvWriteWhileLocked(unittest.TestCase):
    ROW = [1, "Alice", "ok", "with media", "WhatsApp Chat with Alice.zip", "ab" * 32,
           "2026-10-23T20:32:16+05:30"]

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "exported_chats.csv")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("OLD-CONTENT")
        wb._CSV_FALLBACK.clear()

    def _read(self, p):
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_update_is_saved_to_a_side_file_when_the_csv_stays_locked(self):
        # field log: csv write failed: [WinError 5] Access is denied (CSV open in Excel)
        logs = []
        with open(self.path, "rb"), mock.patch.object(wb.time, "sleep", lambda s: None):
            wb.write_csv_atomic(self.path, [self.ROW], logs.append)
        self.assertEqual(self._read(self.path), "OLD-CONTENT")
        side = glob.glob(os.path.join(self.d, "exported_chats.unsaved-*.csv"))
        self.assertEqual(len(side), 1, os.listdir(self.d))
        self.assertIn("WhatsApp Chat with Alice.zip", self._read(side[0]))
        self.assertTrue(any(os.path.basename(side[0]) in m for m in logs), logs)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_a_briefly_locked_csv_is_updated_after_retrying(self):
        handle = open(self.path, "rb")
        calls = []

        def sleep_then_release(s):
            calls.append(s)
            if len(calls) == 2 and not handle.closed:
                handle.close()                       # e.g. antivirus scan finishes
        try:
            with mock.patch.object(wb.time, "sleep", sleep_then_release):
                wb.write_csv_atomic(self.path, [self.ROW], lambda m: None)
        finally:
            handle.close()
        self.assertIn("WhatsApp Chat with Alice.zip", self._read(self.path))
        self.assertEqual(glob.glob(os.path.join(self.d, "exported_chats.unsaved-*.csv")), [])

    def test_resume_picks_the_newer_side_file_so_finished_chats_are_not_redone(self):
        cases = tempfile.mkdtemp()
        case = os.path.join(cases, "20261023_203012_" + TAG)
        os.makedirs(case)
        main = os.path.join(case, "exported_chats.csv")
        side = os.path.join(case, "exported_chats.unsaved-20261023_203500.csv")
        for p, ts in ((main, 1_000_000), (side, 1_000_500)):
            with open(p, "w", encoding="utf-8") as f:
                f.write("#,chat\n1,x\n")
            os.utime(p, (ts, ts))
        self.assertEqual(wb.latest_csv_for_serial(cases, SERIAL), side)


# ----------------------------------------------------------------- device serial never written out
class TestSerialIsNotWrittenOut(unittest.TestCase):
    def test_device_tag_is_stable_and_hides_the_serial(self):
        self.assertEqual(core.device_tag(SERIAL), TAG)
        self.assertNotEqual(core.device_tag("OTHER999"), TAG)
        self.assertEqual(core.device_tag(None), "unknown")

    def test_case_folder_name_uses_the_tag(self):
        case = core.CaseLog(tempfile.mkdtemp(), SERIAL)
        self.assertNotIn(SERIAL, case.dir.name)
        self.assertTrue(case.dir.name.endswith("_" + TAG), case.dir.name)

    def test_logged_commands_and_errors_hide_the_serial(self):
        logs = []
        adb = core.Adb("adb", SERIAL, log=logs.append)
        failed = types.SimpleNamespace(returncode=1, stdout=b"",
                                       stderr=f"error: device '{SERIAL}' not found".encode())
        with mock.patch.object(core.subprocess, "run", return_value=failed):
            with self.assertRaises(core.AdbError) as ctx:
                adb.run("shell", "input", "keyevent", "4")
        self.assertNotIn(SERIAL, str(ctx.exception))
        self.assertIn(TAG, str(ctx.exception))
        self.assertFalse(any(SERIAL in m for m in logs), logs)

    def test_the_command_still_targets_the_real_device(self):
        adb = core.Adb("adb", SERIAL)
        self.assertEqual(adb.cmd("shell", "ls")[:3], ["adb", "-s", SERIAL])

    def test_device_info_does_not_carry_the_serial(self):
        adb = core.Adb("adb", SERIAL)
        outputs = {"wm size": "Physical size: 1440x3120", "wm density": "Physical density: 600"}
        with mock.patch.object(core.Adb, "shell",
                               lambda self, *a, **k: outputs.get(" ".join(a), "x")):
            info = adb.device_info()
        self.assertNotIn(SERIAL, repr(info))
        self.assertEqual(info["device"], TAG)

    def test_device_wait_message_hides_the_serial(self):
        with mock.patch.object(core.Adb, "devices",
                               return_value=[{"serial": SERIAL, "state": "unauthorized"}]):
            msg = wb._device_wait_reason("adb")
        self.assertNotIn(SERIAL, msg)
        self.assertIn("UNAUTHORIZED", msg)

    def test_device_picker_label_hides_the_serial(self):
        label = core.device_label({"serial": SERIAL, "state": "device", "model": "SM_S928B"})
        self.assertNotIn(SERIAL, label)
        self.assertIn("SM_S928B", label)
        self.assertIn(TAG, label)

    def test_continues_a_case_folder_named_with_the_tag(self):
        cases = tempfile.mkdtemp()
        case = os.path.join(cases, "20261023_203012_" + TAG)
        os.makedirs(case)
        csv_path = os.path.join(case, "exported_chats.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("#,chat\n1,x\n")
        self.assertEqual(wb.latest_csv_for_serial(cases, SERIAL), csv_path)

    def test_still_continues_a_legacy_case_folder_named_with_the_raw_serial(self):
        # existing case folders on other PCs were created before this change
        cases = tempfile.mkdtemp()
        case = os.path.join(cases, "20261005_131117_" + SERIAL)
        os.makedirs(case)
        csv_path = os.path.join(case, "exported_chats.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("#,chat\n1,x\n")
        self.assertEqual(wb.latest_csv_for_serial(cases, SERIAL), csv_path)


# ----------------------------------------------------------------- live view after a disconnect
class TestFailStreak(unittest.TestCase):
    def test_repeated_failures_are_reported_once_and_recovery_once(self):
        s = core.FailStreak()
        got = [s.fail("live view paused"), s.fail("live view paused"), s.fail("live view paused"),
               s.ok("live view resumed"), s.ok("live view resumed"), s.fail("live view paused")]
        self.assertEqual(got, ["live view paused", None, None, "live view resumed", None,
                               "live view paused"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
