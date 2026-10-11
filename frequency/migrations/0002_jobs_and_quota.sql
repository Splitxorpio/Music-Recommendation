-- Durable jobs and the cross-process provider quota scheduler.

-- ───────────────────────────── JOBS ─────────────────────────────
-- A job is claimed with a lease (FOR UPDATE SKIP LOCKED). A worker that dies simply stops renewing it;
-- once the lease expires another worker takes the job over. Every state change after claiming is fenced
-- by lease_token, so a worker that lost its lease can't overwrite the new owner's progress.

CREATE TABLE jobs (
    id               bigserial PRIMARY KEY,
    kind             text NOT NULL,
    user_id          uuid REFERENCES users ON DELETE CASCADE,   -- NULL = global work (e.g. analyse a track)
    tenant           text NOT NULL,            -- fairness key: the user it serves, or 'global'
    idempotency_key  text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}',
    status           text NOT NULL DEFAULT 'queued' CHECK (status IN
                       ('queued', 'running', 'retry_wait', 'completed', 'partially_completed', 'failed', 'cancelled')),
    priority         smallint NOT NULL DEFAULT 100,           -- lower runs first
    attempts         integer NOT NULL DEFAULT 0,              -- failed/expired attempts (waiting doesn't count)
    max_attempts     integer NOT NULL DEFAULT 5,
    run_after        timestamptz NOT NULL DEFAULT now(),
    lease_token      text,
    lease_expires_at timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    progress         jsonb NOT NULL DEFAULT '{}',
    result           jsonb,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
-- At most one live job per idempotency key: enqueueing the same work twice returns the existing job.
CREATE UNIQUE INDEX jobs_live_key ON jobs (idempotency_key) WHERE status IN ('queued', 'running', 'retry_wait');
CREATE INDEX jobs_claimable ON jobs (kind, priority, run_after) WHERE status IN ('queued', 'retry_wait');
CREATE INDEX jobs_running ON jobs (lease_expires_at) WHERE status = 'running';
CREATE INDEX jobs_user ON jobs (user_id, created_at DESC);

-- Which jobs a job is waiting on (a user's run waits on the shared analyses it needs).
CREATE TABLE job_dependencies (
    job_id      bigint NOT NULL REFERENCES jobs ON DELETE CASCADE,
    depends_on  bigint NOT NULL REFERENCES jobs ON DELETE CASCADE,
    PRIMARY KEY (job_id, depends_on)
);
CREATE INDEX job_dependencies_rev ON job_dependencies (depends_on);

-- Round-robin fairness: the claimer prefers the tenant whose last job started longest ago, so one user's
-- 250 analyses can't starve another user's 10.
CREATE TABLE tenant_turns (
    tenant         text PRIMARY KEY,
    last_claim_at  timestamptz NOT NULL
);

ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
-- Workers (no user context) see every job; a signed-in user sees only their own.
CREATE POLICY tenant ON jobs USING (app_user() IS NULL OR user_id = app_user())
    WITH CHECK (app_user() IS NULL OR user_id = app_user());

-- ───────────────────────────── QUOTA ─────────────────────────────
-- One row per provider allowance, shared by every process. Two models, both measured from the providers:
--   interval        a steady pace (Deezer ~9/s, MusicBrainz 0.9/s, iTunes 0.33/s): next start >= last + interval*cost
--   aligned_window  fixed windows on the provider's clock (ReccoBeats: 15 units per 30 s, reset ~10 s past each
--                   :00/:30): book `cost` units into the earliest window with room, inside its safe span.
-- quota_acquire() books atomically under the row lock and returns the unix time the caller may send at.

CREATE TABLE quota_buckets (
    bucket         text PRIMARY KEY,
    model          text NOT NULL CHECK (model IN ('interval', 'aligned_window')),
    interval_s     double precision,
    capacity       double precision,
    window_s       double precision,
    phase_s        double precision,
    after_reset_s  double precision NOT NULL DEFAULT 0.5,
    end_guard_s    double precision NOT NULL DEFAULT 2.0,
    next_free_at   double precision NOT NULL DEFAULT 0,
    blocked_until  double precision NOT NULL DEFAULT 0,
    rejections     bigint NOT NULL DEFAULT 0,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE quota_window_usage (
    bucket        text NOT NULL REFERENCES quota_buckets ON DELETE CASCADE,
    window_index  bigint NOT NULL,
    used          double precision NOT NULL,
    PRIMARY KEY (bucket, window_index)
);

CREATE FUNCTION epoch_now() RETURNS double precision LANGUAGE sql VOLATILE AS
$$ SELECT extract(epoch FROM clock_timestamp())::double precision $$;

-- Returns the send time (unix seconds), or NULL if the earliest slot is more than p_max_wait away
-- (the caller then gives the work back to the queue instead of holding a worker).
CREATE FUNCTION quota_acquire(p_bucket text, p_cost double precision, p_max_wait double precision)
RETURNS double precision LANGUAGE plpgsql AS $$
DECLARE
    b quota_buckets%ROWTYPE;
    now_s double precision := epoch_now();
    t double precision;
    k bigint;
    start_s double precision;
    used_k double precision;
    guard integer := 0;
BEGIN
    SELECT * INTO b FROM quota_buckets WHERE bucket = p_bucket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown quota bucket %', p_bucket;
    END IF;
    IF b.model = 'interval' THEN
        t := greatest(now_s, b.next_free_at, b.blocked_until);
        IF t - now_s > p_max_wait THEN RETURN NULL; END IF;
        UPDATE quota_buckets SET next_free_at = t + b.interval_s * p_cost, updated_at = now() WHERE bucket = p_bucket;
        RETURN t;
    END IF;
    k := floor((greatest(now_s, b.blocked_until) - b.phase_s) / b.window_s);
    LOOP
        guard := guard + 1;
        IF guard > 10000 THEN RAISE EXCEPTION 'quota % found no window', p_bucket; END IF;
        start_s := b.phase_s + k * b.window_s;
        t := greatest(now_s, start_s + b.after_reset_s, b.blocked_until);
        IF t <= start_s + b.window_s - b.end_guard_s THEN
            SELECT used INTO used_k FROM quota_window_usage WHERE bucket = p_bucket AND window_index = k;
            IF coalesce(used_k, 0) + p_cost <= b.capacity + 1e-9 THEN
                IF t - now_s > p_max_wait THEN RETURN NULL; END IF;
                INSERT INTO quota_window_usage (bucket, window_index, used) VALUES (p_bucket, k, p_cost)
                    ON CONFLICT (bucket, window_index) DO UPDATE SET used = quota_window_usage.used + p_cost;
                DELETE FROM quota_window_usage WHERE bucket = p_bucket AND window_index < k - 2;
                RETURN t;
            END IF;
        END IF;
        k := k + 1;
    END LOOP;
END $$;

-- A 429 from an aligned-window provider: Retry-After (rounded down by the provider) says when its window
-- resets, so re-learn the phase and mark that window as spent. Bookings already handed out stand.
CREATE FUNCTION quota_learn_reset(p_bucket text, p_sent_at double precision, p_retry_after double precision)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    b quota_buckets%ROWTYPE;
    new_phase double precision;
BEGIN
    SELECT * INTO b FROM quota_buckets WHERE bucket = p_bucket FOR UPDATE;
    UPDATE quota_buckets SET rejections = rejections + 1, updated_at = now() WHERE bucket = p_bucket;
    IF b.model <> 'aligned_window' THEN
        UPDATE quota_buckets SET blocked_until = greatest(blocked_until, p_sent_at + p_retry_after) WHERE bucket = p_bucket;
        RETURN;
    END IF;
    new_phase := (p_sent_at + p_retry_after + 1.0) - b.window_s * floor((p_sent_at + p_retry_after + 1.0) / b.window_s);
    DELETE FROM quota_window_usage WHERE bucket = p_bucket;
    INSERT INTO quota_window_usage (bucket, window_index, used)
        VALUES (p_bucket, floor((p_sent_at - new_phase) / b.window_s), b.capacity);
    UPDATE quota_buckets SET phase_s = new_phase WHERE bucket = p_bucket;
END $$;

-- A provider asked everyone to wait (long Retry-After, Deezer quota error): nothing is booked before then.
CREATE FUNCTION quota_block(p_bucket text, p_seconds double precision) RETURNS void LANGUAGE sql AS $$
    UPDATE quota_buckets SET blocked_until = greatest(blocked_until, epoch_now() + p_seconds), rejections = rejections + 1,
                             updated_at = now()
    WHERE bucket = p_bucket
$$;

-- Measured limits (see net.py SERVICES and CLAUDE.md). Units for ReccoBeats: analysis = 1, lookup = 0.125.
INSERT INTO quota_buckets (bucket, model, interval_s) VALUES
    ('deezer', 'interval', 1 / 9.0),
    ('musicbrainz', 'interval', 1 / 0.9),
    ('itunes', 'interval', 1 / 0.33),
    ('spotify_search', 'interval', 2.5);
INSERT INTO quota_buckets (bucket, model, capacity, window_s, phase_s, after_reset_s, end_guard_s) VALUES
    ('reccobeats', 'aligned_window', 15, 30, 10.0, 0.5, 2.0);

-- ───────────────────────────── CATALOG ADDITIONS ─────────────────────────────
ALTER TABLE tracks ADD COLUMN cover_url text;       -- Deezer/Spotify cover (stable CDN link, not signed)
ALTER TABLE tracks ADD COLUMN release_date date;
ALTER TABLE artists ADD COLUMN picture_url text;
