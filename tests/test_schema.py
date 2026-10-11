import psycopg
import pytest

from frequency import db
from conftest import TEST_URL


def test_migrations_are_idempotent():
    assert db.migrate(TEST_URL, verbose=False) == []


def test_edited_migration_is_refused(tmp_path, monkeypatch):
    with db.system() as cur:
        cur.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
    try:
        with pytest.raises(RuntimeError, match="edited after it was applied"):
            db.migrate(TEST_URL, verbose=False)
    finally:
        import hashlib
        real = hashlib.sha256((db.MIGRATIONS / "0001_domain_model.sql").read_bytes()).hexdigest()
        with db.system() as cur:
            cur.execute("UPDATE schema_migrations SET checksum = %s WHERE version = 1", (real,))


def test_reset_refuses_non_test_database():
    with pytest.raises(RuntimeError):
        db.reset("postgresql://u:p@localhost/frequency")


def test_app_role_is_not_superuser_and_cannot_bypass_rls():
    with db.system() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        r = cur.fetchone()
    assert r == {"rolsuper": False, "rolbypassrls": False}


def test_only_one_active_calibration():
    with db.system() as cur:
        cur.execute("INSERT INTO calibration_versions (version, status, model, training_data) VALUES ('a', 'active', '{}', '{}')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.system() as cur:
            cur.execute("INSERT INTO calibration_versions (version, status, model, training_data) VALUES ('b', 'active', '{}', '{}')")
