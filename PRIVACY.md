# Privacy

workflow-mapper is a **local-only** tool. It is designed so that your activity
data never leaves your computer.

## What it captures

**Desktop (the app):**
- The foreground app name and window title
- Clipboard events — the type, length, a hash, and a short (≤80 char) preview
- Idle/active transitions

**Web (the browser extension):**
- The active tab's URL (with the query string removed) and page title
- Navigation, clicks on links/buttons, form-field entries, and text selections
- For each, a short (≤80 char) truncated + hashed preview of text

## What it never captures

- **Passwords and sensitive fields** — inputs of type password/hidden, and
  fields whose name/label looks like a password, card number, CVV, SSN, or
  one-time code, are never read.
- **URL query strings** — stripped before storage.
- **Keystrokes** — no global keystroke logging.

## Where your data goes

- Stored in a local SQLite file on your machine — Windows:
  `%LOCALAPPDATA%\workflow-mapper\activity.db`; macOS:
  `~/Library/Application Support/workflow-mapper/activity.db`.
- The app runs a server bound only to `127.0.0.1` (loopback). It is not
  reachable from your network or the internet.
- **No telemetry, no analytics, no external servers, no accounts.** The
  extension only ever sends data to `http://127.0.0.1:8765` on your own machine.

## Your control

- Capture is off until you click **Start mapping**; toggle the extension from
  its toolbar popup.
- To delete everything captured, quit the app and delete
  `%LOCALAPPDATA%\workflow-mapper\activity.db`.

## Open source

The full source is available so you can verify all of the above:
https://github.com/amishr-stanf/process-mapping
