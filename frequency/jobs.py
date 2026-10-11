"""Durable job queue on PostgreSQL.

Lifecycle:  queued ──claim──▶ running ──▶ completed | partially_completed | failed | cancelled
                                 │  ▲
                 wait / error ───┘  └── claim (after run_after)   [status retry_wait]

  * claim() uses FOR UPDATE SKIP LOCKED, so any number of worker processes can pull work safely, and
    picks round-robin across tenants (see tenant_turns) so one big library can't starve a small one.
  * A claim is a lease. The worker renews it (heartbeat); if it dies the lease expires and the next
    claim takes the job over (counted as a failed attempt; after max_attempts the job fails).
  * Every update after claiming is fenced on lease_token: a worker that lost its lease gets LeaseLost
    and its late writes are discarded.
  * enqueue() is idempotent on idempotency_key while a job with that key is live: the same work asked
    for twice (two users needing one track's analysis) is one job.
  * Waiting on other jobs (wait()) doesn't use up attempts.
"""
import json
import socket
import os
import uuid

from . import db
from .db import jsonb

LIVE = ("queued", "running", "retry_wait")
TERMINAL = ("completed", "partially_completed", "failed", "cancelled")
DEFAULT_LEASE_S = 60


class LeaseLost(Exception):
    """This worker no longer owns the job (lease expired and someone else took it, or it was cancelled)."""


def worker_name():
    return f"{socket.gethostname()}:{os.getpid()}"


def enqueue(cur, kind: str, key: str, payload: dict | None = None, *, user_id=None, priority: int = 100,
            max_attempts: int = 5, delay_s: float = 0, depends_for: int | None = None,
            tenant: str | None = None) -> tuple[int, bool]:
    """Create a job unless a live one with the same key exists. Returns (job_id, created).
    depends_for: also record that job `depends_for` waits on this one.
    tenant: fairness key for global work done on a user's behalf (defaults to the job's user, or 'global')."""
    tenant = str(tenant) if tenant else str(user_id) if user_id else "global"
    for _ in range(5):
        cur.execute("""INSERT INTO jobs (kind, user_id, tenant, idempotency_key, payload, priority, max_attempts, run_after)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, now() + %s * interval '1 second')
                       ON CONFLICT (idempotency_key) WHERE status IN ('queued', 'running', 'retry_wait') DO NOTHING
                       RETURNING id""",
                    (kind, str(user_id) if user_id else None, tenant, key, jsonb(payload or {}), priority,
                     max_attempts, delay_s))
        r = cur.fetchone()
        created = r is not None
        if not created:
            cur.execute("SELECT id FROM jobs WHERE idempotency_key = %s AND status IN ('queued', 'running', 'retry_wait')",
                        (key,))
            r = cur.fetchone()
        if r:
            if depends_for:
                cur.execute("INSERT INTO job_dependencies (job_id, depends_on) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (depends_for, r["id"]))
            return r["id"], created
        # the live job finished between our INSERT and SELECT: try again
    raise RuntimeError(f"could not enqueue {key}")


def claim(kinds, lease_s: int = DEFAULT_LEASE_S, worker: str | None = None) -> dict | None:
    """Take the next runnable job of one of `kinds`, or None. Also takes over jobs whose lease expired."""
    token = f"{worker or worker_name()}:{uuid.uuid4().hex[:12]}"
    with db.system() as cur:
        # Jobs whose worker died: count the attempt; give up if that was the last one.
        cur.execute("""UPDATE jobs SET status = CASE WHEN attempts + 1 >= max_attempts THEN 'failed' ELSE 'retry_wait' END,
                              attempts = attempts + 1, lease_token = NULL, updated_at = now(),
                              last_error = 'worker stopped renewing its lease (crashed or was killed)',
                              finished_at = CASE WHEN attempts + 1 >= max_attempts THEN now() END
                       WHERE status = 'running' AND lease_expires_at < now() AND kind = ANY(%s)""", (list(kinds),))
        cur.execute("""SELECT j.id FROM jobs j LEFT JOIN tenant_turns tt ON tt.tenant = j.tenant
                       WHERE j.status IN ('queued', 'retry_wait') AND j.run_after <= now() AND j.kind = ANY(%s)
                         AND NOT j.cancel_requested
                       ORDER BY j.priority, tt.last_claim_at NULLS FIRST, j.id
                       FOR UPDATE OF j SKIP LOCKED LIMIT 1""", (list(kinds),))
        r = cur.fetchone()
        if not r:
            return None
        cur.execute("""UPDATE jobs SET status = 'running', lease_token = %s,
                              lease_expires_at = now() + %s * interval '1 second',
                              started_at = coalesce(started_at, now()), updated_at = now()
                       WHERE id = %s RETURNING *""", (token, lease_s, r["id"]))
        job = cur.fetchone()
        cur.execute("""INSERT INTO tenant_turns (tenant, last_claim_at) VALUES (%s, clock_timestamp())
                       ON CONFLICT (tenant) DO UPDATE SET last_claim_at = clock_timestamp()""", (job["tenant"],))
    return job


def _fenced(cur, job, sql, params=()):
    cur.execute(sql + " WHERE id = %s AND lease_token = %s AND status = 'running' RETURNING id",
                (*params, job["id"], job["lease_token"]))
    if cur.fetchone() is None:
        raise LeaseLost(f"job {job['id']} is no longer ours")


def heartbeat(job, lease_s: int = DEFAULT_LEASE_S, progress: dict | None = None) -> bool:
    """Renew the lease (and optionally store progress). Returns False if cancellation was requested.
    Raises LeaseLost if another worker owns the job now."""
    with db.system() as cur:
        if progress is None:
            _fenced(cur, job, "UPDATE jobs SET lease_expires_at = now() + %s * interval '1 second', updated_at = now()",
                    (lease_s,))
        else:
            _fenced(cur, job, "UPDATE jobs SET lease_expires_at = now() + %s * interval '1 second', progress = %s, "
                              "updated_at = now()", (lease_s, jsonb(progress)))
        cur.execute("SELECT cancel_requested FROM jobs WHERE id = %s", (job["id"],))
        return not cur.fetchone()["cancel_requested"]


def finish(job, status: str = "completed", result: dict | None = None, progress: dict | None = None):
    assert status in ("completed", "partially_completed", "cancelled")
    with db.system() as cur:
        _fenced(cur, job, "UPDATE jobs SET status = %s, result = %s, progress = coalesce(%s, progress), "
                          "lease_token = NULL, finished_at = now(), updated_at = now()",
                (status, jsonb(result or {}), jsonb(progress) if progress is not None else None))


def wait(job, seconds: float, progress: dict | None = None):
    """Give the job back to the queue until `seconds` from now (waiting for other jobs, or for quota).
    Doesn't count as an attempt."""
    with db.system() as cur:
        _fenced(cur, job, "UPDATE jobs SET status = 'retry_wait', run_after = now() + %s * interval '1 second', "
                          "progress = coalesce(%s, progress), lease_token = NULL, updated_at = now()",
                (seconds, jsonb(progress) if progress is not None else None))


def fail(job, error: str, retryable: bool = True, backoff_s: float | None = None):
    """Record a failed attempt: retried with exponential backoff until max_attempts, then failed."""
    with db.system() as cur:
        cur.execute("SELECT attempts, max_attempts FROM jobs WHERE id = %s", (job["id"],))
        r = cur.fetchone()
        last = not retryable or r["attempts"] + 1 >= r["max_attempts"]
        delay = backoff_s if backoff_s is not None else min(3600, 15 * 2 ** r["attempts"])
        _fenced(cur, job, "UPDATE jobs SET status = %s, attempts = attempts + 1, last_error = %s, lease_token = NULL, "
                          "run_after = now() + %s * interval '1 second', finished_at = CASE WHEN %s THEN now() END, "
                          "updated_at = now()",
                ("failed" if last else "retry_wait", error[:2000], delay, last))


def cancel(cur, job_id) -> bool:
    """Cancel a job: queued/waiting jobs stop at once; a running job is told to stop at its next heartbeat."""
    cur.execute("""UPDATE jobs SET status = CASE WHEN status = 'running' THEN status ELSE 'cancelled' END,
                          cancel_requested = true, finished_at = CASE WHEN status = 'running' THEN NULL ELSE now() END,
                          updated_at = now()
                   WHERE id = %s AND status IN ('queued', 'running', 'retry_wait') RETURNING id""", (job_id,))
    return cur.fetchone() is not None


def get(cur, job_id) -> dict | None:
    cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
    return cur.fetchone()


def dependencies(cur, job_id) -> dict[str, int]:
    """Counts of the jobs `job_id` waits on, by status."""
    cur.execute("""SELECT j.status, count(*) AS n FROM job_dependencies d JOIN jobs j ON j.id = d.depends_on
                   WHERE d.job_id = %s GROUP BY j.status""", (job_id,))
    return {r["status"]: r["n"] for r in cur.fetchall()}


def public(job) -> dict:
    """What the API may show about a job (no lease tokens, no internal payload)."""
    return {"id": job["id"], "kind": job["kind"], "status": job["status"], "progress": job["progress"],
            "result": job["result"], "error": job["last_error"] if job["status"] in ("failed", "retry_wait") else None,
            "attempts": job["attempts"], "created_at": job["created_at"], "started_at": job["started_at"],
            "finished_at": job["finished_at"], "cancel_requested": job["cancel_requested"]}
