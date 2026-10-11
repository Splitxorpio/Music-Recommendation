"""Spotify as a listening source: per-user OAuth (authorization code flow) and a pull of top/recent/saved.

Constraints this code is built around (verified Oct 2026, see CLAUDE.md):
  * An app in development mode works only for up to 5 Spotify accounts the operator adds in the Spotify
    dashboard; everyone else gets 403. That is surfaced as a clear message, not retried.
  * Rate limits are app-wide and unpublished; a burst of ~600 single-track calls earned a 24 h block. So
    the pull is ~20 sequential requests per user, paced app-wide through the shared 'spotify' quota bucket,
    and a 429 makes every user's pull wait (no hidden sleeping through long Retry-Afters).
  * No per-track lookups: ISRCs come inside the pulled track objects.
"""
import base64
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

from .. import quota, settings
from . import Sighting, SourceError

AUTH = "https://accounts.spotify.com/authorize"
TOKEN = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "user-top-read user-read-recently-played user-library-read"
SAVED_MAX_PAGES = 20                 # 1,000 saved songs; more adds little to a taste profile and costs requests
TOP_PAGES = 2                        # ~100 per time range, as the single-user pull did
SHORT_WAIT_S = 30
DEV_MODE_MESSAGE = ("Spotify refused access for this account. This server's Spotify app is in development mode, "
                    "which only works for up to 5 Spotify accounts the operator has added in the Spotify developer "
                    "dashboard. Ask the operator to add your Spotify email, or use Last.fm or a listening-history "
                    "upload instead.")


def authorize_url(state: str) -> str:
    s = settings.get().require("spotify_client_id")
    return AUTH + "?" + urlencode({"client_id": s.spotify_client_id, "response_type": "code",
                                   "redirect_uri": s.spotify_redirect_uri, "scope": SCOPES, "state": state,
                                   "show_dialog": "true"})


def new_state() -> str:
    return secrets.token_urlsafe(24)


def _basic():
    s = settings.get().require("spotify_client_id", "spotify_client_secret")
    return "Basic " + base64.b64encode(f"{s.spotify_client_id}:{s.spotify_client_secret}".encode()).decode()


def _token_request(http, data) -> dict:
    r = http.post(TOKEN, data=data, headers={"Authorization": _basic()}, timeout=20)
    if r.status_code == 400:
        raise SourceError("Spotify didn't accept the sign-in (it may have expired or been revoked). Please connect again.",
                          reauth=True)
    r.raise_for_status()
    j = r.json()
    return {"access_token": j["access_token"], "refresh_token": j.get("refresh_token"),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=int(j.get("expires_in", 3600)) - 60),
            "scopes": (j.get("scope") or "").split()}


def exchange_code(code: str, http=None) -> dict:
    return _token_request(http or requests, {"grant_type": "authorization_code", "code": code,
                                             "redirect_uri": settings.get().spotify_redirect_uri})


def refresh(refresh_token: str, http=None) -> dict:
    t = _token_request(http or requests, {"grant_type": "refresh_token", "refresh_token": refresh_token})
    t["refresh_token"] = t["refresh_token"] or refresh_token      # Spotify may or may not rotate it
    return t


class Client:
    """Spotify Web API calls for one user. `pace` books each call in the shared quota (None in tests)."""

    def __init__(self, access_token: str, http=None, pace=True):
        self.http = http or requests.Session()
        self.token = access_token
        self.pace = pace
        self.requests = 0

    def get(self, path, **params):
        for attempt in range(2):
            if self.pace:
                quota.take_turn("spotify", 1.0, max_wait=quota.MAX_WAIT_S)
            r = self.http.get(f"{API}/{path}", params=params or None,
                              headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
            self.requests += 1
            if r.status_code == 429:
                after = float(r.headers.get("Retry-After") or 5)
                if self.pace:
                    quota.block("spotify", after)               # every user's pull waits, not just this one
                if after <= SHORT_WAIT_S and attempt == 0:
                    time.sleep(after)
                    continue
                raise SourceError(f"Spotify asked us to slow down for {int(after)} s; your import will resume then.",
                                  retry_after=after)
            if r.status_code == 401:
                raise SourceError("Spotify sign-in expired.", reauth=True)
            if r.status_code == 403:
                raise SourceError(DEV_MODE_MESSAGE)
            r.raise_for_status()
            return r.json()
        raise SourceError("Spotify kept asking us to slow down.", retry_after=60)

    def me(self) -> dict:
        return self.get("me")


def _sighting(t: dict, played_at=None) -> Sighting | None:
    if not t or not t.get("id") or t.get("type", "track") != "track":
        return None
    imgs = (t.get("album") or {}).get("images") or []
    return Sighting(title=t["name"],
                    artists=[{"name": a["name"], "ids": {"spotify": a.get("id")}} for a in t.get("artists", [])],
                    ids={"spotify": t["id"]}, isrc=(t.get("external_ids") or {}).get("isrc"),
                    duration_ms=t.get("duration_ms"), cover_url=imgs[0]["url"] if imgs else None,
                    last_played_at=played_at)


def pull(client: Client, progress=lambda msg: None) -> dict[str, list[Sighting]]:
    """{signal: [Sighting]} for the five listening signals the taste profile weights."""
    out: dict[str, list[Sighting]] = {}
    for term in ("short_term", "medium_term", "long_term"):
        items = []
        for page in range(TOP_PAGES):
            j = client.get("me/top/tracks", time_range=term, limit=50, offset=50 * page)
            items += j.get("items", [])
            if not j.get("next"):
                break
        out[f"top_{term}"] = [s for s in map(_sighting, items) if s]
        progress(f"top songs ({term.replace('_', ' ')}): {len(out[f'top_{term}'])}")
    j = client.get("me/player/recently-played", limit=50)
    out["recent"] = [s for s in (_sighting(i.get("track"), _ts(i.get("played_at"))) for i in j.get("items", [])) if s]
    saved = []
    for page in range(SAVED_MAX_PAGES):
        j = client.get("me/tracks", limit=50, offset=50 * page)
        saved += [i.get("track") for i in j.get("items", [])]
        progress(f"saved songs: {len(saved)}")
        if not j.get("next"):
            break
    out["saved"] = [s for s in map(_sighting, saved) if s]
    return out


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None
