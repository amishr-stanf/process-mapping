"""
Platform dispatcher for the capture backend.

logger.py imports the sensor functions from here; the concrete implementation
is chosen at runtime by platform (Windows / macOS / fallback). Imports are
lazy so a missing backend dependency never breaks module import.
"""

import sys

_backend = None


def _load():
    global _backend
    if _backend is not None:
        return _backend
    if sys.platform == "win32":
        import sensors_win as b
    elif sys.platform == "darwin":
        import sensors_mac as b
    else:
        import sensors_null as b
    _backend = b
    return b


def platform_name():
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "unsupported"


def supported():
    return sys.platform in ("win32", "darwin")


def get_foreground():
    return _load().get_foreground()


def idle_seconds():
    return _load().idle_seconds()


def clipboard_sequence():
    return _load().clipboard_sequence()


def read_clipboard(preview_chars=80):
    return _load().read_clipboard(preview_chars)
