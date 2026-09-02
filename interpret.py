"""
Generic action interpretation — turning raw control signals into meaning.

The accessibility sensor tells us a control called "Number Format" was operated
in EXCEL.EXE, or that a dialog called "Find and Replace" opened in a claims
system nobody outside insurance has heard of. This module maps those raw signals
onto a small, stable vocabulary of WORK CATEGORIES.

Deliberately pattern-based rather than per-app: the rules key off control names,
roles and verbs that recur across every Windows UI convention, so an app we have
never seen still gets meaningful interpretation on day one. Adding coverage
means adding a pattern here, not writing a new sensor.

No AI involved — this is deterministic mapping.
"""

import re

# (regex, category, phrasing) — first match wins, so order specific to broad.
RULES = [
    (r"^(find|search|find and replace|find next|quick find|lookup|look up)\b", "search", "searched"),
    (r"^(filter|autofilter|sort|sort (a|de)scending|advanced filter)\b", "organize", "sorted or filtered"),
    (r"\b(go ?to|name box|jump to)\b", "navigate", "jumped to a location"),

    (r"^(font|font size|font color|fill color|bold|italic|underline|borders?|"
     r"number format|cell styles|format painter|alignment|merge|wrap text|"
     r"conditional formatting|styles?)\b", "format", "changed formatting"),
    (r"\b(column width|row height|insert (row|column)|delete (row|column)|freeze panes)\b",
     "structure", "changed the sheet layout"),

    (r"^(autosum|formula|insert function|calculate|refresh|data validation)\b",
     "calculate", "worked with formulas"),
    (r"^(paste|paste special|copy|cut)\b", "clipboard", "moved data via the clipboard"),

    (r"^(save|save as|save a copy|export|download|print)\b", "file", "saved or exported"),
    (r"^(new|add|create|register)\b.*\b(record|customer|claim|policy|case|lead|ticket|patient)\b",
     "record", "created a record"),
    (r"^(open|new|recent|browse|import)\b", "file", "opened a file"),
    (r"^(attach|attachment|insert file)\b", "file", "attached a file"),

    (r"^(send|send and receive|reply|reply all|forward|new (e-?mail|message)|compose)\b",
     "communicate", "sent or drafted a message"),

    (r"^(update|edit|modify|amend)\b", "record", "updated a record"),
    (r"^(submit|approve|confirm|post|commit|finali[sz]e)\b", "submit", "submitted or approved"),
    (r"^(delete|remove|cancel|void)\b", "record", "deleted or cancelled"),

    (r"^(next|previous|back|forward|home|refresh|reload)\b", "navigate", "navigated"),
    (r"^(ok|close|cancel|apply|done)$", "dialog", "dismissed a dialog"),
]
COMPILED = [(re.compile(p, re.I), c, h) for p, c, h in RULES]

ROLE_FALLBACK = {
    "editable text": ("data_entry", "entered data"),
    "split button": ("command", "ran a command"),
    "combobox": ("data_entry", "chose an option"),
    "listitem": ("navigate", "selected an item"),
    "pagetab": ("navigate", "switched tab"),
    "menuitem": ("command", "ran a menu command"),
    "checkbox": ("data_entry", "toggled an option"),
    "button": ("command", "ran a command"),
    "text": ("data_entry", "entered data"),
    "cell": ("data_entry", "edited a cell"),
    "dialog": ("dialog", "worked in a dialog"),
}

VERB_MEANING = {
    "focus":     ("context", "switched to"),
    "copy":      ("clipboard", "copied"),
    "carry":     ("transfer", "carried information from"),
    "visit":     ("navigate", "opened page"),
    "click":     ("command", "clicked"),
    "input":     ("data_entry", "filled in"),
    "read":      ("read", "read"),
    "dialog":    ("dialog", "opened dialog"),
    "menu":      ("command", "opened menu"),
    "invoke":    ("command", "ran"),
    "edit":      ("data_entry", "changed"),
    "select":    ("navigate", "selected"),
    "field":     ("context", "moved to field"),
    "cell":      ("data_entry", "edited cells"),
    "formula":   ("calculate", "entered a formula"),
    "save":      ("file", "saved"),
    "open":      ("file", "opened"),
    "file_new":  ("file", "received a file"),
    "file_save": ("file", "saved a file"),
}

CATEGORY_LABEL = {
    "search": "Lookup", "organize": "Sort / filter", "navigate": "Navigation",
    "format": "Formatting", "structure": "Layout", "calculate": "Calculation",
    "clipboard": "Copy / paste", "file": "File", "communicate": "Communication",
    "record": "Record change", "submit": "Submit", "dialog": "Dialog",
    "data_entry": "Data entry", "command": "Command", "context": "Context switch",
    "transfer": "Information transfer", "read": "Reading",
}

MECHANICAL = {"format", "structure", "calculate", "clipboard", "file",
              "record", "submit", "data_entry", "organize", "search", "transfer"}


def interpret(app, verb, obj):
    """Map one action to (category, phrase, human_sentence)."""
    app_name = (app or "app").replace(".exe", "")
    text = (obj or "").strip()

    for rx, cat, phrase in COMPILED:
        if rx.search(text):
            return cat, phrase, phrase.capitalize() + " in " + app_name

    cat, phrase = VERB_MEANING.get(verb, ("command", verb or "acted"))
    for role, (rcat, rphrase) in ROLE_FALLBACK.items():
        if text.endswith(role) or (" " + role) in text:
            cat, phrase = rcat, rphrase
            break

    detail = text[:60] if text else ""
    sentence = (phrase.capitalize() + " " + detail).strip() + " — " + app_name
    return cat, phrase, sentence


def describe_steps(steps):
    """Turn a flow's raw steps into interpreted, deduped discrete steps."""
    out = []
    for s in steps:
        cat, phrase, sentence = interpret(s.get("app"), s.get("verb"), s.get("obj"))
        if out and out[-1]["category"] == cat and out[-1]["app"] == s.get("app"):
            out[-1]["count"] += 1
            continue
        out.append({
            "app": s.get("app"), "verb": s.get("verb"), "obj": s.get("obj"),
            "category": cat, "label": CATEGORY_LABEL.get(cat, cat.title()),
            "phrase": phrase, "sentence": sentence, "count": 1,
            "mechanical": cat in MECHANICAL,
        })
    return out


def summarize(steps):
    """One-line deterministic summary of what a flow does."""
    described = describe_steps(steps)
    apps, seen = [], set()
    for d in described:
        a = (d["app"] or "").replace(".exe", "")
        if a and a not in seen:
            seen.add(a)
            apps.append(a)
    uniq = list(dict.fromkeys(d["label"] for d in described))[:4]
    return {
        "apps": apps,
        "categories": uniq,
        "mechanical_steps": sum(1 for d in described if d["mechanical"]),
        "total_steps": len(described),
        "headline": " → ".join(uniq) if uniq else "activity",
    }
