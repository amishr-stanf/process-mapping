"""
Mining layer — turns the raw captured event stream into segmented, ranked,
repeated flows.

Two stages, matching the project's architecture boundary:

  1. DETERMINISTIC (no AI, always runs): merge the desktop + web event streams,
     segment into candidate flows by idle/time gaps, normalize each action so
     the same task with different data collapses, cluster identical signatures,
     and rank by total time. This is the backbone and works with AI off.

  2. AI REVIEW (optional, BYOK): hand the deterministic candidates to the
     user's own LLM to name them, judge whether each is one coherent task
     (catching over/under-segmentation), rate automatability, and identify the
     variable slots. Reads deterministic output, annotates it, never captures.

CLI:
    python mining.py --db activity.db            # deterministic report
    python mining.py --db activity.db --review   # + AI review (needs a key)
"""

import argparse
import hashlib
import json
import re
import sqlite3
import time
from collections import defaultdict

import ai
import config

# Admin-authored rules that override the automatic clustering.
RULES_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_rules (
    sig    TEXT PRIMARY KEY,   -- sha1 of the step signature
    action TEXT NOT NULL,      -- rename | merge | hide
    label  TEXT,               -- new name, or the group to merge into
    ts     REAL
);
"""

# Cached AI annotations, keyed by flow signature. Written only when the user
# runs a review; read on every request so the dashboard never calls a model.
ANNOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_annotations (
    sig     TEXT PRIMARY KEY,
    name    TEXT,      -- human task name
    purpose TEXT,      -- what the user is actually accomplishing
    auto    TEXT,      -- auto | assist | skip
    score   INTEGER,   -- 0-100 automatability
    reason  TEXT,
    slots   TEXT,      -- JSON list of variable slots
    coherent INTEGER,  -- 1 if the AI thinks this is one real task
    model   TEXT,
    ts      REAL
);
"""


def ensure_annotations(conn):
    conn.executescript(ANNOT_SCHEMA)
    conn.commit()


def load_annotations(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        ensure_annotations(conn)
        out = {}
        for r in conn.execute("SELECT * FROM flow_annotations"):
            d = dict(r)
            try:
                d["slots"] = json.loads(d.get("slots") or "[]")
            except ValueError:
                d["slots"] = []
            d["coherent"] = bool(d.get("coherent"))
            out[d["sig"]] = d
        return out
    finally:
        conn.close()


def ensure_rules(conn):
    conn.executescript(RULES_SCHEMA)
    conn.commit()


def sig_hash(steps):
    """Stable id for a step signature, used to attach admin rules."""
    raw = "|".join(f"{a}:{v}:{o}" for (a, v, o) in steps)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def load_rules(db):
    conn = sqlite3.connect(db)
    try:
        ensure_rules(conn)
        return {r[0]: {"action": r[1], "label": r[2]}
                for r in conn.execute("SELECT sig, action, label FROM flow_rules")}
    finally:
        conn.close()


def set_rule(db, sig, action, label=None):
    conn = sqlite3.connect(db)
    try:
        ensure_rules(conn)
        if action == "clear":
            conn.execute("DELETE FROM flow_rules WHERE sig=?", (sig,))
        else:
            conn.execute("INSERT OR REPLACE INTO flow_rules (sig, action, label, ts) VALUES (?,?,?,?)",
                         (sig, action, label, time.time()))
        conn.commit()
    finally:
        conn.close()

# Where a flow ENDS.
#
# The primary boundary is real user absence: the logger emits idle_start after
# 60s with no keyboard/mouse input. A gap between *events* is NOT the same
# thing — reading a PDF for five minutes produces one focus event and then
# silence, while the person is working the whole time. Splitting on a short
# event gap shatters long multi-app tasks, so the gap is only a safety net for
# cases where idle detection missed (e.g. the app was closed).
GAP_SECONDS = 900.0      # 15 min event gap = safety-net boundary
MAX_FLOW_SECONDS = 3600  # a single flow never spans more than an hour
MIN_STEPS = 2            # ignore trivial 1-action segments

# --- normalization: strip volatile detail so repeats collapse ---------------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_LONGID = re.compile(r"\b(?=[0-9A-Za-z]*\d)[0-9A-Za-z]{12,}\b")  # ids/hashes (has a digit)
_NUM = re.compile(r"\d+")


def normalize(s):
    if not s:
        return ""
    s = _EMAIL.sub("<email>", s.strip())
    s = _DATE.sub("<date>", s)
    s = _LONGID.sub("<id>", s)
    s = _NUM.sub("<n>", s)
    return s.lower()


def _app_name(app):
    return (app or "?").lower().replace(".exe", "")


def _domain(origin):
    return re.sub(r"^https?://", "", origin or "").rstrip("/") or "web"


def _field_name(target):
    if not target:
        return "?"
    m = re.search(r"name=([^\]\s]+)", target)
    if m:
        return normalize(m.group(1))
    m = re.search(r"«([^»]+)»", target)
    if m:
        return normalize(m.group(1))
    return normalize(target.split()[0]) if target.split() else "?"


def _click_label(target, text):
    m = re.search(r"«([^»]+)»", target or "")
    if m:
        return normalize(m.group(1))
    return normalize(text or target or "click")


# --- load + unify both event streams ----------------------------------------
def load_actions(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    actions = []
    try:
        for r in conn.execute("SELECT ts, kind, app, title, clip_type, clip_preview FROM events ORDER BY ts"):
            k = r["kind"]
            if k == "idle_start":
                actions.append({"ts": r["ts"], "verb": "_idle"})
            elif k == "focus":
                actions.append({"ts": r["ts"], "app": _app_name(r["app"]), "verb": "focus",
                                "obj": normalize(r["title"]), "_text": r["title"]})
            elif k == "clipboard":
                actions.append({"ts": r["ts"], "app": _app_name(r["app"]), "verb": "copy",
                                "obj": "clip:" + (r["clip_type"] or "?"), "_text": r["clip_preview"]})
        # In-app control interactions (generic OS accessibility sensor).
        try:
            ui = conn.execute("SELECT ts, app, verb, control, role, detail "
                              "FROM ui_events ORDER BY ts").fetchall()
        except sqlite3.OperationalError:
            ui = []
        for r in ui:
            ctrl = normalize(r["control"])
            if not ctrl:
                continue
            obj = ctrl if not r["detail"] else f"{ctrl} {normalize(r['detail'])}"
            actions.append({"ts": r["ts"], "app": _app_name(r["app"]),
                            "verb": r["verb"] or "invoke", "obj": obj,
                            "_text": r["control"]})

        try:
            web = conn.execute("SELECT ts, kind, origin, path, title, target, text_preview "
                               "FROM web_events ORDER BY ts").fetchall()
        except sqlite3.OperationalError:
            web = []
        for r in web:
            dom = _domain(r["origin"])
            k = r["kind"]
            if k in ("pageview", "nav"):
                actions.append({"ts": r["ts"], "app": dom, "verb": "visit", "obj": normalize(r["path"])})
            elif k == "click":
                actions.append({"ts": r["ts"], "app": dom, "verb": "click",
                                "obj": _click_label(r["target"], r["text_preview"])})
            elif k == "input":
                actions.append({"ts": r["ts"], "app": dom, "verb": "input",
                                "obj": "field:" + _field_name(r["target"]), "_text": r["text_preview"]})
            elif k == "select":
                actions.append({"ts": r["ts"], "app": dom, "verb": "read",
                                "obj": "selection", "_text": r["text_preview"]})
    finally:
        conn.close()
    actions.sort(key=lambda a: a["ts"])
    link_transfers(actions)
    return actions


# --- read-here -> type-there ------------------------------------------------
TRANSFER_WINDOW = 300.0   # seconds a read can influence a later write
_TOKEN = re.compile(r"[a-z0-9@._-]{3,}")


def _tokens(s):
    return set(_TOKEN.findall((s or "").lower()))


def _similar(src, dst):
    """True if the written text plausibly came from the read text."""
    a, b = (src or "").lower().strip(), (dst or "").lower().strip()
    if len(a) < 4 or len(b) < 4:
        return False
    if a in b or b in a:               # verbatim carry (retyped exactly)
        return True
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / min(len(ta), len(tb))
    return overlap >= 0.6              # reworded but same entities


def link_transfers(actions):
    """Detect information carried between apps WITHOUT the clipboard.

    Looking something up in app A and then typing it (verbatim or reworded)
    into app B is a real hand-off, but leaves no clipboard trace. We match the
    text of a read/copy against later writes and insert a synthetic 'carry'
    step so the flow signature records the dependency.
    """
    sources = [a for a in actions if a.get("_text") and a["verb"] in ("read", "copy", "focus")]
    if not sources:
        return actions
    inserts = []
    for a in actions:
        if a["verb"] != "input" or not a.get("_text"):
            continue
        for s in reversed(sources):
            if s["ts"] >= a["ts"] or a["ts"] - s["ts"] > TRANSFER_WINDOW:
                continue
            if s.get("app") == a.get("app"):
                continue               # same app: not a cross-app carry
            if _similar(s["_text"], a["_text"]):
                inserts.append({"ts": a["ts"] - 0.001, "app": a["app"], "verb": "carry",
                                "obj": "from:" + str(s.get("app")), "_text": None})
                break
    if inserts:
        actions.extend(inserts)
        actions.sort(key=lambda x: x["ts"])
    return actions


# --- stage 1: deterministic segmentation + clustering -----------------------
def segment(actions):
    """Cut the action stream into end-to-end sequences.

    Boundaries, in order of authority:
      1. idle_start  — the person actually stopped working (primary)
      2. a > GAP_SECONDS event gap — safety net when idle wasn't recorded
      3. MAX_FLOW_SECONDS — a runaway guard so one flow can't swallow a day
    """
    segments, cur, prev_ts = [], [], None
    for a in actions:
        if a["verb"] == "_idle":
            if cur:
                segments.append(cur); cur = []
            prev_ts = None
            continue
        too_long = cur and (a["ts"] - cur[0]["ts"]) > MAX_FLOW_SECONDS
        big_gap = prev_ts is not None and (a["ts"] - prev_ts) > GAP_SECONDS
        if (big_gap or too_long) and cur:
            segments.append(cur); cur = []
        cur.append(a)
        prev_ts = a["ts"]
    if cur:
        segments.append(cur)
    return [s for s in segments if len(s) >= MIN_STEPS]


def _steps(seg):
    """Ordered (app, verb, obj) with consecutive duplicates collapsed."""
    out = []
    for a in seg:
        step = (a["app"], a["verb"], a["obj"])
        if not out or out[-1] != step:
            out.append(step)
    return out


# --- deterministic automatability scoring -----------------------------------
# Repetition is ONE signal, not the gate. A sequence of concrete UI actions on a
# reachable interface is automatable the first time we see it.
ACTIONABLE = {
    "click": 12, "input": 14, "visit": 7, "copy": 8, "carry": 10, "read": 3, "focus": 1,
    # in-app control interactions carry real automation signal
    "invoke": 12, "edit": 11, "select": 8, "dialog": 9, "menu": 8, "field": 2,
    # enriched by an app-specific probe (exact range/formula) — the strongest signal
    "cell": 15, "formula": 16, "save": 9,
}


def auto_score(steps, count):
    """0-100 automatability from the step composition alone (no AI)."""
    if not steps:
        return 0, "no steps"
    pts = sum(ACTIONABLE.get(v, 1) for (_a, v, _o) in steps)
    concrete = sum(1 for (_a, v, _o) in steps if v in ("click", "input", "copy", "carry"))
    apps = {a for (a, _v, _o) in steps}
    web = sum(1 for a in apps if "." in a and not a.endswith("exe"))

    score = min(60, pts)                          # substance of the actions
    score += min(15, int(web / max(1, len(apps)) * 15))   # reachable interfaces
    score += min(15, (count - 1) * 7)             # repetition boosts, never gates
    if any(v == "carry" for (_a, v, _o) in steps):
        score += 6                                # a real data hand-off
    if concrete == 0:
        score = min(score, 20)                    # focus-only: we can't see the work

    reasons = []
    if concrete:
        reasons.append(f"{concrete} concrete action{'s' if concrete != 1 else ''}")
    if web:
        reasons.append("web interface" if web == len(apps) else "partly web")
    else:
        reasons.append("native GUI only")
    if count >= 2:
        reasons.append(f"seen {count}×")
    if concrete == 0:
        reasons.append("no in-app detail captured")
    return max(0, min(100, score)), ", ".join(reasons)


def mine(db, review=False, top=25, with_annotations=True, min_score=45):
    actions = load_actions(db)
    segments = segment(actions)

    clusters = defaultdict(list)
    for seg in segments:
        steps = _steps(seg)
        if len(steps) < MIN_STEPS:
            continue
        clusters[tuple(steps)].append(seg)

    # Apply admin rules: hide drops a flow; rename relabels it; merge groups
    # several distinct signatures under one label so they count as one flow.
    rules = load_rules(db)
    grouped = {}   # key -> {"steps", "segs", "label", "sigs"}
    for sig, segs in clusters.items():
        h = sig_hash(sig)
        rule = rules.get(h) or {}
        if rule.get("action") == "hide":
            continue
        label = rule.get("label") if rule.get("action") in ("rename", "merge") else None
        key = ("L:" + label) if (rule.get("action") == "merge" and label) else h
        g = grouped.setdefault(key, {"steps": sig, "segs": [], "label": label, "sigs": []})
        g["segs"].extend(segs)
        g["sigs"].append(h)
        if label and not g["label"]:
            g["label"] = label
        # keep the richest signature as the representative step list
        if len(sig) > len(g["steps"]):
            g["steps"] = sig

    flows = []
    for i, (key, g) in enumerate(grouped.items()):
        sig, segs = g["steps"], g["segs"]
        durs = [s[-1]["ts"] - s[0]["ts"] for s in segs]
        apps = list(dict.fromkeys(st[0] for st in sig))  # unique, in order
        avg = sum(durs) / len(durs) if durs else 0.0
        auto_name = (apps[0] + " (in-app)") if len(apps) == 1 else " → ".join(apps[:4])
        d_score, d_reason = auto_score(sig, len(segs))
        flows.append({
            "det_score": d_score,
            "det_reason": d_reason,
            "surfaced": (len(segs) >= 2) or (d_score >= min_score),
            "id": i,
            "sig": g["sigs"][0],
            "sigs": g["sigs"],
            "name": g["label"] or auto_name,
            "labeled": bool(g["label"]),
            "count": len(segs),
            "cross_app": len(apps) > 1,
            "apps": apps,
            "steps": [{"app": a, "verb": v, "obj": o} for (a, v, o) in sig],
            "avg_seconds": round(avg, 1),
            "total_seconds": round(sum(durs), 1),
            "first_seen": min(s[0]["ts"] for s in segs),
            "last_seen": max(s[-1]["ts"] for s in segs),
        })

    # Layer cached AI annotations on top (never calls a model here).
    annots = load_annotations(db) if with_annotations else {}
    for f in flows:
        a = annots.get(f["sig"])
        if a:
            f["review"] = {
                "name": a.get("name"), "purpose": a.get("purpose"),
                "automatability": a.get("auto"), "score": a.get("score"),
                "reason": a.get("reason"), "variable_slots": a.get("slots") or [],
                "coherent": a.get("coherent"),
            }

    # Rank by automatability: the AI score when present, else the deterministic
    # one. Repetition and time cost break ties.
    flows.sort(key=lambda f: (
        (f.get("review") or {}).get("score") or f["det_score"],
        f["count"],
        f["total_seconds"]), reverse=True)

    surfaced = [f for f in flows if f["surfaced"]][:top]
    return {
        "generated_ts": None,   # stamped by caller if wanted
        "event_count": len(actions),
        "segment_count": len(segments),
        "candidate_count": len(flows),
        "repeated_flow_count": sum(1 for f in flows if f["count"] >= 2),
        "annotated_count": sum(1 for f in surfaced if f.get("review")),
        "ai_enabled": ai.enabled(),
        "flows": surfaced,
        "all_flows": flows[:top],
    }


# --- stage 2: AI review (BYOK, optional) ------------------------------------
REVIEW_PROMPT = """You analyse a knowledge worker's captured computer activity.

A deterministic segmenter grouped raw events into candidate flows and counted how
often each repeated. Steps are normalized: <n>, <id>, <email>, <date> are
placeholders for values that CHANGE between repetitions. Apps are process names
or web domains. Verbs: focus (window focused), copy (clipboard), visit (page),
click (button/link), input (form field), read (text selected/read).

For EACH flow id give:
- name: short human task name, specific ("Create a Salesforce lead", not "web work")
- purpose: one sentence on what the person is actually accomplishing and why
- coherent: true if these steps are ONE real task; false if the segmenter merged
  unrelated work or cut a task in half
- automatability: "auto" (deterministic, scriptable end-to-end) | "assist" (needs
  judgement; AI can draft) | "skip" (too variable or not worth automating)
- score: 0-100 automatability. Weigh: same steps every time, stable inputs,
  reachable interfaces (web/API easier than native GUI), low risk if wrong.
  Irreversible sends/payments cap the score at 60.
- reason: one short sentence justifying the score
- variable_slots: the parts that change between runs (short strings)

Return ONLY strict JSON, no prose or markdown:
{"flows":[{"id":0,"name":"...","purpose":"...","coherent":true,"automatability":"auto","score":85,"reason":"...","variable_slots":["..."]}]}

Candidate flows:
"""


def _parse_json(raw):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[i:j + 1])


def annotate(db, min_count=2, force=False, limit=25):
    """Run the AI pass over repeated flows and CACHE the result.

    Deterministic mining is untouched by this; annotations are a separate table
    layered on top. Only flows seen >= min_count are sent, and already-annotated
    flows are skipped unless force=True — so this stays cheap.
    """
    if not ai.enabled():
        return {"reviewed": False,
                "reason": "AI is off. Turn it on in Settings and add your own API key."}

    base = mine(db, top=200)
    cached = load_annotations(db)
    todo = [f for f in base["flows"]
            if f["count"] >= min_count and (force or f["sig"] not in cached)][:limit]
    if not todo:
        return {"reviewed": True, "annotated": 0, "reason": "Nothing new to analyse."}

    payload = [{"id": f["id"], "seen_times": f["count"],
                "avg_seconds": f["avg_seconds"], "steps": f["steps"]} for f in todo]
    model = config.load()["ai"].get("model") or config.default_model(config.load()["ai"].get("provider"))
    try:
        data = _parse_json(ai.generate(REVIEW_PROMPT + json.dumps(payload, indent=1), max_tokens=2000))
    except Exception as e:
        return {"reviewed": False, "reason": f"AI review failed: {e}"}

    by_id = {r.get("id"): r for r in data.get("flows", [])}
    conn = sqlite3.connect(db)
    n = 0
    try:
        ensure_annotations(conn)
        for f in todo:
            r = by_id.get(f["id"])
            if not r:
                continue
            score = r.get("score")
            try:
                score = max(0, min(100, int(score)))
            except (TypeError, ValueError):
                score = None
            conn.execute(
                "INSERT OR REPLACE INTO flow_annotations "
                "(sig,name,purpose,auto,score,reason,slots,coherent,model,ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f["sig"], r.get("name"), r.get("purpose"),
                 r.get("automatability") if r.get("automatability") in ("auto", "assist", "skip") else "assist",
                 score, r.get("reason"), json.dumps(r.get("variable_slots") or []),
                 1 if r.get("coherent", True) else 0, model, time.time()))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return {"reviewed": True, "annotated": n, "model": model}


def recent_log(db, limit=40):
    """Every tracked action, newest first, with the sequence it belongs to.

    This is what the live log in the UI renders: it shows that each raw action
    is captured AND which end-to-end sequence it is being committed to.
    """
    actions = load_actions(db)
    segs = segment(actions)
    annots = load_annotations(db)

    owner = {}          # id(action) -> (segment number, signature, name)
    for i, seg in enumerate(segs, 1):
        steps = _steps(seg)
        if len(steps) < MIN_STEPS:
            continue
        h = sig_hash(tuple(steps))
        score, _ = auto_score(tuple(steps), 1)
        name = (annots.get(h) or {}).get("name")
        for a in seg:
            owner[id(a)] = (i, h, name, score)

    rows = []
    for a in actions[-limit:][::-1]:
        if a["verb"] == "_idle":
            rows.append({"ts": a["ts"], "verb": "idle", "app": None, "obj": "— session boundary —",
                         "seq": None, "sig": None, "name": None, "score": None})
            continue
        seq, sig, name, score = owner.get(id(a), (None, None, None, None))
        rows.append({"ts": a["ts"], "app": a.get("app"), "verb": a["verb"], "obj": a.get("obj"),
                     "seq": seq, "sig": sig, "name": name, "score": score})
    return {"rows": rows, "total_actions": len(actions), "sequences": len(segs)}


def purge_flow(db, sig):
    """Delete the captured events behind every occurrence of one flow."""
    actions = load_actions(db)
    windows = []
    for seg in segment(actions):
        steps = _steps(seg)
        if len(steps) >= MIN_STEPS and sig_hash(tuple(steps)) == sig:
            windows.append((seg[0]["ts"], seg[-1]["ts"]))
    if not windows:
        return 0
    conn = sqlite3.connect(db)
    removed = 0
    try:
        for a, b in windows:
            cur = conn.execute("DELETE FROM events WHERE ts BETWEEN ? AND ?", (a, b))
            removed += cur.rowcount or 0
            try:
                cur = conn.execute("DELETE FROM web_events WHERE ts BETWEEN ? AND ?", (a, b))
                removed += cur.rowcount or 0
            except sqlite3.OperationalError:
                pass
        conn.execute("DELETE FROM flow_rules WHERE sig=?", (sig,))
        conn.commit()
    finally:
        conn.close()
    return removed


def purge(db, scope="all", before_ts=None, app=None):
    """Bulk delete captured data. scope: all | desktop | web | screenshots."""
    conn = sqlite3.connect(db)
    removed = 0
    try:
        targets = []
        if scope in ("all", "desktop"):
            targets.append("events")
        if scope in ("all", "web"):
            targets.append("web_events")
        if scope in ("all", "screenshots"):
            targets.append("screenshots")
        for t in targets:
            sql, args = f"DELETE FROM {t}", []
            where = []
            if before_ts:
                where.append("ts < ?"); args.append(before_ts)
            if app and t == "events":
                where.append("app = ?"); args.append(app)
            if where:
                sql += " WHERE " + " AND ".join(where)
            try:
                removed += conn.execute(sql, args).rowcount or 0
            except sqlite3.OperationalError:
                pass
        if scope == "all" and not before_ts and not app:
            try:
                conn.execute("DELETE FROM flow_rules")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()
    return removed


def _fmt(sec):
    m = sec / 60
    return f"{m:.1f}m" if m >= 1 else f"{sec:.0f}s"


def main():
    ap = argparse.ArgumentParser(description="workflow-mapper mining layer")
    ap.add_argument("--db", default="activity.db")
    ap.add_argument("--review", action="store_true", help="also run AI review (needs a BYOK key)")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    res = mine(args.db, review=args.review, top=args.top)
    print(f"events={res['event_count']}  segments={res['segment_count']}  "
          f"repeated flows={res['repeated_flow_count']}\n")
    for f in res["flows"]:
        tag = f"x{f['count']}" if f["count"] >= 2 else "once"
        print(f"[{tag:>4}] {f['name']}  ({_fmt(f['total_seconds'])} total, {_fmt(f['avg_seconds'])} each)")
        for s in f["steps"]:
            print(f"          {s['app']:22.22} {s['verb']:6} {s['obj']}")
        if f.get("review"):
            r = f["review"]
            print(f"          AI: {r.get('name')} · {r.get('automatability')} · "
                  f"coherent={r.get('coherent')} · {r.get('reason')}")
        print()
    if res.get("ai"):
        print("AI review:", res["ai"])


if __name__ == "__main__":
    main()
