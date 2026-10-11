"""Settings from the environment (and the project .env). Nothing secret is ever logged or returned."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name, default=False):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    secret_key: str = field(default_factory=lambda: os.environ.get("FREQUENCY_SECRET_KEY", ""), repr=False)
    spotify_client_id: str = field(default_factory=lambda: os.environ.get("SPOTIPY_CLIENT_ID", ""))
    spotify_client_secret: str = field(default_factory=lambda: os.environ.get("SPOTIPY_CLIENT_SECRET", ""), repr=False)
    # Where Spotify sends users back after they approve access. Must be registered in the Spotify app.
    spotify_redirect_uri: str = field(default_factory=lambda: os.environ.get(
        "FREQUENCY_SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8060/api/v1/connections/spotify/callback"))
    lastfm_api_key: str = field(default_factory=lambda: os.environ.get("LASTFM_API_KEY", ""), repr=False)
    host: str = field(default_factory=lambda: os.environ.get("FREQUENCY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("FREQUENCY_PORT", "8060")))
    # Cookies are marked Secure when the site is served over HTTPS (set this when a TLS proxy is in front).
    secure_cookies: bool = field(default_factory=lambda: _bool("FREQUENCY_SECURE_COOKIES"))
    session_days: int = field(default_factory=lambda: int(os.environ.get("FREQUENCY_SESSION_DAYS", "14")))
    allow_signup: bool = field(default_factory=lambda: _bool("FREQUENCY_ALLOW_SIGNUP", True))
    clip_store: Path = field(default_factory=lambda: Path(os.environ.get("FREQUENCY_CLIP_STORE") or ROOT / "data" / "clips"))
    # Preview clips are deleted once measured (~0.5 MB each, ~1,000 per new library); 1 keeps them.
    keep_clips: bool = field(default_factory=lambda: _bool("FREQUENCY_KEEP_CLIPS"))
    upload_max_bytes: int = field(default_factory=lambda: int(os.environ.get("FREQUENCY_UPLOAD_MAX_MB", "50")) * 1024 * 1024)

    def require(self, *names):
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(f"Missing settings: {', '.join(missing)} (see .env.example)")
        return self


def get():
    return Settings()
