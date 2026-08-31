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
import json
import re
import sqlite3
import time
from collections import defaultdict

import ai

GAP_SECONDS = 45.0   # gap between actions that starts a new candidate flow
MIN_STEPS = 2        # ignore trivial 1-action segments

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
        for r in conn.execute("SELECT ts, kind, app, title, clip_type FROM events ORDER BY ts"):
            k = r["kind"]
            if k == "idle_start":
                actions.append({"ts": r["ts"], "verb": "_idle"})
            elif k == "focus":
                actions.append({"ts": r["ts"], "app": _app_name(r["app"]), "verb": "focus",
                                "obj": normalize(r["title"])})
            elif k == "clipboard":
                actions.append({"ts": r["ts"], "app": _app_name(r["app"]), "verb": "copy",
                                "obj": "clip:" + (r["clip_type"] or "?")})
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
                actions.append({"ts": r["ts"], "app": dom, "verb": "input", "obj": "field:" + _field_name(r["target"])})
            elif k == "select":
                actions.append({"ts": r["ts"], "app": dom, "verb": "read", "obj": "selection"})
    finally:
        conn.close()
    actions.sort(key=lambda a: a["ts"])
    return actions


# --- stage 1: deterministic segmentation + clustering -----------------------
def segment(actions):
    segments, cur, prev_ts = [], [], None
    for a in actions:
        if a["verb"] == "_idle":
            if cur:
                segments.append(cur); cur = []
            prev_ts = None
            continue
        if prev_ts is not None and a["ts"] - prev_ts > GAP_SECONDS:
            if cur:
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


def mine(db, review=False, top=25):
    actions = load_actions(db)
    segments = segment(actions)

    clusters = defaultdict(list)
    for seg in segments:
        steps = _steps(seg)
        if len(steps) < MIN_STEPS:
            continue
        clusters[tuple(steps)].append(seg)

    flows = []
    for i, (sig, segs) in enumerate(clusters.items()):
        durs = [s[-1]["ts"] - s[0]["ts"] for s in segs]
        apps = list(dict.fromkeys(st[0] for st in sig))  # unique, in order
        avg = sum(durs) / len(durs) if durs else 0.0
        flows.append({
            "id": i,
            "name": (apps[0] + " (in-app)") if len(apps) == 1 else " → ".join(apps[:4]),
            "count": len(segs),
            "cross_app": len(apps) > 1,
            "apps": apps,
            "steps": [{"app": a, "verb": v, "obj": o} for (a, v, o) in sig],
            "avg_seconds": round(avg, 1),
            "total_seconds": round(sum(durs), 1),
            "first_seen": min(s[0]["ts"] for s in segs),
            "last_seen": max(s[-1]["ts"] for s in segs),
        })

    flows.sort(key=lambda f: (f["count"] >= 2, f["total_seconds"]), reverse=True)
    flows = flows[:top]

    result = {
        "generated_ts": None,   # stamped by caller if wanted
        "event_count": len(actions),
        "segment_count": len(segments),
        "repeated_flow_count": sum(1 for f in flows if f["count"] >= 2),
        "flows": flows,
        "ai": None,
    }
    if review:
        result["ai"] = ai_review(flows)
    return result


# --- stage 2: AI review (BYOK, optional) ------------------------------------
REVIEW_PROMPT = """You review a user's captured work to help automate repetitive tasks.
A deterministic segmenter produced these candidate flows (steps are normalized:
<n>/<id>/<email> are placeholders for values that vary between repetitions).

For EACH flow id, decide:
- name: a short human task name (e.g. "Create a Salesforce lead")
- coherent: true if the steps look like ONE real task, false if mis-segmented
- automatability: "auto" | "assist" | "skip"
- reason: one short sentence
- variable_slots: the parts that change between repetitions (list of short strings)

Return ONLY strict JSON, no prose:
{"flows":[{"id":0,"name":"...","coherent":true,"automatability":"auto","reason":"...","variable_slots":["..."]}]}

Candidate flows:
"""


def ai_review(flows):
    if not ai.available():
        return {"reviewed": False, "reason": "No API key set — add your own key in Settings to enable AI review."}
    payload = [{"id": f["id"], "count": f["count"], "steps": f["steps"]} for f in flows]
    prompt = REVIEW_PROMPT + json.dumps(payload, indent=1)
    try:
        raw = ai.generate(prompt, max_tokens=1500)
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        by_id = {r["id"]: r for r in data.get("flows", [])}
        for f in flows:
            r = by_id.get(f["id"])
            if r:
                f["review"] = r
        return {"reviewed": True, "count": len(by_id)}
    except Exception as e:
        return {"reviewed": False, "reason": f"AI review failed: {e}"}


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
