"""Typed request/response shapes for /api/v1. Profile and recommendation shapes mirror frontend/src/types/api.ts."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SignUp(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str | None = Field(default=None, max_length=80)


class SignIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


class User(BaseModel):
    id: str
    email: str
    display_name: str | None
    calibration_consent: bool
    created_at: datetime


class Session(BaseModel):
    user: User
    csrf_token: str


class UpdateMe(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    calibration_consent: bool | None = None


class ChangePassword(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class ConfirmPassword(BaseModel):
    password: str = Field(max_length=1024)


class Connection(BaseModel):
    provider: Literal["spotify", "lastfm", "history_upload"]
    account: str | None
    status: str
    connected_at: datetime
    updated_at: datetime


class SourcesAvailable(BaseModel):
    spotify: bool
    lastfm: bool
    history_upload: bool = True
    spotify_note: str | None = None


class Connections(BaseModel):
    connections: list[Connection]
    available: SourcesAvailable


class LastfmConnect(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")


class AuthorizeUrl(BaseModel):
    url: str


class StartRun(BaseModel):
    source: Literal["spotify", "lastfm"] | None = None


class Job(BaseModel):
    id: int
    kind: str
    status: Literal["queued", "running", "retry_wait", "completed", "partially_completed", "failed", "cancelled"]
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested: bool


class RunStarted(BaseModel):
    job: Job
    created: bool


class Parts(BaseModel):
    sound: float | None
    scene: float | None
    connection: float | None


class Pick(BaseModel):
    id: str
    title: str
    artist: str
    artists: list[str]
    album: str | None = None
    cover: str | None = None
    cover_m: str | None = None
    score: float
    parts: Parts
    cluster: int | None
    reason: str
    tempo: float | None
    bass: float | None
    bass_word: str | None
    estimated: bool
    scene_inferred: bool
    mode: str | None = None
    hop: int | None = None
    release: str | None = None
    genres: list[str]
    spotify_url: str | None = None


class Recommendations(BaseModel):
    count: int
    items: list[Pick]


class Profile(BaseModel):
    """The dashboard profile payload (nested shapes as in frontend/src/types/api.ts)."""
    meta: dict[str, Any]
    coverage: dict[str, Any]
    traits: dict[str, Any]
    genres: list[Any]
    artists: list[dict[str, Any]]
    clusters: list[dict[str, Any]]
    landscapes: list[dict[str, Any]]
    landscape_bpm: list[float]
    genre_links: list[dict[str, Any]]
    library_covers: list[dict[str, Any]]
    ranges: dict[str, list[float]]
    default_weights: dict[str, float]
    separation: float | None


class Health(BaseModel):
    ok: bool
    database: bool
    schema_pending: list[str]
    queue: dict[str, int]


class Error(BaseModel):
    error: str
