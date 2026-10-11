"""The user's own Spotify data export as a listening source (no Spotify API involved).

Accepts what Spotify's privacy page sends (a .zip, or the .json files inside it):
  * Extended streaming history  Streaming_History_Audio_*.json  entries with ts, ms_played,
    master_metadata_track_name, master_metadata_album_artist_name, spotify_track_uri
  * Basic account data          StreamingHistory_music_*.json   entries with endTime, artistName, trackName, msPlayed
Podcasts, audiobooks and plays under 30 s are skipped.

Plays become signals shaped like the live Spotify ones (relative to the newest play in the file, so an
older export still has a "recent"): most played in the last 90 days / year / ever, plus frequent plays.
"""
import io
import json
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import Sighting, SourceError

MIN_PLAY_MS = 30_000
MAX_MEMBERS = 200
MAX_UNCOMPRESSED = 600 * 1024 * 1024      # guards against zip bombs
SIGNAL_SIZES = {"history_90d": 100, "history_365d": 100, "history_all": 200, "history_frequent": 600}
FREQUENT_MIN_PLAYS = 5


def _json_members(data: bytes, filename: str):
    if filename.lower().endswith(".zip") or data[:4] == b"PK\x03\x04":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as ex:
            raise SourceError("That file isn't a readable zip.") from ex
        infos = [i for i in z.infolist() if i.filename.lower().endswith(".json")
                 and ("streaming_history_audio" in i.filename.lower() or "streaminghistory_music" in i.filename.lower()
                      or ("streaminghistory" in i.filename.lower() and "podcast" not in i.filename.lower()
                          and "video" not in i.filename.lower()))]
        if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
            raise SourceError("That export is larger than this server accepts.")
        if not infos:
            raise SourceError("No streaming history files found in that zip (looking for Streaming_History_Audio_*.json "
                              "or StreamingHistory_music_*.json).")
        for i in infos:
            yield i.filename, z.read(i)
    else:
        yield filename, data


def parse(files: list[tuple[str, bytes]]) -> list[dict]:
    """[{title, artist, spotify_id|None, at, ms}] for every music play, from any mix of supported files."""
    plays = []
    for fname, data in files:
        for name, raw in _json_members(data, fname):
            try:
                rows = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as ex:
                raise SourceError(f"{name} isn't valid JSON.") from ex
            if not isinstance(rows, list):
                raise SourceError(f"{name} doesn't look like a streaming history file.")
            for r in rows:
                if not isinstance(r, dict):
                    continue
                if "ts" in r:                                     # extended history
                    title, artist = r.get("master_metadata_track_name"), r.get("master_metadata_album_artist_name")
                    uri, ms = r.get("spotify_track_uri") or "", r.get("ms_played") or 0
                    at = _parse_time(r["ts"])
                    sid = uri.split(":")[-1] if uri.startswith("spotify:track:") else None
                elif "endTime" in r:                              # basic account data
                    title, artist, ms, sid = r.get("trackName"), r.get("artistName"), r.get("msPlayed") or 0, None
                    at = _parse_time(r["endTime"])
                else:
                    continue
                if title and artist and at and ms >= MIN_PLAY_MS:
                    plays.append({"title": title, "artist": artist, "spotify_id": sid, "at": at, "ms": ms})
    if not plays:
        raise SourceError("No music plays (30 s or longer) found in that upload.")
    return plays


def _parse_time(s):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def signals(plays: list[dict]) -> tuple[dict[str, list[Sighting]], dict]:
    newest = max(p["at"] for p in plays)
    by_song = defaultdict(lambda: {"plays": [], "sid": None, "title": None, "artist": None})
    for p in plays:
        k = p["spotify_id"] or (p["artist"].lower(), p["title"].lower())
        s = by_song[k]
        s["plays"].append(p["at"])
        s["sid"] = s["sid"] or p["spotify_id"]
        s["title"], s["artist"] = p["title"], p["artist"]

    def top(since, n, min_plays=1):
        counted = []
        for k, s in by_song.items():
            c = sum(1 for t in s["plays"] if since is None or t >= since)
            if c >= min_plays:
                counted.append((c, max(s["plays"]), k))
        counted.sort(key=lambda x: (-x[0], -x[1].timestamp()))
        out = []
        for c, last, k in counted[:n]:
            s = by_song[k]
            out.append(Sighting(title=s["title"], artists=[s["artist"]], ids={"spotify": s["sid"]} if s["sid"] else {},
                                play_count=c, last_played_at=last))
        return out

    out = {"history_90d": top(newest - timedelta(days=90), SIGNAL_SIZES["history_90d"]),
           "history_365d": top(newest - timedelta(days=365), SIGNAL_SIZES["history_365d"]),
           "history_all": top(None, SIGNAL_SIZES["history_all"]),
           "history_frequent": top(None, SIGNAL_SIZES["history_frequent"], FREQUENT_MIN_PLAYS)}
    stats = {"plays": len(plays), "songs": len(by_song), "with_spotify_id": sum(1 for s in by_song.values() if s["sid"]),
             "first_play": min(p["at"] for p in plays).isoformat(), "last_play": newest.isoformat()}
    return out, stats
