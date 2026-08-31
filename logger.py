"""
Phase 0 activity logger for the workflow-mapper project.

Captures the minimum signal needed to later reconstruct end-to-end cross-app
tasks:

  * focus      - foreground app + window title changes (the backbone;
                 window titles carry most of the semantic content for free)
  * clipboard  - copy events, stored as {type, length, hash, short preview}
                 (the "seam" signal that stitches App A -> App B into one task)
  * idle/active- boundaries that segment the day into sessions

Everything is written append-only to a local SQLite file. Nothing leaves
this machine. No keystrokes, no screenshots. Clipboard text is reduced to a
hash plus a short capped preview so we can detect the *same data* moving
between apps without retaining full content.

Cross-platform via the `sensors` dispatcher (Windows / macOS backends).

Usage:
    python logger.py                 # run forever (Ctrl+C to stop)
    python logger.py --seconds 30    # run for 30s (smoke test)
    python logger.py --verbose       # also print each event to the console
    python logger.py --no-clip-text  # store clipboard hash/len only, no preview
"""

import argparse
import os
import signal
import sqlite3
import sys
import threading
import time

import sensors

# --------------------------------------------------------------------------
# Config (safe defaults; override via CLI)
# --------------------------------------------------------------------------
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity.db")
POLL_INTERVAL = 1.0          # seconds between polls
IDLE_THRESHOLD = 60.0        # seconds of no input -> "idle"
CLIP_PREVIEW_CHARS = 80      # max chars of clipboard text kept as preview (0 = none)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,        -- unix epoch seconds
    kind        TEXT    NOT NULL,        -- focus | clipboard | idle_start | idle_end
    app         TEXT,                    -- app / process name at time of event
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
        try:
            last_clip_seq = sensors.clipboard_sequence()
        except Exception:
            last_clip_seq = 0
        is_idle = False
        try:
            while not self._stop.is_set():
                # A transient sensor error must skip one poll, never kill capture.
                try:
                    idle = sensors.idle_seconds()
                    app, title = sensors.get_foreground()
                    if idle >= IDLE_THRESHOLD and not is_idle:
                        is_idle = True
                        log_event(conn, "idle_start", app=app, title=title)
                    elif idle < IDLE_THRESHOLD and is_idle:
                        is_idle = False
                        log_event(conn, "idle_end", app=app, title=title)
                    if not is_idle and (app, title) != last_focus and (app or title):
                        last_focus = (app, title)
                        log_event(conn, "focus", app=app, title=title)
                    seq = sensors.clipboard_sequence()
                    if seq != last_clip_seq:
                        last_clip_seq = seq
                        ctype, clen, chash, cprev = sensors.read_clipboard(self.preview_chars)
                        if ctype is not None:
                            log_event(conn, "clipboard", app=app, title=title,
                                      clip_type=ctype, clip_len=clen, clip_hash=chash, clip_preview=cprev)
                except Exception:
                    pass
                self._stop.wait(POLL_INTERVAL)
        finally:
            conn.close()


# --------------------------------------------------------------------------
# Main loop (CLI)
# --------------------------------------------------------------------------
def run(db_path, seconds, verbose):
    conn = open_db(db_path)

    last_focus = (None, None)          # (app, title)
    last_clip_seq = sensors.clipboard_sequence()
    is_idle = False

    start = time.time()
    stop = {"flag": False}

    def _handle_sigint(signum, frame):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _handle_sigint)

    print(f"[logger] platform: {sensors.platform_name()} | writing to {db_path}")
    print(f"[logger] preview chars: {CLIP_PREVIEW_CHARS} | idle threshold: {IDLE_THRESHOLD}s")
    print("[logger] capturing... (Ctrl+C to stop)")

    while not stop["flag"]:
        now = time.time()

        # --- idle / active transitions -------------------------------------
        idle = sensors.idle_seconds()
        app, title = sensors.get_foreground()
        if idle >= IDLE_THRESHOLD and not is_idle:
            is_idle = True
            log_event(conn, "idle_start", app=app, title=title)
            if verbose:
                print("  [idle_start]")
        elif idle < IDLE_THRESHOLD and is_idle:
            is_idle = False
            log_event(conn, "idle_end", app=app, title=title)
            if verbose:
                print("  [idle_end]")

        # --- focus changes (only while active) -----------------------------
        if not is_idle and (app, title) != last_focus and (app or title):
            last_focus = (app, title)
            log_event(conn, "focus", app=app, title=title)
            if verbose:
                print(f"  [focus] {app} :: {title}")

        # --- clipboard changes ---------------------------------------------
        seq = sensors.clipboard_sequence()
        if seq != last_clip_seq:
            last_clip_seq = seq
            ctype, clen, chash, cprev = sensors.read_clipboard(CLIP_PREVIEW_CHARS)
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

    if not sensors.supported():
        print(f"Capture isn't supported on this platform ({sys.platform}).", file=sys.stderr)
        sys.exit(1)

    run(args.db, args.seconds, args.verbose)


if __name__ == "__main__":
    main()
