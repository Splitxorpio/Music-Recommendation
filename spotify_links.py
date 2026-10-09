"""Exact Spotify links for recommended songs, looked up lazily and remembered.

The analysis never searches Spotify. A new song gets its link from ReccoBeats' data (`href`), or from
this store if it was found before; otherwise it carries a Spotify search link, which works when clicked.
The dashboard then resolves exact links in the background, one paced search at a time, only for songs
actually being shown (Spotify's docs recommend lazy loading; ~80 searches per run previously drew a
multi-hour block on search). Results are kept by recording code (ISRC): found links forever, "not on
Spotify" answers for NOT_FOUND_DAYS.
"""
import json
import queue
import threading
import time
from urllib.parse import quote

from config import CACHE
from spotify_auth import RateLimited, call, client

FILE = CACHE / "spotify_links.json"
NOT_FOUND_DAYS = 7
PACE_S = 2.5                       # gentle: at most one search every 2.5 s
_lock = threading.Lock()


def _load():
    try:
        return json.loads(FILE.read_text())
    except Exception:
        return {}


def _save(d):
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(FILE)


def search_link(title, artist):
    return "https://open.spotify.com/search/" + quote(f"{title} {artist}")


def known(isrc):
    """(url or None, settled): settled is True when we have a link or a recent 'not on Spotify'."""
    with _lock:
        rec = _load().get(isrc)
    if not rec:
        return None, False
    if rec["url"]:
        return rec["url"], True
    return None, time.time() - rec["t"] < NOT_FOUND_DAYS * 86400


def remember(isrc, url):
    with _lock:
        d = _load()
        d[isrc] = {"url": url, "t": time.time()}
        _save(d)


class Resolver:
    """Background, one-at-a-time exact-link lookups for songs the dashboard is showing."""

    def __init__(self, on_found):
        self.on_found = on_found
        self.q, self.queued = queue.Queue(), set()
        threading.Thread(target=self._run, daemon=True).start()

    def want(self, isrc):
        if isrc and isrc not in self.queued and not known(isrc)[1]:
            self.queued.add(isrc)
            self.q.put(isrc)

    def _run(self):
        sp = None
        while True:
            isrc = self.q.get()
            try:
                sp = sp or client()
                hit = call(sp.search, q=f"isrc:{isrc}", type="track", limit=1)["tracks"]["items"]
                url = hit[0]["external_urls"]["spotify"] if hit else None
                remember(isrc, url)
                if url:
                    self.on_found(isrc, url)
            except RateLimited:
                pass                                   # search is blocked: leave it queued-for-later, send nothing
            except Exception as ex:
                print(f"  spotify link lookup failed for {isrc}: {ex}")
            finally:
                self.queued.discard(isrc)
            time.sleep(PACE_S)
