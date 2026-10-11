# Capacity report

All figures are measured unless marked otherwise. Provider allowances are one per provider for the whole server:
adding workers or machines does not add capacity, and we don't try to get around that.

## What one new library costs

These are the single-user benchmark runs of Oct 9 2026: three fresh runs, a 634–638 song library, empty cache.
The multi-user worker makes the same requests through the same code.

| Provider | Requests per fresh library | Allowance (whole server) | Quota units |
|---|---|---|---|
| ReccoBeats clip analysis | 244–247 | 15 units / 30 s (measured, fixed windows) = 43,200 units/day | 1 each |
| ReccoBeats lookups | 53–57 | shares the same 15 units / 30 s | ⅛ each (≈7) |
| Deezer API | 2,359–2,480 | ~9/s as configured (documented ~50 per 5 s) = 777,600/day | — |
| Deezer CDN (clip downloads) | ~980 | not an API quota | — |
| MusicBrainz | 103–110 | 1/s requested (we use 0.9/s) = 77,760/day | — |
| iTunes | 25–26 | ~20/min (we use 0.33/s) = 28,500/day | — |
| Spotify | 20 (Spotify source only) | development mode: **max 5 users** | — |

Measured costs for an existing user:

* **Re-dig the same day**: 0 analyses and about 84 Deezer requests.
* **Re-dig the next day**: 5 analyses and 763 Deezer refetches.

**Bottleneck: ReccoBeats.** A fresh library takes about 252 units, roughly 8.4 minutes of the whole server's
analysis allowance. Worst case, with no overlap with songs already in the catalog, that is about **171 new
libraries per day**. Deezer comes second at about 320 per day.

**Overlap helps.** A song measured for one user is never analysed again for anyone (proven by the end-to-end
test). The marginal cost of a new user therefore falls as the shared catalog grows. This is an estimate, not
measured, because real-world overlap between users isn't known yet:

| Share of a new user's unmeasured songs already in the catalog | Units per new library | New libraries / day |
|---|---|---|
| 0% (worst case) | ~252 | ~171 |
| 25% | ~190 | ~227 |
| 50% | ~130 | ~332 |

## Local machine (measured, `tools/loadtest.py`, this Mac, PostgreSQL 17)

| Component | Measured | Needed at the provider ceiling | Headroom |
|---|---|---|---|
| Job queue (4 processes, claim+finish) | 939 jobs/s, p95 6.9 ms | ~0.5 analyses/s | >1,000× |
| Shared quota scheduler (4 processes) | 4,668 bookings/s, p95 0.8 ms | ~10 requests/s total | >400× |
| API, 25 concurrent signed-in users | 487 requests/s; recommendations p50 31 ms, p95 50 ms; first profile load p95 164 ms | — | — |

None of the local parts is close to limiting. The ceiling is the providers.

**Storage per new library** (estimated from the request counts):

* Database: about 650 library rows plus about 1,300 candidates, roughly 2 MB per run.
* Shared provider answers: a few MB per library, de-duplicated across users and evicted after expiry.
* Preview clips: deleted after measuring (`FREQUENCY_KEEP_CLIPS=0`). Kept, they would be about 470 MB per library.

## Three scenarios

### 1. Five Spotify users (development-mode cap), all starting at once

* Total work is 5 × 252 = 1,260 units, about **42 minutes** of analysis allowance.
* The queue is round-robin across users, so all five progress together and finish around 35–42 minutes.
  Run one at a time, each would take about 8.4 minutes.
* Round-robin was chosen so a small library isn't stuck behind a big one.
* Deezer needs about 12,000 requests, roughly 22 minutes in parallel, so it doesn't limit.
* **Fine as is.**

### 2. 100 users joining on one day (Last.fm and history uploads; Spotify can't serve them in dev mode)

* Worst case is 25,200 units, about **14 hours** of analysis allowance. With 25% overlap it's about 10.5 hours.
* The last user to join could wait most of a day for their first results. The loading screen shows progress
  meanwhile.
* Deezer needs about 242,000 requests, about 7.5 hours, so it doesn't limit.
* Afterwards, daily re-digs cost about 100 × 5 units, roughly 17 minutes a day.
* **Feasible. First-day waits are long, but nothing fails.**

### 3. 1,000 users joining over a week

* About 143 new libraries a day is about 36,000 units a day, **83% of ReccoBeats' daily ceiling** with no
  overlap. Deezer would be at about 45%.
* Daily re-digs for 1,000 users would add another 5,000–10,000 units, more than the remaining 17%.
* **Not sustainable on the free ReccoBeats allowance.** The options, in order of preference:
  1. Ask ReccoBeats for a higher allowance. The quota email is drafted and not yet sent.
  2. Measure fewer candidates per run: `STAGE2_PER_BUCKET` 120 → 60 saves about 40 units per library.
  3. Re-dig weekly instead of on demand, or cap re-digs per user per day.
  4. Skip analysing very-low-weight library songs (for example, saved once, never played).
* Spotify is unusable at this scale. Extended quota is granted only to organisations with at least 250,000
  monthly active users, so these users must use Last.fm or a history upload.

## Caveats

* The ReccoBeats model (15 units per 30 s, lookups ⅛) is fitted to live tests. ReccoBeats hasn't confirmed it.
  It may change, and it may be applied per IP.
* Deezer and MusicBrainz limits are their published guidance. Either may throttle harder without notice.
* Per-library costs come from one real library (about 638 songs, about 25% without official numbers). Libraries
  of newer or underground music need more analyses.
