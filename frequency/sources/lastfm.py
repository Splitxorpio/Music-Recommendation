"""Last.fm scrobbles as a listening source: public listening data by username, no Spotify involved.

Needs the operator's free API key (LASTFM_API_KEY). Signals mirror the Spotify ones, so the taste profile
weights them the same way: top songs of the last week / month / year, recent plays, and loved songs.
Last.fm knows songs by name (and sometimes a MusicBrainz id), not ISRC: resolve.py matches them.
"""
from datetime import datetime, timezone

import requests

from .. import quota, settings
from . import Sighting, SourceError

API = "https://ws.audioscrobbler.com/2.0/"
UA = "Frequency/1.0 (multi-user listening-analysis project)"
PERIODS = {"top_7day": "7day", "top_1month": "1month", "top_12month": "12month"}
TOP_LIMIT = 100
RECENT_LIMIT = 200
LOVED_LIMIT = 200


class Client:
    def __init__(self, http=None, pace=True):
        self.key = settings.get().lastfm_api_key
        if not self.key:
            raise SourceError("Last.fm isn't set up on this server yet (the operator needs to add LASTFM_API_KEY).")
        self.http = http or requests.Session()
        self.pace = pace
        self.requests = 0

    def call(self, method, **params):
        if self.pace:
            quota.take_turn("lastfm", 1.0)
        r = self.http.get(API, params={"method": method, "api_key": self.key, "format": "json", **params},
                          headers={"User-Agent": UA}, timeout=20)
        self.requests += 1
        try:
            j = r.json()
        except ValueError:
            r.raise_for_status()
            raise
        err = j.get("error") if isinstance(j, dict) else None
        if err == 6:
            raise SourceError("Last.fm doesn't know that username.")
        if err == 17:
            raise SourceError("That Last.fm profile hides its listening. Make it public in Last.fm's privacy settings.")
        if err == 29:
            if self.pace:
                quota.block("lastfm", 60)
            raise SourceError("Last.fm asked us to slow down; your import will resume shortly.", retry_after=60)
        if err in (10, 26):
            raise SourceError("This server's Last.fm API key was refused (operator: check LASTFM_API_KEY).")
        if err:
            raise SourceError(f"Last.fm error {err}: {j.get('message', '')}", retry_after=300)
        r.raise_for_status()
        return j


def _artist(t):
    a = t.get("artist") or {}
    name = a.get("name") or a.get("#text") or ""
    mbid = a.get("mbid") or None
    return {"name": name, "mbid": mbid} if mbid else {"name": name}


def _sighting(t, played=None) -> Sighting | None:
    a = _artist(t)
    if not t.get("name") or not a["name"]:
        return None
    pc = t.get("playcount")
    return Sighting(title=t["name"], artists=[a], ids={"lastfm": t["url"]} if t.get("url") else {},
                    play_count=int(pc) if pc and str(pc).isdigit() else None, last_played_at=played)


def pull(client: Client, username: str, progress=lambda msg: None) -> dict[str, list[Sighting]]:
    username = username.strip()
    info = client.call("user.getinfo", user=username)
    out = {}
    for signal, period in PERIODS.items():
        j = client.call("user.gettoptracks", user=username, period=period, limit=TOP_LIMIT)
        out[signal] = [s for s in map(_sighting, (j.get("toptracks") or {}).get("track", [])) if s]
        progress(f"top songs ({period}): {len(out[signal])}")
    j = client.call("user.getrecenttracks", user=username, limit=RECENT_LIMIT)
    recent = []
    for t in (j.get("recenttracks") or {}).get("track", []):
        if (t.get("@attr") or {}).get("nowplaying"):
            continue
        uts = (t.get("date") or {}).get("uts")
        s = _sighting(t, datetime.fromtimestamp(int(uts), timezone.utc) if uts else None)
        if s:
            recent.append(s)
    out["recent_scrobbles"] = recent
    j = client.call("user.getlovedtracks", user=username, limit=LOVED_LIMIT)
    out["loved"] = [s for s in map(_sighting, (j.get("lovedtracks") or {}).get("track", [])) if s]
    progress(f"loved songs: {len(out['loved'])}")
    return out, {"external_user_id": (info.get("user") or {}).get("name") or username}
