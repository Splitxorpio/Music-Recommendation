"""The traffic controller: every outside request goes through here.

Per service: one pooled keep-alive session, a steady rate limit, a cap on requests in flight,
timeouts, retries with exponential backoff + jitter, a short circuit-breaker when a service keeps
failing, and a hard stop (ServicePaused) instead of sleeping through a long "come back later".
Also: pmap() for controlled fan-out, once() to coalesce identical in-flight work, and metrics
(latency percentiles, retries, rate-limit hits, cache hits) written to cache/metrics.json.

Spotify is the exception: it goes through spotify_auth (spotipy), one call at a time.
"""
import json
import random
import threading
import time
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter

from config import CACHE

UA = "PersonalMusicTaste/0.2 (personal listening-analysis project)"

# rate = steady requests/second (None = no limit), concurrency = max in flight.
# Deezer documents ~50 requests / 5 s; MusicBrainz asks for 1/s; iTunes ~20/min; ReccoBeats is
# undocumented, so it starts modest and backs off on 429s. CDNs serve files, not API quota.
SERVICES = {
    "deezer":      {"rate": 9.0,  "concurrency": 8, "timeout": 20},
    "deezer_cdn":  {"rate": None, "concurrency": 8, "timeout": 60},
    "itunes":      {"rate": 0.33, "concurrency": 1, "timeout": 20},
    "itunes_cdn":  {"rate": None, "concurrency": 6, "timeout": 60},
    "reccobeats":  {"rate": 4.0,  "concurrency": 4, "timeout": 60},     # batch lookups
    # Clip analysis: ReccoBeats caps it server-side at ~0.55/s (measured: 0.55-0.56/s whether we send
    # 2 or 3 at once). Pacing to that cap gives the same throughput without a wall of 429s.
    "reccobeats_upload": {"rate": 0.55, "concurrency": 2, "timeout": 180},
    "musicbrainz": {"rate": 0.9,  "concurrency": 1, "timeout": 30},
    "images":      {"rate": None, "concurrency": 8, "timeout": 20},
}
LONG_PAUSE = 120          # a Retry-After longer than this stops the caller instead of sleeping
BREAKER_FAILS = 8         # consecutive failures before a service is given a breather
BREAKER_COOLDOWN = 20


class ServicePaused(Exception):
    def __init__(self, service, seconds):
        self.service, self.seconds = service, seconds
        super().__init__(f"{service} asked us to wait {seconds}s")


class _Service:
    def __init__(self, name, rate, concurrency, timeout):
        self.name, self.timeout = name, timeout
        self.interval = 1 / rate if rate else 0.0
        self.slots = threading.BoundedSemaphore(concurrency)
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.fail_streak, self.closed_until = 0, 0.0
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=concurrency + 2, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers["User-Agent"] = UA

    def take_turn(self):
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        if at > now:
            time.sleep(at - now)

    def hold_off(self, seconds):
        with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + seconds)


_services = {k: _Service(k, **v) for k, v in SERVICES.items()}

# ------------------------------------------------------------------ metrics
_mlock = threading.Lock()
_stats = defaultdict(lambda: {"requests": 0, "ok": 0, "errors": 0, "retries": 0, "rate_limited": 0,
                              "breaker_trips": 0, "bytes": 0, "retry_after_max": 0, "status": {}, "latency": []})
_cache = defaultdict(lambda: {"hits": 0, "misses": 0})


def _record(name, seconds, resp, err):
    with _mlock:
        s = _stats[name]
        s["requests"] += 1
        s["latency"].append(seconds)
        code = str(resp.status_code) if resp is not None else type(err).__name__
        s["status"][code] = s["status"].get(code, 0) + 1
        if resp is not None and resp.status_code < 400:
            s["ok"] += 1
            s["bytes"] += len(resp.content or b"")
        else:
            s["errors"] += 1


def _bump(name, field):
    with _mlock:
        _stats[name][field] += 1


def cache_hit(name, hit=True):
    """Callers report their own cache lookups (deezer responses, clips on disk, genres...)."""
    with _mlock:
        _cache[name]["hits" if hit else "misses"] += 1


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))] * 1000)


def metrics():
    with _mlock:
        services = {}
        for k, s in _stats.items():
            services[k] = {**{f: s[f] for f in s if f != "latency"},
                           "p50_ms": _pct(s["latency"], .5), "p95_ms": _pct(s["latency"], .95),
                           "p99_ms": _pct(s["latency"], .99), "busy_s": round(sum(s["latency"]), 1)}
        caches = {k: {**v, "hit_rate": round(v["hits"] / max(1, v["hits"] + v["misses"]), 3)} for k, v in _cache.items()}
    return {"services": services, "caches": caches}


def write_metrics(extra=None):
    out = {**metrics(), **(extra or {})}
    (CACHE / "metrics.json").write_text(json.dumps(out, indent=1))
    return out


# ------------------------------------------------------------------ requests
def _backoff(attempt):
    return min(30.0, 0.5 * 2 ** attempt) * (0.5 + random.random())   # exponential, with jitter


def request(service, method, url, retries=4, **kw):
    """One request through the controller. Returns the Response (including 4xx, for the caller to
    judge). Raises ServicePaused for a long Retry-After, or the last network error after retries."""
    sv = _services[service]
    last_err = None
    for attempt in range(retries + 1):
        wait = sv.closed_until - time.monotonic()
        if wait > 0:
            time.sleep(wait)                          # breaker open: let the service breathe
        sv.take_turn()
        with sv.slots:
            t = time.perf_counter()
            try:
                r, err = sv.session.request(method, url, timeout=sv.timeout, **kw), None
            except requests.RequestException as ex:
                r, err = None, ex
            _record(service, time.perf_counter() - t, r, err)
        if attempt:
            _bump(service, "retries")
        if r is not None and r.status_code == 429:
            _bump(service, "rate_limited")
            after = int(r.headers.get("Retry-After") or 0)
            with _mlock:
                _stats[service]["retry_after_max"] = max(_stats[service]["retry_after_max"], after)
            if after > LONG_PAUSE:
                raise ServicePaused(service, after)
            sv.hold_off(max(after, _backoff(attempt)))
            continue
        if r is not None and r.status_code < 500:
            sv.fail_streak = 0
            return r
        last_err = err or requests.HTTPError(f"{service} returned {r.status_code}")
        sv.fail_streak += 1
        if sv.fail_streak >= BREAKER_FAILS:
            sv.closed_until = time.monotonic() + BREAKER_COOLDOWN
            sv.fail_streak = 0
            _bump(service, "breaker_trips")
        time.sleep(_backoff(attempt))
    raise last_err


def get(service, url, **kw):
    return request(service, "GET", url, **kw)


def post(service, url, **kw):
    return request(service, "POST", url, **kw)


def deezer_json(path, **params):
    """Deezer API call. Deezer signals quota with HTTP 200 + error code 4; 'not found' returns None."""
    for attempt in range(6):
        r = get("deezer", f"https://api.deezer.com/{path}", params=params or None)
        try:
            j = r.json()
        except ValueError:
            return None
        err = j.get("error") if isinstance(j, dict) else None
        if not err:
            return j
        if err.get("code") == 4:                      # quota exceeded: slow the whole service down
            _bump("deezer", "rate_limited")
            _services["deezer"].hold_off(2 + _backoff(attempt))
            continue
        return None
    return None


# ------------------------------------------------------------------ fan-out + coalescing
def pmap(fn, items, workers=16, on_result=None):
    """Run fn over items in a thread pool (services still enforce their own limits).
    Returns {item: result}; an exception is stored as the result rather than raised, so one bad
    item never sinks the batch. on_result(item, result, n_done) runs in the caller's thread."""
    items = list(items)
    out = {}
    if not items:
        return out
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for n, f in enumerate(as_completed(futs), 1):
            it = futs[f]
            try:
                res = f.result()
            except Exception as e:                    # noqa: BLE001 - surfaced to the caller as data
                res = e
            out[it] = res
            if on_result:
                on_result(it, res, n)
    return out


_inflight, _ilock = {}, threading.Lock()


def once(key, fn):
    """If identical work is already running, wait for it and share the result instead of repeating it."""
    with _ilock:
        fut = _inflight.get(key)
        owner = fut is None
        if owner:
            fut = _inflight[key] = Future()
    if not owner:
        return fut.result()
    try:
        res = fn()
        fut.set_result(res)
        return res
    except Exception as ex:
        fut.set_exception(ex)
        raise
    finally:
        with _ilock:
            _inflight.pop(key, None)
