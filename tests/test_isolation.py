"""Tenant isolation is enforced by the database, not just by query discipline."""
import psycopg
import pytest

from conftest import make_user
from frequency import catalog, db, userdata


def _seed(uid, sid):
    with db.system() as cur:
        t = catalog.resolve_track(cur, "T" + sid, ["A"], ids={"spotify": sid}, source="t")
    with db.as_user(uid) as cur:
        imp = userdata.start_import(cur, uid, "spotify")
        userdata.replace_signals(cur, uid, imp, {"saved": [{"track_id": t, "weight": 1}]})
        pid = userdata.save_profile(cur, uid, {"secret": sid}, None)
        run = userdata.create_run(cur, uid, pid, {})
        userdata.save_results(cur, uid, run, {t: {"why": sid}}, "completed", {})
    return t


def test_users_only_see_their_own_rows_even_without_where_clauses():
    a, b = make_user("a@x.com"), make_user("b@x.com")
    ta, tb = _seed(a, "sa"), _seed(b, "sb")
    for uid, mine in ((a, ta), (b, tb)):
        with db.as_user(uid) as cur:
            for table in ("user_library", "library_imports", "user_listening_profiles",
                          "recommendation_runs", "recommendation_results"):
                cur.execute(f"SELECT DISTINCT user_id FROM {table}")
                assert [r["user_id"] for r in cur.fetchall()] == [uid], table
            assert set(userdata.library(cur)) == {mine}


def test_no_user_context_sees_no_user_rows():
    a = make_user()
    _seed(a, "s")
    with db.system() as cur:
        cur.execute("SELECT count(*) AS n FROM user_library")
        assert cur.fetchone()["n"] == 0


def test_cannot_write_into_another_users_rows():
    a, b = make_user("a@x.com"), make_user("b@x.com")
    t = _seed(a, "s")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.as_user(a) as cur:
            cur.execute("INSERT INTO user_library (user_id, track_id, signal, weight) VALUES (%s, %s, 'saved', 1)", (b, t))
    with db.as_user(b) as cur:                        # updates/deletes of A's rows silently match nothing
        cur.execute("UPDATE user_library SET weight = 99")
        cur.execute("DELETE FROM recommendation_runs")
    with db.as_user(a) as cur:
        assert userdata.library(cur) == {t: {"saved": 1.0}}
        assert userdata.latest_run(cur) is not None


def test_as_user_rejects_non_uuid():
    with pytest.raises(ValueError):
        with db.as_user("x' OR 1=1 --"):
            pass


def test_deleting_a_user_removes_all_their_data_and_keeps_others():
    a, b = make_user("a@x.com"), make_user("b@x.com")
    _seed(a, "sa"), _seed(b, "sb")
    with db.system() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (a,))
    with db.as_user(a) as cur:
        for table in ("user_library", "library_imports", "user_listening_profiles", "recommendation_runs",
                      "recommendation_results", "provider_connections"):
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            assert cur.fetchone()["n"] == 0, table
    with db.as_user(b) as cur:
        assert len(userdata.library(cur)) == 1
    with db.system() as cur:                          # global catalog is untouched
        cur.execute("SELECT count(*) AS n FROM tracks")
        assert cur.fetchone()["n"] == 2


def test_replace_signals_keeps_unmentioned_lists():
    a = make_user()
    with db.system() as cur:
        t1 = catalog.resolve_track(cur, "One", ["A"], ids={"spotify": "1"}, source="t")
        t2 = catalog.resolve_track(cur, "Two", ["A"], ids={"spotify": "2"}, source="t")
    with db.as_user(a) as cur:
        userdata.replace_signals(cur, a, None, {"saved": [{"track_id": t1, "weight": 1}],
                                                "recent": [{"track_id": t1, "weight": 1.5}]})
        userdata.replace_signals(cur, a, None, {"recent": [{"track_id": t2, "weight": 1.5}]})
        assert userdata.library(cur) == {t1: {"saved": 1.0}, t2: {"recent": 1.5}}
