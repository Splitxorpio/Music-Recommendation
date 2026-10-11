import json

from conftest import make_user
from frequency import catalog, db, legacy_import, userdata


def _cache(tmp_path):
    c = tmp_path / "cache"
    c.mkdir()
    files = {
        "tracks.json": {"sp1": {"name": "One", "artists": ["A"], "sources": ["saved", "top_short_term"], "isrc": "QZ1"},
                        "sp2": {"name": "Two", "artists": ["B", "A"], "sources": ["recent"], "isrc": "QZ2"}},
        "features.json": {"sp1": {"energy": 0.8, "tempo": 120, "isrc": "QZ1"}},
        "estimated_features.json": {"sp2": {"energy": 0.4, "tempo": 90}},
        "deezer_matches.json": {"sp2": {"source": "deezer", "isrc": "QZ2"}},
        "bass.json": {"sp1": {"low_end_share": 0.3}, "sp2": {"low_end_share": 0.5}},
        "artist_mbids.json": {"A": "11111111-1111-1111-1111-111111111111"},
        "artist_genres.json": {"A": {"trap": 3, "rap": 1}, "B": {}},
    }
    for n, v in files.items():
        (c / n).write_text(json.dumps(v))
    model = tmp_path / "calibration.json"
    model.write_text(json.dumps({"created": 1791479574.6, "pairs": 55, "by_source": {"deezer": 40},
                                 "model": {"deezer": {"energy": {"a": 0.1, "b": 0.9}}, "_report": {}}}))
    return c, model


def test_import_is_faithful_flagged_and_idempotent(tmp_path):
    uid = make_user("me@x.com")
    cache, model = _cache(tmp_path)
    s1 = legacy_import.run("me@x.com", cache, model, verbose=False)
    s2 = legacy_import.run("me@x.com", cache, model, verbose=False)
    assert s1["official"] == 1 and s1["estimates"] == 1 and s1["bass"] == 2
    assert s2["official"] == 0 and s2["estimates"] == 0          # second run adds nothing
    with db.system() as cur:
        cur.execute("SELECT count(*) AS n FROM tracks")
        assert cur.fetchone()["n"] == 2
        cur.execute("SELECT status, notes FROM calibration_versions")
        cal = cur.fetchall()
        assert len(cal) == 1 and cal[0]["status"] == "candidate"     # never auto-activated
        t2 = catalog.track_by_identifier(cur, "spotify", "sp2")[0]
        m = catalog.current_measurements(cur, [t2])[t2]
        assert m["clip_estimate"]["confidence"] == "unvalidated" and m["clip_estimate"]["calibrated"] is None
        assert "legacy_import" in m["clip_estimate"]["flags"] and m["clip_estimate"]["clip_source"] == "deezer"
        cur.execute("SELECT a.mbid::text FROM artists a WHERE name = 'A'")
        assert cur.fetchone()["mbid"] == "11111111-1111-1111-1111-111111111111"
    with db.as_user(uid) as cur:
        lib = userdata.library(cur)
        t1 = next(t for t, s in lib.items() if "saved" in s)
        assert lib[t1] == {"saved": 1.0, "top_short_term": 3.0}
