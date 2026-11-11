"""Owner accounts: argon2 password hashes and short-lived JWT access tokens.

The token carries the owner's tenant id. Dashboard routes read the tenant ONLY from the verified
token, never from the URL or body, so an owner can't even ask for another tenant's data
(see the isolation tests).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

ALGORITHM = "HS256"
TOKEN_TTL = timedelta(hours=12)
_hasher = PasswordHasher()  # argon2id with the library's current recommended parameters
_bearer = HTTPBearer(auto_error=False)

# Verifying against a dummy hash when the email is unknown keeps login timing the same,
# so response time doesn't reveal which emails have accounts.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


@dataclass(frozen=True)
class Owner:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_slug: str
    email: str


def create_token(owner: Owner, secret: str, *, now: datetime | None = None) -> str:
    issued = now or datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(owner.user_id),
        "tid": str(owner.tenant_id),
        "slug": owner.tenant_slug,
        "email": owner.email,
        "iat": int(issued.timestamp()),
        "exp": int((issued + TOKEN_TTL).timestamp()),
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def decode_token(token: str, secret: str) -> Owner:
    claims = jwt.decode(token, secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub"]})
    return Owner(
        user_id=uuid.UUID(claims["sub"]),
        tenant_id=uuid.UUID(claims["tid"]),
        tenant_slug=str(claims["slug"]),
        email=str(claims["email"]),
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "unauthorized", "detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_owner(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Owner:
    secret: str | None = request.app.state.resources.jwt_secret
    if secret is None:
        raise HTTPException(
            status_code=503, detail={"code": "jwt_not_configured", "detail": "set JWT_SECRET"}
        )
    if credentials is None:
        raise _unauthorized("missing bearer token")
    try:
        return decode_token(credentials.credentials, secret)
    except jwt.PyJWTError as exc:
        raise _unauthorized("invalid or expired token") from exc


OwnerDep = Annotated[Owner, Depends(current_owner)]
