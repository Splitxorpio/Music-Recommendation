import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import psycopg
import pytest

from conftest import make_user, TEST_URL
from frequency import db, jobs

HELPERS = Path(__file__).parent / "helpers"


def enq(key, kind="work", **kw):
    with db.system() as cur:
        return jobs.enqueue(cur, kind, key, **kw)


def status(job_id):
    with db.system() as cur:
        return jobs.get(cur, job_id)


def test_enqueue_is_idempotent_while_live_and_reopens_after():
    a, created_a = enq("k")
    b, created_b = enq("k")
    assert a == b and created_a and not created_b
    j = jobs.claim(["work"])
    jobs.finish(j)
    c, created_c = enq("k")
    assert created_c and c != a


def test_each_job_runs_once_across_threads():
    ids = [enq(f"t{i}")[0] for i in range(60)]
    done, lock = [], threading.Lock()

    def worker():
        while (j := jobs.claim(["work"])) is not None:
            jobs.finish(j)
            with lock:
                done.append(j["id"])
    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(done) == sorted(ids)


def test_each_job_runs_once_across_processes():
    ids = [enq(f"p{i}")[0] for i in range(120)]
    env = {**os.environ, "DATABASE_URL": TEST_URL}
    procs = [subprocess.Popen([sys.executable, str(HELPERS / "claimer.py")], stdout=subprocess.PIPE, env=env)
             for _ in range(4)]
    outs = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
    done = [i for o in outs for i in o]
    assert sorted(done) == sorted(ids), "every job exactly once"
    assert sum(1 for o in outs if o) >= 2, "work was actually shared between processes"


def test_expired_lease_is_taken_over_and_old_owner_is_fenced():
    jid, _ = enq("lease", max_attempts=3)
    first = jobs.claim(["work"], lease_s=1)
    time.sleep(1.2)                                   # worker "crashed": no heartbeat
    second = jobs.claim(["work"], lease_s=30)        # taken over straight away
    assert second["id"] == jid and second["lease_token"] != first["lease_token"]
    assert status(jid)["attempts"] == 1
    with pytest.raises(jobs.LeaseLost):
        jobs.finish(first, result={"stale": True})
    with pytest.raises(jobs.LeaseLost):
        jobs.heartbeat(first)
    jobs.finish(second, result={"ok": True})
    assert status(jid)["result"] == {"ok": True}


def test_job_fails_after_max_attempts_of_crashes():
    jid, _ = enq("crashy", max_attempts=2)
    for _ in range(2):
        with db.system() as cur:
            cur.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (jid,))
        assert jobs.claim(["work"], lease_s=1)["id"] == jid
        time.sleep(1.1)
    jobs.claim(["work"])                              # the reaper inside claim() notices the second expiry
    s = status(jid)
    assert s["status"] == "failed" and s["attempts"] == 2 and "lease" in s["last_error"]


def test_wait_does_not_use_attempts_and_fail_backs_off():
    jid, _ = enq("w", max_attempts=2)
    j = jobs.claim(["work"])
    jobs.wait(j, 0, progress={"stage": "waiting"})
    j = jobs.claim(["work"])
    assert j["attempts"] == 0 and j["progress"] == {"stage": "waiting"}
    jobs.fail(j, "boom")
    s = status(jid)
    assert s["status"] == "retry_wait" and s["attempts"] == 1 and s["run_after"] > s["updated_at"]
    with db.system() as cur:
        cur.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (jid,))
    jobs.fail(jobs.claim(["work"]), "boom again")
    assert status(jid)["status"] == "failed"


def test_non_retryable_failure_is_final():
    jid, _ = enq("nr")
    jobs.fail(jobs.claim(["work"]), "bad input", retryable=False)
    assert status(jid)["status"] == "failed"


def test_cancel_queued_and_running():
    q, _ = enq("q")
    r, _ = enq("r", priority=1)
    running = jobs.claim(["work"])
    assert running["id"] == r
    with db.system() as cur:
        assert jobs.cancel(cur, q) and jobs.cancel(cur, r)
    assert status(q)["status"] == "cancelled"
    assert jobs.heartbeat(running) is False           # worker is told to stop
    jobs.finish(running, "cancelled")
    assert status(r)["status"] == "cancelled"
    assert jobs.claim(["work"]) is None


def test_round_robin_between_tenants():
    a, b = make_user("a@x.com"), make_user("b@x.com")
    for i in range(20):
        enq(f"a{i}", user_id=a)
    for i in range(3):
        enq(f"b{i}", user_id=b)
    order = []
    while (j := jobs.claim(["work"])) is not None:
        order.append("b" if j["tenant"] == str(b) else "a")
        jobs.finish(j)
    assert order[:6].count("b") == 3, order          # the small tenant isn't stuck behind 20 jobs


def test_priority_beats_fairness():
    enq("low", priority=200)
    enq("high", priority=10)
    assert jobs.claim(["work"])["idempotency_key"] == "high"


def test_users_see_only_their_jobs_and_cannot_enqueue_global_work():
    a, b = make_user("a@x.com"), make_user("b@x.com")
    ja, _ = enq("ja", user_id=a)
    enq("global")
    with db.as_user(b) as cur:
        assert jobs.get(cur, ja) is None
        cur.execute("SELECT count(*) AS n FROM jobs")
        assert cur.fetchone()["n"] == 0
    with db.as_user(a) as cur:
        assert jobs.get(cur, ja)["id"] == ja
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.as_user(a) as cur:
            jobs.enqueue(cur, "work", "sneaky-global")


def test_dependencies_are_shared_between_waiting_jobs():
    a, b = make_user("a@x.com"), make_user("b@x.com")
    pa, _ = enq("run-a", kind="pipeline", user_id=a)
    pb, _ = enq("run-b", kind="pipeline", user_id=b)
    t1a, c1 = enq("analyse:1", depends_for=pa)
    t1b, c2 = enq("analyse:1", depends_for=pb)        # same track needed by both users
    assert t1a == t1b and c1 and not c2
    with db.system() as cur:
        assert jobs.dependencies(cur, pa) == {"queued": 1} == jobs.dependencies(cur, pb)
    jobs.finish(jobs.claim(["work"]))
    with db.system() as cur:
        assert jobs.dependencies(cur, pa) == {"completed": 1}
