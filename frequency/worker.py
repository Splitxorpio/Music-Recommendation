"""The worker: runs queued jobs. Start as many as you like (each claims work safely from PostgreSQL).

    python -m frequency.worker [--measure-threads 3] [--pipeline-slots 2]

  measure_track   shared track measurements (and operator-requested calibration_pair jobs), run on threads in this process. Provider pacing is shared with
                  every other worker through PostgreSQL (frequency.quota), so more workers never means more
                  requests per second to a provider than its one allowance.
  user_pipeline   one user's run, executed step by step in a child process per job (python -m frequency.worker
                  --child JOB_ID) with its own work folder; this process renews the job's lease meanwhile, copies
                  the child's progress into the job, and applies the outcome the child reports.

Stops gracefully on SIGTERM/SIGINT: no new claims; running measurements finish; running pipeline steps are
handed back to the queue (they resume from their last stage).
"""
import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from .settings import ROOT

WORK = ROOT / "data" / "work"
MEASURE_LEASE_S = int(os.environ.get("FREQUENCY_MEASURE_LEASE_S", 180))
PIPELINE_LEASE_S = int(os.environ.get("FREQUENCY_PIPELINE_LEASE_S", 60))
HEARTBEAT_S = float(os.environ.get("FREQUENCY_HEARTBEAT_S", 15))


def event(**kw):
    """One structured log line per job event (JSON on stdout; launchd/systemd keep it)."""
    print(json.dumps({"t": round(time.time(), 3), "worker": os.getpid(), **kw}, default=str), flush=True)


def _setup_process(cache_dir: Path):
    """Point the single-user modules at a private scratch folder, and their pacing/caches at the shared ones."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["FREQUENCY_CACHE"] = str(cache_dir)
    if os.environ.get("FREQUENCY_TEST_BLOCK_NETWORK") == "1":
        _block_outside_network()
    sys.path.insert(0, str(ROOT))
    import deezer
    import net
    from . import provider_cache, quota
    net.install_scheduler(quota.Scheduler())
    deezer.install_backend(provider_cache.DeezerBackend())


def _block_outside_network():
    """Test runs: refuse any connection that isn't to this machine (the fake providers and PostgreSQL)."""
    import socket
    real = socket.socket.connect

    def guarded(self, addr):
        host = addr[0] if isinstance(addr, tuple) else addr
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
            raise RuntimeError(f"test worker tried to reach {host}")
        return real(self, addr)
    socket.socket.connect = guarded


def _exit_with_parent():
    """A pipeline child must not outlive its worker: an orphan would keep working on a job whose lease
    another worker is about to take over."""
    parent = os.getppid()

    def watch():
        while True:
            if os.getppid() != parent:
                os._exit(70)
            time.sleep(1)
    threading.Thread(target=watch, daemon=True).start()


class Heartbeat(threading.Thread):
    """Renews a job's lease until stopped. Sets .cancelled / .lost when that happens."""

    def __init__(self, job, lease_s, progress_fn=None):
        super().__init__(daemon=True)
        self.job, self.lease_s, self.progress_fn = job, lease_s, progress_fn
        self.stop_evt = threading.Event()
        self.cancelled = self.lost = False

    def run(self):
        from . import jobs
        while not self.stop_evt.wait(HEARTBEAT_S):
            try:
                if not jobs.heartbeat(self.job, self.lease_s, self.progress_fn() if self.progress_fn else None):
                    self.cancelled = True
            except jobs.LeaseLost:
                self.lost = True
                return
            except Exception:                                  # database blip: try again next beat
                traceback.print_exc()

    def stop(self):
        self.stop_evt.set()


class Worker:
    def __init__(self, measure_threads, pipeline_slots):
        self.stopping = threading.Event()
        self.n_measure, self.n_pipeline = measure_threads, pipeline_slots
        self.children = {}

    def measure_loop(self):
        import net
        from . import jobs
        from .services import calibration_pairs, measure
        handlers = {"measure_track": measure.run_measure_track,
                    "calibration_pair": calibration_pairs.run_calibration_pair}
        while not self.stopping.is_set():
            job = jobs.claim(list(handlers), lease_s=MEASURE_LEASE_S)
            if not job:
                self.stopping.wait(0.5 + random.random())
                continue
            hb = Heartbeat(job, MEASURE_LEASE_S)
            hb.start()
            t0 = time.time()
            try:
                res = handlers[job["kind"]](job)
                hb.stop()
                jobs.finish(job, result=res)
                event(job=job["id"], kind=job["kind"], outcome=res.get("outcome"), s=round(time.time() - t0, 2))
            except jobs.LeaseLost:
                event(job=job["id"], kind=job["kind"], outcome="lease_lost", s=round(time.time() - t0, 2))
            except net.ServicePaused as ex:
                hb.stop()
                jobs.wait(job, ex.seconds)
                event(job=job["id"], kind=job["kind"], outcome="provider_pause", service=ex.service, wait_s=ex.seconds)
            except Exception as ex:                          # noqa: BLE001 - recorded on the job
                hb.stop()
                event(job=job["id"], kind=job["kind"], outcome="error", error=f"{type(ex).__name__}: {str(ex)[:200]}")
                try:
                    jobs.fail(job, f"{type(ex).__name__}: {ex}")
                except jobs.LeaseLost:
                    pass
            finally:
                hb.stop()

    def pipeline_loop(self):
        from . import jobs
        while not self.stopping.is_set():
            job = jobs.claim(["user_pipeline"], lease_s=PIPELINE_LEASE_S)
            if not job:
                self.stopping.wait(1 + random.random())
                continue
            try:
                self.run_child(job)
            except jobs.LeaseLost:
                pass
            except Exception:
                traceback.print_exc()

    def run_child(self, job):
        from . import jobs
        work = WORK / f"job-{job['id']}"
        work.mkdir(parents=True, exist_ok=True)
        outcome_file = work / "outcome.json"
        outcome_file.unlink(missing_ok=True)
        state = (job["progress"] or {}).get("pipeline")

        def progress():
            try:
                display = json.loads((work / "progress.json").read_text())
            except Exception:
                display = {}
            return {"display": display, "pipeline": state}
        env = {**os.environ, "FREQUENCY_CACHE": str(work)}
        log = open(work / "child.log", "ab")
        child = subprocess.Popen([sys.executable, "-u", "-m", "frequency.worker", "--child", str(job["id"]),
                                  job["lease_token"]], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        self.children[job["id"]] = child
        hb = Heartbeat(job, PIPELINE_LEASE_S, progress)
        hb.start()
        try:
            while child.poll() is None:
                if hb.lost or hb.cancelled or self.stopping.is_set():
                    child.terminate()
                    try:
                        child.wait(20)
                    except subprocess.TimeoutExpired:
                        child.kill()
                    break
                time.sleep(0.5)
        finally:
            hb.stop()
            self.children.pop(job["id"], None)
            log.close()
        if hb.lost:
            return
        if hb.cancelled:
            jobs.finish(job, "cancelled", progress=progress())
            shutil.rmtree(work, ignore_errors=True)
            return
        try:
            out = json.loads(outcome_file.read_text())
        except Exception:
            out = None
        if out is None:
            if self.stopping.is_set():
                jobs.wait(job, 0, progress=progress())     # handed back; resumes from its last stage
            else:
                tail = (work / "child.log").read_bytes()[-1500:].decode(errors="replace")
                jobs.fail(job, f"pipeline step exited with code {child.returncode} without an outcome:\n{tail}")
            return
        state = out.get("state")
        event(job=job["id"], kind="user_pipeline", action=out["action"], stage=(state or {}).get("stage"),
              status=out.get("status"), wait_s=out.get("seconds"))
        if out["action"] == "wait":
            jobs.wait(job, out["seconds"], progress=progress())
        elif out["action"] == "finish":
            jobs.finish(job, out["status"], out.get("result"), progress=progress())
            shutil.rmtree(work, ignore_errors=True)
        else:
            from . import db
            jobs.fail(job, out["error"], retryable=out.get("retryable", True), backoff_s=out.get("backoff_s"))
            with db.system() as cur:
                final = jobs.get(cur, job["id"])["status"] == "failed"
                if not final:                               # keep the stage reached, so the retry resumes there
                    cur.execute("UPDATE jobs SET progress = %s WHERE id = %s", (db.jsonb(progress()), job["id"]))
            if final:
                shutil.rmtree(work, ignore_errors=True)

    def maintenance_loop(self):
        from . import db, provider_cache
        while not self.stopping.wait(3600):
            try:
                provider_cache.evict()
                with db.system() as cur:
                    cur.execute("DELETE FROM sessions WHERE expires_at < now()")
                    cur.execute("SELECT id FROM jobs WHERE kind = 'user_pipeline' AND status IN ('completed', 'partially_completed', 'failed', 'cancelled')"
                                " AND finished_at < now() - interval '1 day'")
                    done = {r["id"] for r in cur.fetchall()}
                    cur.execute("SELECT id FROM jobs WHERE kind = 'user_pipeline'")
                    exists = {r["id"] for r in cur.fetchall()}
                for d in WORK.glob("job-*"):
                    jid = int(d.name[4:]) if d.name[4:].isdigit() else None
                    gone = jid is not None and jid not in exists and time.time() - d.stat().st_mtime > 3600
                    if jid in done or gone:                    # finished a day ago, or the job was deleted with its user
                        shutil.rmtree(d, ignore_errors=True)
            except Exception:
                traceback.print_exc()

    def start(self):
        threads = [threading.Thread(target=self.measure_loop, daemon=True) for _ in range(self.n_measure)]
        threads += [threading.Thread(target=self.pipeline_loop, daemon=True) for _ in range(self.n_pipeline)]
        threads.append(threading.Thread(target=self.maintenance_loop, daemon=True))
        for t in threads:
            t.start()
        return threads

    def shutdown(self, *_):
        if not self.stopping.is_set():
            print("[worker] stopping: finishing current work, handing the rest back", flush=True)
        self.stopping.set()


def child_main(job_id: int, lease_token: str):
    work = Path(os.environ["FREQUENCY_CACHE"])
    _exit_with_parent()
    _setup_process(work)
    from . import db, jobs
    from .services import pipeline
    with db.system() as cur:
        job = jobs.get(cur, job_id)
    if not job or job["lease_token"] != lease_token or job["status"] != "running":
        sys.exit("lease no longer held; not running")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    out = pipeline.run(job, work)
    tmp = work / "outcome.json.tmp"
    tmp.write_text(json.dumps(out, default=str))
    tmp.replace(work / "outcome.json")


def main():
    ap = argparse.ArgumentParser(description="Frequency worker")
    ap.add_argument("--measure-threads", type=int, default=3)
    ap.add_argument("--pipeline-slots", type=int, default=2)
    ap.add_argument("--child", nargs=2, metavar=("JOB_ID", "LEASE_TOKEN"), help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.child:
        return child_main(int(a.child[0]), a.child[1])
    _setup_process(WORK / f"worker-{os.getpid()}")
    from . import db
    pending = db.pending()
    if pending:
        sys.exit(f"Database schema is behind ({', '.join(pending)}). Run: python -m frequency.cli migrate")
    w = Worker(a.measure_threads, a.pipeline_slots)
    signal.signal(signal.SIGTERM, w.shutdown)
    signal.signal(signal.SIGINT, w.shutdown)
    threads = w.start()
    print(f"[worker {os.getpid()}] {a.measure_threads} measure threads, {a.pipeline_slots} pipeline slots", flush=True)
    while not w.stopping.is_set():
        time.sleep(0.5)
    for t in threads:
        t.join(timeout=60)
    shutil.rmtree(WORK / f"worker-{os.getpid()}", ignore_errors=True)


if __name__ == "__main__":
    main()
