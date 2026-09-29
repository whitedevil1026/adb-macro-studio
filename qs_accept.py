"""
PC-side helper: auto-click the Quick Share (Windows) 'Accept' button when an
incoming file request pops up. Uses UI Automation via pywinauto.
"""
from __future__ import annotations

import time

# Button label(s) to click. Add your Windows locale's word for "Accept" here if it's
# not English (e.g. a localized Quick Share).
ACCEPT_LABELS = ("accept",)


def accept_quickshare(timeout=45, poll=0.4, log=lambda m: None, labels=ACCEPT_LABELS) -> bool:
    """Wait for the Quick Share 'Accept' button and click it. True if clicked."""
    labels = tuple(l.lower() for l in labels)
    try:
        from pywinauto import Desktop
    except Exception as e:  # pragma: no cover
        log(f"pywinauto missing: {e}")
        return False

    end = time.time() + timeout
    while time.time() < end:
        try:
            dt = Desktop(backend="uia")
            for w in dt.windows():
                try:
                    if "quick share" not in (w.window_text() or "").lower():
                        continue
                except Exception:
                    continue
                try:
                    btns = w.descendants(control_type="Button")
                except Exception:
                    btns = []
                for b in btns:
                    try:
                        if (b.window_text() or "").strip().lower() in labels:
                            try:
                                b.invoke()
                            except Exception:
                                b.click_input()
                            log("clicked Accept")
                            return True
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(poll)
    return False


if __name__ == "__main__":
    print("watching for a Quick Share Accept dialog for 45s...")
    print("accepted:" , accept_quickshare(log=print))
