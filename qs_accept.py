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

    def _matches(text):
        t = (text or "").strip().lower()
        return bool(t) and any(lbl in t for lbl in labels)   # substring: "Accept", "Accept & save", ...

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
                # try Buttons first, then any control with a matching name (some builds use a
                # hyperlink / custom control for Accept).
                cands = []
                for ct in ("Button", None):
                    try:
                        cands = w.descendants(control_type=ct) if ct else w.descendants()
                    except Exception:
                        cands = []
                    for b in cands:
                        try:
                            if _matches(b.window_text()):
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
