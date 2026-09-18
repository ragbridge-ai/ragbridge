"""API key generation and verification."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.db.models import ApiKey, Tenant
from ragbridge.db.session import get_session

API_KEY_PREFIX = "rb_"
PREFIX_DISPLAY_LENGTH = len(API_KEY_PREFIX) + 8

LAST_USED_REFRESH_INTERVAL = timedelta(hours=1)
"""How stale last_used_at may be before a request refreshes it.

Writing it on every request would turn every authenticated GET into a
write transaction, for a field nobody reads at minute resolution.
"""

bearer_scheme = HTTPBearer(auto_error=False)
"""auto_error=False: a missing header is handled by get_tenant itself, so
every failure - missing, malformed, unknown, or revoked - raises the same
401 with the same message (see get_tenant).
"""


def generate_api_key() -> str:
    """Return a new, random API key.

    Uses ``secrets``, not ``random``: ``random`` is a deterministic
    pseudo-random generator, fine for simulations and unsuitable for
    anything that must not be guessable. 32 random bytes give the key
    ~256 bits of entropy - see decision 2 in docs/plans/phase-3.md for
    why that is what makes a fast, unsalted hash safe to store it as.
    """
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    """Return the SHA-256 hex digest of an API key, for storage and lookup."""
    return hashlib.sha256(key.encode()).hexdigest()


def api_key_prefix(key: str) -> str:
    """Return the first few characters of a key, safe to store and display.

    Enough to tell two of a tenant's keys apart in a list; far too short
    to guess the rest of a 256-bit secret from.
    """
    return key[:PREFIX_DISPLAY_LENGTH]


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


async def get_tenant(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Tenant:
    """FastAPI dependency: authenticate a request and return its tenant.

    Missing, malformed, unknown, and revoked keys all raise the same 401
    with the same message - an error that distinguished them would be a
    free oracle for anyone probing for valid keys.
    """
    if credentials is None:
        raise _unauthorized()

    row = (
        await session.execute(
            select(ApiKey, Tenant)
            .join(Tenant, ApiKey.tenant_id == Tenant.id)
            .where(
                ApiKey.key_hash == hash_api_key(credentials.credentials),
                ApiKey.revoked_at.is_(None),
            )
        )
    ).first()
    if row is None:
        raise _unauthorized()
    api_key: ApiKey
    tenant: Tenant
    api_key, tenant = row

    now = datetime.now(UTC)
    if api_key.last_used_at is None or now - api_key.last_used_at > LAST_USED_REFRESH_INTERVAL:
        api_key.last_used_at = now
        await session.commit()

    return tenant
