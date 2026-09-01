"""
BYOK AI client — the ONLY place workflow-mapper talks to an LLM.

Every call uses the local user's own API key (from config.py). There is no
fallback to any shared/author key: if the user hasn't configured one, AI
features simply stay disabled. Uses stdlib urllib, so no SDK dependency and
it works whether the user picks Anthropic or OpenAI.

The future "reasoning" layer (deciding which captured actions form a flow,
what's automatable, and how) calls generate() here — so it always runs on,
and is billed to, the person using the tool.
"""

import json
import urllib.error
import urllib.request

import config


def available():
    """True if a provider + key are configured (regardless of the on/off switch)."""
    ai = config.load()["ai"]
    return bool(ai.get("provider")) and bool(config.resolve_key(ai.get("provider")))


def enabled():
    """True only if the user has BOTH switched AI on and supplied a key.

    Every AI call site checks this. With it off the tool is fully deterministic.
    """
    return bool(config.load()["ai"].get("enabled")) and available()


def generate(prompt, max_tokens=512, timeout=30):
    """Send one prompt to the user's chosen provider; return the text reply."""
    ai = config.load()["ai"]
    provider = ai.get("provider")
    key = config.resolve_key(provider)
    model = ai.get("model") or config.default_model(provider)
    if not provider or not key:
        raise RuntimeError("No API key configured. Add your own key in Settings to enable AI features.")

    if provider == "anthropic":
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({
                "model": model, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            },
        )
    elif provider == "openai":
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps({
                "model": model, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }).encode("utf-8"),
            headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
        )
    else:
        raise RuntimeError(f"Unknown provider: {provider}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"{provider} API error {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error reaching {provider}: {e.reason}")

    if provider == "anthropic":
        parts = data.get("content") or []
        return "".join(p.get("text", "") for p in parts).strip()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


def test_key():
    """Tiny call to verify the user's key works (uses their tokens, not the author's)."""
    reply = generate("Reply with exactly one word: ok", max_tokens=5)
    return reply
