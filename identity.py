"""
Install identity and data continuity.

Deployment shape this supports: a zip is emailed to employees at several
companies. They may re-download and re-open the app many times a day. All of
those opens must behave as ONE continuous dataset for that person.

That works because the executable is stateless and all data lives in a
per-user directory outside it:

    Windows  %LOCALAPPDATA%\\workflow-mapper\\
    macOS    ~/Library/Application Support/workflow-mapper/

Re-downloading the zip replaces only the .exe. activity.db, config.json and
install.json are untouched, so history simply continues. install.json also
carries a stable install_id generated once, so weekly exports from the same
person are recognisable as the same person even if they rename the file.
"""

import json
import os
import socket
import time
import uuid

import config

INSTALL_FILE = "install.json"


def _path():
    return os.path.join(config.data_dir(), INSTALL_FILE)


def load():
    """Read (and create on first run) this install's identity."""
    p = _path()
    data = {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}

    changed = False
    if not data.get("install_id"):
        data["install_id"] = uuid.uuid4().hex
        data["created_at"] = time.time()
        changed = True
    if not data.get("machine"):
        try:
            data["machine"] = socket.gethostname()
        except Exception:
            data["machine"] = "unknown"
        changed = True
    data.setdefault("employee", "")
    data.setdefault("company", "")
    data.setdefault("opens", 0)
    data.setdefault("last_export_at", None)
    if changed:
        save(data)
    return data


def save(data):
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def record_open():
    """Count each launch; proves continuity across re-downloads."""
    d = load()
    d["opens"] = int(d.get("opens") or 0) + 1
    d["last_open_at"] = time.time()
    save(d)
    return d


def update(employee=None, company=None):
    d = load()
    if employee is not None:
        d["employee"] = employee.strip()
    if company is not None:
        d["company"] = company.strip()
    save(d)
    return d


def public():
    d = load()
    return {
        "install_id": d["install_id"],
        "machine": d.get("machine"),
        "employee": d.get("employee", ""),
        "company": d.get("company", ""),
        "opens": d.get("opens", 0),
        "created_at": d.get("created_at"),
        "last_export_at": d.get("last_export_at"),
        "identified": bool(d.get("employee") and d.get("company")),
        "data_dir": config.data_dir(),
    }
