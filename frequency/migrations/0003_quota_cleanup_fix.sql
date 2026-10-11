-- Fix: quota_acquire() cleaned up old windows relative to the window it was booking into, so booking a few
-- windows ahead deleted the still-open current window's count and let later callers over-book it
-- (caught by test_window_bucket_never_overbooks_across_processes: 14 sends granted in a 3-unit window).
-- Clean up relative to the current window instead.
CREATE OR REPLACE FUNCTION quota_acquire(p_bucket text, p_cost double precision, p_max_wait double precision)
RETURNS double precision LANGUAGE plpgsql AS $$
DECLARE
    b quota_buckets%ROWTYPE;
    now_s double precision := epoch_now();
    t double precision;
    k bigint;
    k_now bigint;
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
    k_now := floor((now_s - b.phase_s) / b.window_s);
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
                DELETE FROM quota_window_usage WHERE bucket = p_bucket AND window_index < k_now - 1;
                RETURN t;
            END IF;
        END IF;
        k := k + 1;
    END LOOP;
END $$;
