from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from app.config import Settings

SESSION_COOKIE_NAME = "grafinder_session"
_PASSWORD_ALGORITHM = "pbkdf2_sha256"
_PASSWORD_ITERATIONS = 210_000


def parse_auth_users(raw: str | None) -> list[tuple[str, str]]:
    users: list[tuple[str, str]] = []
    for item in (raw or "").split(","):
        if ":" not in item:
            continue
        username, password = item.split(":", 1)
        username = username.strip()
        password = password.strip()
        if username and password:
            users.append((username, password))
    return users


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        _PASSWORD_ITERATIONS,
    )
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{_PASSWORD_ALGORITHM}${_PASSWORD_ITERATIONS}${salt}${encoded}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt, expected = stored_hash.split("$", 3)
        iterations = int(iterations_raw)
    except ValueError:
        return False

    if algorithm != _PASSWORD_ALGORITHM:
        return False

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    actual = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return hmac.compare_digest(actual, expected)


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def create_session_token(username: str, settings: Settings) -> str:
    payload = {
        "u": username,
        "exp": int(time.time()) + settings.auth_session_max_age_seconds,
    }
    payload_segment = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(
        settings.auth_session_secret.encode("utf-8"),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_segment}.{_b64_encode(signature)}"


def read_session_username(token: str | None, settings: Settings) -> str | None:
    if not token or "." not in token:
        return None

    payload_segment, signature_segment = token.split(".", 1)
    expected_signature = hmac.new(
        settings.auth_session_secret.encode("utf-8"),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        actual_signature = _b64_decode(signature_segment)
    except Exception:
        return None

    if not hmac.compare_digest(actual_signature, expected_signature):
        return None

    try:
        payload = json.loads(_b64_decode(payload_segment).decode("utf-8"))
    except Exception:
        return None

    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    username = payload.get("u")
    return username if isinstance(username, str) and username else None
