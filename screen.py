"""
Optional screenshot capture for understanding repeated tasks inside a single
native app (where window title + clipboard aren't enough).

OFF by default. Privacy-sensitive: it captures pixels of the focused window.
Enable it in Settings; everything stays local (shots saved next to the DB).

Deterministic-first: each shot is reduced to an 8x8 average-hash (aHash) so
repeated *visual states* (the same dialog/screen/step) can be detected WITHOUT
any AI — same task → near-identical hash. The stored thumbnail is what a BYOK
vision model can later look at to actually name the step.

NOTE: focused-window capture is implemented for Windows; on macOS it falls back
to a full-screen grab (needs Screen Recording permission) or is skipped.

Requires Pillow (bundled with the packaged app).
"""

import os
import sys
import time


def ahash(img, size=8):
    """64-bit average hash as hex — near-equal for visually similar screens."""
    g = img.convert("L").resize((size, size))
    px = list(g.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for i, p in enumerate(px):
        if p >= avg:
            bits |= (1 << i)
    return f"{bits:016x}"


def _focused_bbox():
    """(l,t,r,b) of the foreground window on Windows, else None (full screen)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    if (rect.right - rect.left) < 50 or (rect.bottom - rect.top) < 50:
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


SCHEMA = """
CREATE TABLE IF NOT EXISTS screenshots (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    REAL NOT NULL,
    app   TEXT,
    title TEXT,
    ahash TEXT,     -- perceptual hash for repeat detection (no AI needed)
    path  TEXT      -- local thumbnail path
);
CREATE INDEX IF NOT EXISTS idx_shot_ts ON screenshots(ts);
CREATE INDEX IF NOT EXISTS idx_shot_hash ON screenshots(ahash);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def capture(conn, app, title, shots_dir, mode="focused"):
    """Grab the focused window, hash + thumbnail it, record a row. Never raises."""
    try:
        from PIL import ImageGrab
    except Exception:
        return None  # Pillow not available -> silently disabled
    try:
        bbox = _focused_bbox() if mode == "focused" else None
        img = ImageGrab.grab(bbox=bbox)
        if img is None:
            return None
        h = ahash(img)
        ts = time.time()
        os.makedirs(shots_dir, exist_ok=True)
        thumb = img.convert("RGB")
        thumb.thumbnail((800, 800))
        path = os.path.join(shots_dir, f"{int(ts)}_{h}.jpg")
        thumb.save(path, "JPEG", quality=70)
        ensure_schema(conn)
        conn.execute("INSERT INTO screenshots (ts, app, title, ahash, path) VALUES (?,?,?,?,?)",
                     (ts, app, title, h, path))
        conn.commit()
        return h
    except Exception:
        return None
