"""Password hashing and JWT issuance.

Identity is deliberately an adapter: nothing outside this module reads a
password, so swapping in an OIDC provider later touches only this file and the
login route.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt

from app.core.config import get_settings
from app.core.errors import AuthenticationError

TokenKind = Literal["access", "refresh"]

MIN_PASSWORD_LENGTH = 12
_BCRYPT_ROUNDS = 12


def _prehash(raw: str) -> bytes:
    """SHA-256 then base64, so bcrypt never sees more than 72 bytes.

    bcrypt truncates its input at 72 bytes. Feeding it a raw password means
    everything past byte 72 is silently ignored -- two different long
    passphrases can then authenticate against the same hash. Pre-hashing gives
    a fixed 44-byte input in which every byte of the original is significant.

    bcrypt is used directly rather than through passlib: passlib's bcrypt
    backend is incompatible with bcrypt >= 4.1 and the project is no longer
    actively maintained.
    """
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(raw: str) -> str:
    if len(raw) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    return bcrypt.hashpw(_prehash(raw), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(raw: str, hashed: str | None) -> bool:
    """Constant-time comparison. A user with no local hash never authenticates."""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_prehash(raw), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_token(
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    kind: TokenKind = "access",
    extra: dict[str, Any] | None = None,
) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    ttl = (
        timedelta(minutes=s.access_token_ttl_minutes)
        if kind == "access"
        else timedelta(days=s.refresh_token_ttl_days)
    )
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org": str(org_id),
        "kind": kind,
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, s.secret_key, algorithm=s.jwt_algorithm)


def decode_token(token: str, *, expect: TokenKind = "access") -> dict[str, Any]:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.secret_key, algorithms=[s.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Token is invalid.") from exc
    if payload.get("kind") != expect:
        raise AuthenticationError(f"Expected a {expect} token.")
    return payload
