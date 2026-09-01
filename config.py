"""
Local configuration for workflow-mapper — including bring-your-own-key (BYOK)
AI credentials.

Design guarantees:
  * There is NO bundled/default API key. AI features are disabled until the
    person running the tool supplies their OWN key.
  * The key is stored only on this machine (config.json next to activity.db)
    and is used only to call the provider that person chose. It is never sent
    anywhere else, and the tool's author never sees it or pays for usage.
"""

import json
import os
import sys

APP_NAME = "workflow-mapper"

# Small, cheap models by default: the AI layer only annotates already-detected
# flows, which is a light classification job — not worth a frontier model.
DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
}
ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def data_dir():
    """Writable per-user data directory (shared with activity.db)."""
    if getattr(sys, "frozen", False):  # packaged .exe / .app
        if sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Application Support")
        else:
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, APP_NAME)
        os.makedirs(d, exist_ok=True)
        return d
    return os.path.dirname(os.path.abspath(__file__))  # dev: repo folder


def config_path():
    return os.path.join(data_dir(), "config.json")


def _defaults():
    return {
        # enabled: the explicit AI on/off switch. The whole tool runs with this
        # off — capture, segmentation, flows and the dashboard are deterministic.
        "ai": {"enabled": False, "provider": None, "api_key": "", "model": ""},
        "capture": {"screenshots": False, "interval": 30, "mode": "focused"},
    }


def load():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    base = _defaults()
    base["ai"].update((cfg.get("ai") or {}))
    base["capture"].update((cfg.get("capture") or {}))
    return base


def set_screenshots(enabled):
    cfg = load()
    cfg["capture"]["screenshots"] = bool(enabled)
    save(cfg)
    return public_status()


def save(cfg):
    path = config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    # Best-effort: restrict the file since it holds the user's key.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def default_model(provider):
    return DEFAULT_MODELS.get(provider, "")


def resolve_key(provider):
    """The user's key: explicit config value first, then their env var."""
    ai = load()["ai"]
    if ai.get("api_key"):
        return ai["api_key"]
    env = ENV_KEYS.get(provider or "")
    return os.environ.get(env) if env else None


def public_status():
    """AI settings for the UI — never returns the raw key."""
    ai = load()["ai"]
    provider = ai.get("provider")
    key = resolve_key(provider)
    cap = load()["capture"]
    return {
        "ai_enabled": bool(ai.get("enabled")) and bool(key),
        "ai_requested": bool(ai.get("enabled")),
        "provider": provider,
        "model": ai.get("model") or default_model(provider),
        "key_set": bool(key),
        "key_hint": ("…" + key[-4:]) if key else "",
        "source": "config" if ai.get("api_key") else ("env" if key else "none"),
        "screenshots": bool(cap.get("screenshots")),
    }


def update_ai(provider=None, api_key=None, model=None, clear_key=False, enabled=None):
    cfg = load()
    if enabled is not None:
        cfg["ai"]["enabled"] = bool(enabled)
    if provider is not None:
        cfg["ai"]["provider"] = provider or None
    if clear_key:
        cfg["ai"]["api_key"] = ""
    elif api_key:
        cfg["ai"]["api_key"] = api_key
    if model is not None:
        cfg["ai"]["model"] = model
    save(cfg)
    return public_status()
