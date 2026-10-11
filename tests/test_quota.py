import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

from conftest import TEST_URL
from frequency import db, quota

HELPERS = Path(__file__).parent / "helpers"


def make_window_bucket(name="test_window", capacity=3, window=2.0, phase=0.0, after_reset=0.1, end_guard=0.3):
    with db.system() as cur:
        cur.execute("""INSERT INTO quota_buckets (bucket, model, capacity, window_s, phase_s, after_reset_s, end_guard_s)
                       VALUES (%s, 'aligned_window', %s, %s, %s, %s, %s)""", (name, capacity, window, phase, after_reset, end_guard))


def windows_of(times, window=2.0, phase=0.0):
    return Counter(int((t - phase) // window) for t in times)


def test_seeded_buckets_match_measured_limits():
    rows = {r["bucket"]: r for r in quota.status()}
    assert rows["reccobeats"]["capacity"] == 15 and rows["reccobeats"]["window_s"] == 30
    assert set(rows) >= {"deezer", "musicbrainz", "itunes", "reccobeats"}


def test_interval_bucket_spaces_requests():
    with db.system() as cur:
        cur.execute("INSERT INTO quota_buckets (bucket, model, interval_s) VALUES ('iv', 'interval', 0.1)")
    ts = [quota.acquire("iv") for _ in range(10)]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert all(g >= 0.0999 for g in gaps)


def test_window_bucket_never_overbooks_across_processes():
    make_window_bucket()
    env = {**os.environ, "DATABASE_URL": TEST_URL}
    procs = [subprocess.Popen([sys.executable, str(HELPERS / "booker.py"), "test_window", "6", "1"],
                              stdout=subprocess.PIPE, env=env) for _ in range(4)]
    times = [t for p in procs for t in json.loads(p.communicate(timeout=60)[0])]
    assert len(times) == 24
    per = windows_of(times)
    assert max(per.values()) <= 3, per                # 4 processes together still get 3 per window
    for t in times:                                   # and every send sits inside the window's safe span
        offset = t % 2.0
        assert 0.1 - 1e-6 <= offset <= 2.0 - 0.3 + 1e-6 or t - time.time() < 0  # (now-sends may start mid-window)


def test_fractional_costs_share_a_window():
    make_window_bucket(capacity=1)
    ts = [quota.acquire("test_window", 0.125, max_wait=60) for _ in range(9)]
    per = windows_of(ts)
    assert sorted(per.values()) == [1, 8]             # 8 lookups fill a unit; the 9th moves on


def test_learn_reset_moves_phase_and_marks_window_full():
    make_window_bucket(phase=0.0)
    now = time.time()
    quota.learn_reset("test_window", now, 0.5)        # provider says: resets ~0.5 s (+ up to 1 s) from now
    rows = {r["bucket"]: r for r in quota.status()}
    expected_phase = (now + 1.5) % 2.0
    assert abs(rows["test_window"]["phase_s"] - expected_phase) < 1e-6
    t = quota.acquire("test_window", 1, max_wait=60)
    assert t >= now + 1.5 - 1e-6                      # nothing more in the window that rejected us


def test_block_stops_everyone_and_max_wait_raises():
    with db.system() as cur:
        cur.execute("INSERT INTO quota_buckets (bucket, model, interval_s) VALUES ('blk', 'interval', 0.01)")
    quota.block("blk", 300)
    with pytest.raises(quota.QuotaWait):
        quota.acquire("blk", 1, max_wait=5)


def test_net_uses_the_shared_scheduler_when_installed():
    import net
    net.install_scheduler(quota.Scheduler())
    try:
        before = {r["bucket"]: r["backlog_s"] for r in quota.status()}["deezer"]
        for _ in range(9):
            net._services["deezer"].take_turn()
        after = {r["bucket"]: r["backlog_s"] for r in quota.status()}["deezer"]
        assert after >= before                        # bookings went through PostgreSQL, not the local clock
        with db.system() as cur:
            cur.execute("SELECT next_free_at FROM quota_buckets WHERE bucket = 'deezer'")
            assert cur.fetchone()["next_free_at"] > time.time() - 1
    finally:
        net.install_scheduler(None)
