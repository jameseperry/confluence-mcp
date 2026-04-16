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

    async def _post(self, path: str, json_body: dict | list) -> dict:
        resp = await self._http.post(path, json=json_body)
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, json_body: dict) -> dict:
        resp = await self._http.put(path, json=json_body)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> None:
        resp = await self._http.delete(path)
        resp.raise_for_status()

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
        params: dict = {
            "cql": cql,
            "limit": limit,
            "excerpt": "highlight",
            "expand": "content.space,content.version",
        }
        if cursor:
            params["cursor"] = cursor
        return await self._get("/wiki/rest/api/search", params=params)

    async def get_ancestors(self, page_id: str) -> list[dict]:
        data = await self._get(
            f"/wiki/rest/api/content/{page_id}",
            params={"expand": "ancestors"},
        )
        return data.get("ancestors", [])

    async def get_space_by_key(self, space_key: str) -> dict | None:
        data = await self._get("/wiki/api/v2/spaces", params={"keys": space_key})
        results = data.get("results", [])
        return results[0] if results else None

    async def get_space_pages(
        self, space_id: str, depth: str = "root", limit: int = 25
    ) -> dict:
        return await self._get(
            "/wiki/api/v2/pages",
            params={"space-id": space_id, "depth": depth, "limit": limit},
        )

    async def get_labels(self, page_id: str) -> list[dict]:
        data = await self._get(f"/wiki/api/v2/pages/{page_id}/labels")
        return data.get("results", [])

    async def create_page(
        self,
        space_id: str,
        title: str,
        body: str,
        parent_id: str | None = None,
    ) -> dict:
        payload: dict = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": body},
        }
        if parent_id:
            payload["parentId"] = parent_id
        return await self._post("/wiki/api/v2/pages", payload)

    async def update_page(
        self,
        page_id: str,
        title: str,
        body: str,
        version_number: int,
        version_message: str | None = None,
    ) -> dict:
        version: dict = {"number": version_number}
        if version_message:
            version["message"] = version_message
        payload = {
            "id": page_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": body},
            "version": version,
        }
        return await self._put(f"/wiki/api/v2/pages/{page_id}", payload)

    async def get_comments(
        self, page_id: str, limit: int = 25
    ) -> dict:
        return await self._get(
            f"/wiki/api/v2/pages/{page_id}/footer-comments",
            params={"body-format": "storage", "limit": limit},
        )

    async def create_comment(
        self, page_id: str, body: str
    ) -> dict:
        return await self._post(
            "/wiki/api/v2/footer-comments",
            {
                "pageId": page_id,
                "body": {"representation": "storage", "value": body},
            },
        )

    async def add_label(self, page_id: str, label: str) -> dict:
        return await self._post(
            f"/wiki/rest/api/content/{page_id}/label",
            [{"prefix": "global", "name": label}],
        )

    async def delete_page(self, page_id: str) -> None:
        await self._delete(f"/wiki/api/v2/pages/{page_id}")

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
