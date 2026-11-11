import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from receptionist_api.auth import Owner, create_token, decode_token, hash_password, verify_password

SECRET = "unit-test-secret-0123456789abcdef0123456789"
OWNER = Owner(uuid.uuid4(), uuid.uuid4(), "demo-salon", "owner@example.com")


def test_password_hash_roundtrip() -> None:
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong")
    assert not verify_password(None, "anything")  # unknown user
    assert not verify_password("not-a-hash", "anything")


def test_token_roundtrip() -> None:
    assert decode_token(create_token(OWNER, SECRET), SECRET) == OWNER


def test_expired_token_rejected() -> None:
    old = create_token(OWNER, SECRET, now=datetime.now(UTC) - timedelta(days=2))
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(old, SECRET)


def test_tampered_or_foreign_token_rejected() -> None:
    token = create_token(OWNER, SECRET)
    with pytest.raises(jwt.InvalidSignatureError):
        decode_token(token, "another-secret-0123456789abcdef0123456789")
    forged = jwt.encode(
        {
            "sub": str(OWNER.user_id),
            "tid": str(uuid.uuid4()),
            "slug": "x",
            "email": "x",
            "exp": 9999999999,
        },
        "attacker-guess-0123456789abcdef0123",
        algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidSignatureError):
        decode_token(forged, SECRET)
    with pytest.raises(jwt.InvalidAlgorithmError):
        claims = {"sub": "x", "exp": 9999999999}
        unsigned = jwt.encode(claims, None, algorithm="none")  # type: ignore[arg-type]
        decode_token(unsigned, SECRET)
