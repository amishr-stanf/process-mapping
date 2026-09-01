#!/usr/bin/env python3
"""
aggregate.py — offline roll-up of workflow-mapper employee exports.

Employees each export a `workflow-mapper-export/1` JSON file (weekly) and send
it to the project owner. This script ingests a folder of those files and
produces one self-contained HTML report plus a short text summary.

    python aggregate.py <folder-of-exports> [--out report.html]

Stdlib only. No network. Nothing is written except the report file.

Design notes
------------
* Re-sends are expected. The unit of identity is (company, employee,
  install_id); only the export with the newest `exported_at` for that triple is
  counted. An employee with two machines therefore contributes two exports,
  while an employee who mails the same week twice contributes one.
* `sig` is a hash of the normalized step signature, so the *same* sig appearing
  under two different employees means they literally perform the same step
  sequence. Those are the shared processes — one automation pays for itself
  several times over — so they get the top section of the report.
* Every optional field is treated as optional: `review` may be null, `daily`
  may be absent, `totals` may be partial. Malformed files are skipped with a
  warning and never abort the run.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from collections import defaultdict

SCHEMA = "workflow-mapper-export/1"

# The Windows console is often cp1252, which cannot encode the characters used
# in flow names ("mail → excel"). Degrade gracefully rather than crash; the HTML
# report is UTF-8 regardless.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

# Automatability bands. Class names match the chip vocabulary already used in
# docs/detection-explained.html so the two documents read as one product.
BANDS = ((75, "full", "auto"), (50, "part", "assist"), (0, "blind", "manual"))


# --------------------------------------------------------------------------- #
# small coercion helpers — exports come from the field, assume nothing
# --------------------------------------------------------------------------- #

def _num(v, default=0.0):
    """Best-effort float. Never raises."""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return default
    return default


def _int(v, default=0):
    return int(_num(v, default))


def _str(v, default=""):
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip()
    return str(v)


def _dict(v):
    return v if isinstance(v, dict) else {}


def _list(v):
    return v if isinstance(v, list) else []


def band(score):
    """(chip-class, label) for an automatability score; None -> unscored."""
    if score is None:
        return ("none", "unscored")
    for cut, cls, label in BANDS:
        if score >= cut:
            return (cls, label)
    return ("blind", "manual")


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #

def iter_json_files(root):
    """Every *.json under root, recursively, in a stable order."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.lower().endswith(".json"):
                yield os.path.join(dirpath, fn)


def read_export(path):
    """Parse and validate one export. Returns (record, error_message)."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except OSError as e:
        return None, "unreadable (%s)" % e.__class__.__name__
    except UnicodeDecodeError:
        return None, "not UTF-8 text"
    except ValueError as e:
        return None, "invalid JSON (%s)" % str(e).split(":")[0]

    if not isinstance(doc, dict):
        return None, "top level is %s, expected object" % type(doc).__name__

    schema = _str(doc.get("schema"))
    if schema != SCHEMA:
        return None, "schema %r, expected %r" % (schema or None, SCHEMA)

    company = _str(doc.get("company"))
    employee = _str(doc.get("employee"))
    if not company or not employee:
        return None, "missing company or employee"

    install_id = _str(doc.get("install_id")) or "unknown-install"
    totals = _dict(doc.get("totals"))
    period = _dict(doc.get("period"))

    apps = []
    for a in _list(doc.get("apps")):
        a = _dict(a)
        name = _str(a.get("app"))
        if not name:
            continue
        apps.append({"app": name,
                     "actions": _int(a.get("actions")),
                     "seconds": _num(a.get("seconds"))})

    daily = []
    for d in _list(doc.get("daily")):
        d = _dict(d)
        date = _str(d.get("date"))
        if not date:
            continue
        daily.append({"date": date,
                      "actions": _int(d.get("actions")),
                      "active_seconds": _num(d.get("active_seconds")),
                      "sequences": _int(d.get("sequences"))})

    flows = []
    for f in _list(doc.get("flows")):
        f = _dict(f)
        sig = _str(f.get("sig"))
        if not sig:
            continue
        review = _dict(f.get("review"))          # may be null / absent
        det_score = f.get("det_score")
        det_score = _int(det_score) if det_score is not None else None
        r_score = review.get("score")
        r_score = _int(r_score) if r_score is not None else None
        score = r_score if r_score is not None else det_score

        steps = [_str(_dict(s).get("app")) for s in _list(f.get("steps"))]
        steps = [s for s in steps if s]
        count = max(_int(f.get("count")), 0)
        avg = _num(f.get("avg_seconds"))
        total = _num(f.get("total_seconds"))
        # Spec: estimated weekly cost = count * avg_seconds. Fall back to the
        # reported total when avg is missing, which happens on older exports.
        est = count * avg if avg else total

        flows.append({
            "sig": sig,
            "name": _str(review.get("name")) or _str(f.get("name")) or sig[:8],
            "review_name": _str(review.get("name")),
            "count": count,
            "avg_seconds": avg,
            "total_seconds": total,
            "est_seconds": est,
            "score": score,
            "score_src": "review" if r_score is not None else ("det" if det_score is not None else None),
            "det_score": det_score,
            "automatability": _str(review.get("automatability")),
            "reason": _str(review.get("reason")) or _str(f.get("det_reason")),
            "purpose": _str(review.get("purpose")),
            "slots": [_str(s) for s in _list(review.get("variable_slots")) if _str(s)],
            "apps": list(dict.fromkeys(steps)),
            "steps": len(steps),
            "first_seen": _num(f.get("first_seen")),
            "last_seen": _num(f.get("last_seen")),
        })

    rec = {
        "path": path,
        "company": company,
        "company_key": company.casefold(),
        "employee": employee,
        "employee_key": employee.casefold(),
        "install_id": install_id,
        "exported_at": _num(doc.get("exported_at")),
        "period_from": _num(period.get("from")),
        "period_to": _num(period.get("to")),
        "actions": _int(totals.get("actions")),
        "sequences": _int(totals.get("sequences")),
        "flow_count": _int(totals.get("flows")) or len(flows),
        "app_count": _int(totals.get("apps")) or len(apps),
        "active_seconds": _num(totals.get("active_seconds")),
        "apps": apps,
        "daily": daily,
        "flows": flows,
    }
    return rec, None


def dedupe(records):
    """Keep the newest export per (company, employee, install_id)."""
    best = {}
    superseded = []
    for r in records:
        key = (r["company_key"], r["employee_key"], r["install_id"])
        prev = best.get(key)
        if prev is None:
            best[key] = r
        elif r["exported_at"] >= prev["exported_at"]:
            best[key] = r
            superseded.append(prev)
        else:
            superseded.append(r)
    return list(best.values()), superseded


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #

def _merge_flow(bucket, f, employee_key, employee, company):
    """Fold one flow occurrence into a per-sig accumulator."""
    b = bucket.get(f["sig"])
    if b is None:
        b = bucket[f["sig"]] = {
            "sig": f["sig"], "names": defaultdict(int), "review_names": defaultdict(int),
            "count": 0, "est_seconds": 0.0, "total_seconds": 0.0,
            "best_score": None, "scores": [], "employees": {}, "companies": {},
            "apps": [], "steps": f["steps"], "reason": f["reason"],
            "purpose": f["purpose"], "slots": list(f["slots"]),
            "automatability": f["automatability"], "reviewed": False,
        }
    b["names"][f["name"]] += 1
    if f["review_name"]:
        b["review_names"][f["review_name"]] += 1
        b["reviewed"] = True
    b["count"] += f["count"]
    b["est_seconds"] += f["est_seconds"]
    b["total_seconds"] += f["total_seconds"]
    if f["score"] is not None:
        b["scores"].append(f["score"])
        if b["best_score"] is None or f["score"] > b["best_score"]:
            b["best_score"] = f["score"]
    if f["apps"] and len(f["apps"]) > len(b["apps"]):
        b["apps"] = f["apps"]
    b["steps"] = max(b["steps"], f["steps"])
    if f["purpose"] and not b["purpose"]:
        b["purpose"] = f["purpose"]
    if f["reason"] and not b["reason"]:
        b["reason"] = f["reason"]
    if f["automatability"] and not b["automatability"]:
        b["automatability"] = f["automatability"]
    for s in f["slots"]:
        if s not in b["slots"]:
            b["slots"].append(s)
    b["employees"].setdefault(employee_key, employee)
    b["companies"].setdefault(company, True)
    return b


def _finish_flow(b):
    names = b["review_names"] or b["names"]
    b["name"] = max(names.items(), key=lambda kv: (kv[1], -len(kv[0])))[0] if names else b["sig"][:8]
    b["employee_list"] = sorted(b["employees"].values(), key=str.casefold)
    b["company_list"] = sorted(b["companies"], key=str.casefold)
    b["n_employees"] = len(b["employee_list"])
    b["n_companies"] = len(b["company_list"])
    b["avg_seconds"] = (b["est_seconds"] / b["count"]) if b["count"] else 0.0
    return b


def _app_bucket(bucket, apps):
    for a in apps:
        e = bucket.setdefault(a["app"], {"app": a["app"], "actions": 0, "seconds": 0.0})
        e["actions"] += a["actions"]
        e["seconds"] += a["seconds"]


def build(records):
    """Roll records up into the structure the renderers consume."""
    companies = {}
    for r in records:
        c = companies.get(r["company_key"])
        if c is None:
            c = companies[r["company_key"]] = {
                "name": r["company"], "employees": {}, "apps": {}, "flows": {},
                "daily": {}, "installs": 0, "exports": 0,
                "period_from": None, "period_to": None,
            }
        c["exports"] += 1

        e = c["employees"].get(r["employee_key"])
        if e is None:
            e = c["employees"][r["employee_key"]] = {
                "employee": r["employee"], "installs": 0, "actions": 0,
                "sequences": 0, "active_seconds": 0.0, "apps": {}, "flows": {},
                "exported_at": 0.0, "period_from": None, "period_to": None,
            }
        e["installs"] += 1
        c["installs"] += 1
        e["actions"] += r["actions"]
        e["sequences"] += r["sequences"]
        e["active_seconds"] += r["active_seconds"]
        e["exported_at"] = max(e["exported_at"], r["exported_at"])

        for holder in (e, c):
            for key, val in (("period_from", r["period_from"]), ("period_to", r["period_to"])):
                if not val:
                    continue
                cur = holder[key]
                holder[key] = val if cur is None else (min(cur, val) if key == "period_from" else max(cur, val))

        _app_bucket(e["apps"], r["apps"])
        _app_bucket(c["apps"], r["apps"])

        for d in r["daily"]:
            day = c["daily"].setdefault(d["date"], {"date": d["date"], "actions": 0,
                                                    "active_seconds": 0.0, "sequences": 0})
            day["actions"] += d["actions"]
            day["active_seconds"] += d["active_seconds"]
            day["sequences"] += d["sequences"]

        for f in r["flows"]:
            _merge_flow(e["flows"], f, r["employee_key"], r["employee"], r["company"])
            _merge_flow(c["flows"], f, r["employee_key"], r["employee"], r["company"])

    # finalize
    out_companies = []
    global_apps, global_flows, global_daily = {}, {}, {}
    for c in companies.values():
        emps = []
        for e in c["employees"].values():
            flows = [_finish_flow(b) for b in e["flows"].values()]
            flows.sort(key=lambda f: (f["best_score"] if f["best_score"] is not None else -1,
                                      f["est_seconds"], f["count"]), reverse=True)
            e["flow_list"] = flows
            e["flow_count"] = len(flows)
            e["app_count"] = len(e["apps"])
            e["app_list"] = sorted(e["apps"].values(), key=lambda a: a["seconds"], reverse=True)
            e["est_seconds"] = sum(f["est_seconds"] for f in flows)
            e["top_flow"] = flows[0] if flows else None
            emps.append(e)
        emps.sort(key=lambda e: e["active_seconds"], reverse=True)
        c["employee_list"] = emps

        cflows = [_finish_flow(b) for b in c["flows"].values()]
        cflows.sort(key=lambda f: (f["best_score"] if f["best_score"] is not None else -1,
                                   f["est_seconds"], f["count"]), reverse=True)
        c["flow_list"] = cflows
        c["shared"] = sorted([f for f in cflows if f["n_employees"] > 1],
                             key=lambda f: (f["n_employees"], f["est_seconds"]), reverse=True)
        c["app_list"] = sorted(c["apps"].values(), key=lambda a: a["seconds"], reverse=True)
        c["actions"] = sum(e["actions"] for e in emps)
        c["sequences"] = sum(e["sequences"] for e in emps)
        c["active_seconds"] = sum(e["active_seconds"] for e in emps)
        c["est_seconds"] = sum(f["est_seconds"] for f in cflows)
        c["employee_count"] = len(emps)
        c["app_count"] = len(c["apps"])
        c["flow_count"] = len(cflows)
        c["daily_list"] = sorted(c["daily"].values(), key=lambda d: d["date"])
        out_companies.append(c)

        _app_bucket(global_apps, c["app_list"])
        for d in c["daily_list"]:
            g = global_daily.setdefault(d["date"], {"date": d["date"], "actions": 0,
                                                    "active_seconds": 0.0, "sequences": 0})
            g["actions"] += d["actions"]
            g["active_seconds"] += d["active_seconds"]
            g["sequences"] += d["sequences"]

    out_companies.sort(key=lambda c: c["active_seconds"], reverse=True)

    # Global flow index (fresh fold over records so employee sets span companies)
    for r in records:
        for f in r["flows"]:
            _merge_flow(global_flows, f, r["company_key"] + "|" + r["employee_key"],
                        r["employee"], r["company"])
    gflows = [_finish_flow(b) for b in global_flows.values()]
    shared = [f for f in gflows if f["n_employees"] > 1]
    shared.sort(key=lambda f: (f["n_companies"] > 1, f["n_employees"], f["est_seconds"]), reverse=True)
    gflows.sort(key=lambda f: (f["best_score"] if f["best_score"] is not None else -1,
                               f["est_seconds"], f["count"]), reverse=True)

    periods = [r["period_from"] for r in records if r["period_from"]] or [0]
    periods_to = [r["period_to"] for r in records if r["period_to"]] or [0]

    return {
        "companies": out_companies,
        "flows": gflows,
        "shared": shared,
        "apps": sorted(global_apps.values(), key=lambda a: a["seconds"], reverse=True),
        "daily": sorted(global_daily.values(), key=lambda d: d["date"]),
        "period_from": min(periods),
        "period_to": max(periods_to),
        "totals": {
            "companies": len(out_companies),
            "employees": sum(c["employee_count"] for c in out_companies),
            "installs": sum(c["installs"] for c in out_companies),
            "actions": sum(c["actions"] for c in out_companies),
            "sequences": sum(c["sequences"] for c in out_companies),
            "active_seconds": sum(c["active_seconds"] for c in out_companies),
            "apps": len(global_apps),
            "flows": len(gflows),
            "shared": len(shared),
            "shared_seconds": sum(f["est_seconds"] for f in shared),
            "est_seconds": sum(f["est_seconds"] for f in gflows),
        },
    }


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #

def esc(s):
    return html.escape(str(s), quote=True)


def n(v):
    return "{:,}".format(int(round(_num(v))))


def hrs(seconds, dp=1):
    return ("{:,.%df}" % dp).format(_num(seconds) / 3600.0)


def dur(seconds):
    """Compact human duration for a single run: 42s / 7m 0s / 1h 12m."""
    s = int(round(_num(seconds)))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm %02ds" % (s // 60, s % 60)
    return "%dh %02dm" % (s // 3600, (s % 3600) // 60)


def day(ts):
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def stamp(ts):
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def score_cell(score, src=None):
    cls, label = band(score)
    if score is None:
        return '<span class="chip none">unscored</span>'
    tag = "AI" if src == "review" else ""
    return ('<span class="chip %s">%s %s</span>%s'
            % (cls, label, score, ('<span class="src">%s</span>' % tag) if tag else ""))


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

CSS = """
  /* Light palette is the complete set; the other modes redefine only tokens. */
  :root {
    --bg:        #F7F6F3;
    --surface:   #FFFFFF;
    --surface-2: #F1EFEA;
    --line:      #E2DFD7;
    --line-2:    #CFCABE;
    --text:      #1A1A18;
    --muted:     #5C5A54;
    --faint:     #8A867C;
    --accent:    #A87A12;
    --accent-2:  #7A5807;
    --accent-soft: rgba(168,122,18,0.10);
    --ok:        #1F7A4D;
    --ok-soft:   rgba(31,122,77,0.10);
    --warn:      #9A6212;
    --warn-soft: rgba(154,98,18,0.10);
    --blind:     #A33B34;
    --blind-soft:rgba(163,59,52,0.09);
    --bar:       rgba(168,122,18,0.22);
    --serif: "IBM Plex Serif", Georgia, "Times New Roman", serif;
    --sans: "IBM Plex Sans", "Segoe UI", system-ui, -apple-system, sans-serif;
    --mono: "IBM Plex Mono", ui-monospace, Consolas, "Liberation Mono", monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:        #101216;
      --surface:   #171A20;
      --surface-2: #1D2128;
      --line:      #282D36;
      --line-2:    #3A404B;
      --text:      #EDEBE6;
      --muted:     #A2A79F;
      --faint:     #6E7580;
      --accent:    #E8B44A;
      --accent-2:  #F5CE7E;
      --accent-soft: rgba(232,180,74,0.13);
      --ok:        #4FCB8E;
      --ok-soft:   rgba(79,203,142,0.12);
      --warn:      #E0A64A;
      --warn-soft: rgba(224,166,74,0.12);
      --blind:     #E5726A;
      --blind-soft:rgba(229,114,106,0.12);
      --bar:       rgba(232,180,74,0.24);
    }
  }
  :root[data-theme="dark"] {
    --bg:#101216; --surface:#171A20; --surface-2:#1D2128; --line:#282D36; --line-2:#3A404B;
    --text:#EDEBE6; --muted:#A2A79F; --faint:#6E7580;
    --accent:#E8B44A; --accent-2:#F5CE7E; --accent-soft:rgba(232,180,74,0.13);
    --ok:#4FCB8E; --ok-soft:rgba(79,203,142,0.12);
    --warn:#E0A64A; --warn-soft:rgba(224,166,74,0.12);
    --blind:#E5726A; --blind-soft:rgba(229,114,106,0.12);
    --bar:rgba(232,180,74,0.24);
  }
  :root[data-theme="light"] {
    --bg:#F7F6F3; --surface:#FFFFFF; --surface-2:#F1EFEA; --line:#E2DFD7; --line-2:#CFCABE;
    --text:#1A1A18; --muted:#5C5A54; --faint:#8A867C;
    --accent:#A87A12; --accent-2:#7A5807; --accent-soft:rgba(168,122,18,0.10);
    --ok:#1F7A4D; --ok-soft:rgba(31,122,77,0.10);
    --warn:#9A6212; --warn-soft:rgba(154,98,18,0.10);
    --blind:#A33B34; --blind-soft:rgba(163,59,52,0.09);
    --bar:rgba(168,122,18,0.22);
  }

  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: var(--sans); font-size: 16px; line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1060px; margin: 0 auto; padding: 52px 24px 96px; }
  .mono { font-family: var(--mono); }
  .tnum, td.num, th.num { font-variant-numeric: tabular-nums; }
  td.num, th.num { text-align: right; }

  .eyebrow { font-family: var(--mono); font-size: 11px; letter-spacing: .16em;
    text-transform: uppercase; color: var(--faint); }
  h1 { font-family: var(--serif); font-weight: 700; font-size: clamp(30px, 4.4vw, 44px);
    line-height: 1.12; letter-spacing: -.02em; margin: 10px 0 14px; text-wrap: balance; }
  .lede { font-size: 17.5px; color: var(--muted); max-width: 66ch; }
  h2 { font-family: var(--serif); font-weight: 600; font-size: 25px; letter-spacing: -.015em;
    margin: 0 0 6px; text-wrap: balance; }
  h3 { font-size: 15px; font-weight: 600; margin: 0 0 6px; }
  p { max-width: 70ch; }
  section { margin-top: 52px; }
  .sec-head { border-top: 1px solid var(--line); padding-top: 18px; margin-bottom: 20px; }
  .sec-head .eyebrow { display: block; margin-bottom: 8px; }
  .sec-head p { color: var(--muted); margin: 6px 0 0; font-size: 15px; }
  .sec-head.hot { border-top: 2px solid var(--accent); }

  /* chips */
  .chip { display: inline-flex; align-items: center; gap: 6px; font-family: var(--mono);
    font-size: 10.5px; letter-spacing: .04em; text-transform: uppercase;
    padding: 3px 8px; border-radius: 4px; border: 1px solid; white-space: nowrap; }
  .chip.full  { color: var(--ok);    background: var(--ok-soft);    border-color: color-mix(in srgb, var(--ok) 35%, transparent); }
  .chip.part  { color: var(--warn);  background: var(--warn-soft);  border-color: color-mix(in srgb, var(--warn) 35%, transparent); }
  .chip.blind { color: var(--blind); background: var(--blind-soft); border-color: color-mix(in srgb, var(--blind) 35%, transparent); }
  .chip.none  { color: var(--faint); background: var(--surface-2);  border-color: var(--line-2); }
  .chip.gold  { color: var(--accent); background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
  .src { font-family: var(--mono); font-size: 9.5px; color: var(--faint); margin-left: 6px; letter-spacing: .08em; }

  /* stat grid */
  /* Dividers are drawn as inset shadows rather than grid gaps, so a part-filled
     last row leaves clean surface instead of a slab of divider colour. */
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
    overflow: hidden; margin-top: 24px; }
  .stat { background: var(--surface); padding: 15px 17px;
    box-shadow: inset 1px 1px 0 var(--line); }
  .stat .k { font-family: var(--mono); font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase; color: var(--faint); }
  .stat .v { font-family: var(--serif); font-size: 28px; font-weight: 600; margin-top: 4px; letter-spacing: -.02em;
    font-variant-numeric: tabular-nums; }
  .stat .v small { font-family: var(--sans); font-size: 13px; font-weight: 400; color: var(--muted); letter-spacing: 0; }
  .stat.hi .v { color: var(--accent); }

  /* tables */
  /* Wide tables scroll inside their own container rather than squeezing the
     name column down to one word per line. */
  .tablewrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px;
    background: var(--surface); margin-top: 14px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; min-width: 660px; }
  table.narrow { min-width: 380px; }
  th { text-align: left; font-family: var(--mono); font-size: 10.5px; letter-spacing: .1em;
    text-transform: uppercase; color: var(--faint); font-weight: 500;
    padding: 11px 14px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  td { padding: 11px 14px; border-bottom: 1px solid var(--line); vertical-align: top; color: var(--muted); }
  tr:last-child td { border-bottom: none; }
  td.strong { color: var(--text); font-weight: 500; }
  td.mono { font-family: var(--mono); font-size: 12.5px; }
  td.flow { color: var(--text); min-width: 230px; }
  td.flow .path { font-family: var(--mono); font-size: 11.5px; color: var(--accent);
    display: block; margin-top: 3px; overflow-wrap: anywhere; }
  td.flow .why { font-size: 12.5px; color: var(--faint); display: block; margin-top: 3px; max-width: 46ch; }
  td.people { font-size: 12.5px; min-width: 170px; overflow-wrap: anywhere; }
  td.people b { color: var(--text); font-weight: 500; }
  .sig { font-family: var(--mono); font-size: 10.5px; color: var(--faint); }
  tr.hot td { background: var(--accent-soft); }

  /* app time bars — label and value on one line, track full width beneath, so
     the bar stays readable however narrow the column gets */
  .bars { display: flex; flex-direction: column; gap: 10px; margin-top: 14px; }
  .bars .row { display: grid; grid-template-columns: 1fr auto; gap: 4px 10px; align-items: baseline; }
  .bars .lbl { font-family: var(--mono); font-size: 12.5px; color: var(--text); overflow-wrap: anywhere; }
  .bars .val { font-family: var(--mono); font-size: 12px; color: var(--muted);
    font-variant-numeric: tabular-nums; white-space: nowrap; text-align: right; }
  .bars .track { grid-column: 1 / -1; background: var(--surface-2); border: 1px solid var(--line);
    border-radius: 3px; height: 9px; overflow: hidden; }
  .bars .fill { background: var(--bar); border-right: 2px solid var(--accent); height: 100%; }

  /* company block */
  .co { border: 1px solid var(--line); border-radius: 12px; background: var(--surface);
    padding: 20px 22px 24px; margin-top: 18px; }
  .co-head { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; flex-wrap: wrap;
    border-bottom: 1px solid var(--line); padding-bottom: 12px; margin-bottom: 4px; }
  .co-head h3 { font-family: var(--serif); font-size: 21px; font-weight: 600; margin: 0; letter-spacing: -.01em; }
  .co-head .meta { font-family: var(--mono); font-size: 11.5px; color: var(--faint);
    font-variant-numeric: tabular-nums; }
  .sub { font-family: var(--mono); font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase;
    color: var(--faint); margin: 22px 0 0; }
  .grid2 { display: grid; grid-template-columns: 1.25fr 1fr; gap: 26px; align-items: start; }
  /* Grid items default to min-width:auto and would otherwise refuse to shrink
     below the table's min-width, pushing the whole page sideways. */
  .grid2 > * { min-width: 0; }
  .tablewrap { max-width: 100%; }

  /* callout */
  .callout { border: 1px solid var(--line); border-left: 3px solid var(--accent);
    background: var(--surface); border-radius: 0 10px 10px 0; padding: 16px 19px; margin-top: 20px; }
  .callout h3 { font-family: var(--serif); font-size: 17px; margin-bottom: 6px; }
  .callout p { margin: 0; color: var(--muted); font-size: 14.5px; }
  .callout.warn { border-left-color: var(--blind); }
  .callout ul { margin: 8px 0 0; padding-left: 18px; color: var(--muted); font-size: 13.5px; }
  .callout li { margin-bottom: 2px; overflow-wrap: anywhere; }

  .empty { color: var(--faint); font-size: 14px; font-style: italic; margin-top: 12px; }
  .footer { margin-top: 60px; border-top: 1px solid var(--line); padding-top: 18px;
    font-family: var(--mono); font-size: 11.5px; color: var(--faint); line-height: 1.9; }
  @media (max-width: 860px) {
    .grid2 { grid-template-columns: 1fr; gap: 8px; }
    .wrap { padding: 34px 16px 64px; }
    .co { padding: 16px 14px 20px; }
  }
"""

FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@600;700&'
         'family=IBM+Plex+Mono:wght@400;500;600&display=swap">')


def app_bars(apps, limit=8):
    apps = [a for a in apps if a["seconds"] > 0][:limit]
    if not apps:
        return '<p class="empty">No per-app time reported.</p>'
    top = max(a["seconds"] for a in apps)
    rows = ['<div class="bars">']
    for a in apps:
        pct = max(2.0, 100.0 * a["seconds"] / top)
        rows.append(
            '<div class="row"><div class="lbl">%s</div><div class="val">%s h</div>'
            '<div class="track"><div class="fill" style="width:%.1f%%"></div></div></div>'
            % (esc(a["app"]), hrs(a["seconds"]), pct))
    rows.append("</div>")
    return "".join(rows)


def flow_rows(flows, show_people=False, limit=10, hot_multi=False):
    out = []
    for f in flows[:limit]:
        multi = f["n_employees"] > 1
        cls = ' class="hot"' if (hot_multi and multi) else ""
        path = " → ".join(f["apps"][:5]) if f["apps"] else ""
        why = f["purpose"] or f["reason"] or ""
        people = ""
        if show_people:
            names = ", ".join(f["employee_list"][:4])
            if f["n_employees"] > 4:
                names += " +%d more" % (f["n_employees"] - 4)
            cos = ""
            if f["n_companies"] > 1:
                cos = '<br><span class="chip gold">%d companies</span>' % f["n_companies"]
            people = ('<td class="people"><b>%d</b> %s<br>%s%s</td>'
                      % (f["n_employees"], "person" if f["n_employees"] == 1 else "people",
                         esc(names), cos))
        out.append(
            "<tr%s>"
            '<td class="flow"><span class="strong">%s</span>'
            '%s%s<span class="sig">%s</span></td>'
            "%s"
            '<td class="num">%s</td>'
            '<td class="num">%s</td>'
            '<td class="num">%s</td>'
            "<td>%s</td>"
            "</tr>"
            % (cls, esc(f["name"]),
               ('<span class="path">%s</span>' % esc(path)) if path else "",
               ('<span class="why">%s</span>' % esc(why[:180])) if why else "",
               esc(f["sig"][:10]),
               people,
               n(f["count"]),
               dur(f["avg_seconds"]),
               hrs(f["est_seconds"], 1),
               score_cell(f["best_score"], "review" if f["reviewed"] else "det")))
    return "".join(out)


def render_html(rep, meta):
    T = rep["totals"]
    P = []
    A = P.append

    # charset first: flow names carry "→" and app names can be non-ASCII, and a
    # file:// page with no declared charset is decoded as the local codepage.
    A('<meta charset="utf-8">')
    A('<meta name="viewport" content="width=device-width, initial-scale=1">')
    A("<title>Workflow Fleet Report</title>")
    A(FONTS)
    A("<style>%s</style>" % CSS)
    A('<div class="wrap">')

    # ---- header ----------------------------------------------------------
    A("<header>")
    A('<span class="eyebrow">workflow-mapper · fleet roll-up</span>')
    A("<h1>Where the repeated work is</h1>")
    if T["employees"]:
        A('<p class="lede">%s from %s across %s, covering %s to %s. '
          "Ranked by what one automation would buy back.</p>"
          % (plural(meta["files_ok"], "export"), plural(T["employees"], "person", "people"),
             plural(T["companies"], "company", "companies"),
             day(rep["period_from"]), day(rep["period_to"])))
    else:
        A('<p class="lede">No usable exports were found in this folder.</p>')

    A('<div class="stats">')
    A(stat("Companies", n(T["companies"])))
    A(stat("People", n(T["employees"]), "%s installs" % n(T["installs"])))
    A(stat("Actions", n(T["actions"])))
    A(stat("Active time", hrs(T["active_seconds"], 0), "hours"))
    A(stat("Distinct apps", n(T["apps"])))
    A(stat("Distinct flows", n(T["flows"])))
    A(stat("Shared processes", n(T["shared"]), "2+ people", hi=True))
    A(stat("Shared time", hrs(T["shared_seconds"], 0), "hours / period", hi=True))
    A("</div>")
    A("</header>")

    # ---- shared processes (the headline) ---------------------------------
    A("<section>")
    A('<div class="sec-head hot">')
    A('<span class="eyebrow">Priority one</span>')
    A("<h2>Shared processes</h2>")
    A("<p>Flows with an identical step signature seen at more than one person. "
      "These are the highest-value targets: one automation is built once and "
      "removes the same work from every person on the row. Rows spanning more "
      "than one company are worth the most — they are a product, not a favour.</p>")
    A("</div>")
    if rep["shared"]:
        A('<div class="tablewrap"><table><thead><tr>'
          "<th>Process</th><th>Seen by</th><th class=\"num\">Runs</th>"
          '<th class="num">Avg run</th><th class="num">Est. hours</th><th>Automatability</th>'
          "</tr></thead><tbody>")
        A(flow_rows(rep["shared"], show_people=True, limit=25, hot_multi=False))
        A("</tbody></table></div>")
        cross = [f for f in rep["shared"] if f["n_companies"] > 1]
        if cross:
            A('<div class="callout"><h3>Same process at more than one client</h3>'
              "<p>%s appear at multiple companies. Build these first — the same "
              "artefact ships to every client.</p><ul>%s</ul></div>"
              % (plural(len(cross), "process", "processes"),
                 "".join("<li><b>%s</b> — %s · %s people · %s h</li>"
                         % (esc(f["name"]), esc(", ".join(f["company_list"])),
                            f["n_employees"], hrs(f["est_seconds"]))
                         for f in cross[:6])))
    else:
        A('<p class="empty">No flow signature was seen at more than one person yet. '
          "Either coverage is still thin, or these teams genuinely do different work.</p>")
    A("</section>")

    # ---- fleet roll-up ---------------------------------------------------
    A("<section>")
    A('<div class="sec-head"><span class="eyebrow">Fleet</span>'
      "<h2>Across every company</h2>"
      "<p>Where the hours actually go, and how the client companies compare.</p></div>")
    A('<div class="grid2">')
    A("<div>")
    A('<p class="sub" style="margin-top:0">Companies</p>')
    A('<div class="tablewrap"><table class="narrow"><thead><tr>'
      '<th>Company</th><th class="num">People</th><th class="num">Actions</th>'
      '<th class="num">Active h</th><th class="num">Flows</th><th class="num">Shared</th>'
      "</tr></thead><tbody>")
    for c in rep["companies"]:
        A("<tr>"
          '<td class="strong">%s</td>'
          '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
          '<td class="num">%s</td><td class="num">%s</td></tr>'
          % (esc(c["name"]), n(c["employee_count"]), n(c["actions"]),
             hrs(c["active_seconds"], 0), n(c["flow_count"]), n(len(c["shared"]))))
    if not rep["companies"]:
        A('<tr><td colspan="6">No companies.</td></tr>')
    A("</tbody></table></div>")
    A("</div>")
    A("<div>")
    A('<p class="sub" style="margin-top:0">Time by app</p>')
    A(app_bars(rep["apps"], limit=9))
    A("</div>")
    A("</div>")

    if rep["flows"]:
        A('<p class="sub">Most automatable flows, everyone</p>')
        A('<div class="tablewrap"><table><thead><tr>'
          '<th>Flow</th><th>Seen by</th><th class="num">Runs</th><th class="num">Avg run</th>'
          '<th class="num">Est. hours</th><th>Automatability</th>'
          "</tr></thead><tbody>")
        A(flow_rows(rep["flows"], show_people=True, limit=12, hot_multi=True))
        A("</tbody></table></div>")
    A("</section>")

    # ---- per company -----------------------------------------------------
    A("<section>")
    A('<div class="sec-head"><span class="eyebrow">By company</span>'
      "<h2>Client detail</h2>"
      "<p>One block per company: the people, their top apps, and the flows worth "
      "attacking there. Rows highlighted in gold are shared with a colleague.</p></div>")
    for c in rep["companies"]:
        A(company_block(c))
    if not rep["companies"]:
        A('<p class="empty">Nothing to show.</p>')
    A("</section>")

    # ---- ingest log ------------------------------------------------------
    A("<section>")
    A('<div class="sec-head"><span class="eyebrow">Provenance</span>'
      "<h2>What was ingested</h2>"
      "<p>Every file the aggregator touched, and what it decided to do with it.</p></div>")
    A('<div class="stats">')
    A(stat("JSON files seen", n(meta["files_seen"])))
    A(stat("Accepted", n(meta["files_ok"])))
    A(stat("Superseded re-sends", n(meta["dupes"])))
    A(stat("Skipped", n(len(meta["bad"]))))
    A("</div>")
    if meta["bad"]:
        A('<div class="callout warn"><h3>Skipped files</h3>'
          "<p>These were not readable as %s exports. Nothing from them is counted.</p><ul>%s</ul></div>"
          % (SCHEMA, "".join("<li><span class=\"mono\">%s</span> — %s</li>"
                             % (esc(os.path.basename(p)), esc(m)) for p, m in meta["bad"][:20])))
    A("</section>")

    A('<div class="footer">workflow-mapper · aggregate.py · generated %s · '
      "source folder <span class=\"mono\">%s</span><br>"
      "Estimated hours are count × avg_seconds per flow, summed. "
      "Automatability is the reviewed score when a flow has been reviewed, otherwise the "
      "deterministic score. Deduplication key: company + employee + install_id, newest export wins.</div>"
      % (esc(stamp(meta["generated"])), esc(meta["folder"])))
    A("</div>")
    return "\n".join(P)


def plural(count, one, many=None):
    many = many or (one + "s")
    return "%s %s" % (n(count), one if int(count) == 1 else many)


def stat(k, v, small=None, hi=False):
    return ('<div class="stat%s"><div class="k">%s</div><div class="v">%s%s</div></div>'
            % (" hi" if hi else "", esc(k), esc(v),
               (' <small>%s</small>' % esc(small)) if small else ""))


def company_block(c):
    P = []
    A = P.append
    A('<div class="co">')
    A('<div class="co-head"><h3>%s</h3><span class="meta">%s · %s people · %s h active · '
      "%s flows · %s shared</span></div>"
      % (esc(c["name"]), day(c["period_from"]) + " → " + day(c["period_to"]),
         n(c["employee_count"]), hrs(c["active_seconds"], 0),
         n(c["flow_count"]), n(len(c["shared"]))))

    A('<p class="sub">People</p>')
    A('<div class="tablewrap"><table><thead><tr>'
      '<th>Person</th><th class="num">Actions</th><th class="num">Sequences</th>'
      '<th class="num">Active h</th><th class="num">Apps</th><th class="num">Flows</th>'
      "<th>Top flow</th><th>Last export</th>"
      "</tr></thead><tbody>")
    for e in c["employee_list"]:
        tf = e["top_flow"]
        top = ("%s %s" % (esc(tf["name"]), score_cell(tf["best_score"],
                                                      "review" if tf["reviewed"] else "det"))
               ) if tf else '<span class="chip none">none yet</span>'
        A("<tr>"
          '<td class="strong">%s%s</td>'
          '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
          '<td class="num">%s</td><td class="num">%s</td>'
          "<td>%s</td>"
          '<td class="mono">%s</td></tr>'
          % (esc(e["employee"]),
             ('<br><span class="sig">%d installs</span>' % e["installs"]) if e["installs"] > 1 else "",
             n(e["actions"]), n(e["sequences"]), hrs(e["active_seconds"]),
             n(e["app_count"]), n(e["flow_count"]), top, esc(day(e["exported_at"]))))
    A("</tbody></table></div>")

    A('<div class="grid2" style="margin-top:6px">')
    A("<div>")
    A('<p class="sub">Flows worth attacking</p>')
    if c["flow_list"]:
        A('<div class="tablewrap"><table><thead><tr>'
          '<th>Flow</th><th>Seen by</th><th class="num">Runs</th><th class="num">Avg run</th>'
          '<th class="num">Est. h</th><th>Automatability</th>'
          "</tr></thead><tbody>")
        A(flow_rows(c["flow_list"], show_people=True, limit=8, hot_multi=True))
        A("</tbody></table></div>")
    else:
        A('<p class="empty">No flows detected yet at this company.</p>')
    A("</div>")
    A("<div>")
    A('<p class="sub">Time by app</p>')
    A(app_bars(c["app_list"], limit=7))
    A("</div>")
    A("</div>")
    A("</div>")
    return "".join(P)


# --------------------------------------------------------------------------- #
# text summary
# --------------------------------------------------------------------------- #

def print_summary(rep, meta, out_path):
    T = rep["totals"]
    w = sys.stdout.write
    w("\nworkflow-mapper aggregate\n")
    w("=" * 62 + "\n")
    w("folder      %s\n" % meta["folder"])
    w("files       %d json, %d accepted, %d superseded re-sends, %d skipped\n"
      % (meta["files_seen"], meta["files_ok"], meta["dupes"], len(meta["bad"])))
    for p, m in meta["bad"]:
        w("  ! skipped %s - %s\n" % (os.path.basename(p), m))
    if not meta["files_ok"]:
        w("\nNothing to aggregate. Report written to %s\n" % out_path)
        return
    w("period      %s -> %s\n" % (day(rep["period_from"]), day(rep["period_to"])))
    w("fleet       %d companies, %d people, %d installs, %s actions, %s active hours\n"
      % (T["companies"], T["employees"], T["installs"], n(T["actions"]), hrs(T["active_seconds"], 0)))
    w("flows       %d distinct, %d shared by 2+ people, %s est. hours in shared work\n"
      % (T["flows"], T["shared"], hrs(T["shared_seconds"], 0)))

    if rep["shared"]:
        w("\nshared processes (highest value)\n")
        w("-" * 62 + "\n")
        for f in rep["shared"][:8]:
            cls, label = band(f["best_score"])
            w("  %-38s %2d ppl  %6s h  %s %s\n"
              % (f["name"][:38], f["n_employees"], hrs(f["est_seconds"]),
                 label, "" if f["best_score"] is None else f["best_score"]))
            if f["n_companies"] > 1:
                w("      across: %s\n" % ", ".join(f["company_list"]))

    w("\nby company\n")
    w("-" * 62 + "\n")
    for c in rep["companies"]:
        w("  %-28s %2d ppl  %7s h  %3d flows  %2d shared\n"
          % (c["name"][:28], c["employee_count"], hrs(c["active_seconds"], 0),
             c["flow_count"], len(c["shared"])))
        top = c["flow_list"][0] if c["flow_list"] else None
        if top:
            w("      top: %s (%s h, score %s)\n"
              % (top["name"][:44], hrs(top["est_seconds"]),
                 "n/a" if top["best_score"] is None else top["best_score"]))

    w("\nreport      %s\n\n" % out_path)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="aggregate.py",
        description="Roll up workflow-mapper employee exports into one HTML report.")
    ap.add_argument("folder", help="folder of *.json exports (searched recursively)")
    ap.add_argument("--out", default="report.html", help="output HTML file (default: report.html)")
    ap.add_argument("--quiet", action="store_true", help="write the report, skip the text summary")
    args = ap.parse_args(argv)

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        sys.stderr.write("aggregate.py: not a folder: %s\n" % folder)
        return 2

    records, bad, seen = [], [], 0
    for path in iter_json_files(folder):
        seen += 1
        rec, err = read_export(path)
        if err:
            bad.append((path, err))
            sys.stderr.write("warning: skipping %s - %s\n" % (path, err))
            continue
        records.append(rec)

    kept, superseded = dedupe(records)
    rep = build(kept)
    meta = {
        "folder": folder,
        "files_seen": seen,
        "files_ok": len(kept),
        "dupes": len(superseded),
        "bad": bad,
        "generated": time.time(),
    }

    out_path = os.path.abspath(args.out)
    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_html(rep, meta))

    if not args.quiet:
        print_summary(rep, meta, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
