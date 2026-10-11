"""End to end, multi-user, multi-process: real worker processes run real user pipelines against the fake
providers (tests/fakes/providers.py). Proves shared work is done once, quotas hold across processes, users
stay isolated, and a killed worker's runs complete without repeated analyses."""
import os
import signal
import subprocess
import sys
import time
from collections import Counter

import pytest

from conftest import ROOT, TEST_URL, make_user
from fakes import providers
from frequency import catalog, db, jobs, userdata
from frequency.services import library, runs

TIMEOUT_S = 240


@pytest.fixture
def fake(tmp_path):
    srv, st = providers.start(capacity=5.0, window=2.0, phase=0.0)
    with db.system() as cur:                         # the test bucket mirrors the fake's limit
        cur.execute("""UPDATE quota_buckets SET capacity = 5, window_s = 2, phase_s = 0, after_reset_s = 0.1,
                       end_guard_s = 0.3 WHERE bucket = 'reccobeats'""")
        cur.execute("UPDATE quota_buckets SET interval_s = 0.01 WHERE bucket IN ('deezer', 'musicbrainz', 'itunes')")
    yield st
    srv.shutdown()


def start_worker(st, tmp_path, measure_threads=3, slots=2):
    env = {**os.environ, "DATABASE_URL": TEST_URL, "FREQUENCY_TEST_PROVIDER_URL": st.base,
           "FREQUENCY_TEST_BLOCK_NETWORK": "1", "FREQUENCY_N_INIT": "10", "FREQUENCY_CLIP_STORE": str(tmp_path / "clips"),
           "FREQUENCY_PIPELINE_LEASE_S": "6", "FREQUENCY_MEASURE_LEASE_S": "6", "FREQUENCY_HEARTBEAT_S": "1"}
    log = open(tmp_path / f"worker-{time.time_ns()}.log", "wb")
    return subprocess.Popen([sys.executable, "-m", "frequency.worker", "--measure-threads", str(measure_threads),
                             "--pipeline-slots", str(slots)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


def wait_for(job_ids, timeout=TIMEOUT_S, until=None):
    end = time.time() + timeout
    while time.time() < end:
        with db.system() as cur:
            cur.execute("SELECT id, status, last_error FROM jobs WHERE id = ANY(%s)", (list(job_ids),))
            rows = cur.fetchall()
        if until and until():
            return rows
        if all(r["status"] in jobs.TERMINAL for r in rows):
            return rows
        time.sleep(0.5)
    raise AssertionError(f"jobs not finished in time: {rows}")


def start_run(email, artist_range):
    uid = make_user(email)
    path = library.save_upload(uid, "Streaming_History_Audio_2026.json", providers.history_upload(artist_range))
    job_id, created = runs.start(uid, "history_upload", {"upload_path": path})
    assert created
    return uid, job_id


def stop(*procs):
    for p in procs:
        p.send_signal(signal.SIGTERM)
    for p in procs:
        try:
            p.wait(60)
        except subprocess.TimeoutExpired:
            p.kill()


def dump_logs(tmp_path):
    for f in sorted(tmp_path.glob("worker-*.log")):
        print(f.name, f.read_text()[-4000:])
    for f in sorted((ROOT / "data" / "work").glob("job-*/child.log")):
        print(f, f.read_text()[-4000:])


def test_two_users_two_workers_share_work_and_respect_quota(fake, tmp_path):
    a, ja = start_run("a@x.com", range(0, 10))
    b, jb = start_run("b@x.com", range(5, 15))        # artists 5-9 overlap: their songs are needed by both
    w1, w2 = start_worker(fake, tmp_path), start_worker(fake, tmp_path)
    try:
        rows = wait_for([ja, jb])
    finally:
        stop(w1, w2)
    if any(r["status"] != "completed" for r in rows):
        dump_logs(tmp_path)
    assert {r["status"] for r in rows} == {"completed"}, rows

    assert fake.rejections["analysis"] == 0 and fake.rejections["lookup"] == 0, fake.rejections
    assert fake.analyses and max(fake.analyses.values()) == 1, "no clip analysed twice"
    with db.system() as cur:
        cur.execute("SELECT track_id, count(*) AS n FROM track_measurements WHERE kind = 'clip_estimate' GROUP BY track_id HAVING count(*) > 1")
        assert cur.fetchall() == []

    for uid, other in ((a, b), (b, a)):
        with db.as_user(uid) as cur:
            prof = userdata.current_profile(cur)
            run = userdata.latest_run(cur)
            assert prof and run and run["status"] == "completed"
            assert len(userdata.run_results(cur, run["id"])) > 0
            cur.execute("SELECT DISTINCT user_id FROM recommendation_results")
            assert [r["user_id"] for r in cur.fetchall()] == [uid]
    with db.as_user(a) as cur:
        lib_a = set(userdata.library(cur))
    with db.as_user(b) as cur:
        lib_b = set(userdata.library(cur))
    assert lib_a & lib_b and lib_a != lib_b
    # Songs both users play that have no official numbers: analysed once, and that one result serves both.
    with db.system() as cur:
        m = catalog.current_measurements(cur, lib_a & lib_b)
        dz = {t: ids.get("deezer") for t, ids in catalog.identifiers(cur, lib_a & lib_b).items()}
    shared_estimates = [t for t in lib_a & lib_b if "official" not in m.get(t, {})]
    assert shared_estimates, "the overlap includes songs that needed an analysis"
    for t in shared_estimates:
        assert "clip_estimate" in m[t] and fake.analyses[int(dz[t])] == 1
    assert not list((ROOT / "data" / "uploads" / str(a)).glob("*.enc")), "uploads deleted after import"
    assert not [f for f in (tmp_path / "clips").rglob("*") if f.is_file()], "measured clips deleted"


def test_killed_worker_is_taken_over_without_repeating_analyses(fake, tmp_path):
    a, ja = start_run("a@x.com", range(0, 12))
    w1 = start_worker(fake, tmp_path, measure_threads=2, slots=1)

    def some_measured():
        with db.system() as cur:
            cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'measure_track' AND status = 'completed'")
            return cur.fetchone()["n"] >= 5
    wait_for([ja], timeout=120, until=some_measured)
    w1.send_signal(signal.SIGKILL)                   # no goodbye: leases just stop being renewed
    w1.wait()
    with db.system() as cur:
        assert jobs.get(cur, ja)["status"] not in jobs.TERMINAL, "the run was still in progress when the worker died"
    before = Counter(fake.analyses)
    w2 = start_worker(fake, tmp_path, measure_threads=2, slots=1)
    try:
        rows = wait_for([ja])
    finally:
        stop(w2)
    if rows[0]["status"] != "completed":
        dump_logs(tmp_path)
    assert rows[0]["status"] == "completed", rows
    repeated = sum(n - 1 for n in fake.analyses.values() if n > 1)
    print(f"analyses: {sum(fake.analyses.values())} for {len(fake.analyses)} clips, repeated after the kill: {repeated}")
    assert repeated <= 2, f"at most the {2} analyses in flight at the kill may repeat; got {repeated}"
    assert fake.rejections["analysis"] == 0
