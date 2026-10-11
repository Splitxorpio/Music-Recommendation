"""Versioned calibration: held-out validation, minimum evidence, no extrapolation, lifecycle and rollback, consent."""
import random

import pytest

from conftest import make_user
from frequency import calibration, catalog, db, userdata


def synthetic_pairs(n, seed=3, src="deezer", start=1):
    """Clips read acousticness 0.12 too high and loudness 1 dB too low (like the real bias); valence is noise."""
    rnd, out = random.Random(seed), []
    with db.system() as cur:
        for i in range(n):
            t = catalog.resolve_track(cur, f"Cal {start + i}", ["X"], ids={"spotify": f"cal{start + i}"}, source="t")
            off = {"acousticness": rnd.uniform(.05, .6), "loudness": rnd.uniform(-12, -4), "valence": rnd.uniform(.1, .9),
                   "energy": rnd.uniform(.3, .9), "danceability": .5, "instrumentalness": 0.01, "liveness": .1, "speechiness": .05}
            clip = {**off, "acousticness": off["acousticness"] + .12 + rnd.gauss(0, .02),
                    "loudness": off["loudness"] - 1.0 + rnd.gauss(0, .2), "valence": rnd.uniform(.1, .9),
                    "energy": off["energy"] + rnd.gauss(0, .01)}
            cur.execute("INSERT INTO calibration_pairs (track_id, clip_source, official, clip, origin) VALUES (%s, %s, %s, %s, 'public_sample')",
                        (t, src, db.jsonb(off), db.jsonb(clip)))
            out.append({"track_id": t, "clip_source": src, "official": off, "clip": clip})
    return out


def test_split_is_deterministic_and_about_a_quarter():
    s = [calibration.split_of(i) for i in range(4000)]
    assert s == [calibration.split_of(i) for i in range(4000)]
    assert 0.2 < s.count("holdout") / len(s) < 0.3


def test_fit_corrects_real_bias_rejects_noise_and_records_ranges():
    model, ranges, rep = calibration.fit(synthetic_pairs(160))
    assert set(model["deezer"]) >= {"acousticness", "loudness"}
    assert "valence" not in model["deezer"] and rep["deezer"]["valence"]["rough"]
    assert rep["deezer"]["acousticness"]["holdout_error_corrected"] < rep["deezer"]["acousticness"]["holdout_error_raw"]
    lo, hi = ranges["deezer"]["acousticness"]
    assert 0.1 < lo < hi < 0.8
    assert "energy" not in model["deezer"], "unbiased trait left alone"


def test_too_few_pairs_learns_nothing():
    model, _, rep = calibration.fit(synthetic_pairs(20))
    assert model == {} and "training pairs" in rep["deezer"]["acousticness"]["why"]


def test_lifecycle_validate_activate_retire_rollback():
    synthetic_pairs(160)
    with db.system() as cur:
        v1 = calibration.create_candidate(cur, "v1")
        with pytest.raises(ValueError):
            calibration.activate(cur, v1)                     # not validated yet
        assert calibration.validate(cur, v1) == "validated"
        calibration.activate(cur, v1)
        v2 = calibration.create_candidate(cur, "v2")
        calibration.validate(cur, v2)
        calibration.activate(cur, v2)
        cur.execute("SELECT version, status FROM calibration_versions ORDER BY id")
        assert [tuple(r.values()) for r in cur.fetchall()] == [("v1", "retired"), ("v2", "active")]
        assert calibration.rollback(cur) == v1
        assert calibration.active(cur)["version"] == "v1"


def test_candidate_without_holdout_evidence_is_rejected():
    synthetic_pairs(12)
    with db.system() as cur:
        vid = calibration.create_candidate(cur, "thin")
        assert calibration.validate(cur, vid) == "rejected"
        with pytest.raises(ValueError):
            calibration.activate(cur, vid)


def test_apply_never_extrapolates_and_labels_confidence():
    cal = {"id": 1, "model": {"deezer": {"acousticness": {"a": -0.1, "b": 1.0}}},
           "supported_ranges": {"deezer": {"acousticness": [0.2, 0.7]}},
           "validation": {"deezer": {"valence": {"rough": True}}}}
    v, flags, conf = calibration.apply({"acousticness": 0.5, "valence": .4}, "deezer", cal)
    assert v["acousticness"] == pytest.approx(0.4) and conf == "estimated" and "rough:valence" in flags
    v, flags, conf = calibration.apply({"acousticness": 0.95}, "deezer", cal)
    assert v["acousticness"] == 0.95 and conf == "low_confidence" and "out_of_range:acousticness" in flags
    assert calibration.apply({"acousticness": .5}, "deezer", None) == (None, ["no_active_calibration"], "unvalidated")
    assert calibration.apply({"acousticness": .5}, None, cal)[2] == "unvalidated"


def test_activation_recorrects_stored_estimates_without_provider_calls():
    synthetic_pairs(160)
    with db.system() as cur:
        t = catalog.resolve_track(cur, "Est", ["Y"], ids={"spotify": "est"}, source="t")
        catalog.record_measurement(cur, t, "clip_estimate", "reccobeats_analysis", {"acousticness": 0.4, "loudness": -8},
                                   clip_source="deezer", confidence="unvalidated", flags=["legacy_import"])
        vid = calibration.create_candidate(cur, "v1")
        calibration.validate(cur, vid)
        calibration.activate(cur, vid)
        m = catalog.current_measurements(cur, [t])[t]["clip_estimate"]
    assert m["values"] == {"acousticness": 0.4, "loudness": -8}            # raw kept
    assert m["calibrated"]["acousticness"] < 0.35 and m["calibration_version_id"] == vid
    assert m["confidence"] == "estimated" and "legacy_import" in m["flags"]
    vals, conf = catalog.features_for({"clip_estimate": m})
    assert conf == "estimated" and vals == m["calibrated"]


def test_calibration_sampling_sees_only_consenting_users():
    from frequency.services import calibration_pairs
    yes, no = make_user("yes@x.com", consent=True), make_user("no@x.com", consent=False)
    with db.system() as cur:
        t1 = catalog.resolve_track(cur, "Mine", ["A"], ids={"spotify": "m1"}, source="t")
        t2 = catalog.resolve_track(cur, "Theirs", ["B"], ids={"spotify": "m2"}, source="t")
        for t in (t1, t2):
            catalog.record_measurement(cur, t, "official", "reccobeats", {"energy": .5}, confidence="official")
    for uid, t in ((yes, t1), (no, t2)):
        with db.as_user(uid) as cur:
            userdata.replace_signals(cur, uid, None, {"saved": [{"track_id": t, "weight": 1}]})
    picks = calibration_pairs.eligible(10, origins=("consented_user",))
    assert [(p["track_id"], p["contributed_by"]) for p in picks] == [(t1, str(yes))]
    with db.system() as cur:                                               # no purpose declared: nothing visible
        cur.execute("SELECT count(*) AS n FROM user_library")
        assert cur.fetchone()["n"] == 0
