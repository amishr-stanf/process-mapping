"""
Fallback capture backend for unsupported platforms.

Lets the server and UI run everywhere; it just captures nothing.
"""


def get_foreground():
    return None, None


def idle_seconds():
    return 0.0


def clipboard_sequence():
    return 0


def read_clipboard(preview_chars=80):
    return None, None, None, None
