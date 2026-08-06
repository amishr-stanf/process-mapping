"""
Phase 0 activity logger for the workflow-mapper project.

Captures, on Windows, the minimum signal needed to later reconstruct
end-to-end cross-app tasks:

  * focus      - foreground app + window title changes (the backbone;
                 window titles carry most of the semantic content for free)
  * clipboard  - copy events, stored as {type, length, hash, short preview}
                 (the "seam" signal that stitches App A -> App B into one task)
  * idle/active- boundaries that segment the day into sessions

Everything is written append-only to a local SQLite file. Nothing leaves
this machine. No keystrokes, no screenshots. Clipboard text is reduced to a
hash plus a short capped preview so we can detect the *same data* moving
between apps without retaining full content.

Pure ctypes against the Win32 API -- no third-party dependencies.

Usage:
    python logger.py                 # run forever (Ctrl+C to stop)
    python logger.py --seconds 30    # run for 30s (smoke test)
    python logger.py --verbose       # also print each event to the console
    python logger.py --no-clip-text  # store clipboard hash/len only, no preview
"""

import argparse
import ctypes
import hashlib
import os
import signal
import sqlite3
import sys
import threading
import time
from ctypes import wintypes

# --------------------------------------------------------------------------
# Config (safe defaults; override via CLI)
# --------------------------------------------------------------------------
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity.db")
POLL_INTERVAL = 1.0          # seconds between polls
IDLE_THRESHOLD = 60.0        # seconds of no input -> "idle"
CLIP_PREVIEW_CHARS = 80      # max chars of clipboard text kept as preview (0 = none)

# --------------------------------------------------------------------------
# Win32 plumbing
# --------------------------------------------------------------------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Set restypes/argtypes explicitly -- critical on 64-bit so HANDLE/pointer
# values are not silently truncated to 32 bits (which would crash the reads).
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GetTickCount.restype = wintypes.DWORD

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIB = 8
CF_BITMAP = 2


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def get_foreground():
    """Return (process_name, window_title) for the focused window, or (None, None)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return process_name(pid.value), title


def process_name(pid):
    """Best-effort executable name (e.g. 'chrome.exe') for a pid."""
    if not pid:
        return None
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        size = wintypes.DWORD(4096)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(h)
    return None


def idle_seconds():
    """Seconds since the last keyboard/mouse input."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return (kernel32.GetTickCount() - lii.dwTime) / 1000.0


def read_clipboard(preview_chars=CLIP_PREVIEW_CHARS):
    """
    Inspect the clipboard without mutating it.

    Returns (clip_type, length, sha256_hash, preview). Only text content is
    read; for files/images we record the type only. Any failure degrades
    gracefully to (type-or-None, None, None, None) -- never raises.
    """
    try:
        if user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            ctype = "text"
        elif user32.IsClipboardFormatAvailable(CF_HDROP):
            return "files", None, None, None
        elif user32.IsClipboardFormatAvailable(CF_DIB) or user32.IsClipboardFormatAvailable(CF_BITMAP):
            return "image", None, None, None
        else:
            return None, None, None, None
    except Exception:
        return None, None, None, None

    # Read the unicode text with proper pointer handling.
    text = None
    if not user32.OpenClipboard(None):
        return "text", None, None, None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if handle:
            ptr = kernel32.GlobalLock(handle)
            if ptr:
                try:
                    text = ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(handle)
    except Exception:
        text = None
    finally:
        user32.CloseClipboard()

    if text is None:
        return "text", None, None, None

    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    preview = None
    if preview_chars > 0:
        # Collapse whitespace so previews stay compact and single-line.
        preview = " ".join(text.split())[:preview_chars]
    return "text", len(text), digest, preview


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,        -- unix epoch seconds
    kind        TEXT    NOT NULL,        -- focus | clipboard | idle_start | idle_end
    app         TEXT,                    -- process name at time of event
    title       TEXT,                    -- window title (primary semantic signal)
    clip_type   TEXT,                    -- text | files | image
    clip_len    INTEGER,
    clip_hash   TEXT,                    -- sha256 of clipboard text (hand-off matching)
    clip_preview TEXT                    -- short capped preview (may be NULL)
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_hash ON events(clip_hash);
"""


def open_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def log_event(conn, kind, app=None, title=None,
              clip_type=None, clip_len=None, clip_hash=None, clip_preview=None):
    conn.execute(
        "INSERT INTO events (ts, kind, app, title, clip_type, clip_len, clip_hash, clip_preview) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (time.time(), kind, app, title, clip_type, clip_len, clip_hash, clip_preview),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Threaded controller (used by app.py to start/stop capture from the UI)
# --------------------------------------------------------------------------
class Capture:
    """Runs the capture loop in a background thread; start/stop on demand."""

    def __init__(self, db_path, preview_chars=CLIP_PREVIEW_CHARS):
        self.db_path = db_path
        self.preview_chars = preview_chars
        self._thread = None
        self._stop = threading.Event()
        self.started_at = None

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running():
            return False
        self._stop.clear()
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if not self.is_running():
            return False
        self._stop.set()
        self._thread.join(timeout=3)
        return True

    def _run(self):
        # SQLite connections are thread-affine: open ours inside the thread.
        conn = open_db(self.db_path)
        last_focus = (None, None)
        last_clip_seq = user32.GetClipboardSequenceNumber()
        is_idle = False
        try:
            while not self._stop.is_set():
                idle = idle_seconds()
                app, title = get_foreground()
                if idle >= IDLE_THRESHOLD and not is_idle:
                    is_idle = True
                    log_event(conn, "idle_start", app=app, title=title)
                elif idle < IDLE_THRESHOLD and is_idle:
                    is_idle = False
                    log_event(conn, "idle_end", app=app, title=title)
                if not is_idle and (app, title) != last_focus and (app or title):
                    last_focus = (app, title)
                    log_event(conn, "focus", app=app, title=title)
                seq = user32.GetClipboardSequenceNumber()
                if seq != last_clip_seq:
                    last_clip_seq = seq
                    ctype, clen, chash, cprev = read_clipboard(self.preview_chars)
                    if ctype is not None:
                        log_event(conn, "clipboard", app=app, title=title,
                                  clip_type=ctype, clip_len=clen, clip_hash=chash, clip_preview=cprev)
                self._stop.wait(POLL_INTERVAL)
        finally:
            conn.close()


# --------------------------------------------------------------------------
# Main loop (CLI)
# --------------------------------------------------------------------------
def run(db_path, seconds, verbose):
    conn = open_db(db_path)

    last_focus = (None, None)          # (app, title)
    last_clip_seq = user32.GetClipboardSequenceNumber()
    is_idle = False

    start = time.time()
    stop = {"flag": False}

    def _handle_sigint(signum, frame):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _handle_sigint)

    print(f"[logger] writing to {db_path}")
    print(f"[logger] preview chars: {CLIP_PREVIEW_CHARS} | idle threshold: {IDLE_THRESHOLD}s")
    print("[logger] capturing... (Ctrl+C to stop)")

    while not stop["flag"]:
        now = time.time()

        # --- idle / active transitions -------------------------------------
        idle = idle_seconds()
        app, title = get_foreground()
        if idle >= IDLE_THRESHOLD and not is_idle:
            is_idle = True
            log_event(conn, "idle_start", app=app, title=title)
            if verbose:
                print(f"  [idle_start]")
        elif idle < IDLE_THRESHOLD and is_idle:
            is_idle = False
            log_event(conn, "idle_end", app=app, title=title)
            if verbose:
                print(f"  [idle_end]")

        # --- focus changes (only while active) -----------------------------
        if not is_idle and (app, title) != last_focus and (app or title):
            last_focus = (app, title)
            log_event(conn, "focus", app=app, title=title)
            if verbose:
                print(f"  [focus] {app} :: {title}")

        # --- clipboard changes ---------------------------------------------
        seq = user32.GetClipboardSequenceNumber()
        if seq != last_clip_seq:
            last_clip_seq = seq
            ctype, clen, chash, cprev = read_clipboard(CLIP_PREVIEW_CHARS)
            if ctype is not None:
                log_event(conn, "clipboard", app=app, title=title,
                          clip_type=ctype, clip_len=clen, clip_hash=chash, clip_preview=cprev)
                if verbose:
                    shown = f" '{cprev}'" if cprev else ""
                    print(f"  [clip:{ctype}] len={clen}{shown}  (in {app})")

        if seconds and (now - start) >= seconds:
            break
        time.sleep(POLL_INTERVAL)

    conn.close()
    print("\n[logger] stopped.")


def main():
    global CLIP_PREVIEW_CHARS
    ap = argparse.ArgumentParser(description="Phase 0 activity logger")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite path (default: ./activity.db)")
    ap.add_argument("--seconds", type=float, default=0, help="Run for N seconds then stop (0 = forever)")
    ap.add_argument("--verbose", action="store_true", help="Print each event to the console")
    ap.add_argument("--no-clip-text", action="store_true", help="Do not store clipboard text previews")
    ap.add_argument("--preview-chars", type=int, default=CLIP_PREVIEW_CHARS,
                    help=f"Max clipboard preview chars (default {CLIP_PREVIEW_CHARS})")
    args = ap.parse_args()

    CLIP_PREVIEW_CHARS = 0 if args.no_clip_text else max(0, args.preview_chars)

    if sys.platform != "win32":
        print("This logger targets Windows.", file=sys.stderr)
        sys.exit(1)

    run(args.db, args.seconds, args.verbose)


if __name__ == "__main__":
    main()
