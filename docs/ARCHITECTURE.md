# Frequency — multi-user architecture

Frequency learns a listener's taste in detail (tempo, energy, mood, bass, genre scene, artist connections) and
recommends music by scoring candidates against that person's own weighted profile. This document describes the
multi-user backend in `frequency/`. The original single-user pipeline (`pipeline.py`, `dashboard.py` and the
modules beside them) still works unchanged and shares its algorithms with the backend.

## Components

```
 browser ──HTTPS──▶ Caddy (LAN only) ──▶ API  (python -m frequency.api, FastAPI)
                                          │  accounts, sessions, CSRF, connections, uploads, runs, results
                                          ▼
                                   PostgreSQL 17  ◀──────────────┐
                                          ▲                       │
                                          │ claims jobs (SKIP LOCKED, leases)
                                   worker(s) (python -m frequency.worker)
                                   ├─ measure threads: shared measure_track / calibration_pair jobs
                                   └─ pipeline slots: one child process per user run (user_pipeline)
                                          │
                     provider pacing booked in PostgreSQL (quota_buckets) before every request
                                          ▼
                 ReccoBeats · Deezer · MusicBrainz · iTunes · Spotify · Last.fm
```

* **API**: a single process, stateless apart from a small per-process cache of rendered dashboards (keyed by
  the user's current profile and run, so it can never serve stale or foreign data). It never calls providers
  except for one validation call when connecting Last.fm and the Spotify OAuth exchange.
* **Workers**: any number of processes. All provider traffic happens here.
* **PostgreSQL**: the only shared state. Jobs, quota, cache, catalog, user data.

## Domain model: global vs user-owned

Global tables hold facts about recordings, which are the same for everyone. User-owned tables hold one person's data.

| Global (shared, no user reference) | User-owned (row-level security on `user_id`) |
|---|---|
| `tracks`, `track_artists`, `track_identifiers`, `identifier_conflicts` | `users`, `sessions` (accessed only via `accounts.py`) |
| `artists`, `artist_identifiers`, `artist_genres`, `artist_links` | `provider_connections` (tokens encrypted) |
| `track_measurements` (history kept, provenance, calibration version) | `library_imports`, `user_library` |
| `preview_assets` (stable reference + stored clip, never the signed URL) | `user_listening_profiles` (versioned, one current) |
| `provider_cache` (all provider answers, TTL per kind) | `recommendation_runs`, `recommendation_results` |
| `calibration_versions`, `calibration_pairs` | `jobs` for a user's run (users see only their own) |
| `quota_buckets`, `quota_window_usage`, `jobs` for shared work | `audit_events` (no FK: outlives deleted users) |

**Identity.**
* A provider id (Spotify, Deezer, ReccoBeats, iTunes, MusicBrainz, Last.fm URL) names exactly one canonical
  track. This is enforced by a unique index.
* An ISRC is strong evidence but not unique. It merges two sightings only when the normalised title and lead
  artist also agree.
* A clash creates a separate track and is recorded in `identifier_conflicts`, never silently merged.
* Resolving a recording takes a transaction-scoped advisory lock, so concurrent workers create one track, not two.

**Measurements.**
* Raw values are always stored.
* Clip estimates also store calibrated values and the calibration version that produced them.
* Confidence is one of `official`, `estimated`, `low_confidence` or `unvalidated`. It travels to the dashboard,
  which labels estimated songs.

## Tenant isolation

* Every user-owned table has `ENABLE` + `FORCE ROW LEVEL SECURITY` with the policy `user_id = app_user()`.
* `db.as_user(uid)` sets `app.user_id` for one transaction. `db.system()` sets it empty, which shows no user rows.
* The database role is neither superuser nor `BYPASSRLS`, which is verified by a test.
* A forgotten `WHERE user_id = …` therefore returns nothing instead of another user's data. A cross-tenant
  insert is rejected by the policy's `WITH CHECK`.
* Calibration sampling can read the libraries of consenting users only, through a separate policy that requires
  `app.purpose = 'calibration'` (see `db.for_calibration()`).
* Deleting a user cascades to all their rows (tested). Shared catalog facts stay, because they belong to no one.

## Jobs

`jobs.py` implements a PostgreSQL queue.

* **Claiming**: `FOR UPDATE SKIP LOCKED`, ordered by priority and then round-robin across tenants
  (`tenant_turns`), so 250 analyses for one user can't starve another user's 10.
* **Leases**:
  * A worker heartbeats while it works.
  * An expired lease is taken over by the next claim and counted as an attempt.
  * After `max_attempts`, the job fails.
* **Fencing**: every update after the claim is conditional on `lease_token`. A worker that lost its lease gets
  `LeaseLost` and its late writes are discarded.
* **Idempotency**: a unique index on `idempotency_key` covers live jobs. Asking for the same work twice returns
  the existing job.
  * A shared analysis is keyed `measure:<track_id>`, so two users needing the same song share one job and one
    ReccoBeats analysis.
  * `job_dependencies` records that both runs wait on it.
* **States**: `queued → running → completed | partially_completed | failed | cancelled`, plus `retry_wait`.
  `retry_wait` covers backoff after a failure and also a run waiting for its shared measurements; waiting doesn't
  use up attempts.

### The user run (`services/pipeline.py`)

```
import ─▶ measure_library ─▶ genres ─▶ profile ─▶ gather ─▶ measure_candidates ─▶ finalize
               ▲ waits on shared measure_track jobs              ▲ waits
```

* Every stage is idempotent, and the stage reached is stored with the job, so a crash resumes there.
* Each step runs in a **child process dedicated to that job**, with its own work folder (`data/work/job-N`). The
  single-user modules keep state in memory (`candidates`, `deezer`, `progress`), and a process per run means
  that state can never mix two users.
* The child dies if its worker dies, so an orphan can't race the worker that takes the job over.
* The parent worker renews the lease, copies the child's progress into the job (shown by the loading screen),
  and applies the outcome the child reports.
* While a run waits on shared measurements it gives its turn back, so no worker idles holding a lease while
  ReccoBeats paces analyses.

## Provider quota: one allowance per provider, shared by every process

`quota_buckets` holds one row per provider allowance. `quota_acquire()` (PL/pgSQL) books under the row lock and
returns the time at which the caller may send.

* **interval** model: a steady pace (Deezer 9/s, MusicBrainz 0.9/s, iTunes 0.33/s, Spotify 4/s, Last.fm 4/s).
* **aligned_window** model: ReccoBeats' measured fixed windows of 15 units per 30 s on the provider's own clock.
  An analysis costs 1 unit and a lookup ⅛.
* A 429 re-learns the window phase from `Retry-After` (`quota_learn_reset`). A long `Retry-After` blocks the
  bucket for everyone (`quota_block`).

`net.py` (the single-user traffic controller) asks the installed scheduler when to send. Per-process concurrency,
retries and breakers stay in `net.py`. More workers never means more requests per second to a provider: this
coordinates our own traffic within each provider's single allowance and never multiplies it. There is no proxy,
account or credential rotation.

Shared answers live in `provider_cache` (`deezer.install_backend`), with the same per-kind TTLs as the
single-user store: track and artist 30 days, related and search 7 days, top and radio 1 day, not-found 1 day.

## Listening sources (`sources/`)

| Source | How | Notes |
|---|---|---|
| Spotify | per-user OAuth, ~20 sequential requests (top ×3, recent, saved) | development mode: max 5 accounts the operator adds; 403 explained to the user |
| Last.fm | public scrobbles by username | needs `LASTFM_API_KEY`; songs matched by name |
| History upload | the user's own Spotify data export (Extended or basic JSON / zip) | no Spotify API; stored encrypted until imported, then deleted |

`resolve.py` turns sightings into canonical tracks while making as few requests as possible:

1. An ISRC carried in the sighting is used directly.
2. A Spotify id the catalog already knows resolves with no request.
3. Other Spotify ids are looked up with ReccoBeats `/v1/track`, 40 per request.
4. Name-only sightings use an exact-match Deezer search. These are recorded as `fuzzy` matches.

## Calibration (`calibration.py`)

* **Lifecycle**: `candidate → validated → active → retired`, or `rejected`. `rollback()` re-activates the
  previous version.
* **Held-out validation**: every track is permanently in train or holdout (by hash). A per-trait correction is
  kept only if it beats the raw value under cross-validation and on the holdout, with at least 25 training pairs.
* **Shape rule**: a correction needs a correlation of at least 0.7 and a slope between 0.5 and 2. A flatter
  line would lower average error by giving every song the average.
* **No extrapolation**: raw values outside the trained range stay raw and are flagged `low_confidence`.
* **Re-correction**: activation re-corrects stored estimates from their raw values, with no provider calls.
* **Training data**:
  * Pairs come from public catalogue samples, or from users who opted in.
  * Withdrawing consent or deleting the account removes that user's pairs.
  * The imported legacy calibration is a candidate only. It was fit on one library and its raw pairs weren't
    kept, so it can't be validated.

## Security

* **Passwords**: scrypt with N=2¹⁵, compared in constant time. Failed sign-ins are rate-limited per email
  across processes.
* **Sessions**: the session cookie is HttpOnly, SameSite=Lax, and Secure behind TLS. Only a hash of the token
  is stored, with sliding expiry. Changing the password signs out everywhere.
* **CSRF**: double-submit token plus an Origin check on every state-changing request. The Spotify OAuth state
  is HMAC-bound to the session.
* **Provider tokens**: Fernet-encrypted at rest, rotatable (`cli rotate-tokens`), and never returned by the API.
  A test verifies none of them appear in any response.
* **Logs**: access logs record paths only, because OAuth codes travel in query strings.
* **Headers**: CSP, nosniff, frame-deny, and `no-store` on the API.
* **Image proxy**: allow-listed hosts over HTTPS only, and only for signed-in users.

## Observability

* Each job event is a JSON log line from the worker: kind, outcome, timing, pauses.
* `python -m frequency.cli stats` shows throughput, failures, run times, provider pauses and coverage.
  `cli queue` and `cli quota` show the queue and allowances.
* `GET /api/v1/health` reports database and schema status and queue depth.
* A run's progress, including the stage it reached, lives on its job row.
