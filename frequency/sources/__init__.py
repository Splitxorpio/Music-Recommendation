"""Listening sources. Each turns one user's listening into signals: {signal: [Sighting]} where a Sighting
describes a recording as the source knows it. Resolution into canonical tracks happens in resolve.py.

  spotify         OAuth, live pull of top/recent/saved (Spotify app in development mode: max 5 users)
  lastfm          public scrobbles by username (needs LASTFM_API_KEY); no Spotify involvement
  history_upload  the user's own Spotify data export (Extended or basic streaming history JSON / zip)
"""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Sighting:
    title: str
    artists: list                       # names, or {"name", "ids": {provider: id}}
    ids: dict = field(default_factory=dict)      # {"spotify": ..., "deezer": ..., "lastfm": ...}
    isrc: str | None = None
    duration_ms: int | None = None
    cover_url: str | None = None
    play_count: int | None = None
    last_played_at: datetime | None = None

    def key(self):
        return self.ids.get("spotify") or self.isrc or (str(self.artists[0]).lower(), self.title.lower())


class SourceError(Exception):
    """A problem the user can act on (shown to them as-is). `retry_after` set = temporary."""
    def __init__(self, message, retry_after: float | None = None, reauth: bool = False):
        super().__init__(message)
        self.retry_after, self.reauth = retry_after, reauth
