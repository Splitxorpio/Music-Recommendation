-- Shared pacing for the listening sources, and who contributed each calibration pair.

INSERT INTO quota_buckets (bucket, model, interval_s) VALUES
    ('spotify', 'interval', 0.25),      -- app-wide (all users): conservative; Spotify publishes no number
    ('lastfm', 'interval', 0.25)        -- Last.fm asks for "reasonable" use; ~4/s across the whole server
ON CONFLICT (bucket) DO NOTHING;

-- A pair that came from a consenting user's library remembers who, so withdrawing consent (or deleting the
-- account) removes it from future calibrations. Operator-library and public-sample pairs have no user.
ALTER TABLE calibration_pairs ADD COLUMN contributed_by uuid REFERENCES users ON DELETE CASCADE;
