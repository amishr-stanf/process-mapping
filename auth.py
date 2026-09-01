"""
Admin authentication for the developer/back-end console.

The password is NEVER stored in plaintext. What ships here is a salted
PBKDF2-HMAC-SHA256 digest (600k iterations). Verification is constant-time.

Because this repository is public, treat the built-in credential as an
internal-tool login, not a secret: anyone can read this file and attempt an
offline guess. Two mitigations are in place:

  * The admin API is served only on 127.0.0.1 (loopback) — it is not reachable
    from the network, so an attacker needs local access to the machine already.
  * You can override the credential without touching the source, which is the
    recommended setup for anything sensitive:

        set WM_ADMIN_USER=someone
        set WM_ADMIN_SALT=<hex>
        set WM_ADMIN_HASH=<hex>

    Generate a new pair with:  python auth.py "new-password"
"""

import hashlib
import hmac
import os
import secrets
import time

# Built-in credential (operent-admin). Digest only — see the note above.
DEFAULT_USER = "operent-admin"
DEFAULT_SALT = "33595908c1ab1055f3fb8567b277b11e"
DEFAULT_HASH = "f5f49733da68483df7cfe5b88384ee6a3f3eb75a6ef2a6972522af760c86e2fd"

ITERATIONS = 600_000
SESSION_TTL = 8 * 3600      # seconds a login stays valid
MAX_ATTEMPTS = 8            # per window, to blunt local brute force
ATTEMPT_WINDOW = 300

_sessions = {}              # token -> expiry epoch
_attempts = []              # recent failed-attempt timestamps


def _expected():
    return (
        os.environ.get("WM_ADMIN_USER", DEFAULT_USER),
        os.environ.get("WM_ADMIN_SALT", DEFAULT_SALT),
        os.environ.get("WM_ADMIN_HASH", DEFAULT_HASH),
    )


def hash_password(password, salt_hex=None):
    """Return (salt_hex, hash_hex) for a password."""
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return salt.hex(), dk.hex()


def _throttled():
    now = time.time()
    _attempts[:] = [t for t in _attempts if now - t < ATTEMPT_WINDOW]
    return len(_attempts) >= MAX_ATTEMPTS


def login(user, password):
    """Verify credentials; return a session token, or None."""
    if _throttled():
        return None
    exp_user, salt_hex, hash_hex = _expected()
    try:
        _, got = hash_password(password or "", salt_hex)
    except ValueError:
        return None
    ok_user = hmac.compare_digest((user or ""), exp_user)
    ok_pass = hmac.compare_digest(got, hash_hex)
    if not (ok_user and ok_pass):
        _attempts.append(time.time())
        return None
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def valid(token):
    """True if the token is a live session."""
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp:
        return False
    if exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


def logout(token):
    _sessions.pop(token, None)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print('usage: python auth.py "new-password"')
        raise SystemExit(1)
    s, h = hash_password(sys.argv[1])
    print("WM_ADMIN_SALT =", s)
    print("WM_ADMIN_HASH =", h)
