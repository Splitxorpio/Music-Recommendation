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
import re
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlparse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter

from config import CACHE

UA = "PersonalMusicTaste/0.2 (personal listening-analysis project)"

# rate = steady requests/second (None = no limit), concurrency = max in flight,
# window = (max requests, seconds): at most that many request starts in any rolling span of that length.
# Deezer documents ~50 requests / 5 s; MusicBrainz asks for 1/s; iTunes ~20/min. CDNs serve files, not API quota.
SERVICES = {
    "deezer":      {"rate": 9.0,  "concurrency": 8, "timeout": 20},
    "deezer_cdn":  {"rate": None, "concurrency": 8, "timeout": 60},
    "itunes":      {"rate": 0.33, "concurrency": 1, "timeout": 20},
    "itunes_cdn":  {"rate": None, "concurrency": 6, "timeout": 60},
    # Lookups share the analysis allowance (measured Oct 9: 14 analyses + 8 lookups fill a window, the 9th lookup
    # and a 15th analysis are refused; 26 lookups alone fit): a lookup costs ~1/8 of an analysis, whatever its size.
    "reccobeats":  {"rate": 4.0,  "concurrency": 4, "timeout": 60, "budget": ("reccobeats_upload", 0.125)},
    # Clip analysis: measured Oct 9 2026 as a fixed window of 15 requests per 30 s (15 succeed back to
    # back, the 16th gets 429 with Retry-After = time to the window's end, rounded down).
    # Measured again Oct 9: the windows are on a fixed clock (reset ~10 s past each 30 s mark of Unix time,
    # stable across bursts and not anchored to our requests), and Retry-After is rounded down. So requests
    # are lined up with that clock: 15 just after each reset, every 30 s exactly - the full permitted rate.
    "reccobeats_upload": {"rate": None, "concurrency": 2, "timeout": 180, "window": (15, 30.0),
                          "aligned": {"phase": 10.0, "after_reset": 0.5, "end_guard": 2.0}},
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
    def __init__(self, name, rate, concurrency, timeout, window=None, aligned=None, budget=None):
        self.name, self.timeout = name, timeout
        self.interval = 1 / rate if rate else 0.0
        self.window = window
        self.base_span = window[1] if window else None
        self.starts = deque()
        self.log = []                                  # wall-clock start of every request (for idle-gap analysis)
        self.aligned = dict(aligned) if aligned else None
        self.budget = budget                           # (service whose windows we book into, cost per request)
        self.win_counts = {}                           # aligned mode: requests started per provider window
        if window:
            self._restore()
        self.slots = threading.BoundedSemaphore(concurrency)
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.fail_streak, self.closed_until = 0, 0.0
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=concurrency + 2, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers["User-Agent"] = UA

    def _aligned_turn(self, cost=1.0):
        """Next start time (wall clock) inside the provider's own fixed windows, booking `cost` units."""
        cap, period = self.window
        a = self.aligned
        now = time.time()
        k = int((now - a["phase"]) // period)
        while True:
            start = a["phase"] + k * period
            t = max(now, start + a["after_reset"])
            if self.win_counts.get(k, 0) + cost <= cap + 1e-9 and t <= start + period - a["end_guard"]:
                self.win_counts[k] = self.win_counts.get(k, 0) + cost
                for old in [w for w in self.win_counts if w < k - 1]:
                    del self.win_counts[old]
                return t
            k += 1

    def learn_reset(self, sent_at, retry_after):
        """A 429 says the window resets in Retry-After seconds (rounded down): re-learn the clock."""
        if not self.aligned:
            return
        with self.lock:
            cap, period = self.window
            reset = sent_at + retry_after + 1.0           # Retry-After is rounded down: reset is within 1 s after
            self.aligned["phase"] = reset % period
            # Everything before that reset is used up. (Clearing the counts here instead caused a rejection
            # storm in the Oct 9 benchmark: 686 of 818 requests rejected.)
            k_full = int((sent_at - self.aligned["phase"]) // period)
            self.win_counts = {k_full: cap}
            self._persist()

    def take_turn(self, cost=1.0):
        if self.budget:                                # book into another service's windows (shared allowance)
            owner, unit = self.budget
            return _services[owner].take_turn(unit)
        if self.aligned:
            with self.lock:
                at = self._aligned_turn(cost)
                self._persist()
            if at > time.time():
                time.sleep(at - time.time())
            if cost >= 1.0:
                self.log.append(time.time())
            return time.time()
        if not self.interval and not self.window:
            return
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            if self.window:
                cap, span = self.window
                while self.starts and self.starts[0] <= at - span:
                    self.starts.popleft()
                if len(self.starts) >= cap:              # wait until the oldest start leaves the window
                    at = max(at, self.starts[-cap] + span)
                self.starts.append(at)
                self._persist()
            self.next_at = at + self.interval
        if at > now:
            time.sleep(at - now)
        if self.window:
            self.log.append(time.time())

    # A provider's window doesn't reset because our process restarted: a resumed or back-to-back run
    # must count the requests the previous run made in the last window. Kept in cache/rate_windows.json.
    def _restore(self):
        try:
            rec = json.loads((CACHE / "rate_windows.json").read_text()).get(self.name, {})
        except Exception:
            rec = {}
        offset = time.monotonic() - time.time()
        for t in rec.get("starts", []):
            if time.time() - t < self.window[1]:
                self.starts.append(t + offset)
        if rec.get("span"):
            self.window = (self.window[0], min(max(rec["span"], self.base_span), self.base_span + 3.0))
        if self.aligned:
            if rec.get("phase") is not None:
                self.aligned["phase"] = rec["phase"]
            self.win_counts = {int(k): float(v) for k, v in rec.get("counts", {}).items()}

    def _persist(self):
        path = CACHE / "rate_windows.json"
        try:
            allrec = json.loads(path.read_text())
        except Exception:
            allrec = {}
        offset = time.time() - time.monotonic()
        allrec[self.name] = {"starts": [t + offset for t in self.starts], "span": self.window[1]}
        if self.aligned:
            k_now = int((time.time() - self.aligned["phase"]) // self.window[1])
            allrec[self.name].update(phase=self.aligned["phase"],
                                     counts={str(k): v for k, v in self.win_counts.items() if k >= k_now})
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(allrec))
        tmp.replace(path)

    def hold_off(self, seconds):
        with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + seconds)


_services = {k: _Service(k, **v) for k, v in SERVICES.items()}

# ------------------------------------------------------------------ metrics
_mlock = threading.Lock()
_stats = defaultdict(lambda: {"requests": 0, "ok": 0, "errors": 0, "retries": 0, "rate_limited": 0,
                              "breaker_trips": 0, "bytes": 0, "retry_after_max": 0, "status": {}, "latency": [],
                              "ops": defaultdict(lambda: {"requests": 0, "status": {}})})


def _op(service, url):
    """A stable operation name for metrics: the URL path with ids/codes collapsed (track/isrc:*, artist/{id}/top)."""
    if service.endswith("_cdn") or service == "images":
        return "download"
    out = []
    for p in urlparse(url).path.split("/"):
        if not p or p in ("ws", "2", "v1"):
            continue
        if ":" in p:
            p = p.split(":")[0] + ":*"
        elif p.isdigit() or re.fullmatch(r"[0-9a-f-]{36}|[A-Za-z0-9]{22}|[A-Z]{2}[A-Z0-9]{3}\d{7}", p):
            p = "{id}"
        out.append(p)
    return "/".join(out[:4]) or "/"


def _category(code):
    if code.isdigit():
        c = int(code)
        return "ok" if c < 400 else "not_found" if c == 404 else "rate_limited" if c == 429 else "client_error" if c < 500 else "server_error"
    return "timeout" if "Timeout" in code else "network"
_cache = defaultdict(lambda: {"hits": 0, "misses": 0})


def _record(name, seconds, resp, err, op="/"):
    with _mlock:
        s = _stats[name]
        s["requests"] += 1
        s["latency"].append(seconds)
        code = str(resp.status_code) if resp is not None else type(err).__name__
        s["status"][code] = s["status"].get(code, 0) + 1
        o = s["ops"][op]
        o["requests"] += 1
        o["status"][code] = o["status"].get(code, 0) + 1
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
            cats = defaultdict(int)
            for code, n in s["status"].items():
                cats[_category(code)] += n
            services[k] = {**{f: s[f] for f in s if f not in ("latency", "ops")},
                           "ops": {o: dict(v) for o, v in s["ops"].items()}, "outcomes": dict(cats),
                           "p50_ms": _pct(s["latency"], .5), "p95_ms": _pct(s["latency"], .95),
                           "p99_ms": _pct(s["latency"], .99), "busy_s": round(sum(s["latency"]), 1)}
        caches = {k: {**v, "hit_rate": round(v["hits"] / max(1, v["hits"] + v["misses"]), 3)} for k, v in _cache.items()}
    return {"services": services, "caches": caches}


def lane_gaps(service, min_gap=6.0):
    """Where a windowed service sat idle longer than its normal pacing allows (seconds, wall clock)."""
    sv = _services.get(service)
    if not sv or not sv.log:
        return {}
    log = sorted(sv.log)
    pace = sv.window[1] / sv.window[0] if sv.window else 0
    gaps = [(round(a - log[0], 1), round(b - a, 1)) for a, b in zip(log, log[1:]) if b - a > max(min_gap, 3 * pace)]
    return {"first_start": log[0], "last_start": log[-1], "span_s": round(log[-1] - log[0], 1),
            "idle_gaps": gaps, "idle_total_s": round(sum(g for _, g in gaps), 1), "starts": len(log)}


def write_metrics(extra=None):
    out = {**metrics(), **(extra or {})}
    (CACHE / "metrics.json").write_text(json.dumps(out, indent=1))
    return out


# ------------------------------------------------------------------ requests
def _backoff(attempt):
    return min(30.0, 0.5 * 2 ** attempt) * (0.5 + random.random())   # exponential, with jitter


SCHEDULE_RETRIES = 12      # windowed services: a 429 is a scheduling signal, retried on its own budget


def request(service, method, url, retries=4, **kw):
    """One request through the controller. Returns the Response (including 4xx, for the caller to
    judge). Raises ServicePaused for a long Retry-After, or the last network error after retries."""
    sv = _services[service]
    op = _op(service, url)
    last_err = None
    attempt, rejected = 0, 0
    while attempt <= retries:
        wait = sv.closed_until - time.monotonic()
        if wait > 0:
            time.sleep(wait)                          # breaker open: let the service breathe
        with sv.slots:                                # connection first, then the time slot: the booked time
            sent_at = sv.take_turn() or time.time()   # must be the real send time, or bursts spill into the next window
            t = time.perf_counter()
            try:
                r, err = sv.session.request(method, url, timeout=sv.timeout, **kw), None
            except requests.RequestException as ex:
                r, err = None, ex
            _record(service, time.perf_counter() - t, r, err, op)
        if attempt or rejected:
            _bump(service, "retries")
        if r is not None and r.status_code == 429:
            _bump(service, "rate_limited")
            after = int(r.headers.get("Retry-After") or 0)
            with _mlock:
                _stats[service]["retry_after_max"] = max(_stats[service]["retry_after_max"], after)
            if after > LONG_PAUSE:
                raise ServicePaused(service, after)
            owner = _services[sv.budget[0]] if sv.budget else sv
            if owner.aligned:                         # their clock moved / window full: re-learn where windows reset
                owner.learn_reset(sent_at, after)
                rejected += 1
                if rejected > SCHEDULE_RETRIES:
                    return r
                continue
            if sv.window:                             # our clock and theirs disagree slightly: widen the margin
                with sv.lock:
                    cap, span = sv.window
                    sv.window = (cap, min(span + 0.5, sv.base_span + 3.0))
                    sv._persist()
            sv.hold_off(max(after, _backoff(attempt)))
            attempt += 1
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
        attempt += 1
    if last_err is None:                              # ran out of attempts on 429s
        return r
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


# Which songs were actually sent for analysis in this run (for the per-song inventory).
ANALYSED = defaultdict(set)


def analysed(kind, song_id):
    """Record a successful analysis: in memory for the run report, and appended to an audit log the instant
    it returns (survives crashes, so tests can prove no song was ever analysed twice)."""
    with _mlock:
        ANALYSED[kind].add(song_id)
        with open(CACHE / "analysis_log.jsonl", "a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "kind": kind, "id": song_id}) + "\n")


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
