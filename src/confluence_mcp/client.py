"""Async HTTP client for the Confluence Cloud REST API."""

from __future__ import annotations

from typing_extensions import Self

import httpx


class ConfluenceClient:
    """Thin async wrapper around Confluence Cloud v1/v2 REST APIs."""

    def __init__(self, base_url: str, username: str, api_token: str) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=httpx.BasicAuth(username, api_token),
            headers={"Accept": "application/json"},
            timeout=30.0,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        resp = await self._http.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_page(
        self, page_id: str, body_format: str = "storage"
    ) -> dict:
        return await self._get(
            f"/wiki/api/v2/pages/{page_id}",
            params={"body-format": body_format},
        )

    async def get_child_pages(
        self, page_id: str, limit: int = 25, cursor: str | None = None
    ) -> dict:
        params: dict = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._get(f"/wiki/api/v2/pages/{page_id}/children", params=params)

    async def list_spaces(
        self, limit: int = 25, cursor: str | None = None
    ) -> dict:
        params: dict = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/wiki/api/v2/spaces", params=params)

    async def search_cql(
        self, cql: str, limit: int = 10, cursor: str | None = None
    ) -> dict:
        params: dict = {"cql": cql, "limit": limit, "expand": "space,version"}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/wiki/rest/api/content/search", params=params)

    async def get_labels(self, page_id: str) -> list[dict]:
        data = await self._get(f"/wiki/api/v2/pages/{page_id}/labels")
        return data.get("results", [])

    async def close(self) -> None:
        await self._http.aclose()


_client: ConfluenceClient | None = None


def init_client(
    base_url: str, username: str, api_token: str
) -> ConfluenceClient:
    global _client
    _client = ConfluenceClient(base_url, username, api_token)
    return _client


def get_client() -> ConfluenceClient:
    if _client is None:
        raise RuntimeError("Client not initialized — call init_client() first")
    return _client
