"""Local dashboard server: python dashboard.py  ->  http://127.0.0.1:8050

Serves the designed page (React source in frontend/, built to frontend/dist/; rebuilt automatically when the
source is newer than the build) and these endpoints:
  /api/profile             taste profile, sub-tastes, artwork, tempo landscapes, genre links
  /api/recommendations     live rank() with the dashboard's options
  /api/pipeline            analysis progress for the loading screen (GET), start/resume (POST .../start)
  /img?u=<cover url>       same-origin image pass-through (lets the page sample artwork colour), cached to cache/img/
With an empty cache the page shows the loading screen and runs the analysis (pipeline.py) in the background.
Standard library only, bound to 127.0.0.1 so nothing is exposed beyond this machine.
"""
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections import defaultdict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import requests

import progress
import spotify_links
from artist_genres import key
from recommend import WEIGHTS, rank, score_all
from taste_profile import SOURCE_WEIGHT, load, merge_duplicates

ROOT = Path(__file__).parent
from config import CACHE
FRONTEND = ROOT / "frontend"
STATIC, IMG = FRONTEND / "dist", CACHE / "img"
IMG.mkdir(parents=True, exist_ok=True)
PORT = 8050
IMG_HOSTS = ("cdn-images.dzcdn.net", "e-cdns-images.dzcdn.net", "i.scdn.co", "mosaic.scdn.co")
NEEDED = ("enriched.json", "profile.json", "candidates.json", "artwork.json")


def build_page():
    """Build the React page into frontend/dist when it's missing or older than its source (needs Node.js/npm)."""
    built = STATIC / "index.html"
    sources = [f for f in [*FRONTEND.glob("*.*"), *(FRONTEND / "src").rglob("*")] if f.is_file()]
    if built.exists() and built.stat().st_mtime >= max(f.stat().st_mtime for f in sources):
        return
    npm = shutil.which("npm")
    if not npm:
        if built.exists():
            print("Note: the page source changed but npm isn't installed, so the last built page is used.")
            return
        sys.exit("The dashboard page hasn't been built yet and npm isn't installed. Install Node.js, then run this again.")
    print("Building the dashboard page...")
    if not (FRONTEND / "node_modules").exists():
        subprocess.run([npm, "ci", "--no-fund", "--no-audit"], cwd=FRONTEND, check=True)
    if subprocess.run([npm, "run", "build", "--silent"], cwd=FRONTEND).returncode != 0:
        sys.exit("Building the dashboard page failed (see above).")


def stamp():
    """None until every file the dashboard needs exists; else their newest change time."""
    files = [CACHE / n for n in NEEDED]
    return max(f.stat().st_mtime for f in files) if all(f.exists() for f in files) else None


def bass_word(b):
    if b is None:
        return None
    return "heavy" if b >= 0.8 else "light" if b < 0.65 else "medium"


class Data:
    def __init__(self):
        self.profile = load("profile.json", {})
        self.art = load("artwork.json", {"tracks": {}, "artists": {}})
        self.cands = load("candidates.json", {})
        self.enriched = merge_duplicates(load("enriched.json", {}))
        genres = load("artist_genres.json", {})
        self.genres = {}
        for name, g in genres.items():
            if g or key(name) not in self.genres:
                self.genres[key(name)] = g
        self.scored = score_all(self.cands, self.profile)
        for c in self.scored:                          # exact links found by earlier lazy lookups
            if not c.get("spotify_exact") and c.get("isrc"):
                url, _ = spotify_links.known(c["isrc"])
                if url:
                    c["spotify_url"], c["spotify_exact"] = url, True
        measured = [c for c in self.scored if c.get("measured")]
        tempos = [c["features"]["tempo"] for c in measured if (c.get("features") or {}).get("tempo")]
        lows = [c["bass"]["low_end_share"] for c in measured if c.get("bass")]
        self.ranges = {"bpm": [int(min(tempos)), int(max(tempos)) + 1],
                       "bass": [round(min(lows), 2), round(max(lows), 2)]}
        self.profile_payload = self.build_profile()

    def cover(self, i):
        return self.art["tracks"].get(i) or {}

    def build_profile(self):
        p = self.profile
        clusters = []
        for cl in p["clusters"]:
            examples = []
            for i in cl.get("example_ids", []):
                t = self.enriched.get(i) or {}
                examples.append({"id": i, "title": t.get("name"), "artists": t.get("artists", []),
                                 "cover": self.cover(i).get("cover"), "cover_m": self.cover(i).get("cover_m"),
                                 "estimated": t.get("feature_source") != "official",
                                 "spotify_url": f"https://open.spotify.com/track/{i}"})
            clusters.append({
                "id": cl["id"], "label": cl["name"], "share": cl["weight_share"], "tracks": cl["tracks"],
                "estimated_share": cl["estimated_share"], "traits": cl["traits"],
                "artists": [{"name": a, "share": s, "photo": self.art["artists"].get(a)} for a, s in cl["top_artists"][:6]],
                "genres": cl["top_genres"][:8], "examples": examples})

        # Tempo landscape per sub-taste: weighted, smoothed distribution of raw BPM.
        grid = np.linspace(60, 200, 71)
        land = defaultdict(lambda: np.zeros_like(grid))
        for i, tc in p["track_clusters"].items():
            f = (self.enriched.get(i) or {}).get("features") or {}
            if f.get("tempo"):
                land[tc["cluster"]] += tc["weight"] * np.exp(-0.5 * ((grid - f["tempo"]) / 4.0) ** 2)
        peak = max(v.max() for v in land.values()) or 1
        landscapes = [{"cluster": c, "points": (land[c] / peak).round(4).tolist()} for c in sorted(land)]

        # Genre constellation: top genres, linked when the same library artists carry both.
        top = [g for g, _ in p["overall"]["top_genres"]]
        share = defaultdict(float)
        for t in self.enriched.values():
            share[t["artists"][0]] += sum(SOURCE_WEIGHT.get(s, 0) for s in t["sources"])
        links = defaultdict(float)
        for a, w in share.items():
            g = [x for x in top if x in (self.genres.get(key(a)) or {})]
            for x in range(len(g)):
                for y in range(x + 1, len(g)):
                    links[(g[x], g[y])] += w
        mx = max(links.values()) if links else 1
        edges = [{"a": a, "b": b, "w": round(v / mx, 3)} for (a, b), v in sorted(links.items(), key=lambda x: -x[1])[:22]]

        lib_covers = []
        for i in sorted(p["track_clusters"], key=lambda i: -p["track_clusters"][i]["weight"]):
            c = self.cover(i)
            if c.get("cover"):
                lib_covers.append({"cover": c["cover"], "cover_m": c.get("cover_m"),
                                   "title": self.enriched[i]["name"], "artist": self.enriched[i]["artists"][0]})
            if len(lib_covers) == 24:
                break

        return {
            "meta": {"date": date.today().isoformat(), "songs": p["coverage"]["with_traits"]},
            "coverage": p["coverage"],
            "traits": p["overall"]["traits"],
            "genres": p["overall"]["top_genres"],
            "artists": [{"name": a, "share": s, "photo": self.art["artists"].get(a)} for a, s in p["overall"]["top_artists"]],
            "clusters": clusters,
            "landscapes": landscapes,
            "landscape_bpm": [60, 200],
            "genre_links": edges,
            "library_covers": lib_covers,
            "ranges": self.ranges,
            "default_weights": WEIGHTS,
            "separation": p.get("separation_score"),
        }

    def recommendations(self, q):
        def num(name, default):
            try:
                return float(q.get(name, [default])[0])
            except ValueError:
                return default
        def rng(name):
            v = q.get(name, [""])[0]
            try:
                lo, hi = (float(x) for x in v.split(","))
                return (lo, hi)
            except ValueError:
                return None
        weights = {"sound": num("ws", WEIGHTS["sound"]), "scene": num("wc", WEIGHTS["scene"]),
                   "connection": num("wn", WEIGHTS["connection"])}
        if sum(weights.values()) <= 0:
            weights = WEIGHTS
        picks = rank(self.scored, self.profile,
                     include_known=q.get("known", ["0"])[0] == "1",
                     include_far=q.get("far", ["0"])[0] == "1",
                     weights=weights, bpm=rng("bpm"), bass=rng("bass"), n=int(num("n", 24)))
        out = []
        for c in picks:
            if not c.get("spotify_exact") and "/search/" in (c.get("spotify_url") or ""):
                RESOLVER.want(c.get("isrc"))           # exact link fetched in the background for next time
            art = self.cover(c["id"])
            g = self.genres.get(key(c["artist"])) or {}
            b = (c.get("bass") or {}).get("low_end_share")
            out.append({
                "id": c["id"], "title": c["title"], "artist": c["artist"], "artists": c.get("artists", []),
                "album": art.get("album") or c.get("album"), "cover": art.get("cover"), "cover_m": art.get("cover_m"),
                "score": c["score"], "parts": c["parts"], "cluster": c["cluster"], "reason": c["reason"],
                "tempo": round((c.get("features") or {}).get("tempo") or 0) or None,
                "bass": b, "bass_word": bass_word(b),
                "estimated": c.get("feature_source") != "official", "scene_inferred": c.get("scene_inferred", False),
                "mode": c.get("mode"), "hop": c.get("hop"), "release": c.get("release_date"),
                "genres": [x for x, _ in sorted(g.items(), key=lambda x: -x[1])[:3]],
                "spotify_url": c.get("spotify_url")})
        return {"count": len(out), "items": out}


DATA, DATA_STAMP, PROC = None, None, None


def _link_found(isrc, url):
    for c in (DATA.scored if DATA else []):
        if c.get("isrc") == isrc:
            c["spotify_url"], c["spotify_exact"] = url, True


RESOLVER = spotify_links.Resolver(_link_found)
LOCK = threading.Lock()


def get_data():
    """Load (or reload after a run) the dashboard data; None while the cache is incomplete."""
    global DATA, DATA_STAMP
    st = stamp()
    if st is None or pipeline_alive():
        return DATA if st is not None else None
    with LOCK:
        if DATA is None or st != DATA_STAMP:
            print("Loading profile and scoring candidates...")
            DATA, DATA_STAMP = Data(), st
    return DATA


def pipeline_alive():
    if PROC is not None and PROC.poll() is None:
        return True
    pid = progress.read().get("pid")
    if pid and progress.read().get("status") == "running":
        try:
            os.kill(pid, 0)                            # started by hand in a terminal
            return True
        except OSError:
            return False
    return False


def pipeline_status():
    p = progress.read()
    alive = pipeline_alive()
    if p.get("status") == "running" and not alive:
        p["status"] = "paused"                         # the run stopped without finishing; Resume continues it
    return {"progress": p, "stages": progress.STAGES, "running": alive,
            "ready": stamp() is not None and not alive}


def start_pipeline(refresh=False):
    global PROC
    if pipeline_alive():
        return
    log = open(CACHE / "pipeline.log", "a")
    PROC = subprocess.Popen([sys.executable, "-u", str(ROOT / "pipeline.py")] + (["--refresh"] if refresh else []),
                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj):
        self.send(200, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/pipeline":
            return self.json(pipeline_status())
        if u.path in ("/api/profile", "/api/recommendations"):
            data = get_data()
            if data is None:
                return self.send(409, json.dumps({"needs_analysis": True}).encode(), "application/json")
            return self.json(data.profile_payload if u.path == "/api/profile" else data.recommendations(q))
        if u.path == "/img":
            return self.image(q.get("u", [""])[0])
        path = (STATIC / ("index.html" if u.path in ("", "/") else u.path.lstrip("/"))).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            return self.send(404, b"not found", "text/plain")
        self.send(200, path.read_bytes(), mimetypes.guess_type(str(path))[0] or "application/octet-stream")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/pipeline/start":
            start_pipeline(refresh=parse_qs(u.query).get("refresh", ["0"])[0] == "1")
            return self.json(pipeline_status())
        self.send(404, b"not found", "text/plain")

    def image(self, url):
        host = urlparse(url).hostname or ""
        if host not in IMG_HOSTS and not host.endswith(".mzstatic.com"):
            return self.send(400, b"host not allowed", "text/plain")
        f = IMG / (hashlib.md5(url.encode()).hexdigest() + ".jpg")
        if not f.exists():
            try:
                r = requests.get(url, timeout=20)
                r.raise_for_status()
                f.write_bytes(r.content)
            except Exception:
                return self.send(502, b"image fetch failed", "text/plain")
        self.send(200, f.read_bytes(), "image/jpeg", cache=True)


def main():
    build_page()
    CACHE.mkdir(exist_ok=True)
    if stamp() is None:
        print("No analysis yet: the page will show the loading screen and run it for you.")
    else:
        get_data()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Dashboard running at {url}  (Ctrl+C to stop)")
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
