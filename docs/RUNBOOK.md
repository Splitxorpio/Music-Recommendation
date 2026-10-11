# Runbook

Commands run from the project folder with the project's Python: `.venv/bin/python -m frequency.cli …`
(shortened below to `cli …`). Services are launchd jobs (see DEPLOYMENT.md): `com.frequency.api` and
`com.frequency.worker`.

## Daily checks

```
cli stats --hours 24     # throughput, failures, typical run times, provider pauses, coverage
cli queue                # what's waiting / running / failed
cli quota                # provider allowances: rejections and blocks should be ~0
curl -s http://127.0.0.1:8060/api/v1/health
```

Healthy looks like:
* No `failed` user runs.
* `measure_track` outcomes are mostly `measured` or `already_measured`.
* `rejections` stays at 0 or near it for `reccobeats`.
* `blocked_for_s` is 0.

## Start / stop / restart

```
launchctl kickstart -k gui/$(id -u)/com.frequency.api      # restart API
launchctl kickstart -k gui/$(id -u)/com.frequency.worker   # restart worker
launchctl bootout gui/$(id -u)/com.frequency.worker        # stop worker
brew services restart postgresql@17
```

Stopping a worker is always safe:
* SIGTERM finishes the measurement in hand and hands user runs back to the queue at their current stage.
* A hard kill is also safe: leases expire after 60–180 s and another worker takes over.
* At most the analyses that were mid-flight are repeated. A test shows 1.

## Deploying a new version

1. `git pull`, then `.venv/bin/pip install -r requirements.txt`.
2. `cli migrate`. Migrations are transactional and lock out concurrent migrators. An edited, already-applied
   migration is refused: add a new one instead.
3. `cd frontend && npm ci && npm run build`.
4. Restart the API, then the workers. Old workers finish or hand back their jobs, and new ones resume them.
5. `cli doctor`.

## A user says their analysis is stuck

1. `cli queue` and find their `user_pipeline` job. Then, in psql:
   `SELECT status, progress->'pipeline'->>'stage', last_error, run_after FROM jobs WHERE id = N;`
2. Find the status below.

   **`retry_wait` in `measure_library` or `measure_candidates`**
   * This is normal: the run is waiting for shared measurements.
   * Check `cli quota`: a large `backlog_s` for `reccobeats` means the queue is simply long (see CAPACITY.md).

   **`retry_wait` with `last_error` set**
   * It's retrying after a failure, with backoff.

   **`failed`**
   * `last_error` says why. Errors starting with `user:` were shown to the user as-is (e.g. too few songs).
   * Otherwise fix the cause, then `cli jobs retry N`. The run resumes from the stage it reached.

   **`running` but not moving**
   * Is a worker alive? `launchctl list | grep frequency`
   * Check `data/logs/worker.log` and `data/work/job-N/child.log`.

## A provider is refusing requests (429 storm, outage)

* `cli quota` shows `blocked_for_s` and `rejections`.
* The scheduler already blocks the whole bucket for the provider's `Retry-After`. Jobs that hit a long pause go
  to `retry_wait` and resume by themselves.
* **ReccoBeats rejecting with a full window**:
  * The phase is re-learnt automatically.
  * If rejections keep climbing, the provider's limit may have changed. Re-measure it before touching
    `quota_buckets`.
  * Never raise `capacity` above what the provider allows.
* **Deezer quota error (code 4)**: handled as a short block for everyone.
* **Extended outage**: stop the workers; nothing is lost. Start them again later.
* **Never** work around a limit with extra IPs, accounts or keys.

## Spotify development-mode users

* Only Spotify accounts listed in the Spotify developer dashboard (User Management, max 5) can connect. Others
  get a clear message suggesting Last.fm or a history upload.
* **To add someone**: dashboard → app → User Management → add their Spotify email. They can then connect from
  the Sources screen.
* Spotify OAuth needs the redirect URI registered exactly. See DEPLOYMENT.md.

## Accounts and personal data

* **Deletion**: users delete their own account from Sources → Your account (password required). Everything they
  own goes: library, profiles, runs, recommendations, connections, tokens, uploads, sessions, and their
  calibration pairs. An `account_deleted` audit event remains, holding the user id only.
* **Data export**: `GET /api/v1/me/export` while signed in. It returns everything stored about them, except
  tokens.
* **Uploads**: stored encrypted only until imported, then deleted. Orphaned uploads of deleted users are removed
  with the account.
* **Closing sign-ups**: set `FREQUENCY_ALLOW_SIGNUP=0` and restart the API. Create accounts with
  `cli create-user --email …`.

## Secrets

* **Rotating `FREQUENCY_SECRET_KEY`**:
  1. `cli new-secret` gives a new key.
  2. In `.env`, set `FREQUENCY_SECRET_KEY=<new>` and `FREQUENCY_SECRET_KEY_PREVIOUS=<old>`.
  3. Restart the API and workers.
  4. Run `cli rotate-tokens`.
  5. Remove `_PREVIOUS` and restart again.
* **Lost key**: stored provider tokens can't be decrypted. Users must reconnect Spotify (Last.fm stores only a
  username). Nothing else is affected.
* **Database password**: `ALTER ROLE frequency_app PASSWORD '…'`, update `DATABASE_URL` in `.env`, restart.
* `.env` and `data/` must never be committed (both are git-ignored).

## Calibration

Changing the calibration changes everyone's estimated songs, so it's always a deliberate operator step:

```
cli calibration collect --n 80     # queues comparison pairs; costs one ReccoBeats analysis each (~3 min of allowance)
cli calibration fit                # candidate version + per-trait report (train/holdout counts, decisions)
cli calibration validate ID        # validated only with held-out evidence; otherwise rejected
cli calibration activate ID        # stored estimates are re-corrected from their raw values (no provider calls)
cli calibration rollback           # back to the previous version if something looks wrong
cli calibration list
```

* Recalibrate every ~90 days, or after a provider changes its preview encoding.
* Pairs come only from public catalogue samples and consenting users (see ARCHITECTURE.md).

## Backups and restore

* **Back up**, nightly or before upgrades:
  `pg_dump -Fc -d frequency -f ~/Backups/frequency-$(date +%F).dump`
* **Restore**:
  1. Stop the API and workers.
  2. `dropdb frequency && createdb -O frequency_app frequency && pg_restore -d frequency …dump`
  3. Start the services.
* **Also back up** `.env` (the secret key), stored somewhere safe and separate.
* `data/` holds only caches, temporary work and short-lived uploads, so it doesn't need backing up.

## Importing the original single-user data

```
cli create-user --email you@example.com
cli legacy-import --email you@example.com      # reads cache/ and models/calibration.json
```

* Official numbers are imported as-is.
* Clip estimates are imported raw and marked `unvalidated`.
* The old calibration becomes a candidate version that can't be validated (its raw pairs weren't kept).
