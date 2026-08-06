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

## Run the app (recommended)

```bash
python app.py
```

This starts a local server (stdlib only, bound to `127.0.0.1` so nothing is
exposed to your network), opens the UI in your browser, and gives you a
**Start mapping** button that turns capture on/off and shows live stats from
your own `activity.db`. All data stays on your laptop.

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
