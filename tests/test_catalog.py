import threading

from frequency import catalog, db


def resolve(**kw):
    with db.system() as cur:
        return catalog.resolve_track(cur, **kw)


def test_same_recording_from_two_providers_is_one_track():
    a = resolve(title="Rich Spirit", artists=["Kendrick Lamar"], ids={"spotify": "sp1"}, isrc="USUM72207341", source="t")
    b = resolve(title="Rich Spirit", artists=["Kendrick Lamar"], ids={"deezer": "dz9"}, isrc="usum72207341", source="t")
    assert a == b
    with db.system() as cur:
        assert catalog.identifiers(cur, [a])[a] == {"spotify": "sp1", "isrc": "USUM72207341", "deezer": "dz9"}


def test_rerelease_with_same_isrc_and_title_merges():
    a = resolve(title="Song", artists=["X"], ids={"spotify": "single"}, isrc="QZ0000000001", source="t")
    b = resolve(title="Song - 2020 Remaster", artists=["X"], ids={"spotify": "album"}, isrc="QZ0000000001", source="t")
    assert a == b


def test_isrc_clash_creates_separate_track_and_records_conflict():
    a = resolve(title="Song A", artists=["X"], ids={"spotify": "s1"}, isrc="QZ0000000002", source="t")
    b = resolve(title="Completely Different", artists=["Y"], ids={"spotify": "s2"}, isrc="QZ0000000002", source="t")
    assert a != b
    with db.system() as cur:
        assert catalog.track_by_identifier(cur, "isrc", "QZ0000000002") == [a, b]
        cur.execute("SELECT provider, external_id, track_ids FROM identifier_conflicts")
        assert cur.fetchall() == [{"provider": "isrc", "external_id": "QZ0000000002", "track_ids": [a, b]}]


def test_provider_id_is_never_moved_between_tracks():
    a = resolve(title="One", artists=["X"], ids={"spotify": "s1"}, source="t")
    b = resolve(title="Two", artists=["X"], ids={"deezer": "d2"}, source="t")
    c = resolve(title="One", artists=["X"], ids={"spotify": "s1", "deezer": "d2"}, source="t")
    assert c == min(a, b)
    with db.system() as cur:
        assert catalog.track_by_identifier(cur, "deezer", "d2") == [b]   # stays where it was
        cur.execute("SELECT count(*) AS n FROM identifier_conflicts")
        assert cur.fetchone()["n"] == 1


def test_name_only_sightings_match_existing_tracks():
    a = resolve(title="Nights", artists=["Frank Ocean"], ids={"spotify": "s"}, isrc="QZ0000000003", source="t")
    b = resolve(title="nights", artists=["frank ocean"], source="lastfm")
    assert a == b


def test_concurrent_resolution_creates_one_track():
    out, errors = [], []

    def go(n):
        try:
            out.append(resolve(title="Race", artists=["Z"], ids={"deezer": f"d{n}"}, isrc="QZ0000000004", source="t"))
        except Exception as ex:                       # pragma: no cover
            errors.append(ex)
    ts = [threading.Thread(target=go, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and len(set(out)) == 1
    with db.system() as cur:
        cur.execute("SELECT count(*) AS n FROM tracks")
        assert cur.fetchone()["n"] == 1


def test_measurements_keep_history():
    t = resolve(title="M", artists=["A"], ids={"spotify": "m"}, source="t")
    with db.system() as cur:
        catalog.record_measurement(cur, t, "clip_estimate", "reccobeats_analysis", {"energy": 0.5}, confidence="unvalidated")
        catalog.record_measurement(cur, t, "official", "reccobeats", {"energy": 0.6}, confidence="official")
        catalog.record_measurement(cur, t, "official", "reccobeats", {"energy": 0.7}, confidence="official")
        m = catalog.current_measurements(cur, [t])[t]
        assert m["official"]["values"] == {"energy": 0.7}
        assert catalog.features_for(m) == ({"energy": 0.7}, "official")
        cur.execute("SELECT count(*) AS n FROM track_measurements WHERE track_id = %s", (t,))
        assert cur.fetchone()["n"] == 3


def test_unvalidated_estimate_is_labelled():
    t = resolve(title="E", artists=["A"], ids={"spotify": "e"}, source="t")
    with db.system() as cur:
        catalog.record_measurement(cur, t, "clip_estimate", "reccobeats_analysis", {"energy": 0.5}, confidence="unvalidated")
        assert catalog.features_for(catalog.current_measurements(cur, [t])[t]) == ({"energy": 0.5}, "unvalidated")


def test_title_key_ignores_feature_credits_and_versions():
    assert catalog.title_key("Money Trees (feat. Jay Rock)") == catalog.title_key("Money Trees")
    assert catalog.title_key("Song - Remastered 2011") == "song"
    assert catalog.key("Beyoncé") == "beyonce"
