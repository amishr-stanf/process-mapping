"""
Quick inspector for the activity.db captured by logger.py.

    python peek.py              # summary + last 25 events
    python peek.py --tail 100   # last 100 events
    python peek.py --apps        # time-in-app breakdown
"""

import argparse
import os
import sqlite3
import time
from datetime import datetime

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity.db")


def fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def summary(conn):
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if not total:
        print("No events yet. Run: python logger.py")
        return
    span = conn.execute("SELECT MIN(ts), MAX(ts) FROM events").fetchone()
    print(f"Events: {total}")
    print(f"Span:   {fmt(span[0])}  ->  {fmt(span[1])}")
    print("\nBy kind:")
    for kind, n in conn.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind ORDER BY 2 DESC"):
        print(f"  {kind:12} {n}")


def tail(conn, n):
    print(f"\nLast {n} events:")
    rows = conn.execute(
        "SELECT ts, kind, app, title, clip_type, clip_len, clip_preview "
        "FROM events ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    for ts, kind, app, title, ctype, clen, cprev in reversed(rows):
        line = f"{fmt(ts)}  {kind:11} {app or '-':22.22}"
        if kind == "focus":
            line += f" | {title or ''}"
        elif kind == "clipboard":
            extra = f" '{cprev}'" if cprev else ""
            line += f" | {ctype} len={clen}{extra}"
        print(line)


def apps(conn):
    """Rough time-in-app: sum gaps between consecutive focus events."""
    rows = conn.execute(
        "SELECT ts, app FROM events WHERE kind='focus' ORDER BY ts"
    ).fetchall()
    totals = {}
    for (ts0, app0), (ts1, _) in zip(rows, rows[1:]):
        gap = ts1 - ts0
        if gap < 3600:  # ignore overnight-style gaps
            totals[app0] = totals.get(app0, 0) + gap
    print("\nApprox foreground time by app:")
    for app, secs in sorted(totals.items(), key=lambda x: -x[1]):
        m = secs / 60
        print(f"  {app or '-':28.28} {m:7.1f} min")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--tail", type=int, default=25)
    ap.add_argument("--apps", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"No database at {args.db}. Run logger.py first.")
        return
    conn = sqlite3.connect(args.db)
    summary(conn)
    if args.apps:
        apps(conn)
    tail(conn, args.tail)
    conn.close()


if __name__ == "__main__":
    main()
