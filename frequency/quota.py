"""Provider quotas shared by every process, coordinated in PostgreSQL (quota_buckets, quota_acquire()).

net.py keeps per-process concurrency, retries and breakers; when a worker installs this scheduler,
net.py asks it *when* each request may start instead of using its process-local clock. So two worker
processes (or ten) together stay inside one provider allowance, rather than each using all of it.

This is coordination of our own traffic within the provider's single published/measured limit. It
never multiplies quota: one allowance per provider, whatever the number of processes.
"""
import time

from . import db

# net.py service name -> (bucket, cost in that bucket's units)
SERVICE_BUCKETS = {
    "deezer": ("deezer", 1.0),
    "musicbrainz": ("musicbrainz", 1.0),
    "itunes": ("itunes", 1.0),
    "reccobeats_upload": ("reccobeats", 1.0),
    "reccobeats": ("reccobeats", 0.125),      # a lookup costs ~1/8 of an analysis (measured Oct 9 2026)
}
MAX_WAIT_S = 120.0


class QuotaWait(Exception):
    """The next slot is further away than the caller is willing to hold a worker for."""
    def __init__(self, bucket, seconds):
        self.bucket, self.seconds = bucket, seconds
        super().__init__(f"{bucket}: next slot in more than {seconds:.0f}s")


def acquire(bucket: str, cost: float = 1.0, max_wait: float = MAX_WAIT_S) -> float:
    """Book `cost` units; returns the unix time the request may be sent at (now or later)."""
    with db.system() as cur:
        cur.execute("SELECT quota_acquire(%s, %s, %s) AS t", (bucket, cost, max_wait))
        t = cur.fetchone()["t"]
    if t is None:
        raise QuotaWait(bucket, max_wait)
    return t


def take_turn(bucket: str, cost: float = 1.0, max_wait: float = MAX_WAIT_S) -> float:
    t = acquire(bucket, cost, max_wait)
    delay = t - time.time()
    if delay > 0:
        time.sleep(delay)
    return time.time()


def learn_reset(bucket: str, sent_at: float, retry_after: float):
    with db.system() as cur:
        cur.execute("SELECT quota_learn_reset(%s, %s, %s)", (bucket, sent_at, retry_after))


def block(bucket: str, seconds: float):
    with db.system() as cur:
        cur.execute("SELECT quota_block(%s, %s)", (bucket, seconds))


def status() -> list[dict]:
    with db.system() as cur:
        cur.execute("""SELECT b.bucket, b.model, b.capacity, b.window_s, b.phase_s, b.rejections,
                              greatest(0, b.blocked_until - epoch_now()) AS blocked_for_s,
                              greatest(0, b.next_free_at - epoch_now()) AS backlog_s,
                              (SELECT coalesce(sum(used), 0) FROM quota_window_usage u
                               WHERE u.bucket = b.bucket AND b.model = 'aligned_window'
                                 AND u.window_index >= floor((epoch_now() - b.phase_s) / b.window_s)) AS booked_units
                       FROM quota_buckets b ORDER BY b.bucket""")
        return cur.fetchall()


class Scheduler:
    """The object net.install_scheduler() expects."""
    buckets = SERVICE_BUCKETS

    def take_turn(self, service, cost=None):
        bucket, unit = SERVICE_BUCKETS[service]
        return take_turn(bucket, unit if cost is None else cost)

    def learn_reset(self, service, sent_at, retry_after):
        learn_reset(SERVICE_BUCKETS[service][0], sent_at, retry_after)

    def block(self, service, seconds):
        block(SERVICE_BUCKETS[service][0], seconds)
