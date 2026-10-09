"""One-off test: how many of my Spotify tracks does ReccoBeats have audio features for?

Read-only on Spotify. Saves results to cache/ so later runs don't repeat lookups.
"""
import json
import re
import time
from pathlib import Path

import requests
import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

import net
import progress as P
from spotify_auth import RateLimited, call, client

load_dotenv()
from config import CACHE
CACHE.mkdir(exist_ok=True)

SCOPES = "user-top-read user-read-recently-played user-library-read"
RECCO = "https://api.reccobeats.com/v1/audio-features"
BATCH = 40


def collect_tracks(sp):
    """Return {spotify_id: {name, artists, sources}} and a per-source status report."""
    tracks, status = {}, {}

    def add(items, source):
        n = 0
        for t in items:
            if not t or not t.get("id"):
                continue
            n += 1
            imgs = (t.get("album") or {}).get("images") or []
            art = (imgs[1] if len(imgs) > 1 else imgs[0])["url"] if imgs else None
            e = tracks.setdefault(
                t["id"],
                {"name": t["name"], "artists": [a["name"] for a in t["artists"]], "sources": [], "cover": art,
                 "isrc": (t.get("external_ids") or {}).get("isrc")},
            )
            P.cover(art, t["name"], t["artists"][0]["name"] if t["artists"] else "")
            if source not in e["sources"]:
                e["sources"].append(source)
        status[source] = n
        P.tick(len(tracks), now=f"{n} songs from your {source.replace('_', ' ').replace('top ', 'top tracks, ')}")

    for rng in ("short_term", "medium_term", "long_term"):
        try:
            items, offset = [], 0
            while offset < 100:  # top tracks caps out around 99
                page = call(sp.current_user_top_tracks, limit=50, offset=offset, time_range=rng)["items"]
                items += page
                if len(page) < 50:
                    break
                offset += 50
            add(items, f"top_{rng}")
        except RateLimited as ex:                  # this list is blocked; the others still come in
            status[f"top_{rng}"] = ("blocked", ex.seconds)
        except Exception as ex:
            status[f"top_{rng}"] = f"FAILED: {ex}"

    try:
        add([i["track"] for i in call(sp.current_user_recently_played, limit=50)["items"]], "recent")
    except RateLimited as ex:
        status["recent"] = ("blocked", ex.seconds)
    except Exception as ex:
        status["recent"] = f"FAILED: {ex}"

    try:
        items, offset = [], 0
        while True:
            page = call(sp.current_user_saved_tracks, limit=50, offset=offset)["items"]
            items += [p["track"] for p in page]
            if len(page) < 50:
                break
            offset += 50
        add(items, "saved")
    except RateLimited as ex:
        status["saved"] = ("blocked", ex.seconds)
    except Exception as ex:
        status["saved"] = f"FAILED: {ex}"

    return tracks, status


def fetch_features(ids):
    """Batch lookup (40 per request, 4 batches at a time). Missing tracks are silently omitted by
    the API, so results are keyed by the Spotify id in href."""
    found = {}
    P.stage("notes", total=len(ids))
    chunks = [tuple(ids[i:i + BATCH]) for i in range(0, len(ids), BATCH)]

    def batch(chunk):
        r = net.get("reccobeats", RECCO, params={"ids": ",".join(chunk)})
        r.raise_for_status()
        return r.json().get("content", [])

    def done(chunk, res, n):
        if isinstance(res, Exception):
            print(f"  batch failed: {res}")
            return
        for f in res:
            m = re.search(r"/track/([A-Za-z0-9]+)", f.get("href") or "")
            if m:
                found[m.group(1)] = f
        P.tick(min(n * BATCH, len(ids)), now=f"{len(found)} songs with official measurements so far")

    net.pmap(batch, chunks, workers=4, on_result=done)
    return found


def main():
    previous = json.loads((CACHE / "tracks.json").read_text()) if (CACHE / "tracks.json").exists() else None
    P.stage("login")
    sp = client()
    try:
        me = call(sp.current_user)
    except RateLimited as ex:
        if not previous:
            raise
        # Spotify is blocking even the login check: carry on with the last song list, clearly labelled.
        when = time.strftime("%b %-d", time.localtime((CACHE / "tracks.json").stat().st_mtime))
        P.fact(f"{ex} Using your song list from {when} (last known).", key="ratelimit")
        for k in ("login", "pull", "notes"):
            P.stage(k)
            P.finish(k)
        return
    print("Logged in as:", me["id"])
    P.fact(f"Signed in as {me.get('display_name') or me['id']}")
    P.finish("login")

    P.stage("pull", label="songs found")
    tracks, status = collect_tracks(sp)
    blocked = [k for k, v in status.items() if isinstance(v, tuple)]
    if blocked and previous:
        # Lists Spotify is blocking right now are filled from the last saved list (other lists are fresh).
        for i, t in previous.items():
            carried = [s for s in t["sources"] if s in blocked]
            if carried:
                e = tracks.setdefault(i, {**t, "sources": []})
                e["sources"] = sorted(set(e["sources"]) | set(carried))
        P.fact(f"Spotify is pausing your {', '.join(b.replace('_', ' ') for b in blocked)} list; "
               f"used the last saved copy for those.", key="ratelimit")
    elif blocked and not tracks:
        raise RateLimited(max(v[1] for v in status.values() if isinstance(v, tuple)))
    # Spotify already sent each song's recording code (ISRC); keep them so nothing has to ask again.
    known = json.loads((CACHE / "isrc.json").read_text()) if (CACHE / "isrc.json").exists() else {}
    known.update({i: t["isrc"] for i, t in tracks.items() if t.get("isrc")})
    (CACHE / "isrc.json").write_text(json.dumps(known, indent=1))
    from collections import Counter
    top = Counter(t["artists"][0] for t in tracks.values()).most_common(1)
    P.fact(f"{len(tracks)} songs on your shelves")
    if top:
        P.fact(f"Most of the shelf belongs to {top[0][0]}: {top[0][1]} songs", key="top-artist")
    failed = [k for k, v in status.items() if isinstance(v, str) and v.startswith("FAILED")]
    if failed:
        P.fact(f"Spotify wouldn't hand over: {', '.join(failed)}")
    P.finish("pull")
    print("\nSpotify pulled:")
    for k, v in status.items():
        print(f"  {k}: {v}")
    print(f"  unique tracks: {len(tracks)}")

    features = fetch_features(list(tracks))
    (CACHE / "tracks.json").write_text(json.dumps(tracks, indent=1))
    (CACHE / "features.json").write_text(json.dumps(features, indent=1))

    hit = len(features)
    print(f"\nReccoBeats coverage: {hit}/{len(tracks)} = {100 * hit / max(len(tracks), 1):.1f}%")
    gaps = Counter(t["artists"][0] for i, t in tracks.items() if i not in features).most_common(1)
    P.fact(f"Official notes for {100 * hit / max(len(tracks), 1):.0f}% of your songs")
    if gaps:
        P.fact(f"Biggest gap: {gaps[0][0]}, {gaps[0][1]} songs with no notes. We'll go listen ourselves.", key="gap")
    P.finish("notes")

    print("\nBy source:")
    srcs = sorted({s for t in tracks.values() for s in t["sources"]})
    for s in srcs:
        ids = [i for i, t in tracks.items() if s in t["sources"]]
        got = sum(i in features for i in ids)
        print(f"  {s}: {got}/{len(ids)} ({100 * got / len(ids):.0f}%)")

    missing = [t for i, t in tracks.items() if i not in features]
    print(f"\nMissing ({len(missing)}), first 40:")
    for t in missing[:40]:
        print(f"  {t['name']} - {', '.join(t['artists'])}")


if __name__ == "__main__":
    main()
