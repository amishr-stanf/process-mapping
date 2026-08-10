"""
macOS capture backend (PyObjC: AppKit + Quartz).

Exposes the same platform-neutral sensor interface as sensors_win.py.

Requires:
    pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz

Permissions (System Settings -> Privacy & Security):
    * Accessibility     -> reliable foreground app/window detection
    * Screen Recording  -> window *titles* (kCGWindowName is empty without it;
                           the app name is still captured either way)

NOTE: written against the documented PyObjC APIs but NOT yet verified on a Mac.
"""

import hashlib

from AppKit import NSWorkspace, NSPasteboard, NSPasteboardTypeString
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGWindowListOptionOnScreenOnly,
    kCGWindowListExcludeDesktopElements,
    kCGNullWindowID,
    CGEventSourceSecondsSinceLastEventType,
    kCGEventSourceStateHIDSystemState,
    kCGAnyInputEventType,
)


def _window_title(pid):
    """Title of the frontmost on-screen window owned by pid (needs Screen Recording)."""
    try:
        wins = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )
        for w in wins:
            if w.get("kCGWindowOwnerPID") == pid and int(w.get("kCGWindowLayer", 0)) == 0:
                name = w.get("kCGWindowName")
                if name:
                    return str(name)
    except Exception:
        pass
    return None


def get_foreground():
    """Return (app_name, window_title) for the frontmost app, or (None, None)."""
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None, None
        name = app.localizedName()
        pid = app.processIdentifier()
        return (str(name) if name else None), _window_title(pid)
    except Exception:
        return None, None


def idle_seconds():
    """Seconds since the last keyboard/mouse (HID) input."""
    try:
        return float(CGEventSourceSecondsSinceLastEventType(
            kCGEventSourceStateHIDSystemState, kCGAnyInputEventType))
    except Exception:
        return 0.0


def clipboard_sequence():
    """NSPasteboard.changeCount increments on every clipboard change."""
    try:
        return int(NSPasteboard.generalPasteboard().changeCount())
    except Exception:
        return 0


def read_clipboard(preview_chars=80):
    """
    Inspect the clipboard without mutating it. Returns
    (clip_type, length, sha256_hash, preview). Never raises.
    """
    try:
        pb = NSPasteboard.generalPasteboard()
        text = pb.stringForType_(NSPasteboardTypeString)
        if text is not None:
            text = str(text)
            digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
            preview = " ".join(text.split())[:preview_chars] if preview_chars > 0 else None
            return "text", len(text), digest, preview

        types = [str(t) for t in (pb.types() or [])]
        if any("file-url" in t for t in types):
            return "files", None, None, None
        if any(("image" in t) or ("png" in t) or ("tiff" in t) for t in types):
            return "image", None, None, None
        return None, None, None, None
    except Exception:
        return None, None, None, None
