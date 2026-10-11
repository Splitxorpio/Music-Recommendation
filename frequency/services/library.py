"""Import one user's listening from a source into user_library (canonical tracks in the global catalog)."""
import re
import uuid
from pathlib import Path

from .. import connections, crypto, db, resolve, settings, userdata
from ..sources import SourceError, history_upload, lastfm, spotify


def _store(user_id, provider, signals, source_stats, progress):
    flat = [s for ss in signals.values() for s in ss]
    mapping, rstats = resolve.resolve(flat, source=provider, progress=lambda n, t: progress(f"matched {n} of {t} songs"))
    import taste_profile
    rows = {sig: [{"track_id": mapping[s.key()], "weight": taste_profile.SOURCE_WEIGHT.get(sig, 1.0),
                   "play_count": s.play_count, "last_played_at": s.last_played_at} for s in ss]
            for sig, ss in signals.items()}
    stats = {**source_stats, "resolution": rstats, "songs": len({t for r in rows.values() for t in (x["track_id"] for x in r)}),
             "signals": {k: len(v) for k, v in rows.items()}}
    with db.as_user(user_id) as cur:
        imp = userdata.start_import(cur, user_id, provider)
        userdata.replace_signals(cur, user_id, imp, rows)
        userdata.finish_import(cur, imp, "completed", stats)
    return stats


def import_spotify(user_id, progress=lambda m: None) -> dict:
    with db.as_user(user_id) as cur:
        conn = connections.get(cur, "spotify")
    if not conn or conn["status"] == "revoked":
        raise SourceError("Spotify isn't connected.", reauth=True)
    t = connections.tokens(conn)
    if connections.expired(conn):
        try:
            t = spotify.refresh(t["refresh_token"])
        except SourceError:
            with db.as_user(user_id) as cur:
                connections.mark(cur, conn["id"], "needs_reauth")
            raise
        with db.as_user(user_id) as cur:
            connections.update_tokens(cur, conn["id"], t)
    client = spotify.Client(t["access_token"])
    signals = spotify.pull(client, progress)
    return _store(user_id, "spotify", signals, {"spotify_requests": client.requests}, progress)


def import_lastfm(user_id, progress=lambda m: None) -> dict:
    with db.as_user(user_id) as cur:
        conn = connections.get(cur, "lastfm")
    if not conn:
        raise SourceError("Last.fm isn't connected.")
    client = lastfm.Client()
    signals, _ = lastfm.pull(client, conn["external_user_id"], progress)
    return _store(user_id, "lastfm", signals, {"lastfm_requests": client.requests}, progress)


def import_history(user_id, upload_path: str, progress=lambda m: None) -> dict:
    """The upload is stored encrypted until imported, then deleted (only the derived listening is kept)."""
    p = Path(upload_path)
    if not p.exists():
        raise SourceError("The uploaded file is gone (already imported, or removed). Please upload it again.")
    name, _, blob = p.read_bytes().partition(b"\n")
    plays = history_upload.parse([(name.decode(), crypto.decrypt_bytes(blob))])
    signals, stats = history_upload.signals(plays)
    progress(f"{stats['plays']} plays of {stats['songs']} songs in your history")
    out = _store(user_id, "history_upload", signals, stats, progress)
    p.unlink(missing_ok=True)
    return out


def save_upload(user_id, filename: str, data: bytes) -> str:
    """Keep an uploaded export encrypted on disk until the import job has read it."""
    d = settings.ROOT / "data" / "uploads" / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:100] or "upload"
    p = d / f"{uuid.uuid4().hex}.enc"
    p.write_bytes(safe.encode() + b"\n" + crypto.encrypt_bytes(data))
    return str(p)


def delete_uploads(user_id):
    d = settings.ROOT / "data" / "uploads" / str(user_id)
    for f in d.glob("*.enc") if d.exists() else []:
        f.unlink(missing_ok=True)


def import_source(user_id, source: str, payload: dict, progress=lambda m: None) -> dict:
    if source == "spotify":
        return import_spotify(user_id, progress)
    if source == "lastfm":
        return import_lastfm(user_id, progress)
    if source == "history_upload":
        return import_history(user_id, payload["upload_path"], progress)
    raise SourceError(f"Unknown source {source}")
