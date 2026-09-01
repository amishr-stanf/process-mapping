# workflow-mapper

A local-first system that maps your cross-app workflows, ranks the tasks you
repeat, and (later) proposes automations for the most repeatable ones.

Everything runs on your laptop. Captured data stays in a local SQLite file.
Your activity never leaves your machine.

## ⬇️ Download (Windows)

**Get the app from the [latest release](https://github.com/amishr-stanf/process-mapping/releases/latest)** →
download `workflow-mapper-windows.zip`, unzip, run `workflow-mapper.exe`
(on the SmartScreen warning: **More info → Run anyway**).

> The `.exe` is **not** in the source tree — it's a build artifact published on
> the Releases page. Cloning this repo gives you the source, not the app. To
> build it yourself see *Build the app* below.

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

## How flow detection works

Everything below is deterministic — it runs with AI off.

**1. Every action is logged.** Two streams merge into one timeline
(`load_actions`): desktop `focus` / `copy` / idle, and web `visit` / `click` /
`input` / `read`. The dashboard's **Action log** shows this stream live, with the
sequence each action is being committed to.

**2. Actions are cut into sequences.** A sequence ends at an idle event (60s of
no input) or a gap > 45s between actions. This is the weakest step — a long
think-pause splits a task, and back-to-back tasks can merge. The AI layer exists
partly to catch exactly those mistakes.

**3. Steps are normalized so repeats collapse.** Numbers, ids, emails and dates
become `<n>`, `<id>`, `<email>`, `<date>`, so the same task with different data
produces the same signature.

**4. Information carried without the clipboard is linked.** If text you *read*
in one app reappears in something you *type* in another within 5 minutes
(verbatim or ≥60% token overlap), a synthetic `carry` step records the hand-off.
This catches "look it up here, retype it there", which leaves no clipboard trace.

**5. Sequences are scored for automatability** (`auto_score`) — not just
repetition. Concrete actions (click/input/copy/carry) score highest, reachable
interfaces (web/API) beat native GUIs, and repetition *boosts* the score rather
than gating it. A flow surfaces if it repeats **or** scores ≥ 45, so a
one-off-but-clearly-scriptable sequence still appears.

Known limits: inside native apps we only see the window title, so in-app steps in
Excel/Word score low until the UI-Automation sensor lands.

## AI features — bring your own key (BYOK)

Capture, sequencing, flow detection and automatability scores are **fully
deterministic** — no AI, no account, no cost. There is an explicit **AI on/off**
switch in Settings, and with it off nothing above changes.

Turned on, a **small model** (Haiku 4.5 / GPT-4o-mini by default) is layered on
top of the already-detected flows to: name each task, explain what it's actually
accomplishing, judge whether the segmenter got the boundaries right, and give a
sharper 0-100 automatability ranking. It runs only when you press **Analyze
flows**, results are cached per flow signature, and only flows seen 2+ times are
sent — so it stays cheap. The dashboard itself never calls a model.

It runs on **your own** API key:

- Open **⚙ Settings** in the dashboard, pick a provider (Anthropic or OpenAI),
  paste your key, and **Save** (or **Test key** to verify it).
- The key is stored only on your machine (`config.json` next to the database)
  and is used only to call the provider you chose. You can also set it via the
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` environment variable.
- There is **no bundled key**. Whoever shared this tool with you never sees
  your data and never pays for your usage — every AI call is billed to the key
  on your machine.

## Developer console (restricted)

A back-end console at **http://127.0.0.1:8765/admin** (⛭ in the dashboard) for
inspecting and curating captured data:

- **Flows** — every detected flow with its signature and steps. Per flow you can
  **Rename**, **Merge into…** (give several signatures the same group name and
  they count as one flow), **Hide**, **Reset**, or **Purge data** (delete the
  captured events behind every occurrence).
- **Raw capture** — browse `events`, `web_events` and `screenshots` row by row,
  with per-row delete.
- **Purge** — bulk delete by scope (desktop / web / screenshots / everything),
  optionally only data older than N days.

Access is gated by a login and the API is served **only on 127.0.0.1**, so it is
not reachable from the network. The password is stored as a salted
PBKDF2-SHA256 digest (`auth.py`) — never in plaintext.

**Set your own credential** (recommended for anything sensitive, since this repo
is public and the built-in digest can be read by anyone):

```powershell
python auth.py "your-new-password"   # prints WM_ADMIN_SALT / WM_ADMIN_HASH
setx WM_ADMIN_USER "you"
setx WM_ADMIN_SALT "<hex>"
setx WM_ADMIN_HASH "<hex>"
```

## Understanding tasks inside a single native app

Window title + clipboard don't reveal what happens *inside* a desktop app
(Excel edits, Word formatting, a proprietary tool). Two ways to go deeper:

- **UI Automation (recommended, not yet built):** subscribe to the OS
  accessibility layer (Windows UIA / macOS AX) to capture *semantic* actions —
  which control was invoked, which field changed, which menu item was picked —
  with no pixels. It's lighter, more precise, privacy-preserving, and plugs
  straight into the mining pipeline like web events do. This is the intended
  primary method for native-app depth.
- **Screenshots (built, off by default):** enable "Capture screenshots of the
  focused window" in ⚙ Settings. Each shot is reduced to an 8×8 perceptual hash
  so repeated *screens/steps* can be detected deterministically (same task →
  near-identical hash), with a local thumbnail a BYOK vision model can later
  label. Pixels never leave your machine (saved next to the database). Use only
  when you need in-app task detection — it's the heaviest, most sensitive sensor.

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
