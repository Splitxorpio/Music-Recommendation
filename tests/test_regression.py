"""The single-user dashboard's output is unchanged by the multi-user refactor.

tests/golden/legacy_cache/ is a synthetic cache (no real listening data); dashboard_expected.json was produced
from it by dashboard.py as it was before the refactor (commit 13b7fb7). The shared presenter must reproduce it."""
import json
from pathlib import Path

import presenter

G = Path(__file__).parent / "golden"


def load(name):
    return json.loads((G / "legacy_cache" / name).read_text())


def test_presenter_reproduces_the_pre_refactor_dashboard_exactly():
    d = presenter.Data(load("profile.json"), load("artwork.json"), load("candidates.json"), load("enriched.json"),
                       load("artist_genres.json"), calibration=load("calibration_model.json"))
    p = d.profile_payload
    p["meta"].pop("date")
    got = json.loads(json.dumps({"profile": p, "recs": [d.recommendations(q) for q in json.loads((G / "queries.json").read_text())]},
                                sort_keys=True))
    assert got == json.loads((G / "dashboard_expected.json").read_text())
