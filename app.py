"""
Local control server for workflow-mapper.

Run it, and it opens the UI in your browser. The "Start mapping" button then
actually starts/stops the activity logger on THIS laptop and streams live
capture stats from your local activity.db.

    python app.py                 # start, open browser at http://127.0.0.1:8765
    python app.py --port 9000     # different port
    python app.py --no-browser    # don't auto-open

Stdlib only. All data stays local; the server binds to 127.0.0.1 (loopback),
so nothing is exposed to your network.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import ai
import auth
import config
import logger
import mining

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = getattr(sys, "_MEIPASS", HERE)       # where bundled data files live

DB = os.path.join(config.data_dir(), "activity.db")
UI = os.path.join(BUNDLE, "ui", "prototype.html")

capture = logger.Capture(DB)

# Web events come from the browser extension (deterministic capture, no AI).
WEB_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,   -- pageview | nav | click | input | select
    origin       TEXT,            -- scheme://host
    path         TEXT,            -- url path (query stripped for privacy)
    title        TEXT,
    target       TEXT,            -- element descriptor
    text_len     INTEGER,
    text_hash    TEXT,
    text_preview TEXT
);
CREATE INDEX IF NOT EXISTS idx_web_ts ON web_events(ts);
CREATE INDEX IF NOT EXISTS idx_web_origin ON web_events(origin);
"""


def ensure_web_schema():
    conn = sqlite3.connect(DB)
    conn.executescript(WEB_SCHEMA)
    conn.commit()
    conn.close()


def ingest_web(events):
    """Store a batch of web events. Query strings are dropped; text is hashed."""
    conn = sqlite3.connect(DB)
    try:
        for e in events:
            url = e.get("url") or ""
            parts = urlsplit(url)
            origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme else None
            preview = (e.get("text") or "")[:80] or None
            thash = hashlib.sha256(preview.encode("utf-8", "replace")).hexdigest() if preview else None
            conn.execute(
                "INSERT INTO web_events (ts, kind, origin, path, title, target, text_len, text_hash, text_preview) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (e.get("ts") or time.time(), e.get("kind", "?"), origin, parts.path or None,
                 e.get("title"), e.get("target"), e.get("len"), thash, preview),
            )
        conn.commit()
        return len(events)
    finally:
        conn.close()


def db_stats():
    """Read live counts + a few recent events from activity.db."""
    logger.open_db(DB).close()  # ensure schema exists
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
        today = conn.execute("SELECT COUNT(*) FROM events WHERE ts >= ?", (midnight,)).fetchone()[0]
        apps = conn.execute("SELECT COUNT(DISTINCT app) FROM events WHERE app IS NOT NULL").fetchone()[0]
        handoffs = conn.execute("SELECT COUNT(*) FROM events WHERE kind='clipboard'").fetchone()[0]
        rows = conn.execute(
            "SELECT ts, kind, app, title, clip_type, clip_preview FROM events "
            "WHERE kind IN ('focus','clipboard') ORDER BY id DESC LIMIT 8"
        ).fetchall()
        recent = [{
            "time": datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S"),
            "kind": r["kind"],
            "app": r["app"] or "",
            "title": (r["title"] or "")[:70],
            "clip": r["clip_preview"] or (r["clip_type"] or ""),
        } for r in rows]
        # Web events (from the browser extension), if that table exists yet.
        web_total = web_today = web_sites = 0
        try:
            web_total = conn.execute("SELECT COUNT(*) FROM web_events").fetchone()[0]
            web_today = conn.execute("SELECT COUNT(*) FROM web_events WHERE ts >= ?", (midnight,)).fetchone()[0]
            web_sites = conn.execute("SELECT COUNT(DISTINCT origin) FROM web_events WHERE origin IS NOT NULL").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        return {"total": total, "events_today": today, "apps": apps, "handoffs": handoffs,
                "web_total": web_total, "web_today": web_today, "web_sites": web_sites,
                "recent": recent}
    finally:
        conn.close()


def status_payload():
    s = db_stats()
    s["running"] = capture.is_running()
    s["started_at"] = capture.started_at
    return s


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # Loopback-only server; allow the browser extension to post events.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _admin_ok(self):
        """Bearer token from the admin console; 401 if missing/expired."""
        tok = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
        if auth.valid(tok):
            return True
        self._json({"error": "unauthorized"}, 401)
        return False

    def _admin_events(self, qs):
        from urllib.parse import parse_qs
        q = parse_qs(qs)
        limit = min(int((q.get("limit") or [200])[0]), 1000)
        offset = int((q.get("offset") or [0])[0])
        table = (q.get("table") or ["events"])[0]
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        try:
            if table == "web_events":
                cols = "id, ts, kind, origin, path, title, target, text_preview"
            elif table == "screenshots":
                cols = "id, ts, app, title, ahash, path"
            else:
                table, cols = "events", "id, ts, kind, app, title, clip_type, clip_preview"
            try:
                total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                rows = conn.execute(
                    f"SELECT {cols} FROM {table} ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()
            except sqlite3.OperationalError:
                return {"table": table, "total": 0, "rows": []}
            return {"table": table, "total": total, "rows": [dict(r) for r in rows]}
        finally:
            conn.close()

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(UI, "r", encoding="utf-8") as f:
                    html = f.read()
            except OSError:
                return self._send(500, "UI file not found", "text/plain")
            return self._send(200, "<!doctype html><meta charset=\"utf-8\">\n" + html, "text/html")
        if self.path == "/api/status":
            return self._json(status_payload())
        if self.path == "/api/config":
            return self._json(config.public_status())
        if self.path.split("?")[0] == "/api/flows":
            # Always deterministic. Cached AI annotations are layered on if the
            # user has run a review; no model is ever called from this path.
            data = mining.mine(DB)
            data["generated_ts"] = time.time()
            return self._json(data)
        if self.path.split("?")[0] == "/api/log":
            return self._json(mining.recent_log(DB, limit=40))
        if self.path == "/admin" or self.path.startswith("/admin?"):
            try:
                with open(os.path.join(BUNDLE, "ui", "admin.html"), "r", encoding="utf-8") as f:
                    return self._send(200, '<!doctype html><meta charset="utf-8">\n' + f.read(), "text/html")
            except OSError:
                return self._send(500, "admin UI not found", "text/plain")
        if self.path.split("?")[0] == "/api/admin/events":
            if not self._admin_ok():
                return
            return self._json(self._admin_events(self.path.partition("?")[2]))
        if self.path == "/api/admin/flows":
            if not self._admin_ok():
                return
            return self._json(mining.mine(DB, top=200))
        if self.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self._send(404, {"error": "not found"} if False else "not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/start":
            capture.start()
            return self._json(status_payload())
        if self.path == "/api/stop":
            capture.stop()
            return self._json(status_payload())
        if self.path == "/api/quit":
            # Fully end the app: stop capture, reply, then exit the whole process
            # (the tray keeps it alive otherwise, so a hard exit is the reliable way).
            capture.stop()
            self._json({"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        if self.path == "/api/ingest":
            body = self._read_json() or {}
            events = body.get("events") if isinstance(body, dict) else None
            n = ingest_web(events) if events else 0
            return self._json({"stored": n})
        if self.path == "/api/config":
            body = self._read_json() or {}
            config.update_ai(
                provider=body.get("provider"),
                api_key=body.get("api_key"),
                model=body.get("model"),
                clear_key=bool(body.get("clear_key")),
            )
            if "enabled" in body:
                config.update_ai(enabled=body.get("enabled"))
            if "screenshots" in body:
                config.set_screenshots(body.get("screenshots"))
            return self._json(config.public_status())
        if self.path == "/api/ai/review":
            # Explicit, user-initiated AI pass over already-detected flows.
            body = self._read_json() or {}
            return self._json(mining.annotate(DB, force=bool(body.get("force"))))
        # ---- admin (developer console) ------------------------------------
        if self.path == "/api/admin/login":
            body = self._read_json() or {}
            tok = auth.login(body.get("user"), body.get("password"))
            if not tok:
                return self._json({"error": "invalid credentials"}, 401)
            return self._json({"token": tok})
        if self.path == "/api/admin/logout":
            tok = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
            auth.logout(tok)
            return self._json({"ok": True})
        if self.path == "/api/admin/rule":
            if not self._admin_ok():
                return
            b = self._read_json() or {}
            mining.set_rule(DB, b.get("sig"), b.get("action"), b.get("label"))
            return self._json({"ok": True})
        if self.path == "/api/admin/purge-flow":
            if not self._admin_ok():
                return
            b = self._read_json() or {}
            return self._json({"removed": mining.purge_flow(DB, b.get("sig"))})
        if self.path == "/api/admin/purge":
            if not self._admin_ok():
                return
            b = self._read_json() or {}
            n = mining.purge(DB, b.get("scope", "all"), b.get("before_ts"), b.get("app"))
            return self._json({"removed": n})
        if self.path == "/api/admin/delete-row":
            if not self._admin_ok():
                return
            b = self._read_json() or {}
            table = b.get("table") if b.get("table") in ("events", "web_events", "screenshots") else None
            if not table or not b.get("id"):
                return self._json({"error": "bad request"}, 400)
            conn = sqlite3.connect(DB)
            try:
                n = conn.execute(f"DELETE FROM {table} WHERE id=?", (b["id"],)).rowcount
                conn.commit()
            finally:
                conn.close()
            return self._json({"removed": n})
        if self.path == "/api/ai/test":
            # Uses the local user's OWN key — billed to them, never the author.
            try:
                reply = ai.test_key()
                return self._json({"ok": True, "reply": reply})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        self._send(404, "not found", "text/plain")

    def log_message(self, *args):
        pass  # quiet


def build_server(port=8765):
    """Create the loopback server (and ensure the DB schema exists)."""
    logger.open_db(DB).close()  # create the DB/schema up front
    ensure_web_schema()         # create the web_events table up front
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    ap = argparse.ArgumentParser(description="workflow-mapper local server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    server = build_server(args.port)
    print(f"workflow-mapper running at {url}")
    print("Open it in your browser, then click “Start mapping”. Ctrl+C to quit.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
        capture.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
