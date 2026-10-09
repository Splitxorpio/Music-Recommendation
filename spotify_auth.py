"""The one Spotify client every script uses.

Spotify answers bursts from new developer apps with HTTP 429 and a Retry-After that can be a whole
day. spotipy would silently sleep through it, so 429 isn't auto-retried here: short waits (<= 30s)
are waited out once, anything longer raises RateLimited straight away and callers decide whether
to carry on without Spotify or stop.
"""
import json
import threading
import time

import requests
import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth
from urllib3.util.retry import Retry

import net
from config import CACHE, ROOT
SCOPES = "user-top-read user-read-recently-played user-library-read"


class RateLimited(Exception):
    def __init__(self, seconds):
        self.seconds = seconds
        until = time.strftime("%a %-I:%M %p", time.localtime(time.time() + seconds))
        hours = seconds / 3600
        span = f"about {hours:.0f} hours" if hours >= 1.5 else f"about {max(1, round(seconds / 60))} minutes"
        super().__init__(f"Spotify has paused requests from this app for {span} (until {until}).")


def client():
    load_dotenv(ROOT / ".env")
    sp = spotipy.Spotify(
        auth_manager=SpotifyOAuth(scope=SCOPES, cache_path=str(CACHE / ".spotify_token")),
        requests_timeout=20)
    # urllib3 honours a 429's Retry-After on its own (even outside status_forcelist), sleeping for as
    # long as Spotify says. Swap in a retry policy that never does that, so call() sees the 429.
    retry = Retry(total=3, connect=3, read=False, status=3, backoff_factor=0.3,
                  status_forcelist=(500, 502, 503, 504), respect_retry_after_header=False,
                  allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]))
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    sp._session.mount("https://", adapter)
    sp._session.mount("http://", adapter)
    return sp


# Spotify rate-limits each kind of request separately (verified Oct 8 2026: GET /me answered 200 while
# GET /tracks/{id} was still under a 24 h block and search under a ~7 h one). So blocks are remembered per
# operation, in a file every process shares, and no request of a blocked kind is sent until it expires.
COOLDOWNS = CACHE / "spotify_cooldowns.json"
_lock = threading.Lock()


def _cooldowns():
    try:
        return json.loads(COOLDOWNS.read_text())
    except Exception:
        return {}


def _save_cooldowns(d):
    tmp = COOLDOWNS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(COOLDOWNS)


def blocked(op):
    """Seconds left on a known block for this kind of request (0 if none)."""
    with _lock:
        until = _cooldowns().get(op, {}).get("until", 0)
    return max(0, until - time.time())


def call(fn, *args, **kwargs):
    """Run one Spotify request. A short rate limit (<= 30 s) is waited out once; a longer one is
    recorded for this kind of request and raised as RateLimited. Requests of a kind that is known
    to be blocked are not sent at all."""
    op = getattr(fn, "__name__", "spotify")
    left = blocked(op)
    if left:
        net.cache_hit("spotify_avoided_during_block")
        raise RateLimited(int(left))
    for attempt in range(2):
        t = time.perf_counter()
        try:
            res = fn(*args, **kwargs)
            net._record("spotify", time.perf_counter() - t, _Ok(), None, op)
            return res
        except spotipy.SpotifyException as ex:
            net._record("spotify", time.perf_counter() - t, _Status(ex.http_status), None, op)
            if ex.http_status != 429:
                raise
            wait = int((ex.headers or {}).get("Retry-After", 30))
            net._bump("spotify", "rate_limited")
            if wait > 30 or attempt:
                with _lock:
                    d = _cooldowns()
                    d[op] = {"until": time.time() + wait, "retry_after": wait, "status": 429,
                             "seen": time.strftime("%Y-%m-%d %H:%M:%S")}
                    _save_cooldowns(d)
                raise RateLimited(wait) from ex
            time.sleep(wait + 1)


class _Status:                                   # minimal stand-ins so net's metrics can record Spotify calls
    def __init__(self, code):
        self.status_code, self.content = code, b""


class _Ok(_Status):
    def __init__(self):
        super().__init__(200)
