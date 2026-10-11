"""A fake of every outside music service the backend uses, served on 127.0.0.1 (no real requests in tests).

Workers started with FREQUENCY_TEST_PROVIDER_URL=<this server> send every provider request here as
/<original host>/<original path>. The fake has a small deterministic catalog (artists in four "scenes",
tracks with Spotify ids, Deezer ids and ISRCs; two thirds have official ReccoBeats numbers) and it enforces a
ReccoBeats-style fixed-window limit (analysis = 1 unit, lookup = 1/8, windows aligned to the clock), so tests
can count rejections and how many times each clip was analysed.
"""
import hashlib
import io
import json
import math
import threading
import time
import uuid
import wave
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

SCENES = ["trap", "indie rock", "house", "folk"]
N_ARTISTS, TRACKS_PER_ARTIST = 20, 6


def _sid(n):
    return f"{n:022d}"


def _r(seed, lo=0.0, hi=1.0):
    h = int(hashlib.sha256(str(seed).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return lo + (hi - lo) * h


class Catalog:
    def __init__(self):
        self.artists, self.tracks = {}, {}
        for a in range(N_ARTISTS):
            scene = a % 4
            self.artists[1000 + a] = {"id": 1000 + a, "name": f"Artist {a}", "scene": scene,
                                      "mbid": str(uuid.UUID(int=0xA000 + a)), "nb_fan": 1000 * (a + 1)}
            for j in range(TRACKS_PER_ARTIST):
                n = a * 10 + j
                dz = 10000 + n
                base = {0: (0.8, 0.4, 0.05, 0.3), 1: (0.6, 0.5, 0.2, 0.05), 2: (0.9, 0.7, 0.02, 0.05), 3: (0.3, 0.4, 0.8, 0.03)}[scene]
                feats = {"energy": base[0] + _r((n, "e"), -.1, .1), "valence": base[1] + _r((n, "v"), -.2, .2),
                         "acousticness": max(0, base[2] + _r((n, "a"), -.05, .05)), "speechiness": base[3],
                         "danceability": _r((n, "d"), .3, .9), "instrumentalness": _r((n, "i"), 0, .1),
                         "liveness": _r((n, "l"), .05, .3), "loudness": -14 + 10 * base[0], "tempo": 80 + 15 * scene + _r((n, "t"), 0, 30),
                         "key": n % 12, "mode": n % 2}
                self.tracks[dz] = {"dz": dz, "sid": _sid(n + 1), "isrc": f"QZFAKE{n:06d}", "title": f"Song {a}-{j}",
                                   "artist": 1000 + a, "official": (a + j) % 3 != 0, "features": feats,
                                   "bass_hz": 40 + 30 * scene}
        self.by_sid = {t["sid"]: t for t in self.tracks.values()}
        self.by_isrc = {t["isrc"]: t for t in self.tracks.values()}

    def related(self, artist_id, k=3):
        a = self.artists[artist_id]
        same = [x for x in self.artists.values() if x["scene"] == a["scene"] and x["id"] != artist_id]
        return same[:k]


class State:
    def __init__(self, capacity=5.0, window=2.0, phase=0.0):
        self.cat = Catalog()
        self.capacity, self.window, self.phase = capacity, window, phase
        self.lock = threading.Lock()
        self.used = Counter()
        self.analyses = Counter()            # track deezer id -> successful analyses
        self.clip_owner = {}                 # sha256 of clip bytes -> deezer id
        self.rejections = Counter()
        self.requests = Counter()
        self.base = None

    def admit(self, cost, kind):
        with self.lock:
            now = time.time()
            k = math.floor((now - self.phase) / self.window)
            if self.used[k] + cost > self.capacity + 1e-9:
                self.rejections[kind] += 1
                return int(self.phase + (k + 1) * self.window - now)   # rounded down, like the real one
            self.used[k] += cost
            return None

    def clip(self, dz):
        t = self.cat.tracks[dz]
        sr = 8000
        x = np.arange(sr * 2) / sr
        y = 0.5 * np.sin(2 * np.pi * t["bass_hz"] * x) + 0.3 * np.sin(2 * np.pi * (600 + dz % 400) * x)
        b = io.BytesIO()
        w = wave.open(b, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 12000).astype("<i2").tobytes())
        w.close()
        data = b.getvalue()
        self.clip_owner[hashlib.sha256(data).hexdigest()] = dz
        return data


def _deezer_track(st, t):
    a = st.cat.artists[t["artist"]]
    return {"id": t["dz"], "title": t["title"], "isrc": t["isrc"], "duration": 180, "rank": 500000 - t["dz"],
            "release_date": "2026-01-01", "preview": f"https://cdnt-preview.dzcdn.net/api/1/1/fake/{t['dz']}.mp3?exp={int(time.time()) + 900}",
            "artist": {"id": a["id"], "name": a["name"]}, "contributors": [{"id": a["id"], "name": a["name"]}],
            "album": {"title": f"Album {a['id']}", "cover_big": f"https://cdn-images.dzcdn.net/images/cover/{t['dz']}/500x500.jpg"}}


def _recco_track(t, st):
    a = st.cat.artists[t["artist"]]
    return {"id": str(uuid.UUID(int=t["dz"])), "trackTitle": t["title"], "durationMs": 180000, "isrc": t["isrc"],
            "href": f"https://open.spotify.com/track/{t['sid']}",
            "artists": [{"id": str(uuid.UUID(int=a["id"])), "name": a["name"], "href": f"https://open.spotify.com/artist/A{a['id']}"}]}


def make_handler(st):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def out(self, obj, code=200, ctype="application/json", headers=None):
            body = obj if isinstance(obj, bytes) else (obj if isinstance(obj, str) else json.dumps(obj)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            host, _, path = u.path.lstrip("/").partition("/")
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            st.requests[host] += 1
            cat = st.cat
            if host == "cdnt-preview.dzcdn.net":
                return self.out(st.clip(int(path.rsplit("/", 1)[-1].split(".")[0])), ctype="audio/mpeg")
            if host == "api.deezer.com":
                parts = path.split("/")
                if parts[0] == "track":
                    ref = parts[1]
                    t = cat.by_isrc.get(ref.split(":", 1)[1]) if ref.startswith("isrc:") else cat.tracks.get(int(ref)) if ref.isdigit() else None
                    return self.out(_deezer_track(st, t) if t else {"error": {"type": "DataException", "code": 800}})
                if parts[0] == "search" and len(parts) == 1:
                    words = q.get("q", "").lower()
                    hits = [_deezer_track(st, t) for t in cat.tracks.values()
                            if t["title"].lower() in words and cat.artists[t["artist"]]["name"].lower() in words]
                    return self.out({"data": hits, "total": len(hits)})
                if parts[:2] == ["search", "artist"]:
                    name = q.get("q", "").lower()
                    return self.out({"data": [{"id": a["id"], "name": a["name"], "nb_fan": a["nb_fan"],
                                               "picture_big": f"https://cdn-images.dzcdn.net/images/artist/{a['id']}/500x500.jpg"}
                                              for a in cat.artists.values() if a["name"].lower() == name]})
                if parts[0] == "artist":
                    aid = int(parts[1])
                    lim = int(q.get("limit", 10))
                    if len(parts) == 2:
                        return self.out(cat.artists[aid])
                    if parts[2] == "related":
                        return self.out({"data": [{"id": a["id"], "name": a["name"]} for a in cat.related(aid)][:lim]})
                    if parts[2] == "top":
                        return self.out({"data": [_deezer_track(st, t) for t in cat.tracks.values() if t["artist"] == aid][:lim]})
                    if parts[2] == "radio":
                        rel = [a["id"] for a in cat.related(aid)]
                        return self.out({"data": [_deezer_track(st, t) for t in cat.tracks.values() if t["artist"] in rel][::4][:lim]})
                return self.out({"error": {"code": 800}})
            if host == "api.reccobeats.com":
                wait = st.admit(0.125, "lookup")
                if wait is not None:
                    return self.out({"error": "too many"}, 429, headers={"Retry-After": str(wait)})
                ids = [x for x in q.get("ids", "").split(",") if x]
                if path == "v1/track":
                    return self.out({"content": [_recco_track(cat.by_sid[i], st) for i in ids if i in cat.by_sid]})
                if path == "v1/audio-features":
                    found = [cat.by_sid.get(i) or cat.by_isrc.get(i) for i in ids]
                    return self.out({"content": [{**{k: round(v, 4) for k, v in t["features"].items()}, "id": str(uuid.UUID(int=t["dz"])),
                                                  "isrc": t["isrc"], "href": f"https://open.spotify.com/track/{t['sid']}"}
                                                 for t in found if t and t["official"]]})
                if path == "v1/track/recommendation":
                    return self.out({"content": []})
            if host == "musicbrainz.org":
                if path == "ws/2/genre/all":
                    return self.out("\n".join(SCENES), ctype="text/plain")
                if path == "ws/2/recording":
                    codes = [c.strip("() ") for c in q.get("query", "").replace("isrc:", "").split(" OR ")]
                    recs = []
                    for c in codes:
                        t = cat.by_isrc.get(c)
                        if t:
                            a = cat.artists[t["artist"]]
                            recs.append({"isrcs": [c], "artist-credit": [{"name": a["name"], "artist": {"id": a["mbid"], "name": a["name"]}}]})
                    return self.out({"recordings": recs})
                if path == "ws/2/artist":
                    query = q.get("query", "")
                    if "arid:" in query:
                        ids = [x.strip() for x in query.replace("arid:", "").split(" OR ")]
                        arts = [a for a in cat.artists.values() if a["mbid"] in ids]
                        return self.out({"artists": [{"id": a["mbid"], "name": a["name"],
                                                      "tags": [{"name": SCENES[a["scene"]], "count": 3}, {"name": "seen live", "count": 1}]}
                                                     for a in arts]})
                    return self.out({"artists": [{"id": a["mbid"], "name": a["name"]} for a in cat.artists.values()
                                                 if f'"{a["name"]}"' in query]})
                if path.startswith("ws/2/artist/"):
                    return self.out({"relations": []})
                if path.startswith("ws/2/isrc/"):
                    return self.out({"error": "not found"}, 404)
            return self.out({"error": f"fake has no {host}/{path}"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            host, _, path = u.path.lstrip("/").partition("/")
            st.requests[host] += 1
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if host == "api.reccobeats.com" and path == "v1/analysis/audio-features":
                wait = st.admit(1.0, "analysis")
                if wait is not None:
                    return self.out({"error": "too many"}, 429, headers={"Retry-After": str(wait)})
                dz = next((owner for sha, owner in list(st.clip_owner.items()) if _contains(body, sha, st)), None)
                if dz is None:
                    return self.out({"error": "unreadable"}, 400)
                st.analyses[dz] += 1
                f = st.cat.tracks[dz]["features"]
                return self.out({k: round(v + 0.05, 4) if k == "acousticness" else round(v, 4) for k, v in f.items()})
            return self.out({"error": "no"}, 404)
    return H


def _contains(body, sha, st):
    """Which served clip is inside this multipart upload (match by the clip's bytes)."""
    cache = st.__dict__.setdefault("_clip_bytes", {})
    if sha not in cache:
        cache[sha] = st.clip(st.clip_owner[sha])
    return cache[sha] in body


def start(capacity=5.0, window=2.0, phase=0.0):
    st = State(capacity, window, phase)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st))
    st.base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, st


def history_upload(artist_range, plays_per_track=6):
    """An Extended Streaming History file for a user who plays the given artists' songs."""
    cat = Catalog()
    rows, t0 = [], 1790000000
    for t in cat.tracks.values():
        a = t["artist"] - 1000
        if a in artist_range:
            for k in range(plays_per_track - (t["dz"] % 3)):
                rows.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + 3600 * (t["dz"] % 500) + 86400 * k)),
                             "ms_played": 120000, "master_metadata_track_name": t["title"],
                             "master_metadata_album_artist_name": cat.artists[t["artist"]]["name"],
                             "spotify_track_uri": f"spotify:track:{t['sid']}"})
    return json.dumps(rows).encode()
