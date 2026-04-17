"""Client for communicating with the Confluence semantic indexer API."""

from __future__ import annotations

import hashlib
import logging
import time

import httpx

logger = logging.getLogger(__name__)

BODY_PREFIX_LEN = 1000


class IndexerClient:
    """Handles auth flow and search against the indexer API."""

    def __init__(
        self,
        indexer_url: str,
        confluence_base_url: str,
        confluence_email: str,
        confluence_api_token: str,
    ) -> None:
        self._indexer_url = indexer_url.rstrip("/")
        self._confluence = httpx.AsyncClient(
            base_url=confluence_base_url,
            auth=httpx.BasicAuth(confluence_email, confluence_api_token),
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
        self._http = httpx.AsyncClient(timeout=30.0)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def _authenticate(self) -> None:
        resp = await self._http.get(f"{self._indexer_url}/auth/challenge")
        resp.raise_for_status()
        challenge = resp.json()

        token = challenge["token"]
        page_id = challenge["challenge_page_id"]
        version = challenge["challenge_version"]

        page_resp = await self._confluence.get(
            f"/wiki/rest/api/content/{page_id}",
            params={
                "status": "historical",
                "version": version,
                "expand": "body.storage",
            },
        )
        page_resp.raise_for_status()
        body = page_resp.json().get("body", {}).get("storage", {}).get("value", "")
        body_prefix = body[:BODY_PREFIX_LEN]

        response_hash = hashlib.sha256((token + body_prefix).encode()).hexdigest()

        verify_resp = await self._http.put(
            f"{self._indexer_url}/auth/verify",
            json={"token": token, "hash": response_hash},
        )
        verify_resp.raise_for_status()
        result = verify_resp.json()

        if not result.get("verified"):
            raise RuntimeError("Indexer auth verification failed")

        self._token = token
        expires_hours = result.get("expires_in_hours", 24)
        self._token_expires_at = time.time() + (expires_hours * 3600) - 300

    async def _ensure_auth(self) -> str:
        if self._token is None or time.time() >= self._token_expires_at:
            await self._authenticate()
        return self._token  # type: ignore[return-value]

    async def search(
        self,
        query: str,
        scope: str | None = None,
        perspective: str | None = None,
        limit: int = 10,
    ) -> dict:
        token = await self._ensure_auth()
        body: dict = {"query": query, "limit": limit}
        if scope:
            body["scope"] = scope
        if perspective:
            body["perspective"] = perspective

        resp = await self._http.post(
            f"{self._indexer_url}/search",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 401:
            self._token = None
            token = await self._ensure_auth()
            resp = await self._http.post(
                f"{self._indexer_url}/search",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._http.aclose()
        await self._confluence.aclose()


_indexer_client: IndexerClient | None = None


def init_indexer_client(
    indexer_url: str,
    confluence_base_url: str,
    confluence_email: str,
    confluence_api_token: str,
) -> IndexerClient:
    global _indexer_client
    _indexer_client = IndexerClient(
        indexer_url, confluence_base_url, confluence_email, confluence_api_token,
    )
    return _indexer_client


def get_indexer_client() -> IndexerClient | None:
    return _indexer_client
