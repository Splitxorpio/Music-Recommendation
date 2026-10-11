"""Accounts and sessions.

  * Passwords: scrypt (N=2^15, r=8, p=1, 16-byte salt), compared in constant time. Never logged or returned.
  * Sessions: a random 256-bit token in an HttpOnly cookie; only its SHA-256 is stored, so a database leak
    doesn't hand out live sessions. Sliding expiry (FREQUENCY_SESSION_DAYS).
  * CSRF: double-submit token. A second random token sits in a readable cookie; unsafe requests must echo it
    in X-CSRF-Token, and its hash must match the one stored with the session.
  * Brute force: more than LOGIN_MAX_FAILS failed sign-ins for one email in LOGIN_WINDOW_MIN minutes are refused
    (counted in audit_events, so it holds across API processes).
"""
import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import db, settings, userdata

SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSWORD = 10
LOGIN_MAX_FAILS, LOGIN_WINDOW_MIN = 10, 15
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


class AuthError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, dklen=64, maxmem=64 * 1024 * 1024, **SCRYPT)
    b = lambda x: base64.b64encode(x).decode()
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${b(salt)}${b(h)}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=64,
                             maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


_DUMMY = None


def _dummy_hash():
    global _DUMMY
    _DUMMY = _DUMMY or hash_password(secrets.token_urlsafe(12))
    return _DUMMY


def _sha(x: str) -> bytes:
    return hashlib.sha256(x.encode()).digest()


def _email_tag(email: str) -> str:
    """Audit-log key for an email without storing the address itself."""
    return hmac.new(settings.get().require("secret_key").secret_key.encode(), email.lower().encode(), "sha256").hexdigest()[:24]


def public_user(u: dict) -> dict:
    return {"id": str(u["id"]), "email": u["email"], "display_name": u["display_name"],
            "calibration_consent": u["calibration_consent"], "created_at": u["created_at"]}


def create_user(email: str, password: str, display_name: str | None = None) -> dict:
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        raise AuthError("That doesn't look like an email address.")
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"Use a password of at least {MIN_PASSWORD} characters.")
    with db.system() as cur:
        cur.execute("SELECT 1 FROM users WHERE lower(email) = lower(%s)", (email,))
        if cur.fetchone():
            raise AuthError("An account with that email already exists.", 409)
        cur.execute("INSERT INTO users (email, password_hash, display_name) VALUES (%s, %s, %s) RETURNING *",
                    (email, hash_password(password), (display_name or "").strip()[:80] or None))
        u = cur.fetchone()
        userdata.audit(cur, u["id"], "account_created")
    return u


def authenticate(email: str, password: str) -> dict:
    email = (email or "").strip()
    tag = _email_tag(email)
    with db.system() as cur:
        cur.execute("SELECT count(*) AS n FROM audit_events WHERE event = 'login_failed' AND detail->>'email_tag' = %s "
                    "AND at > now() - %s * interval '1 minute'", (tag, LOGIN_WINDOW_MIN))
        if cur.fetchone()["n"] >= LOGIN_MAX_FAILS:
            raise AuthError("Too many sign-in attempts. Try again in a few minutes.", 429)
        cur.execute("SELECT * FROM users WHERE lower(email) = lower(%s)", (email,))
        u = cur.fetchone()
    ok = verify_password(password or "", u["password_hash"] if u else _dummy_hash())   # same cost either way
    if not (u and ok):
        with db.system() as cur:                       # committed before raising, or the attempt wouldn't count
            userdata.audit(cur, u and u["id"], "login_failed", {"email_tag": tag})
        raise AuthError("Email or password is wrong.", 401)
    with db.system() as cur:
        cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (u["id"],))
    return u


def new_session(user_id, user_agent: str | None = None) -> tuple[str, str, datetime]:
    """(session token, csrf token, expiry). Only hashes are stored."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.get().session_days)
    with db.system() as cur:
        cur.execute("INSERT INTO sessions (token_hash, user_id, csrf_hash, expires_at, user_agent) VALUES (%s, %s, %s, %s, %s)",
                    (_sha(token), str(user_id), _sha(csrf), expires, (user_agent or "")[:200]))
    return token, csrf, expires


def session_user(token: str | None) -> tuple[dict, dict] | None:
    """(user, session) for a live session token, extending it (sliding expiry); None otherwise."""
    if not token:
        return None
    with db.system() as cur:
        cur.execute("""UPDATE sessions SET expires_at = greatest(expires_at, now() + %s * interval '1 day' / 2)
                       WHERE token_hash = %s AND expires_at > now() RETURNING *""",
                    (settings.get().session_days, _sha(token)))
        s = cur.fetchone()
        if not s:
            return None
        cur.execute("SELECT * FROM users WHERE id = %s", (s["user_id"],))
        return cur.fetchone(), s


def csrf_ok(session: dict, header_token: str | None, cookie_token: str | None) -> bool:
    if not header_token or not cookie_token or not hmac.compare_digest(header_token, cookie_token):
        return False
    return hmac.compare_digest(_sha(header_token), bytes(session["csrf_hash"]))


def end_session(token: str | None):
    if token:
        with db.system() as cur:
            cur.execute("DELETE FROM sessions WHERE token_hash = %s", (_sha(token),))


def update_profile(user_id, display_name=None, calibration_consent=None) -> dict:
    with db.system() as cur:
        if display_name is not None:
            cur.execute("UPDATE users SET display_name = %s WHERE id = %s", (display_name.strip()[:80] or None, str(user_id)))
        if calibration_consent is not None:
            cur.execute("UPDATE users SET calibration_consent = %s, consent_updated_at = now() WHERE id = %s",
                        (bool(calibration_consent), str(user_id)))
            if not calibration_consent:                 # withdrawing removes their pairs from future calibrations
                cur.execute("DELETE FROM calibration_pairs WHERE contributed_by = %s", (str(user_id),))
            userdata.audit(cur, user_id, "calibration_consent", {"granted": bool(calibration_consent)})
        cur.execute("SELECT * FROM users WHERE id = %s", (str(user_id),))
        return cur.fetchone()


def change_password(user_id, current: str, new: str):
    with db.system() as cur:
        cur.execute("SELECT password_hash FROM users WHERE id = %s", (str(user_id),))
        if not verify_password(current or "", cur.fetchone()["password_hash"]):
            raise AuthError("Current password is wrong.", 401)
        if len(new or "") < MIN_PASSWORD:
            raise AuthError(f"Use a password of at least {MIN_PASSWORD} characters.")
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(new), str(user_id)))
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (str(user_id),))   # sign out everywhere
        userdata.audit(cur, user_id, "password_changed")


def delete_account(user_id, password: str):
    """Delete the account and everything that belongs to it (listening, connections and their tokens, profiles,
    recommendations, runs, uploads, sessions). Shared catalog facts about songs stay: they belong to no one."""
    from .services import library
    with db.system() as cur:
        cur.execute("SELECT password_hash FROM users WHERE id = %s", (str(user_id),))
        row = cur.fetchone()
        if not row or not verify_password(password or "", row["password_hash"]):
            raise AuthError("Password is wrong.", 401)
        cur.execute("UPDATE jobs SET cancel_requested = true WHERE user_id = %s AND status IN ('queued', 'running', 'retry_wait')",
                    (str(user_id),))
        cur.execute("DELETE FROM users WHERE id = %s", (str(user_id),))
        userdata.audit(cur, user_id, "account_deleted")
    library.delete_uploads(user_id)
