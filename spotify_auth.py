"""The one Spotify client every script uses.

Spotify answers bursts from new developer apps with HTTP 429 and a Retry-After that can be a whole
day. spotipy would silently sleep through it, so 429 isn't auto-retried here: short waits (<= 30s)
are waited out once, anything longer raises RateLimited straight away and callers decide whether
to carry on without Spotify or stop.
"""
import time
from pathlib import Path

import requests
import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth
from urllib3.util.retry import Retry

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


def call(fn, *args, **kwargs):
    """Run one Spotify request; wait out a short rate limit once, raise RateLimited for a long one."""
    for attempt in range(2):
        try:
            return fn(*args, **kwargs)
        except spotipy.SpotifyException as ex:
            if ex.http_status != 429:
                raise
            wait = int((ex.headers or {}).get("Retry-After", 30))
            if wait > 30 or attempt:
                raise RateLimited(wait) from ex
            time.sleep(wait + 1)
