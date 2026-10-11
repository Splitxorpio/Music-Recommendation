"""PostgreSQL access: connection pool, migrations, and the two ways code is allowed to touch data.

  with db.system() as cur:        global tables (catalog, measurements, cache, jobs, quota). No user rows visible.
  with db.as_user(uid) as cur:    one user's data. Row-level security limits every user table to that user,
                                  so a forgotten WHERE clause can't leak another tenant's rows.

Both open one transaction that commits on success and rolls back on any exception.
"""
import atexit
import hashlib
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import settings

MIGRATIONS = Path(__file__).parent / "migrations"
MIGRATION_LOCK = 0x46524551  # advisory lock id ("FREQ"): two processes never migrate at once

_pools: dict[str, ConnectionPool] = {}
_url_override: str | None = None


def use(url: str | None):
    """Point this process at another database (tests use the disposable test database)."""
    global _url_override
    _url_override = url


def url() -> str:
    u = _url_override or settings.get().database_url
    if not u:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return u


def pool() -> ConnectionPool:
    u = url()
    p = _pools.get(u)
    if p is None:
        p = _pools[u] = ConnectionPool(u, min_size=1, max_size=10, kwargs={"row_factory": dict_row},
                                       open=True, name="frequency")
    return p


def close_pools():
    for p in _pools.values():
        p.close()
    _pools.clear()


atexit.register(close_pools)


@contextmanager
def system():
    with pool().connection() as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT set_config('app.user_id', '', true)")
            yield cur


@contextmanager
def for_calibration():
    """Background calibration work: sees library rows of users who consented to calibration use, nothing else."""
    with pool().connection() as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT set_config('app.user_id', '', true), set_config('app.purpose', 'calibration', true)")
            yield cur


@contextmanager
def as_user(user_id):
    uid = str(UUID(str(user_id)))  # validates: never interpolate anything but a real UUID
    with pool().connection() as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT set_config('app.user_id', %s, true)", (uid,))
            yield cur


def jsonb(v):
    return Jsonb(v)


# ─── migrations ───

def _files():
    out = []
    for p in sorted(MIGRATIONS.glob("*.sql")):
        m = re.match(r"(\d{4})_(.+)\.sql$", p.name)
        if m:
            out.append((int(m.group(1)), m.group(2), p))
    return out


def migrate(target_url: str | None = None, verbose=True) -> list[str]:
    """Apply pending migrations in order, each in its own transaction. Refuses to run if an
    already-applied migration file has been edited (its checksum changed)."""
    applied_now = []
    with psycopg.connect(target_url or url(), autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version integer PRIMARY KEY, name text NOT NULL, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now())""")
            done = {r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
            for version, name, path in _files():
                sql = path.read_text()
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if version in done:
                    if done[version] != checksum:
                        raise RuntimeError(f"Migration {path.name} was edited after it was applied; "
                                           f"add a new migration instead.")
                    continue
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                                 (version, name, checksum))
                applied_now.append(path.name)
                if verbose:
                    print(f"[migrate] applied {path.name}")
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))
    return applied_now


def pending(target_url: str | None = None) -> list[str]:
    """Migration files not yet applied to the database."""
    with psycopg.connect(target_url or url()) as conn:
        exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()[0]
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")} if exists else set()
    return [p.name for v, _, p in _files() if v not in done]


def reset(target_url: str):
    """Drop everything in a database. Only for the disposable test database."""
    if not re.search(r"/[^/]*test[^/]*$", target_url.split("?")[0]):
        raise RuntimeError("reset() only runs against a database whose name contains 'test'")
    with psycopg.connect(target_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


if __name__ == "__main__":
    migrate()
