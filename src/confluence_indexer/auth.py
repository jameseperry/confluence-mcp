"""Challenge-response authentication for the Confluence indexer API."""

from __future__ import annotations

import hashlib
import hmac
import logging
import random
import secrets
import sqlite3
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

BODY_PREFIX_LEN = 1000


@dataclass
class AuthToken:
    token: str
    challenge_page_id: str
    challenge_version: int
    expected_hash: str
    verified: bool = False
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0


class TokenStore:
    """In-memory token management with TTL-based expiry."""

    def __init__(self, ttl_hours: int = 24) -> None:
        self._ttl_seconds = ttl_hours * 3600
        self._tokens: dict[str, AuthToken] = {}

    def create_challenge(
        self, page_id: str, version: int, body_prefix: str
    ) -> AuthToken:
        token = secrets.token_hex(32)
        expected = compute_challenge_hash(token, body_prefix)
        entry = AuthToken(
            token=token,
            challenge_page_id=page_id,
            challenge_version=version,
            expected_hash=expected,
        )
        self._tokens[token] = entry
        return entry

    def verify(self, token: str, response_hash: str) -> bool:
        entry = self._tokens.get(token)
        if entry is None:
            return False
        if entry.verified:
            return True
        if not hmac.compare_digest(entry.expected_hash, response_hash):
            return False
        entry.verified = True
        entry.expires_at = time.time() + self._ttl_seconds
        return True

    def is_valid(self, token: str) -> bool | str:
        """Check if a token is valid.

        Returns True if valid, "expired" if expired, False if unknown.
        """
        entry = self._tokens.get(token)
        if entry is None:
            return False
        if not entry.verified:
            return False
        if time.time() > entry.expires_at:
            return "expired"
        return True

    def cleanup_expired(self) -> int:
        now = time.time()
        expired = [
            t
            for t, e in self._tokens.items()
            if (e.verified and now > e.expires_at)
            or (not e.verified and now - e.created_at > 3600)
        ]
        for t in expired:
            del self._tokens[t]
        return len(expired)


def compute_challenge_hash(token: str, body_prefix: str) -> str:
    return hashlib.sha256((token + body_prefix).encode()).hexdigest()


def pick_challenge_page(
    connections: dict[str, sqlite3.Connection],
) -> tuple[str, int] | None:
    """Select a random indexed page for the auth challenge.

    Returns (page_id, version) or None if no pages indexed.
    """
    all_pages: list[tuple[str, int]] = []
    for conn in connections.values():
        rows = conn.execute("SELECT page_id, version FROM pages").fetchall()
        all_pages.extend((r["page_id"], r["version"]) for r in rows)

    if not all_pages:
        return None

    return random.choice(all_pages)
