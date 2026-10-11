"""Test setup: every test runs against the disposable test database (DATABASE_URL_TEST), rebuilt from the
migrations once per session and emptied before each test. No test makes a real provider request: the
`no_network` guard below fails any attempt to open an outside connection."""
import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from frequency import db, settings  # noqa: E402

TEST_URL = os.environ.get("DATABASE_URL_TEST", "")
SEEDED = {}


@pytest.fixture(scope="session", autouse=True)
def test_database():
    if not TEST_URL:
        pytest.exit("DATABASE_URL_TEST is not set; refusing to run against any other database", 2)
    db.reset(TEST_URL)
    db.migrate(TEST_URL, verbose=False)
    db.use(TEST_URL)
    os.environ["DATABASE_URL"] = TEST_URL      # subprocesses started by tests use it too
    with db.system() as cur:                   # rows the migrations seed, restored before each test
        cur.execute("SELECT * FROM quota_buckets")
        SEEDED["quota_buckets"] = cur.fetchall()
    yield
    db.close_pools()


@pytest.fixture(autouse=True)
def clean(test_database):
    with db.system() as cur:
        cur.execute("""SELECT string_agg(format('%I', tablename), ', ') AS t FROM pg_tables
                       WHERE schemaname = 'public' AND tablename <> 'schema_migrations'""")
        cur.execute(f"TRUNCATE {cur.fetchone()['t']} RESTART IDENTITY CASCADE")
        for row in SEEDED["quota_buckets"]:
            cols = list(row)
            cur.execute(f"INSERT INTO quota_buckets ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                        list(row.values()))
    yield


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real = socket.socket.connect

    def guarded(self, addr):
        host = addr[0] if isinstance(addr, tuple) else addr
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
            raise RuntimeError(f"test tried to reach {host}: tests must not use live providers")
        return real(self, addr)
    monkeypatch.setattr(socket.socket, "connect", guarded)


def make_user(email="a@example.com", consent=False):
    with db.system() as cur:
        cur.execute("INSERT INTO users (email, password_hash, calibration_consent) VALUES (%s, 'x', %s) RETURNING id",
                    (email, consent))
        return cur.fetchone()["id"]
