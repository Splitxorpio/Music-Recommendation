"""Load simulation for the multi-user backend. Runs ONLY against the disposable test database
(DATABASE_URL_TEST) and never contacts a real provider.

    python tools/loadtest.py [--users 50] [--clients 20] [--jobs 3000] [--procs 4]

Measures the parts this machine controls (provider limits are fixed and measured separately, see
docs/CAPACITY.md):
  1. queue      claim+finish throughput of the PostgreSQL job queue with several worker processes
  2. scheduler  shared quota bookings per second under contention (the per-request overhead of coordination)
  3. api        latency of profile + recommendation requests with many signed-in users at once
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from dotenv import dotenv_values  # noqa: E402

TEST_URL = dotenv_values(ROOT / ".env").get("DATABASE_URL_TEST") or os.environ.get("DATABASE_URL_TEST")
if not TEST_URL or "test" not in TEST_URL.rsplit("/", 1)[-1]:
    sys.exit("DATABASE_URL_TEST must point at a database whose name contains 'test'")
os.environ["DATABASE_URL"] = TEST_URL
os.environ.setdefault("FREQUENCY_N_INIT", "20")

from frequency import db, jobs  # noqa: E402

CHILD = """
import os, sys, time, json
sys.path.insert(0, {root!r})
from frequency import db, jobs, quota
db.use(os.environ["DATABASE_URL"])
mode, n = sys.argv[1], int(sys.argv[2])
lat, done = [], 0
if mode == "queue":
    idle = time.time()
    while time.time() - idle < 1.0:
        t = time.perf_counter(); j = jobs.claim(["load"])
        if not j:
            time.sleep(0.01); continue
        jobs.finish(j); lat.append(time.perf_counter() - t); done += 1; idle = time.time()
else:
    for _ in range(n):
        t = time.perf_counter(); quota.acquire("load_iv", 1, 600); lat.append(time.perf_counter() - t); done += 1
print(json.dumps({{"done": done, "lat": lat}}))
"""


def pct(xs, q):
    xs = sorted(xs)
    return round(1000 * xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


def clean():
    with db.system() as cur:
        cur.execute("""SELECT string_agg(format('%I', tablename), ', ') AS t FROM pg_tables WHERE schemaname = 'public'
                       AND tablename NOT IN ('schema_migrations', 'quota_buckets')""")
        cur.execute(f"TRUNCATE {cur.fetchone()['t']} RESTART IDENTITY CASCADE")
        cur.execute("DELETE FROM quota_buckets WHERE bucket LIKE 'load_%'")


def run_children(mode, procs, n):
    code = CHILD.format(root=str(ROOT))
    t0 = time.perf_counter()
    ps = [subprocess.Popen([sys.executable, "-c", code, mode, str(n)], stdout=subprocess.PIPE, env=os.environ) for _ in range(procs)]
    outs = [json.loads(p.communicate()[0]) for p in ps]
    wall = time.perf_counter() - t0
    lat = [x for o in outs for x in o["lat"]]
    return sum(o["done"] for o in outs), wall, lat


def bench_queue(n_jobs, procs):
    clean()
    with db.system() as cur:
        cur.executemany("INSERT INTO jobs (kind, tenant, idempotency_key) VALUES ('load', %s, %s)",
                        [(f"t{i % 50}", f"load:{i}") for i in range(n_jobs)])
    done, wall, lat = run_children("queue", procs, 0)
    wall -= 1.0                                        # each child idles 1 s before deciding the queue is empty
    return {"jobs": done, "processes": procs, "jobs_per_s": round(done / wall), "claim_finish_p50_ms": pct(lat, .5),
            "claim_finish_p95_ms": pct(lat, .95)}


def bench_scheduler(procs, per_proc):
    clean()
    with db.system() as cur:
        cur.execute("INSERT INTO quota_buckets (bucket, model, interval_s) VALUES ('load_iv', 'interval', 0)")
    done, wall, lat = run_children("quota", procs, per_proc)
    return {"bookings": done, "processes": procs, "bookings_per_s": round(done / wall), "p50_ms": pct(lat, .5),
            "p95_ms": pct(lat, .95)}


def bench_api(users, clients, rounds=10):
    import httpx
    import uvicorn

    from frequency import accounts
    from frequency.api.app import create_app
    from seed import seed_analysis
    clean()
    creds = []
    t = time.time()
    for i in range(users):
        u = accounts.create_user(f"load{i}@example.com", "load test password", None)
        seed_analysis(u["id"], range(i % 12, i % 12 + 6), seed=i)
        creds.append(f"load{i}@example.com")
    seed_s = time.time() - t
    server = uvicorn.Server(uvicorn.Config(create_app(serve_frontend=False), host="127.0.0.1", port=8071, log_level="warning",
                                           access_log=False))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.05)
    lat = {"profile_cold": [], "profile_warm": [], "recommendations": []}
    lock = threading.Lock()
    errors = []

    def client(email):
        try:
            with httpx.Client(base_url="http://127.0.0.1:8071", timeout=60) as c:
                assert c.post("/api/v1/auth/login", json={"email": email, "password": "load test password"}).status_code == 200
                for k in range(rounds):
                    t0 = time.perf_counter()
                    r = c.get("/api/v1/profile")
                    key = "profile_cold" if k == 0 else "profile_warm"
                    with lock:
                        lat[key].append(time.perf_counter() - t0)
                    assert r.status_code == 200, r.text
                    t0 = time.perf_counter()
                    r = c.get(f"/api/v1/recommendations?ws={(k % 5) / 4}&wc=0.3&wn=0.3&known={k % 2}&n=25")
                    with lock:
                        lat["recommendations"].append(time.perf_counter() - t0)
                    assert r.status_code == 200
        except Exception as ex:
            errors.append(repr(ex))
    t0 = time.perf_counter()
    ths = [threading.Thread(target=client, args=(creds[i % users],)) for i in range(clients)]
    [x.start() for x in ths]
    [x.join() for x in ths]
    wall = time.perf_counter() - t0
    server.should_exit = True
    n = sum(len(v) for v in lat.values())
    return {"users": users, "concurrent_clients": clients, "requests": n, "requests_per_s": round(n / wall),
            **{f"{k}_p50_ms": pct(v, .5) for k, v in lat.items()}, **{f"{k}_p95_ms": pct(v, .95) for k, v in lat.items()},
            "errors": errors[:3], "seed_s": round(seed_s, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--clients", type=int, default=20)
    ap.add_argument("--jobs", type=int, default=3000)
    ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    db.use(TEST_URL)
    out = {"queue": bench_queue(a.jobs, a.procs), "scheduler": bench_scheduler(a.procs, 500),
           "api": bench_api(a.users, a.clients)}
    clean()
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
