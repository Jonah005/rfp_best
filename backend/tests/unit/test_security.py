from __future__ import annotations

import uuid

import pytest

from app.core.errors import AuthenticationError
from app.core.security import create_token, decode_token, hash_password, verify_password


def test_password_round_trip():
    hashed = hash_password("correct-horse-battery")
    assert hashed != "correct-horse-battery"
    assert verify_password("correct-horse-battery", hashed)
    assert not verify_password("wrong-password-entirely", hashed)


def test_short_passwords_rejected():
    with pytest.raises(ValueError):
        hash_password("short")


def test_verify_against_missing_hash_is_false():
    # A user backed by an identity provider has no local hash; that must not
    # authenticate, and must not raise.
    assert verify_password("anything", None) is False


def test_access_token_round_trip():
    uid, oid = uuid.uuid4(), uuid.uuid4()
    token = create_token(user_id=uid, org_id=oid, kind="access")
    claims = decode_token(token, expect="access")
    assert claims["sub"] == str(uid)
    assert claims["org"] == str(oid)


def test_refresh_token_is_not_accepted_as_access():
    token = create_token(user_id=uuid.uuid4(), org_id=uuid.uuid4(), kind="refresh")
    with pytest.raises(AuthenticationError):
        decode_token(token, expect="access")


def test_tampered_token_rejected():
    token = create_token(user_id=uuid.uuid4(), org_id=uuid.uuid4())
    with pytest.raises(AuthenticationError):
        decode_token(token[:-3] + "aaa", expect="access")


def test_long_passwords_are_fully_significant():
    """bcrypt truncates at 72 bytes; the pre-hash must prevent that.

    Without pre-hashing, two passphrases sharing a 72-byte prefix would
    authenticate against the same hash.
    """
    base = "x" * 80
    hashed = hash_password(base + "AAAA")
    assert verify_password(base + "AAAA", hashed)
    assert not verify_password(base + "BBBB", hashed)


def test_unicode_passwords_work():
    pw = "كلمة-المرور-الطويلة-2026"
    assert verify_password(pw, hash_password(pw))


def test_hashes_are_salted():
    a, b = hash_password("same-password-twice"), hash_password("same-password-twice")
    assert a != b
    assert verify_password("same-password-twice", a)
    assert verify_password("same-password-twice", b)


# --- Alembic configuration escaping ----------------------------------------


def test_percent_in_a_database_url_survives_alembic_config():
    """Alembic's set_main_option stores values in a ConfigParser that performs
    ``%`` interpolation, so a literal ``%`` raises at read time. Percent signs
    are common in real connection strings -- a percent-encoded socket path, or a
    password containing ``%`` after URL encoding -- so the value must be escaped
    rather than assumed safe.
    """
    from configparser import ConfigParser

    from app.db.session import escape_for_alembic_config

    url = "postgresql+psycopg://user:pa%%ss@/db?host=%2Ftmp%2Fsocket"
    parser = ConfigParser()
    parser.add_section("alembic")

    parser.set("alembic", "sqlalchemy.url", escape_for_alembic_config(url))
    assert parser.get("alembic", "sqlalchemy.url") == url


def test_urls_without_percent_are_unchanged():
    from app.db.session import escape_for_alembic_config

    plain = "postgresql+psycopg://user:pass@localhost:5432/bidready"
    assert escape_for_alembic_config(plain) == plain
