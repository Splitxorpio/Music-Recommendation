"""Operator commands.  python -m frequency.cli <command> ...

  migrate                         apply database migrations
  doctor                          check configuration and dependencies
  new-secret                      print a fresh FREQUENCY_SECRET_KEY
  rotate-tokens                   re-encrypt stored provider tokens with the current key (after a key change)
  create-user --email E [--name N]          create an account (password prompted)
  legacy-import --email E [--cache DIR]     import the single-user cache into an account
  queue                           job counts by kind and status, oldest waiting job
  quota                           provider allowances: bookings, blocks, rejections
  stats [--hours 1]               throughput, failures, run times, provider pauses, cache and measurement coverage
  jobs retry ID | jobs cancel ID  put a failed job back in the queue / cancel one
  calibration list                versions and their status
  calibration collect [--n 80]    queue comparison pairs (costs one ReccoBeats analysis each)
  calibration fit [--notes TEXT]  fit a candidate version from the stored pairs
  calibration validate ID         candidate -> validated / rejected (held-out evidence required)
  calibration activate ID         make a validated version active (stored estimates re-corrected)
  calibration rollback            re-activate the previously active version
"""
import argparse
import getpass
import json
import os
import shutil
import sys

from . import db, settings


def _print(rows):
    print(json.dumps(rows, indent=1, default=str))


def cmd_migrate(a):
    applied = db.migrate()
    print("up to date" if not applied else f"applied {len(applied)} migration(s)")


def cmd_doctor(a):
    s = settings.get()
    checks = []

    def check(name, ok, hint=""):
        checks.append(ok)
        print(f"  [{'ok' if ok else '!!'}] {name}" + ("" if ok else f" — {hint}"))
    try:
        pending = db.pending()
        check("database reachable", True)
        check("schema up to date", not pending, f"run: python -m frequency.cli migrate ({', '.join(pending)})")
    except Exception as ex:
        check("database reachable", False, str(ex)[:120])
    try:
        from . import crypto
        crypto.decrypt(crypto.encrypt("x"))
        check("FREQUENCY_SECRET_KEY valid", True)
    except Exception as ex:
        check("FREQUENCY_SECRET_KEY valid", False, str(ex)[:120])
    check("Spotify credentials (optional)", bool(s.spotify_client_id and s.spotify_client_secret), "Spotify connect disabled")
    check("Last.fm API key (optional)", bool(s.lastfm_api_key), "Last.fm connect disabled; get a free key at last.fm/api")
    try:
        s.clip_store.mkdir(parents=True, exist_ok=True)
        (s.clip_store / ".w").write_text("x")
        (s.clip_store / ".w").unlink()
        check(f"clip store writable ({s.clip_store})", True)
    except Exception as ex:
        check("clip store writable", False, str(ex)[:120])
    check("afconvert (iTunes M4A clips)", bool(shutil.which("afconvert")), "macOS only; on Linux iTunes clips can't be measured")
    check("page built (frontend/dist)", (settings.ROOT / "frontend" / "dist" / "index.html").exists(), "cd frontend && npm ci && npm run build")
    check("secure cookies when exposed", s.host in ("127.0.0.1", "localhost") or s.secure_cookies,
          "serving beyond this machine: put HTTPS in front and set FREQUENCY_SECURE_COOKIES=1")
    sys.exit(0 if all(checks[:3]) else 1)


def cmd_new_secret(a):
    from . import crypto
    print(crypto.new_key())


def cmd_rotate_tokens(a):
    from . import crypto
    n = 0
    # provider_connections is row-level secured: walk it one user at a time.
    with db.system() as cur:
        cur.execute("SELECT id FROM users")
        users = [r["id"] for r in cur.fetchall()]
    for uid in users:
        with db.as_user(uid) as cur:
            cur.execute("SELECT id, access_token_enc, refresh_token_enc FROM provider_connections")
            for c in cur.fetchall():
                cur.execute("UPDATE provider_connections SET access_token_enc = %s, refresh_token_enc = %s WHERE id = %s",
                            (crypto.rotate(c["access_token_enc"]), crypto.rotate(c["refresh_token_enc"]), c["id"]))
                n += 1
    print(f"re-encrypted {n} connection(s)")


def cmd_create_user(a):
    from . import accounts
    pw = os.environ.get("FREQUENCY_NEW_USER_PASSWORD") or getpass.getpass("Password (10+ characters): ")
    u = accounts.create_user(a.email, pw, a.name)
    print(f"created {u['email']} ({u['id']})")


def cmd_legacy_import(a):
    from . import legacy_import
    legacy_import.run(a.email, a.cache, a.calibration)


def cmd_queue(a):
    with db.system() as cur:
        cur.execute("SELECT kind, status, count(*) AS n, min(created_at) AS oldest FROM jobs GROUP BY kind, status ORDER BY kind, status")
        _print(cur.fetchall())


def cmd_stats(a):
    """The last hour (or --hours) at a glance: throughput, failures, run times, provider pauses."""
    with db.system() as cur:
        cur.execute("""SELECT kind, status, count(*) AS n,
                              round(avg(extract(epoch FROM finished_at - started_at))::numeric, 1) AS avg_s,
                              round(percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at))::numeric, 1) AS p95_s
                       FROM jobs WHERE finished_at > now() - %s * interval '1 hour' GROUP BY kind, status ORDER BY kind, status""",
                    (a.hours,))
        finished = cur.fetchall()
        cur.execute("SELECT kind, status, count(*) AS n FROM jobs WHERE status IN ('queued', 'running', 'retry_wait') GROUP BY 1, 2")
        live = cur.fetchall()
        cur.execute("SELECT result->>'outcome' AS outcome, count(*) AS n FROM jobs WHERE kind = 'measure_track' "
                    "AND finished_at > now() - %s * interval '1 hour' GROUP BY 1", (a.hours,))
        outcomes = cur.fetchall()
        cur.execute("SELECT provider, count(*) AS entries, count(*) FILTER (WHERE expires_at > now()) AS fresh FROM provider_cache GROUP BY 1")
        cache = cur.fetchall()
        cur.execute("SELECT kind, confidence, count(*) AS n FROM track_measurements WHERE superseded_at IS NULL GROUP BY 1, 2 ORDER BY 1, 2")
        measurements = cur.fetchall()
        cur.execute("SELECT count(*) AS users FROM users")
        users = cur.fetchone()["users"]
    from . import quota
    _print({"window_hours": a.hours, "users": users, "finished_jobs": finished, "live_jobs": live,
            "measure_outcomes": outcomes, "quota": [{k: r[k] for k in ("bucket", "rejections", "blocked_for_s", "backlog_s", "booked_units")}
                                                    for r in quota.status()],
            "provider_cache": cache, "measurements": measurements})


def cmd_quota(a):
    from . import quota
    _print(quota.status())


def cmd_jobs(a):
    with db.system() as cur:
        if a.action == "retry":
            cur.execute("UPDATE jobs SET status = 'queued', attempts = 0, run_after = now(), finished_at = NULL, "
                        "last_error = NULL WHERE id = %s AND status IN ('failed', 'cancelled') RETURNING id", (a.id,))
        else:
            from . import jobs
            print("cancelled" if jobs.cancel(cur, a.id) else "not live")
            return
        print("requeued" if cur.fetchone() else "not failed/cancelled (nothing done)")


def cmd_calibration(a):
    from . import calibration
    from .services import calibration_pairs
    if a.action == "list":
        with db.system() as cur:
            cur.execute("SELECT id, version, status, training_data->'pairs' AS pairs, created_at, validated_at, activated_at, retired_at "
                        "FROM calibration_versions ORDER BY id")
            _print(cur.fetchall())
    elif a.action == "collect":
        _print(calibration_pairs.request(a.n))
        print("Workers will analyse them (one ReccoBeats analysis each); then run: calibration fit")
    elif a.action == "fit":
        with db.system() as cur:
            vid = calibration.create_candidate(cur, notes=a.notes)
            cur.execute("SELECT version, validation FROM calibration_versions WHERE id = %s", (vid,))
            v = cur.fetchone()
        print(f"candidate {v['version']} (id {vid})")
        for src, traits in (v["validation"] or {}).items():
            for t, r in traits.items():
                print(f"  {src:7} {t:17} train {r['n_train']:3} holdout {r['n_holdout']:3}  "
                      f"{'CORRECT' if r['applied'] else 'leave as is'}  {r.get('why', '')}")
    elif a.action == "validate":
        with db.system() as cur:
            print(calibration.validate(cur, a.id))
    elif a.action == "activate":
        with db.system() as cur:
            calibration.activate(cur, a.id)
        print("active; stored clip estimates re-corrected")
    elif a.action == "rollback":
        with db.system() as cur:
            vid = calibration.rollback(cur)
        print(f"re-activated version {vid}" if vid else "no earlier version to go back to")


def main():
    ap = argparse.ArgumentParser(prog="python -m frequency.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("new-secret").set_defaults(fn=cmd_new_secret)
    sub.add_parser("rotate-tokens").set_defaults(fn=cmd_rotate_tokens)
    p = sub.add_parser("create-user")
    p.add_argument("--email", required=True)
    p.add_argument("--name")
    p.set_defaults(fn=cmd_create_user)
    p = sub.add_parser("legacy-import")
    p.add_argument("--email", required=True)
    p.add_argument("--cache", type=lambda x: __import__("pathlib").Path(x), default=settings.ROOT / "cache")
    p.add_argument("--calibration", type=lambda x: __import__("pathlib").Path(x), default=settings.ROOT / "models" / "calibration.json")
    p.set_defaults(fn=cmd_legacy_import)
    sub.add_parser("queue").set_defaults(fn=cmd_queue)
    sub.add_parser("quota").set_defaults(fn=cmd_quota)
    p = sub.add_parser("stats")
    p.add_argument("--hours", type=float, default=1)
    p.set_defaults(fn=cmd_stats)
    p = sub.add_parser("jobs")
    p.add_argument("action", choices=["retry", "cancel"])
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_jobs)
    p = sub.add_parser("calibration")
    p.add_argument("action", choices=["list", "collect", "fit", "validate", "activate", "rollback"])
    p.add_argument("id", type=int, nargs="?")
    p.add_argument("--n", type=int, default=80)
    p.add_argument("--notes")
    p.set_defaults(fn=cmd_calibration)
    a = ap.parse_args()
    sys.path.insert(0, str(settings.ROOT))
    if a.cmd == "calibration" and a.action in ("validate", "activate") and a.id is None:
        ap.error(f"calibration {a.action} needs a version id (see: calibration list)")
    a.fn(a)


if __name__ == "__main__":
    main()
