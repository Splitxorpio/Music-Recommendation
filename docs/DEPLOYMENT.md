# Deployment: this Mac, local network

The target is one Mac serving people on the same network. PostgreSQL, the API and one or more workers all run
on it as background services that start at login and restart if they crash.

## 1. One-time setup

```
brew install postgresql@17 && brew services start postgresql@17
cd /path/to/Music-Recommendation
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                # then fill it in (see below)
```

Create the database role and databases. Pick your own password and put it in both URLs in `.env`:

```
/opt/homebrew/opt/postgresql@17/bin/psql -d postgres -c "CREATE ROLE frequency_app LOGIN PASSWORD 'CHANGE_ME'"
/opt/homebrew/opt/postgresql@17/bin/createdb -O frequency_app frequency
/opt/homebrew/opt/postgresql@17/bin/createdb -O frequency_app frequency_test
```

The role must **not** be a superuser or have `BYPASSRLS`: row-level security is what keeps users' data apart.

Then:

```
.venv/bin/python -m frequency.cli new-secret      # paste into FREQUENCY_SECRET_KEY
.venv/bin/python -m frequency.cli migrate
cd frontend && npm ci && npm run build && cd ..
.venv/bin/python -m frequency.cli doctor
.venv/bin/python -m pytest -q tests               # uses only the test database and fake providers
```

## 2. Run as background services (launchd)

```
mkdir -p data/logs
for f in deploy/com.frequency.api.plist deploy/com.frequency.worker.plist; do
  sed "s|REPO|$PWD|g" "$f" > ~/Library/LaunchAgents/$(basename "$f")
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$(basename "$f")
done
```

* Logs go to `data/logs/api.log` and `data/logs/worker.log`. The worker writes one JSON line per job event.
* For more measuring throughput on a busy day, add a second worker under another label. It won't speed up
  providers, whose allowances are shared, but it overlaps the local work (clip download, bass measurement).
* The worker plist runs under `caffeinate -i`, so the Mac doesn't idle-sleep mid-run. A closed lid on battery
  still sleeps it: keep it on power.

## 3. Reaching it from other devices: HTTPS required

Browsers only keep secure session cookies over HTTPS, and Spotify only accepts `https://` redirect addresses
(or `127.0.0.1` on the same machine). For the local network, put Caddy in front:

```
brew install caddy
# edit deploy/Caddyfile: replace frequency.local with this Mac's local hostname
sudo caddy start --config deploy/Caddyfile
caddy trust                         # trust Caddy's local certificate authority on this Mac
```

* Every other device that uses the site must also trust Caddy's root certificate once. On macOS and iOS, AirDrop
  or email the file from `~/Library/Application Support/Caddy/pki/authorities/local/root.crt` and mark it trusted
  in the device's settings.
* Then set in `.env`:

```
FREQUENCY_HOST=127.0.0.1            # the API stays private; only Caddy listens on the network
FREQUENCY_SECURE_COOKIES=1
FREQUENCY_SPOTIFY_REDIRECT_URI=https://frequency.local/api/v1/connections/spotify/callback
```

* Register that redirect URI in the Spotify developer dashboard, and add each Spotify user there too (max 5 in
  development mode).
* Restart the API.
* macOS firewall: allow incoming connections for Caddy only.

**On this Mac only, no other devices:** skip Caddy and use http://127.0.0.1:8060. Spotify accepts the loopback
redirect, and cookies work without `Secure` on loopback.

## 4. Not covered here

* **Serving the public internet**: this setup has no DDoS protection, abuse handling or managed backups. Also,
  every provider allowance is shared by all users (see CAPACITY.md), so a public site would hit the ReccoBeats
  ceiling quickly.
* **Linux**: works, but iTunes M4A clips need `afconvert` (macOS only) for bass measurement. Deezer clips, the
  vast majority, work everywhere.
