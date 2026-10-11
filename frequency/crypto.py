"""Encryption for stored provider tokens (Fernet: AES-128-CBC + HMAC-SHA256, from `cryptography`).

FREQUENCY_SECRET_KEY encrypts; FREQUENCY_SECRET_KEY_PREVIOUS (optional, comma-separated) still decrypts,
so the key can be rotated: set the new key, move the old one to _PREVIOUS, run
`python -m frequency.cli rotate-tokens`, then drop _PREVIOUS.
"""
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from . import settings


def _fernet() -> MultiFernet:
    s = settings.get().require("secret_key")
    keys = [s.secret_key] + [k.strip() for k in os.environ.get("FREQUENCY_SECRET_KEY_PREVIOUS", "").split(",") if k.strip()]
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except ValueError as ex:
        raise RuntimeError("FREQUENCY_SECRET_KEY must be 32 url-safe base64-encoded bytes "
                           "(python -m frequency.cli new-secret prints one)") from ex


def encrypt(text: str | None) -> bytes | None:
    return None if text is None else _fernet().encrypt(text.encode())


def decrypt(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    try:
        return _fernet().decrypt(bytes(blob)).decode()
    except InvalidToken as ex:
        raise RuntimeError("stored token can't be decrypted with the configured keys") from ex


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(blob: bytes) -> bytes:
    try:
        return _fernet().decrypt(bytes(blob))
    except InvalidToken as ex:
        raise RuntimeError("stored data can't be decrypted with the configured keys") from ex


def rotate(blob: bytes | None) -> bytes | None:
    return None if blob is None else _fernet().rotate(bytes(blob))


def new_key() -> str:
    return Fernet.generate_key().decode()
