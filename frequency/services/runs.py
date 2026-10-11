"""Starting and inspecting a user's analysis run (job kind 'user_pipeline'). One live run per user."""
from .. import db, jobs

RUN_PRIORITY = 40


def start(user_id, source: str | None, payload: dict | None = None) -> tuple[int, bool]:
    """Queue a run. Returns (job_id, created); created=False means a run was already in progress."""
    with db.as_user(user_id) as cur:
        return jobs.enqueue(cur, "user_pipeline", f"pipeline:{user_id}", {"source": source, **(payload or {})},
                            user_id=user_id, priority=RUN_PRIORITY, max_attempts=4)


def latest(user_id) -> dict | None:
    with db.as_user(user_id) as cur:
        cur.execute("SELECT * FROM jobs WHERE kind = 'user_pipeline' ORDER BY created_at DESC LIMIT 1")
        return cur.fetchone()


def cancel(user_id, job_id) -> bool:
    with db.as_user(user_id) as cur:
        if not jobs.get(cur, job_id):
            return False
        return jobs.cancel(cur, job_id)
