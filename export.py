"""
Weekly export: package this person's captured workflow data into one file.

Consent matters here. This file is meant to be sent to the person's employer,
so nothing is uploaded automatically and nothing leaves the machine without an
explicit click. The app shows exactly what the file contains before sending,
and the raw capture (window titles, clipboard previews, typed values) is NOT
included -- only aggregates and normalized flow signatures.

Consumed by aggregate.py, which rolls many of these up per employee and company.
"""

import json
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime

import config
import identity
import mining

SCHEMA = "workflow-mapper-export/1"


def _app_usage(db, since):
    """Approximate seconds spent per app, from consecutive focus events."""
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT ts, app FROM events WHERE kind='focus' AND ts >= ? ORDER BY ts",
            (since,)).fetchall()
        ui = defaultdict(int)
        try:
            for app, n in conn.execute(
                    "SELECT app, COUNT(*) FROM ui_events WHERE ts >= ? GROUP BY app", (since,)):
                ui[(app or "?").lower().replace(".exe", "")] += n
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()

    secs = defaultdict(float)
    counts = defaultdict(int)
    for (t0, a0), (t1, _) in zip(rows, rows[1:]):
        gap = t1 - t0
        if 0 < gap < 900:                      # ignore overnight-style gaps
            secs[(a0 or "?").lower().replace(".exe", "")] += gap
    for _, a in rows:
        counts[(a or "?").lower().replace(".exe", "")] += 1
    for a, n in ui.items():
        counts[a] += n

    apps = [{"app": a, "actions": counts.get(a, 0), "seconds": round(secs.get(a, 0.0), 1)}
            for a in set(list(secs) + list(counts))]
    apps.sort(key=lambda x: -x["seconds"])
    return apps, sum(secs.values())


def _daily(db, since):
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT ts FROM events WHERE ts >= ?", (since,)).fetchall()
    finally:
        conn.close()
    by_day = defaultdict(int)
    for (ts,) in rows:
        by_day[datetime.fromtimestamp(ts).strftime("%Y-%m-%d")] += 1
    return [{"date": d, "actions": n} for d, n in sorted(by_day.items())]


def build(db, days=7):
    """Assemble the export payload. Never includes raw captured content."""
    ident = identity.load()
    now = time.time()
    since = now - days * 86400

    mined = mining.mine(db, top=200)
    flows = []
    for f in mined.get("all_flows") or mined.get("flows") or []:
        flows.append({
            "sig": f["sig"],
            "sigs": f.get("sigs") or [f["sig"]],     # merged signatures
            "name": f["name"],
            "count": f["count"],
            "steps": f["steps"],
            "avg_seconds": f["avg_seconds"],
            "total_seconds": f["total_seconds"],
            "det_score": f.get("det_score"),
            "det_reason": f.get("det_reason"),
            "review": f.get("review"),
            "first_seen": f.get("first_seen"),
            "last_seen": f.get("last_seen"),
        })

    apps, active_seconds = _app_usage(db, since)
    return {
        "schema": SCHEMA,
        "exported_at": now,
        "install_id": ident["install_id"],
        "machine": ident.get("machine"),
        "employee": ident.get("employee", ""),
        "company": ident.get("company", ""),
        "opens": ident.get("opens", 0),
        "period": {"from": since, "to": now, "days": days},
        "period_days": days,
        "totals": {
            "actions": mined.get("event_count", 0),
            "sequences": mined.get("segment_count", 0),
            "flows": len(flows),
            "apps": len(apps),
            "active_seconds": round(active_seconds, 1),
        },
        "apps": apps[:40],
        "daily": _daily(db, since),
        "flows": flows,
    }


def preview(db, days=7):
    """What the export will contain — shown to the person before they send."""
    p = build(db, days)
    return {
        "employee": p["employee"], "company": p["company"],
        "machine": p.get("machine"), "days": days,
        "actions": p["totals"]["actions"], "sequences": p["totals"]["sequences"],
        "flows": p["totals"]["flows"], "apps": p["totals"]["apps"],
        "active_hours": round(p["totals"]["active_seconds"] / 3600, 1),
        "includes_raw_titles": False, "includes_clipboard_text": False,
        "identified": bool(p["employee"] and p["company"]),
    }


def write(db, days=7, out_dir=None):
    """Write the export next to the data dir; returns the path."""
    p = build(db, days)
    out_dir = out_dir or config.data_dir()
    os.makedirs(out_dir, exist_ok=True)
    who = (p["employee"] or "unknown").replace("@", "_at_")
    safe = "".join(ch for ch in who if ch.isalnum() or ch in "._-")[:40] or "unknown"
    co = "".join(ch for ch in (p["company"] or "company") if ch.isalnum() or ch in "._-")[:30]
    stamp = datetime.fromtimestamp(p["exported_at"]).strftime("%Y%m%d")
    path = os.path.join(out_dir, f"workflow-export_{co}_{safe}_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=1)

    d = identity.load()
    d["last_export_at"] = p["exported_at"]
    identity.save(d)
    return path
