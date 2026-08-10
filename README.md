# workflow-mapper

A local-first system that maps your cross-app workflows, ranks the tasks you
repeat, and (later) proposes automations for the most repeatable ones.

Everything runs on your laptop. Captured data stays in a local SQLite file
(`activity.db`). No keystrokes, no screenshots, no network.

## Status: Phase 0 — activity capture

`logger.py` records the minimum signal needed to reconstruct end-to-end tasks:

| Signal | What's stored | Why |
|--------|---------------|-----|
| **focus** | app name + window title | Backbone. Titles carry most semantic content for free (email subjects, doc names, page titles). |
| **clipboard** | type, length, sha256 hash, short capped preview | The "seam" that stitches App A → App B into one task and shows what data flowed. |
| **idle/active** | transitions after 60s of no input | Segments the day into sessions. |

**Minimal text footprint:** window titles are metadata (captured in full). The
only actual content capture is a clipboard preview, capped at 80 chars and
stored alongside a hash. Disable it entirely with `--no-clip-text`.

## Install (beta testers)

No Python needed — it's a single Windows app.

1. Download `workflow-mapper.exe` (from the sender / GitHub Releases).
2. Double-click it. Windows SmartScreen may warn about an unknown publisher
   (the build isn't code-signed yet) — choose **More info → Run anyway**.
3. It starts in the system tray (gold icon) and opens the dashboard. Right-click
   the tray icon for Start / Stop / Quit.
4. For web capture, also load the browser extension (see below).

Your data stays on your machine — see [PRIVACY.md](PRIVACY.md).

### macOS

macOS is supported via a separate capture backend (PyObjC). On first launch,
grant permissions in **System Settings → Privacy & Security**:
- **Accessibility** — reliable foreground app/window detection
- **Screen Recording** — needed for window *titles* (the app name works without it)

Data is stored at `~/Library/Application Support/workflow-mapper/activity.db`.

## Build the app (developers)

**Windows:**
```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```
Requires `pip install pyinstaller pystray Pillow`. Produces `dist\workflow-mapper.exe`.

**macOS:**
```bash
bash packaging/build_mac.sh
```
Requires `pip install pyinstaller pystray Pillow pyobjc-framework-Cocoa pyobjc-framework-Quartz`.
Produces `dist/workflow-mapper.app`.

### Architecture note

Capture is split by platform: `sensors.py` dispatches to `sensors_win.py`
(Win32 ctypes) or `sensors_mac.py` (PyObjC). Everything else — the server
(`app.py`), tray (`tray.py`), UI, and browser extension — is cross-platform.

## Run the app (recommended)

```bash
python app.py
```

This starts a local server (stdlib only, bound to `127.0.0.1` so nothing is
exposed to your network), opens the UI in your browser, and gives you a
**Start mapping** button that turns capture on/off and shows live stats from
your own `activity.db`. All data stays on your laptop.

## Capture web actions (Chrome extension)

The OS logger can't see inside web apps (Salesforce, web Gmail, internal
tools). The browser extension captures that deterministically — active URL,
navigation, clicks, form entries, and text selections — and posts them to the
local server (which must be running: `python app.py`).

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top-right)
3. Click **Load unpacked** and select the `browser-extension/` folder
4. Use the toolbar popup to toggle capture on/off and see server status

Privacy: password/hidden/sensitive fields are never read, URL query strings
are dropped, text is truncated and hashed, and events go only to
`127.0.0.1`. Stored in the `web_events` table of `activity.db`.

## Run the logger directly (CLI)

```bash
# smoke test (30s, prints events)
python logger.py --seconds 30 --verbose

# run continuously (Ctrl+C to stop)
python logger.py

# inspect what's been captured
python peek.py
python peek.py --apps          # time-in-app breakdown
python peek.py --tail 100
```

Run it in the background without a console window:

```powershell
Start-Process pythonw -ArgumentList "logger.py" -WorkingDirectory "C:\Users\amishr\Documents\Claude code\workflow-mapper"
```

## Roadmap

- **Phase 0 (done):** focus + clipboard + idle → SQLite.
- **Phase 1:** browser URL + file-watch; daily timeline viewer.
- **Phase 2:** sessionize → normalize → mine recurring cross-app tasks.
  Produces **Output 1**: ranked task streams (most frequent / longest / most
  total time / most app-hops).
- **Phase 3:** content sub-clustering so tasks are specific
  ("reply to a refund-status question", not "reply to a customer").
  Produces **Output 2**: ranked automation candidates.
- **Phase 4:** AI-assisted flow generation with dry-run + confirmation gates.

## Schema (`events` table)

`ts, kind, app, title, clip_type, clip_len, clip_hash, clip_preview`

where `kind ∈ {focus, clipboard, idle_start, idle_end}`.
