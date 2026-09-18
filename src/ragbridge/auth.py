"""API key generation and verification."""

import hashlib
import secrets

API_KEY_PREFIX = "rb_"
PREFIX_DISPLAY_LENGTH = len(API_KEY_PREFIX) + 8


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
