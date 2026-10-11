"""The web API: accounts, sessions, CSRF, tenant isolation over HTTP, connections, uploads, deletion."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import make_user
from fakes import providers
from frequency import accounts, connections, crypto, db, jobs
from frequency.api.app import create_app
from frequency.sources import spotify
from seed import seed_analysis

PW = "correct horse battery"


@pytest.fixture
def app():
    return create_app(serve_frontend=False)


def client(app):
    return TestClient(app, base_url="http://testserver")


def signup(c, email="a@x.com"):
    r = c.post("/api/v1/auth/signup", json={"email": email, "password": PW})
    assert r.status_code == 201, r.text
    return r.json()["csrf_token"]


def write(c, method, url, csrf, **kw):
    return c.request(method, url, headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"}, **kw)


def test_signup_sets_secure_session_cookies_and_me_works(app):
    c = client(app)
    r = c.post("/api/v1/auth/signup", json={"email": "a@x.com", "password": PW, "display_name": "A"})
    cookies = r.headers.get_list("set-cookie")
    sess = next(x for x in cookies if x.startswith("freq_session="))
    assert "HttpOnly" in sess and "samesite=lax" in sess.lower()
    assert "HttpOnly" not in next(x for x in cookies if x.startswith("freq_csrf="))
    assert c.get("/api/v1/me").json()["email"] == "a@x.com"
    with db.system() as cur:
        cur.execute("SELECT password_hash FROM users")
        assert cur.fetchone()["password_hash"].startswith("scrypt$") 


def test_weak_password_duplicate_email_and_bad_email(app):
    c = client(app)
    assert c.post("/api/v1/auth/signup", json={"email": "a@x.com", "password": "short"}).status_code == 400
    assert c.post("/api/v1/auth/signup", json={"email": "nope", "password": PW}).status_code == 400
    signup(c)
    assert client(app).post("/api/v1/auth/signup", json={"email": "A@x.com", "password": PW}).status_code == 409


def test_unauthenticated_requests_are_refused(app):
    c = client(app)
    for url in ("/api/v1/me", "/api/v1/profile", "/api/pipeline", "/api/profile", "/api/recommendations", "/img?u=https://i.scdn.co/x"):
        assert c.get(url).status_code == 401, url


def test_state_changes_need_csrf_token_and_same_origin(app):
    c = client(app)
    csrf = signup(c)
    assert c.post("/api/v1/runs", json={}).status_code == 403                       # no token
    assert c.post("/api/v1/runs", json={}, headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert c.post("/api/v1/runs", json={}, headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"}).status_code == 403
    assert write(c, "POST", "/api/v1/runs", csrf, json={}).status_code == 409       # passes CSRF; no source yet


def test_login_logout_and_brute_force_limit(app):
    c = client(app)
    csrf = signup(c)
    assert write(c, "POST", "/api/v1/auth/logout", csrf).status_code == 204
    assert c.get("/api/v1/me").status_code == 401
    bad = client(app)
    for _ in range(accounts.LOGIN_MAX_FAILS):
        assert bad.post("/api/v1/auth/login", json={"email": "a@x.com", "password": "wrong password"}).status_code == 401
    assert bad.post("/api/v1/auth/login", json={"email": "a@x.com", "password": PW}).status_code == 429
    assert client(app).post("/api/v1/auth/login", json={"email": "b@x.com", "password": PW}).status_code == 401


def test_users_only_ever_see_their_own_analysis(app):
    ca, cb = client(app), client(app)
    signup(ca, "a@x.com"), signup(cb, "b@x.com")
    ua = ca.get("/api/v1/me").json()["id"]
    cands = seed_analysis(ua, range(0, 8))
    prof = ca.get("/api/v1/profile")
    assert prof.status_code == 200 and prof.json()["clusters"]
    recs = ca.get("/api/v1/recommendations?n=25&known=1&far=1").json()
    assert recs["count"] > 0 and {p["id"] for p in recs["items"]} <= {c["id"] for c in cands.values()}
    assert ca.get("/api/profile").json() == prof.json()                              # page endpoint, same data
    assert cb.get("/api/v1/profile").status_code == 409                              # B has nothing, sees nothing of A
    assert cb.get("/api/recommendations").status_code == 409
    st = cb.get("/api/pipeline").json()
    assert st["needs_source"] and not st["ready"] and st["account"]["email"] == "b@x.com"
    assert set(ca.get("/api/pipeline").json()) >= {"progress", "stages", "running", "ready"}


def test_recommendation_dials_change_the_ranking(app):
    c = client(app)
    signup(c)
    seed_analysis(c.get("/api/v1/me").json()["id"], range(0, 8))
    a = [p["id"] for p in c.get("/api/v1/recommendations?ws=1&wc=0&wn=0&n=10").json()["items"]]
    b = [p["id"] for p in c.get("/api/v1/recommendations?ws=0&wc=0&wn=1&n=10").json()["items"]]
    assert a and b and a != b
    narrow = c.get("/api/v1/recommendations?bpm=100,120&n=25").json()["items"]
    assert all(100 <= p["tempo"] <= 120 for p in narrow if p["tempo"])


def test_spotify_connection_tokens_are_encrypted_and_never_returned(app, monkeypatch):
    c = client(app)
    csrf = signup(c)
    secret = "SPOTIFY-ACCESS-TOKEN-xyz"
    url = write(c, "POST", "/api/v1/connections/spotify/authorize", csrf).json()["url"]
    state = dict(x.split("=", 1) for x in url.split("?", 1)[1].split("&"))["state"]
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(spotify, "exchange_code", lambda code, http=None: {
        "access_token": secret, "refresh_token": "REFRESH-abc", "scopes": ["user-top-read"],
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)})
    monkeypatch.setattr(spotify.Client, "me", lambda self: {"id": "spuser", "display_name": "Sp User"})
    assert c.get(f"/api/v1/connections/spotify/callback?code=c&state=forged.sig", follow_redirects=False).status_code == 400
    r = c.get(f"/api/v1/connections/spotify/callback?code=c&state={state}", follow_redirects=False)
    assert r.status_code == 303
    uid = c.get("/api/v1/me").json()["id"]
    with db.as_user(uid) as cur:
        conn = connections.get(cur, "spotify")
    assert secret.encode() not in bytes(conn["access_token_enc"]) and connections.tokens(conn)["access_token"] == secret
    for body in (c.get("/api/v1/connections").text, c.get("/api/v1/me/export").text, c.get("/api/pipeline").text):
        assert secret not in body and "REFRESH-abc" not in body
    assert c.get("/api/v1/runs/current").json()["status"] == "queued"              # connecting starts a run


def test_history_upload_validates_then_queues_one_run(app):
    c = client(app)
    csrf = signup(c)
    bad = write(c, "POST", "/api/v1/uploads/history", csrf, files={"file": ("x.json", b"{not json", "application/json")})
    assert bad.status_code == 400
    good = providers.history_upload(range(0, 3))
    r = write(c, "POST", "/api/v1/uploads/history", csrf, files={"file": ("Streaming_History_Audio_2026.json", good, "application/json")})
    assert r.status_code == 202 and r.json()["created"] and r.json()["job"]["status"] == "queued"
    again = write(c, "POST", "/api/v1/uploads/history", csrf, files={"file": ("h.json", good, "application/json")})
    assert again.status_code == 409


def test_cancel_run_and_other_users_cannot_cancel_it(app):
    ca, cb = client(app), client(app)
    csrf_a, csrf_b = signup(ca, "a@x.com"), signup(cb, "b@x.com")
    r = write(ca, "POST", "/api/v1/uploads/history", csrf_a,
              files={"file": ("Streaming_History_Audio_2026.json", providers.history_upload(range(0, 3)), "application/json")})
    jid = r.json()["job"]["id"]
    assert write(cb, "POST", f"/api/v1/runs/{jid}/cancel", csrf_b).status_code == 404
    assert write(ca, "POST", f"/api/v1/runs/{jid}/cancel", csrf_a).status_code == 204
    assert ca.get("/api/v1/runs/current").json()["status"] == "cancelled"


def test_password_change_signs_out_everywhere_and_delete_account_removes_everything(app):
    c, other = client(app), client(app)
    csrf = signup(c)
    signup(other, "b@x.com")
    uid = c.get("/api/v1/me").json()["id"]
    seed_analysis(uid, range(0, 6))
    assert write(c, "POST", "/api/v1/me/password", csrf, json={"current_password": "nope", "new_password": "x" * 12}).status_code == 401
    assert write(c, "POST", "/api/v1/me/password", csrf, json={"current_password": PW, "new_password": "new password 123"}).status_code == 204
    assert c.get("/api/v1/me").status_code == 401
    r = c.post("/api/v1/auth/login", json={"email": "a@x.com", "password": "new password 123"})
    csrf = r.json()["csrf_token"]
    assert write(c, "POST", "/api/v1/me/delete", csrf, json={"password": "wrong"}).status_code == 401
    assert write(c, "POST", "/api/v1/me/delete", csrf, json={"password": "new password 123"}).status_code == 204
    assert c.get("/api/v1/me").status_code == 401
    with db.system() as cur:
        cur.execute("SELECT count(*) AS n FROM users WHERE id = %s", (uid,))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM sessions WHERE user_id = %s", (uid,))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT event FROM audit_events WHERE user_id = %s ORDER BY id DESC LIMIT 1", (uid,))
        assert cur.fetchone()["event"] == "account_deleted"
    with db.as_user(uid) as cur:
        cur.execute("SELECT (SELECT count(*) FROM user_library) + (SELECT count(*) FROM recommendation_runs) AS n")
        assert cur.fetchone()["n"] == 0
    assert other.get("/api/v1/me").status_code == 200


def test_consent_withdrawal_removes_contributed_calibration_pairs(app):
    c = client(app)
    csrf = signup(c)
    uid = c.get("/api/v1/me").json()["id"]
    assert write(c, "PATCH", "/api/v1/me", csrf, json={"calibration_consent": True}).json()["calibration_consent"]
    seed_analysis(uid, range(0, 2))
    with db.system() as cur:
        cur.execute("SELECT id FROM tracks LIMIT 1")
        t = cur.fetchone()["id"]
        cur.execute("INSERT INTO calibration_pairs (track_id, clip_source, official, clip, origin, contributed_by) "
                    "VALUES (%s, 'deezer', '{}', '{}', 'consented_user', %s)", (t, uid))
    write(c, "PATCH", "/api/v1/me", csrf, json={"calibration_consent": False})
    with db.system() as cur:
        cur.execute("SELECT count(*) AS n FROM calibration_pairs")
        assert cur.fetchone()["n"] == 0


def test_security_headers_and_health(app):
    c = client(app)
    r = c.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["database"] and r.json()["schema_pending"] == []
    assert r.headers["x-content-type-options"] == "nosniff" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_image_proxy_only_allows_known_hosts(app):
    c = client(app)
    signup(c)
    assert c.get("/img?u=http://169.254.169.254/latest").status_code == 400
    assert c.get("/img?u=https://evil.example/x.jpg").status_code == 400


def test_oversized_upload_is_refused_before_reading(app, monkeypatch):
    c = client(app)
    csrf = signup(c)
    monkeypatch.setenv("FREQUENCY_UPLOAD_MAX_MB", "1")
    r = write(c, "POST", "/api/v1/uploads/history", csrf, files={"file": ("big.json", b"[" + b" " * (3 * 1024 * 1024) + b"]", "application/json")})
    assert r.status_code == 413
