"""The multi-user web API (FastAPI) and the page it serves.

    python -m frequency.api            # http://127.0.0.1:8060 by default (FREQUENCY_HOST / FREQUENCY_PORT)

  /api/v1/...        versioned, typed API (models.py): accounts, sessions, connections, uploads, runs, profile,
                     recommendations, health
  /api/profile, /api/recommendations, /api/pipeline, /api/pipeline/start
                     the four endpoints the existing React page already calls, now answered for the signed-in
                     user (same shapes as dashboard.py), so the page works unchanged
  /img?u=            same-origin cover pass-through (allow-listed image hosts), for artwork colour sampling
  /                  the built page (frontend/dist)

Security: session cookie (HttpOnly, SameSite=Lax, Secure behind TLS), CSRF double-submit token plus an Origin
check on every state-changing request, provider tokens encrypted at rest and never returned, no query strings
in access logs (OAuth codes travel in them), security headers on every response.
"""
import hashlib
import hmac
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import accounts, connections, db, jobs, settings, userdata
from ..sources import SourceError, lastfm, spotify
from ..services import dashboard, library, runs
from . import models as M

COOKIE, CSRF_COOKIE, CSRF_HEADER = "freq_session", "freq_csrf", "x-csrf-token"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
IMG_HOSTS = ("cdn-images.dzcdn.net", "e-cdns-images.dzcdn.net", "i.scdn.co", "mosaic.scdn.co")
DIST = settings.ROOT / "frontend" / "dist"
IMG = settings.ROOT / "data" / "img"
GENERIC_FAILURE = ("Something went wrong on our side and the run stopped. Everything done so far is saved; "
                   "start it again to carry on.")
log = logging.getLogger("frequency.api")

CSP = ("default-src 'self'; img-src 'self' data: https://*.dzcdn.net https://i.scdn.co https://mosaic.scdn.co "
       "https://*.mzstatic.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class Ctx:
    def __init__(self, user, session, token):
        self.user, self.session, self.token = user, session, token

    @property
    def uid(self):
        return self.user["id"]


def auth(request: Request) -> Ctx:
    token = request.cookies.get(COOKIE)
    found = accounts.session_user(token)
    if not found:
        raise HTTPException(401, "Sign in first.")
    return Ctx(found[0], found[1], token)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return not origin or urlparse(origin).netloc == request.headers.get("host")


def auth_write(request: Request, ctx: Ctx = Depends(auth)) -> Ctx:
    if not _same_origin(request) or not accounts.csrf_ok(ctx.session, request.headers.get(CSRF_HEADER),
                                                         request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(403, "This request didn't come from the Frequency page (missing or wrong CSRF token).")
    return ctx


def _set_session(resp: Response, token, csrf, expires):
    secure = settings.get().secure_cookies
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=secure, expires=expires, path="/")
    resp.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="strict", secure=secure, expires=expires, path="/")


def _clear_session(resp: Response):
    resp.delete_cookie(COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")


def _available() -> M.SourcesAvailable:
    s = settings.get()
    sp = bool(s.spotify_client_id and s.spotify_client_secret)
    return M.SourcesAvailable(spotify=sp, lastfm=bool(s.lastfm_api_key),
                              spotify_note="This server's Spotify app is in development mode: it only works for up to "
                                           "5 Spotify accounts the operator has added." if sp else None)


def _connections(uid):
    with db.as_user(uid) as cur:
        cur.execute("SELECT * FROM provider_connections ORDER BY created_at")
        return cur.fetchall()


def _default_source(uid) -> str | None:
    have = {c["provider"]: c for c in _connections(uid)}
    for p in ("spotify", "lastfm"):
        if p in have and have[p]["status"] == "active":
            return p
    return None


def _start(uid, source, payload=None):
    job_id, created = runs.start(uid, source, payload)
    dashboard.forget(uid)
    with db.as_user(uid) as cur:
        return jobs.get(cur, job_id), created


def _display_error(job) -> str | None:
    err = job["last_error"] or ""
    return err[len("user: "):] if err.startswith("user: ") else GENERIC_FAILURE if err else None


def pipeline_status(ctx: Ctx) -> dict:
    """The loading screen's /api/pipeline shape for this user's latest run, plus account/source info."""
    import progress as P
    job = runs.latest(ctx.uid)
    data = dashboard.load(ctx.uid)
    conns = _connections(ctx.uid)
    live = bool(job and job["status"] in jobs.LIVE)
    disp = dict(((job or {}).get("progress") or {}).get("display") or P._blank())
    disp.setdefault("covers", []), disp.setdefault("facts", [])
    if job:
        disp["status"] = ("running" if live else "error" if job["status"] == "failed" else
                          "idle" if job["status"] == "cancelled" else "done")
        if job["status"] == "failed":
            disp["error"] = _display_error(job)
    disp["pid"] = None
    return {"progress": disp, "stages": P.STAGES, "running": live, "ready": data is not None and not live,
            "account": {"email": ctx.user["email"], "display_name": ctx.user["display_name"]},
            "sources": {"connected": [connections.public(c) for c in conns], "available": _available().model_dump()},
            "needs_source": not conns and data is None and not live, "job_id": job and job["id"]}


def create_app(serve_frontend: bool = True) -> FastAPI:
    app = FastAPI(title="Frequency API", version="1.0.0", docs_url="/api/v1/docs", openapi_url="/api/v1/openapi.json",
                  redoc_url=None)

    @app.middleware("http")
    async def headers_and_log(request: Request, call_next):
        t = time.perf_counter()
        limit = settings.get().upload_max_bytes + 1024 * 1024          # file plus form overhead
        if request.method in UNSAFE and int(request.headers.get("content-length") or 0) > limit:
            resp = JSONResponse({"error": f"That upload is larger than {limit // (1024 * 1024)} MB."}, status_code=413)
        else:                                                          # refused before the body is read, not after
            resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        log.info("%s %s %s %.0fms", request.method, request.url.path, resp.status_code, 1000 * (time.perf_counter() - t))
        return resp

    @app.exception_handler(HTTPException)
    async def http_error(request, ex: HTTPException):
        return JSONResponse({"error": ex.detail}, status_code=ex.status_code)

    @app.exception_handler(accounts.AuthError)
    async def auth_error(request, ex: accounts.AuthError):
        return JSONResponse({"error": str(ex)}, status_code=ex.status)

    @app.exception_handler(SourceError)
    async def source_error(request, ex: SourceError):
        return JSONResponse({"error": str(ex)}, status_code=503 if ex.retry_after else 400)

    v1 = "/api/v1"

    # ── health ──
    @app.get(f"{v1}/health", response_model=M.Health)
    def health():
        try:
            pending = db.pending()
            with db.system() as cur:
                cur.execute("SELECT status, count(*) AS n FROM jobs WHERE status IN ('queued', 'running', 'retry_wait') GROUP BY status")
                queue = {r["status"]: r["n"] for r in cur.fetchall()}
            return M.Health(ok=not pending, database=True, schema_pending=pending, queue=queue)
        except Exception:
            return JSONResponse(M.Health(ok=False, database=False, schema_pending=[], queue={}).model_dump(), status_code=503)

    # ── accounts ──
    @app.post(f"{v1}/auth/signup", response_model=M.Session, status_code=201)
    def signup(body: M.SignUp, request: Request, response: Response):
        if not settings.get().allow_signup:
            raise HTTPException(403, "Sign-ups are closed on this server.")
        if not _same_origin(request):
            raise HTTPException(403, "Cross-site request refused.")
        u = accounts.create_user(body.email, body.password, body.display_name)
        token, csrf, exp = accounts.new_session(u["id"], request.headers.get("user-agent"))
        _set_session(response, token, csrf, exp)
        return M.Session(user=M.User(**accounts.public_user(u)), csrf_token=csrf)

    @app.post(f"{v1}/auth/login", response_model=M.Session)
    def login(body: M.SignIn, request: Request, response: Response):
        if not _same_origin(request):
            raise HTTPException(403, "Cross-site request refused.")
        u = accounts.authenticate(body.email, body.password)
        token, csrf, exp = accounts.new_session(u["id"], request.headers.get("user-agent"))
        _set_session(response, token, csrf, exp)
        return M.Session(user=M.User(**accounts.public_user(u)), csrf_token=csrf)

    @app.post(f"{v1}/auth/logout", status_code=204)
    def logout(response: Response, ctx: Ctx = Depends(auth_write)):
        accounts.end_session(ctx.token)
        _clear_session(response)

    @app.get(f"{v1}/me", response_model=M.User)
    def me(ctx: Ctx = Depends(auth)):
        return M.User(**accounts.public_user(ctx.user))

    @app.patch(f"{v1}/me", response_model=M.User)
    def update_me(body: M.UpdateMe, ctx: Ctx = Depends(auth_write)):
        return M.User(**accounts.public_user(accounts.update_profile(ctx.uid, body.display_name, body.calibration_consent)))

    @app.post(f"{v1}/me/password", status_code=204)
    def change_password(body: M.ChangePassword, response: Response, ctx: Ctx = Depends(auth_write)):
        accounts.change_password(ctx.uid, body.current_password, body.new_password)
        _clear_session(response)

    @app.post(f"{v1}/me/delete", status_code=204)
    def delete_me(body: M.ConfirmPassword, response: Response, ctx: Ctx = Depends(auth_write)):
        accounts.delete_account(ctx.uid, body.password)
        dashboard.forget(ctx.uid)
        _clear_session(response)

    @app.get(f"{v1}/me/export")
    def export_me(ctx: Ctx = Depends(auth)):
        """Everything stored about this user, as JSON (provider tokens excluded: they're secrets, not data)."""
        from .. import catalog
        with db.as_user(ctx.uid) as cur:
            lib = userdata.library(cur)
            cur.execute("SELECT id, provider, status, stats, started_at, finished_at FROM library_imports ORDER BY started_at")
            imports = cur.fetchall()
            cur.execute("SELECT id, params, status, stats, created_at, finished_at FROM recommendation_runs ORDER BY created_at")
            runs_ = cur.fetchall()
            prof = userdata.current_profile(cur)
            conns = [connections.public(c) for c in _connections(ctx.uid)]
        with db.system() as cur:
            info = catalog.track_info(cur, list(lib))
            cur.execute("SELECT event, detail, at FROM audit_events WHERE user_id = %s ORDER BY at", (ctx.uid,))
            events = cur.fetchall()
        return {"user": accounts.public_user(ctx.user), "connections": conns, "imports": imports, "runs": runs_,
                "library": [{"title": info[t]["title"], "artists": info[t]["artists"], "signals": s} for t, s in lib.items()],
                "profile": prof and prof["profile"], "events": events}

    # ── connections ──
    @app.get(f"{v1}/connections", response_model=M.Connections)
    def list_connections(ctx: Ctx = Depends(auth)):
        return M.Connections(connections=[M.Connection(**connections.public(c)) for c in _connections(ctx.uid)],
                             available=_available())

    def _state_sig(ctx: Ctx, nonce: str) -> str:
        key = settings.get().require("secret_key").secret_key.encode()
        return hmac.new(key, f"spotify-oauth:{hashlib.sha256(ctx.token.encode()).hexdigest()}:{nonce}".encode(),
                        "sha256").hexdigest()[:32]

    @app.post(f"{v1}/connections/spotify/authorize", response_model=M.AuthorizeUrl)
    def spotify_authorize(ctx: Ctx = Depends(auth_write)):
        if not _available().spotify:
            raise HTTPException(400, "Spotify isn't set up on this server.")
        nonce = secrets.token_urlsafe(16)
        return M.AuthorizeUrl(url=spotify.authorize_url(f"{nonce}.{_state_sig(ctx, nonce)}"))

    @app.get(f"{v1}/connections/spotify/callback")
    def spotify_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
                         ctx: Ctx = Depends(auth)):
        nonce, _, sig = (state or "").partition(".")
        if not nonce or not hmac.compare_digest(sig, _state_sig(ctx, nonce)):
            raise HTTPException(400, "That Spotify sign-in link isn't valid for this session. Try connecting again.")
        if error or not code:
            return RedirectResponse("/?spotify=denied", status_code=303)
        t = spotify.exchange_code(code)
        me_ = spotify.Client(t["access_token"]).me()
        with db.as_user(ctx.uid) as cur:
            connections.save(cur, ctx.uid, "spotify", external_user_id=me_.get("id"), display_name=me_.get("display_name"),
                             tokens=t)
        _start(ctx.uid, "spotify")
        return RedirectResponse("/?spotify=connected", status_code=303)

    @app.post(f"{v1}/connections/lastfm", response_model=M.Connection, status_code=201)
    def lastfm_connect(body: M.LastfmConnect, ctx: Ctx = Depends(auth_write)):
        info = lastfm.Client().call("user.getinfo", user=body.username)
        name = (info.get("user") or {}).get("name") or body.username
        with db.as_user(ctx.uid) as cur:
            connections.save(cur, ctx.uid, "lastfm", external_user_id=name, display_name=name)
            return M.Connection(**connections.public(connections.get(cur, "lastfm")))

    @app.delete(f"{v1}/connections/{{provider}}", status_code=204)
    def disconnect(provider: str, ctx: Ctx = Depends(auth_write)):
        with db.as_user(ctx.uid) as cur:
            if not connections.disconnect(cur, ctx.uid, provider):
                raise HTTPException(404, "Not connected.")

    # ── uploads and runs ──
    @app.post(f"{v1}/uploads/history", response_model=M.RunStarted, status_code=202)
    async def upload_history(file: UploadFile = File(...), ctx: Ctx = Depends(auth_write)):
        limit = settings.get().upload_max_bytes
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"That file is larger than {limit // (1024 * 1024)} MB.")
        from ..sources import history_upload
        history_upload.parse([(file.filename or "upload.json", data)])     # reject bad files now, not in the worker
        live = runs.latest(ctx.uid)
        if live and live["status"] in jobs.LIVE:
            raise HTTPException(409, "A run is already in progress; upload again when it's finished.")
        path = library.save_upload(ctx.uid, file.filename or "upload.json", data)
        with db.as_user(ctx.uid) as cur:
            connections.save(cur, ctx.uid, "history_upload", display_name=file.filename)
        job, created = _start(ctx.uid, "history_upload", {"upload_path": path})
        return M.RunStarted(job=M.Job(**jobs.public(job)), created=created)

    @app.post(f"{v1}/runs", response_model=M.RunStarted, status_code=202)
    def start_run(body: M.StartRun, ctx: Ctx = Depends(auth_write)):
        source = body.source or _default_source(ctx.uid)
        if source is None and not _has_library(ctx.uid):
            raise HTTPException(409, "Connect Spotify or Last.fm, or upload your listening history first.")
        job, created = _start(ctx.uid, source)
        return M.RunStarted(job=M.Job(**jobs.public(job)), created=created)

    @app.get(f"{v1}/runs/current", response_model=M.Job | None)
    def current_run(ctx: Ctx = Depends(auth)):
        job = runs.latest(ctx.uid)
        if not job:
            return None
        j = jobs.public(job)
        if job["status"] == "failed":
            j["error"] = _display_error(job)
        return M.Job(**j)

    @app.post(f"{v1}/runs/{{job_id}}/cancel", status_code=204)
    def cancel_run(job_id: int, ctx: Ctx = Depends(auth_write)):
        if not runs.cancel(ctx.uid, job_id):
            raise HTTPException(404, "No such run in progress.")

    # ── results ──
    def _data(ctx: Ctx):
        d = dashboard.load(ctx.uid)
        if d is None:
            raise HTTPException(409, "No analysis yet.")
        return d

    @app.get(f"{v1}/profile", response_model=M.Profile)
    def profile_v1(ctx: Ctx = Depends(auth)):
        return _data(ctx).profile_payload

    @app.get(f"{v1}/recommendations", response_model=M.Recommendations)
    def recommendations_v1(request: Request, ctx: Ctx = Depends(auth)):
        return _data(ctx).recommendations(_qs(request))

    # ── the existing page's endpoints, for the signed-in user ──
    @app.get("/api/pipeline")
    def pipeline_compat(ctx: Ctx = Depends(auth)):
        return pipeline_status(ctx)

    @app.post("/api/pipeline/start")
    def pipeline_start_compat(ctx: Ctx = Depends(auth_write)):
        source = _default_source(ctx.uid)
        if source is None and not _has_library(ctx.uid):
            raise HTTPException(409, "Connect Spotify or Last.fm, or upload your listening history first.")
        _start(ctx.uid, source)
        return pipeline_status(ctx)

    @app.get("/api/profile")
    def profile_compat(ctx: Ctx = Depends(auth)):
        d = dashboard.load(ctx.uid)
        return d.profile_payload if d else JSONResponse({"needs_analysis": True}, status_code=409)

    @app.get("/api/recommendations")
    def recommendations_compat(request: Request, ctx: Ctx = Depends(auth)):
        d = dashboard.load(ctx.uid)
        return d.recommendations(_qs(request)) if d else JSONResponse({"needs_analysis": True}, status_code=409)

    @app.get("/img")
    def image(u: str, ctx: Ctx = Depends(auth)):
        host = urlparse(u).hostname or ""
        if urlparse(u).scheme != "https" or (host not in IMG_HOSTS and not host.endswith(".mzstatic.com")):
            raise HTTPException(400, "host not allowed")
        IMG.mkdir(parents=True, exist_ok=True)
        f = IMG / (hashlib.sha256(u.encode()).hexdigest() + ".jpg")
        if not f.exists():
            try:
                r = requests.get(u, timeout=20)
                r.raise_for_status()
                if not r.headers.get("content-type", "").startswith("image/"):
                    raise ValueError("not an image")
                tmp = f.with_suffix(".part")
                tmp.write_bytes(r.content)
                tmp.replace(f)
            except Exception:
                raise HTTPException(502, "image fetch failed")
        return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    if serve_frontend and (DIST / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def page(path: str):
            f = (DIST / path).resolve()
            if path and DIST.resolve() in f.parents and f.is_file():
                return FileResponse(f)
            return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def _qs(request: Request) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in request.query_params.multi_items():
        out.setdefault(k, []).append(v)
    return out


def _has_library(uid) -> bool:
    with db.as_user(uid) as cur:
        cur.execute("SELECT 1 FROM user_library LIMIT 1")
        return cur.fetchone() is not None
