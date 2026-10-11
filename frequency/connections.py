"""A user's links to listening sources. Tokens are encrypted at rest and never leave this module in clear
except to make the provider call that needs them; public() is what the API may return."""
from datetime import datetime, timezone

from . import crypto, userdata


def save(cur, user_id, provider: str, *, external_user_id=None, display_name=None, tokens: dict | None = None):
    tokens = tokens or {}
    cur.execute("""INSERT INTO provider_connections (user_id, provider, external_user_id, display_name, access_token_enc,
                       refresh_token_enc, scopes, token_expires_at, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active')
                   ON CONFLICT (user_id, provider) DO UPDATE SET external_user_id = EXCLUDED.external_user_id,
                       display_name = EXCLUDED.display_name, access_token_enc = EXCLUDED.access_token_enc,
                       refresh_token_enc = coalesce(EXCLUDED.refresh_token_enc, provider_connections.refresh_token_enc),
                       scopes = EXCLUDED.scopes, token_expires_at = EXCLUDED.token_expires_at, status = 'active',
                       updated_at = now()
                   RETURNING id""",
                (str(user_id), provider, external_user_id, display_name, crypto.encrypt(tokens.get("access_token")),
                 crypto.encrypt(tokens.get("refresh_token")), tokens.get("scopes") or [], tokens.get("expires_at")))
    conn_id = cur.fetchone()["id"]
    userdata.audit(cur, user_id, "connection_saved", {"provider": provider})
    return conn_id


def get(cur, provider: str) -> dict | None:
    cur.execute("SELECT * FROM provider_connections WHERE provider = %s", (provider,))
    return cur.fetchone()


def tokens(conn: dict) -> dict:
    return {"access_token": crypto.decrypt(conn["access_token_enc"]),
            "refresh_token": crypto.decrypt(conn["refresh_token_enc"]),
            "expires_at": conn["token_expires_at"]}


def update_tokens(cur, conn_id, t: dict):
    cur.execute("""UPDATE provider_connections SET access_token_enc = %s,
                       refresh_token_enc = coalesce(%s, refresh_token_enc), token_expires_at = %s, status = 'active',
                       updated_at = now() WHERE id = %s""",
                (crypto.encrypt(t["access_token"]), crypto.encrypt(t.get("refresh_token")), t["expires_at"], conn_id))


def mark(cur, conn_id, status: str):
    cur.execute("UPDATE provider_connections SET status = %s, updated_at = now() WHERE id = %s", (status, conn_id))


def disconnect(cur, user_id, provider: str) -> bool:
    """Forget the connection and its tokens. (Listening already imported stays until the user deletes it.)"""
    cur.execute("DELETE FROM provider_connections WHERE provider = %s RETURNING id", (provider,))
    gone = cur.fetchone() is not None
    if gone:
        userdata.audit(cur, user_id, "connection_removed", {"provider": provider})
    return gone


def expired(conn) -> bool:
    return bool(conn["token_expires_at"] and conn["token_expires_at"] <= datetime.now(timezone.utc))


def public(conn: dict) -> dict:
    return {"provider": conn["provider"], "account": conn["display_name"] or conn["external_user_id"],
            "status": conn["status"], "connected_at": conn["created_at"], "updated_at": conn["updated_at"]}
