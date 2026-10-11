"""Test data: a user with a finished analysis, built from the fake catalog without any providers."""
import random

from fakes.providers import Catalog
from frequency import catalog, db, userdata


def seed_analysis(uid, artist_range, seed=1):
    import taste_profile
    from frequency.services import pipeline
    cat, rnd = Catalog(), random.Random(seed)
    signals = {"top_short_term": [], "saved": []}
    with db.system() as cur:
        for t in cat.tracks.values():
            a = cat.artists[t["artist"]]
            if t["artist"] - 1000 not in artist_range:
                continue
            tid = catalog.resolve_track(cur, t["title"], [{"name": a["name"], "mbid": a["mbid"]}],
                                        ids={"spotify": t["sid"], "deezer": t["dz"]}, isrc=t["isrc"], source="seed")
            catalog.record_measurement(cur, tid, "official", "reccobeats", t["features"], confidence="official")
            catalog.record_measurement(cur, tid, "bass", "librosa", {"low_end_share": rnd.uniform(.5, .9)}, confidence="estimated")
            aid = catalog.track_info(cur, [tid])[tid]["artist_ids"][0]
            catalog.set_artist_genres(cur, aid, "musicbrainz", {["trap", "indie rock", "house", "folk"][a["scene"]]: 3})
            signals["saved"].append({"track_id": tid, "weight": 1.0})
            if rnd.random() < .4:
                signals["top_short_term"].append({"track_id": tid, "weight": 3.0})
    with db.as_user(uid) as cur:
        userdata.replace_signals(cur, uid, None, signals)
    enriched, _, _, keys = pipeline.build_enriched(uid)
    g = {a["name"]: {["trap", "indie rock", "house", "folk"][a["scene"]]: 3} for a in cat.artists.values()}
    profile = taste_profile.build_profile(enriched, g, calibration={})
    profile["keys"] = keys
    cands = {}
    with db.system() as cur:
        for k, t in enumerate(x for x in cat.tracks.values() if x["artist"] - 1000 not in artist_range):
            a = cat.artists[t["artist"]]
            tid = catalog.resolve_track(cur, t["title"], [a["name"]], ids={"deezer": t["dz"]}, isrc=t["isrc"], source="seed")
            catalog.record_measurement(cur, tid, "official", "reccobeats", t["features"], confidence="official")
            catalog.record_measurement(cur, tid, "bass", "librosa", {"low_end_share": rnd.uniform(.5, .9)}, confidence="estimated")
            cands[tid] = {"id": f"dz{t['dz']}", "deezer_id": t["dz"], "title": t["title"], "artist": a["name"], "artists": [a["name"]],
                          "mode": "new", "hop": 1, "isrc": t["isrc"], "measured": True, "track_id": tid,
                          "links": [{"via": "Artist 0", "type": "related", "strength": rnd.random()}],
                          "spotify_url": f"https://open.spotify.com/search/{t['title']}", "spotify_exact": False}
    with db.as_user(uid) as cur:
        pid = userdata.save_profile(cur, uid, profile, None)
        run = userdata.create_run(cur, uid, pid, {})
        userdata.save_results(cur, uid, run, cands, "completed", {})
    return cands
