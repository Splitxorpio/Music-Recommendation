"""User-owned data. Every function takes a cursor from db.as_user(uid); row-level security confines it to
that user, and user_id is written explicitly so the policy's WITH CHECK rejects any cross-tenant insert."""
from .db import jsonb

ALGORITHM_VERSION = "taste_profile/2026-10-09"   # bump when taste_profile's maths changes


def start_import(cur, user_id, provider: str) -> str:
    cur.execute("INSERT INTO library_imports (user_id, provider) VALUES (%s, %s) RETURNING id", (str(user_id), provider))
    return cur.fetchone()["id"]


def finish_import(cur, import_id, status: str, stats: dict):
    cur.execute("UPDATE library_imports SET status = %s, stats = %s, finished_at = now() WHERE id = %s",
                (status, jsonb(stats), import_id))


def replace_signals(cur, user_id, import_id, signals: dict[str, list[dict]]):
    """Replace the listed signals wholesale (e.g. a fresh 'top_short_term' list). Signals not mentioned are
    kept, so a list the provider refused this time keeps its previous contents (same as the legacy pull).
    Each entry: {track_id, weight, play_count?, last_played_at?}."""
    uid = str(user_id)
    for signal, entries in signals.items():
        cur.execute("DELETE FROM user_library WHERE user_id = %s AND signal = %s", (uid, signal))
        merged: dict[int, dict] = {}
        for e in entries:                               # one row per track per signal
            m = merged.setdefault(e["track_id"], {"weight": 0.0, "play_count": None, "last_played_at": None})
            m["weight"] = max(m["weight"], float(e.get("weight", 1.0)))
            if e.get("play_count") is not None:
                m["play_count"] = (m["play_count"] or 0) + int(e["play_count"])
            if e.get("last_played_at") and (not m["last_played_at"] or e["last_played_at"] > m["last_played_at"]):
                m["last_played_at"] = e["last_played_at"]
        # (COPY isn't permitted on row-level-secured tables, so batched INSERTs)
        cur.executemany("INSERT INTO user_library (user_id, track_id, signal, weight, play_count, last_played_at, import_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        [(uid, tid, signal, m["weight"], m["play_count"], m["last_played_at"], import_id)
                         for tid, m in merged.items()])


def library(cur) -> dict[int, dict[str, float]]:
    """{track_id: {signal: weight}} for the current user."""
    cur.execute("SELECT track_id, signal, weight FROM user_library")
    out: dict[int, dict] = {}
    for r in cur.fetchall():
        out.setdefault(r["track_id"], {})[r["signal"]] = r["weight"]
    return out


def save_profile(cur, user_id, profile: dict, calibration_version_id: int | None) -> str:
    cur.execute("UPDATE user_listening_profiles SET is_current = false WHERE is_current")
    cur.execute("""INSERT INTO user_listening_profiles (user_id, profile, calibration_version_id, algorithm_version)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (str(user_id), jsonb(profile), calibration_version_id, ALGORITHM_VERSION))
    return cur.fetchone()["id"]


def current_profile(cur) -> dict | None:
    cur.execute("SELECT * FROM user_listening_profiles WHERE is_current")
    return cur.fetchone()


def create_run(cur, user_id, profile_id, params: dict) -> str:
    cur.execute("INSERT INTO recommendation_runs (user_id, profile_id, params, status) VALUES (%s, %s, %s, 'running') "
                "RETURNING id", (str(user_id), profile_id, jsonb(params)))
    return cur.fetchone()["id"]


def save_results(cur, user_id, run_id, candidates: dict[int, dict], status: str, stats: dict):
    uid = str(user_id)
    cur.execute("DELETE FROM recommendation_results WHERE run_id = %s", (run_id,))   # idempotent on retry
    cur.executemany("INSERT INTO recommendation_results (run_id, user_id, track_id, candidate) VALUES (%s, %s, %s, %s)",
                    [(run_id, uid, tid, jsonb(c)) for tid, c in candidates.items()])
    cur.execute("UPDATE recommendation_runs SET status = %s, stats = %s, finished_at = now() WHERE id = %s",
                (status, jsonb(stats), run_id))


def latest_run(cur, completed_only=True) -> dict | None:
    cur.execute("SELECT * FROM recommendation_runs " +
                ("WHERE status IN ('completed', 'partially_completed') " if completed_only else "") +
                "ORDER BY created_at DESC LIMIT 1")
    return cur.fetchone()


def run_results(cur, run_id) -> dict[int, dict]:
    cur.execute("SELECT track_id, candidate FROM recommendation_results WHERE run_id = %s", (run_id,))
    return {r["track_id"]: r["candidate"] for r in cur.fetchall()}


def audit(cur, user_id, event: str, detail: dict | None = None):
    cur.execute("INSERT INTO audit_events (user_id, event, detail) VALUES (%s, %s, %s)",
                (str(user_id) if user_id else None, event, jsonb(detail or {})))
