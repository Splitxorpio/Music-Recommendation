"""Provider answers shared by every user and process (table provider_cache).

Freshness rules stay with the code that knows the provider (deezer.ttl etc.); this table stores when each
answer was fetched and when it may be evicted. Not-found answers are cached too (briefly), so a missing
song isn't asked about by every user's run.
"""
import time

from . import db

EVICT_AFTER_EXPIRY_DAYS = 30


def get(provider: str, key: str) -> dict | None:
    """{'t': fetched_at (unix seconds), 'v': payload} or None."""
    with db.system() as cur:
        cur.execute("SELECT extract(epoch FROM fetched_at)::float8 AS t, payload FROM provider_cache "
                    "WHERE provider = %s AND cache_key = %s", (provider, key))
        r = cur.fetchone()
    return {"t": r["t"], "v": r["payload"]} if r else None


def put(provider: str, key: str, value, ttl_s: float, status: int = 200):
    with db.system() as cur:
        cur.execute("""INSERT INTO provider_cache (provider, cache_key, status, payload, fetched_at, expires_at)
                       VALUES (%s, %s, %s, %s, now(), now() + %s * interval '1 second')
                       ON CONFLICT (provider, cache_key) DO UPDATE SET status = EXCLUDED.status, payload = EXCLUDED.payload,
                           fetched_at = EXCLUDED.fetched_at, expires_at = EXCLUDED.expires_at""",
                    (provider, key, status, db.jsonb(value), ttl_s))


def fresh(provider: str, key: str, max_age_s: float):
    """The payload if fetched within max_age_s, else None (and a miss is indistinguishable from a stale hit)."""
    rec = get(provider, key)
    return rec if rec and time.time() - rec["t"] < max_age_s else None


def evict() -> int:
    with db.system() as cur:
        cur.execute("DELETE FROM provider_cache WHERE expires_at < now() - %s * interval '1 day'", (EVICT_AFTER_EXPIRY_DAYS,))
        return cur.rowcount


class DeezerBackend:
    """Plugs into deezer.install_backend(): the shared table replaces cache/deezer_cache.json."""

    def get(self, key):
        return get("deezer", key)

    def put(self, key, value, ttl_s):
        put("deezer", key, value, ttl_s, 200 if value is not None else 404)
