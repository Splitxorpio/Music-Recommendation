-- Domain model: global (shared, provider-derived) data vs user-owned data.
--
-- GLOBAL tables hold facts about recordings that are the same for everyone (identifiers, measurements,
-- clips, provider answers, calibration). They never reference a user.
-- USER tables hold one person's listening, connections, profiles and recommendations. Every one carries
-- user_id and is protected by row-level security (FORCE'd, so the owning role is bound by it too):
-- a query only sees rows whose user_id equals the transaction's `app.user_id` setting.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- ───────────────────────────── GLOBAL ─────────────────────────────

CREATE TABLE artists (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    name_key    text NOT NULL,                 -- lowercase alphanumerics, for matching only
    mbid        uuid UNIQUE,                   -- MusicBrainz id when resolved exactly
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX artists_name_key ON artists (name_key);

CREATE TABLE artist_identifiers (
    provider     text NOT NULL CHECK (provider IN ('spotify', 'deezer', 'musicbrainz', 'reccobeats', 'lastfm')),
    external_id  text NOT NULL,
    artist_id    bigint NOT NULL REFERENCES artists ON DELETE CASCADE,
    PRIMARY KEY (provider, external_id)
);
CREATE INDEX artist_identifiers_artist ON artist_identifiers (artist_id);

CREATE TABLE artist_genres (
    artist_id   bigint NOT NULL REFERENCES artists ON DELETE CASCADE,
    genre       text NOT NULL,
    weight      real NOT NULL CHECK (weight > 0),
    source      text NOT NULL CHECK (source IN ('musicbrainz', 'musicbrainz_tag', 'lastfm', 'deezer', 'legacy_cache')),
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (artist_id, source, genre)
);

-- Relationships between artists (MusicBrainz collaborations, Deezer "related"), used for candidates.
CREATE TABLE artist_links (
    from_artist  bigint NOT NULL REFERENCES artists ON DELETE CASCADE,
    to_artist    bigint NOT NULL REFERENCES artists ON DELETE CASCADE,
    kind         text NOT NULL,                -- e.g. 'deezer_related', 'mb_collaboration'
    strength     real NOT NULL DEFAULT 1,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (from_artist, to_artist, kind)
);

CREATE TABLE tracks (
    id           bigserial PRIMARY KEY,
    title        text NOT NULL,
    title_key    text NOT NULL,
    duration_ms  integer,
    merged_into  bigint REFERENCES tracks,     -- set when two rows turn out to be the same recording
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tracks_title_key ON tracks (title_key);

CREATE TABLE track_artists (
    track_id   bigint NOT NULL REFERENCES tracks ON DELETE CASCADE,
    position   smallint NOT NULL,
    artist_id  bigint NOT NULL REFERENCES artists,
    PRIMARY KEY (track_id, position)
);
CREATE INDEX track_artists_artist ON track_artists (artist_id);

-- External identifiers. Provider ids (Spotify, Deezer, ...) name exactly one track. ISRC does NOT:
-- the same ISRC is legitimately reused across releases and is sometimes mis-assigned by labels, so it
-- may point at several tracks (each with how the link was made) and conflicts are recorded, not hidden.
CREATE TABLE track_identifiers (
    provider     text NOT NULL CHECK (provider IN ('isrc', 'spotify', 'deezer', 'reccobeats', 'itunes', 'musicbrainz', 'lastfm')),
    external_id  text NOT NULL,
    track_id     bigint NOT NULL REFERENCES tracks ON DELETE CASCADE,
    match        text NOT NULL DEFAULT 'exact' CHECK (match IN ('exact', 'fuzzy', 'legacy')),
    source       text NOT NULL,                -- who told us (e.g. 'spotify_pull', 'deezer_isrc', 'itunes_search')
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, external_id, track_id)
);
CREATE UNIQUE INDEX track_identifiers_one_track ON track_identifiers (provider, external_id) WHERE provider <> 'isrc';
CREATE INDEX track_identifiers_track ON track_identifiers (track_id);

CREATE TABLE identifier_conflicts (
    id           bigserial PRIMARY KEY,
    provider     text NOT NULL,
    external_id  text NOT NULL,
    track_ids    bigint[] NOT NULL,
    detail       jsonb NOT NULL DEFAULT '{}',
    detected_at  timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz,
    resolution   text
);
CREATE UNIQUE INDEX identifier_conflicts_open ON identifier_conflicts (provider, external_id) WHERE resolved_at IS NULL;

-- Calibration versions (see Phase 6): a model only corrects estimates once validated and activated.
CREATE TABLE calibration_versions (
    id                serial PRIMARY KEY,
    version           text NOT NULL UNIQUE,
    status            text NOT NULL CHECK (status IN ('candidate', 'validated', 'rejected', 'active', 'retired')),
    model             jsonb NOT NULL,          -- {source: {trait: {a, b}}}
    supported_ranges  jsonb NOT NULL DEFAULT '{}',   -- {source: {trait: [lo, hi]}} of raw clip values seen in training
    validation        jsonb,                   -- held-out results; NULL until validated
    training_data     jsonb NOT NULL,          -- provenance: which tracks, from where, under what consent
    notes             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    validated_at      timestamptz,
    activated_at      timestamptz,
    retired_at        timestamptz
);
CREATE UNIQUE INDEX calibration_one_active ON calibration_versions ((true)) WHERE status = 'active';

-- Raw comparison pairs a calibration is trained/validated on. Global: they describe providers' clips,
-- and only tracks permitted for calibration use are included (see users.calibration_consent).
CREATE TABLE calibration_pairs (
    id          bigserial PRIMARY KEY,
    track_id    bigint NOT NULL REFERENCES tracks ON DELETE CASCADE,
    clip_source text NOT NULL CHECK (clip_source IN ('deezer', 'itunes')),
    official    jsonb NOT NULL,
    clip        jsonb NOT NULL,
    origin      text NOT NULL,                 -- 'operator_library' | 'consented_user' | 'public_sample' | 'legacy'
    measured_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (track_id, clip_source)
);

-- One row per measurement. History is kept: a newer measurement of the same kind supersedes the old one.
CREATE TABLE track_measurements (
    id                     bigserial PRIMARY KEY,
    track_id               bigint NOT NULL REFERENCES tracks ON DELETE CASCADE,
    kind                   text NOT NULL CHECK (kind IN ('official', 'clip_estimate', 'bass')),
    provider               text NOT NULL,      -- 'reccobeats' | 'reccobeats_analysis' | 'librosa'
    clip_source            text CHECK (clip_source IN ('deezer', 'itunes')),
    values                 jsonb NOT NULL,     -- raw values exactly as measured
    calibrated             jsonb,              -- corrected values (clip estimates only)
    calibration_version_id integer REFERENCES calibration_versions,
    confidence             text NOT NULL CHECK (confidence IN ('official', 'estimated', 'low_confidence', 'unvalidated')),
    flags                  text[] NOT NULL DEFAULT '{}',   -- e.g. 'out_of_range:loudness', 'legacy_import'
    provenance             jsonb NOT NULL DEFAULT '{}',    -- analyser version, clip hash, request id, ...
    measured_at            timestamptz NOT NULL DEFAULT now(),
    superseded_at          timestamptz
);
CREATE UNIQUE INDEX track_measurements_current ON track_measurements (track_id, kind) WHERE superseded_at IS NULL;

-- Where a track's preview clip came from. Signed preview URLs expire, so only the stable reference
-- (provider + provider id) and the stored file are kept, never the URL.
CREATE TABLE preview_assets (
    id            bigserial PRIMARY KEY,
    track_id      bigint NOT NULL REFERENCES tracks ON DELETE CASCADE,
    provider      text NOT NULL CHECK (provider IN ('deezer', 'itunes')),
    provider_ref  text NOT NULL,               -- Deezer track id / iTunes trackId
    match         text NOT NULL CHECK (match IN ('isrc', 'fuzzy', 'legacy')),
    format        text NOT NULL CHECK (format IN ('mp3', 'm4a')),
    storage_key   text,                        -- path under the clip store; NULL once evicted
    sha256        text,
    bytes         integer,
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (track_id, provider)
);

-- Cached provider answers, shared by every user and process (replaces cache/deezer_cache.json etc.).
CREATE TABLE provider_cache (
    provider    text NOT NULL,
    cache_key   text NOT NULL,
    status      smallint NOT NULL,             -- HTTP status we got (404s are cached too, briefly)
    payload     jsonb,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    PRIMARY KEY (provider, cache_key)
);
CREATE INDEX provider_cache_expiry ON provider_cache (expires_at);

-- ───────────────────────────── USER-OWNED ─────────────────────────────

CREATE TABLE users (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email                text NOT NULL,
    password_hash        text NOT NULL,
    display_name         text,
    calibration_consent  boolean NOT NULL DEFAULT false,  -- may this user's library seed calibration pairs?
    consent_updated_at   timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    last_login_at        timestamptz
);
CREATE UNIQUE INDEX users_email ON users (lower(email));

CREATE TABLE sessions (
    token_hash  bytea PRIMARY KEY,             -- sha256 of the cookie value; the value itself is never stored
    user_id     uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    csrf_hash   bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    user_agent  text
);
CREATE INDEX sessions_user ON sessions (user_id);

CREATE TABLE provider_connections (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    provider          text NOT NULL CHECK (provider IN ('spotify', 'lastfm', 'history_upload')),
    external_user_id  text,
    display_name      text,
    access_token_enc  bytea,                   -- encrypted with FREQUENCY_SECRET_KEY; never returned by the API
    refresh_token_enc bytea,
    scopes            text[] NOT NULL DEFAULT '{}',
    token_expires_at  timestamptz,
    status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'needs_reauth', 'revoked')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, provider)
);

CREATE TABLE library_imports (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    provider    text NOT NULL,
    status      text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
    stats       jsonb NOT NULL DEFAULT '{}',
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

-- What a user listens to. One row per (track, listening signal); weights are summed across signals.
CREATE TABLE user_library (
    user_id        uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    track_id       bigint NOT NULL REFERENCES tracks,
    signal         text NOT NULL,              -- 'top_short_term', 'saved', 'recent', 'scrobbles', 'history', ...
    weight         real NOT NULL CHECK (weight >= 0),
    play_count     integer,
    last_played_at timestamptz,
    import_id      uuid REFERENCES library_imports ON DELETE SET NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, track_id, signal)
);
CREATE INDEX user_library_track ON user_library (track_id);

CREATE TABLE user_listening_profiles (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    profile                jsonb NOT NULL,     -- same shape as the legacy cache/profile.json
    calibration_version_id integer REFERENCES calibration_versions,
    algorithm_version      text NOT NULL,
    built_at               timestamptz NOT NULL DEFAULT now(),
    is_current             boolean NOT NULL DEFAULT true
);
CREATE UNIQUE INDEX user_profiles_current ON user_listening_profiles (user_id) WHERE is_current;

CREATE TABLE recommendation_runs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    profile_id  uuid REFERENCES user_listening_profiles ON DELETE SET NULL,
    params      jsonb NOT NULL DEFAULT '{}',
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'partially_completed', 'failed')),
    stats       jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX recommendation_runs_user ON recommendation_runs (user_id, created_at DESC);

-- A user's candidate pool for a run (everything measured; ranking with live dials happens on read).
CREATE TABLE recommendation_results (
    run_id     uuid NOT NULL REFERENCES recommendation_runs ON DELETE CASCADE,
    user_id    uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    track_id   bigint NOT NULL REFERENCES tracks,
    candidate  jsonb NOT NULL,                 -- links, genres, mode, pre-scores: the legacy candidates.json entry
    PRIMARY KEY (run_id, track_id)
);
CREATE INDEX recommendation_results_user ON recommendation_results (user_id);

CREATE TABLE audit_events (
    id         bigserial PRIMARY KEY,
    user_id    uuid,                           -- deliberately no FK: deletion events outlive the user
    event      text NOT NULL,
    detail     jsonb NOT NULL DEFAULT '{}',
    at         timestamptz NOT NULL DEFAULT now()
);

-- ───────────────────────────── TENANT ISOLATION ─────────────────────────────

CREATE FUNCTION app_user() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['provider_connections', 'library_imports', 'user_library',
                             'user_listening_profiles', 'recommendation_runs', 'recommendation_results']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY tenant ON %I USING (user_id = app_user()) WITH CHECK (user_id = app_user())', t);
    END LOOP;
END $$;
