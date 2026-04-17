"""Page discovery, fetching, chunking, embedding, and index management."""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from confluence_mcp.client import ConfluenceClient

from confluence_indexer.chunker import Chunk, chunk_page, content_hash
from confluence_indexer.config import KBScope, get_settings
from confluence_indexer.db import get_perspectives
from confluence_indexer.embeddings import get_embedder, serialize_vector

logger = logging.getLogger(__name__)

FETCH_CONCURRENCY = 5


class IndexerClient(ConfluenceClient):
    """Extended Confluence client with paginated search and historical versions."""

    async def search_cql_paginated(
        self, cql: str, limit_per_page: int = 50, max_results: int = 5000
    ) -> list[dict]:
        results: list[dict] = []
        start = 0
        while len(results) < max_results:
            data = await self._get(
                "/wiki/rest/api/search",
                params={
                    "cql": cql,
                    "limit": min(limit_per_page, max_results - len(results)),
                    "start": start,
                    "expand": "content.version",
                },
            )
            batch = data.get("results", [])
            if not batch:
                break
            results.extend(batch)
            total_size = data.get("totalSize", 0)
            if len(results) >= total_size:
                break
            start += len(batch)
        return results

    async def get_page_version(self, page_id: str, version: int) -> dict:
        """Fetch a specific historical version of a page via v1 API."""
        return await self._get(
            f"/wiki/rest/api/content/{page_id}",
            params={
                "status": "historical",
                "version": version,
                "expand": "body.storage",
            },
        )

    async def get_page_current_version(self, page_id: str) -> int:
        data = await self._get(f"/wiki/api/v2/pages/{page_id}")
        return data.get("version", {}).get("number", 1)


async def discover_pages(
    client: IndexerClient, scope: KBScope
) -> list[dict]:
    """Discover all pages in a scope. Returns [{page_id, title, version, space_key}]."""
    if scope.scope_type == "space":
        cql = f'type=page AND space.key="{scope.scope_id}"'
    else:
        cql = f"type=page AND ancestor={scope.scope_id}"

    raw_results = await client.search_cql_paginated(cql)

    pages: list[dict] = []
    for r in raw_results:
        content = r.get("content", {})
        if not content:
            continue
        page_id = str(content.get("id", ""))
        if not page_id:
            continue
        version_info = content.get("version", {})
        pages.append({
            "page_id": page_id,
            "title": content.get("title", ""),
            "version": version_info.get("number", 1),
            "space_key": content.get("space", {}).get("key", ""),
        })

    if scope.scope_type == "page_tree":
        root_ids = {p["page_id"] for p in pages}
        if scope.scope_id not in root_ids:
            try:
                root_data = await client.get_page(scope.scope_id)
                pages.append({
                    "page_id": scope.scope_id,
                    "title": root_data.get("title", ""),
                    "version": root_data.get("version", {}).get("number", 1),
                    "space_key": root_data.get("spaceId", ""),
                })
            except Exception:
                logger.warning("Could not fetch root page %s", scope.scope_id)

    return pages


async def fetch_page_body(
    client: IndexerClient, page_id: str
) -> tuple[str, dict]:
    """Fetch a page's storage body and metadata.

    Returns (storage_html, metadata_dict).
    """
    data = await client.get_page(page_id, body_format="storage")
    body = data.get("body", {}).get("storage", {}).get("value", "")
    return body, data


def index_page(
    conn: sqlite3.Connection,
    page_id: str,
    title: str,
    space_key: str,
    version: int,
    storage_html: str,
    base_url: str,
    max_chars: int | None = None,
) -> dict:
    """Index a single page: convert, chunk, store (without embedding).

    Returns {status, chunks, pending_ids, pending_texts}.
    """
    new_hash = content_hash(storage_html)

    row = conn.execute(
        "SELECT id, content_hash FROM pages WHERE page_id = ?", (page_id,)
    ).fetchone()

    if row and row["content_hash"] == new_hash:
        return {"status": "unchanged", "chunks": 0}

    if row:
        remove_page_from_index(conn, row["id"])

    _, chunks = chunk_page(storage_html, base_url, max_chars)
    if not chunks:
        return {"status": "empty", "chunks": 0}

    cursor = conn.execute(
        "INSERT INTO pages (page_id, title, space_key, version, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (page_id, title, space_key, version, new_hash),
    )
    page_db_id = cursor.lastrowid

    chunk_ids: list[int] = []
    chunk_texts: list[str] = []
    for chunk in chunks:
        c = conn.execute(
            "INSERT INTO chunks (page_db_id, heading_path, content, char_start, char_end) "
            "VALUES (?, ?, ?, ?, ?)",
            (page_db_id, chunk.heading_path, chunk.content, chunk.char_start, chunk.char_end),
        )
        chunk_ids.append(c.lastrowid)
        chunk_texts.append(chunk.content)

    full_text = "\n\n".join(c.content for c in chunks)
    limit = max_chars or 2000
    if len(full_text) > limit:
        full_text = full_text[:limit]
    c = conn.execute(
        "INSERT INTO chunks (page_db_id, heading_path, content, char_start, char_end) "
        "VALUES (?, ?, ?, ?, ?)",
        (page_db_id, "[page]", full_text, 0, len(full_text)),
    )
    chunk_ids.append(c.lastrowid)
    chunk_texts.append(full_text)

    status = "updated" if row else "indexed"
    return {
        "status": status,
        "chunks": len(chunks) + 1,
        "pending_ids": chunk_ids,
        "pending_texts": chunk_texts,
    }


def embed_and_store_chunks(
    conn: sqlite3.Connection,
    chunk_ids: list[int],
    chunk_texts: list[str],
) -> None:
    """Embed chunks and store vectors in all perspective vec tables."""
    if not chunk_ids:
        return

    perspectives = get_perspectives(conn)
    for p in perspectives:
        vectors = get_embedder().embed_documents(chunk_texts, p["instruction"])
        table = f"vec_p{p['id']}"
        for cid, vec in zip(chunk_ids, vectors):
            conn.execute(
                f"INSERT INTO [{table}] (rowid, embedding) VALUES (?, ?)",
                (cid, serialize_vector(vec)),
            )


def remove_page_from_index(conn: sqlite3.Connection, page_db_id: int) -> int:
    """Remove a page and its chunks/embeddings. Returns chunk count removed."""
    chunk_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM chunks WHERE page_db_id = ?", (page_db_id,)
        ).fetchall()
    ]

    if chunk_ids:
        perspectives = get_perspectives(conn)
        for p in perspectives:
            table = f"vec_p{p['id']}"
            for cid in chunk_ids:
                try:
                    conn.execute(f"DELETE FROM [{table}] WHERE rowid = ?", (cid,))
                except sqlite3.OperationalError:
                    pass
        conn.execute("DELETE FROM chunks WHERE page_db_id = ?", (page_db_id,))

    conn.execute("DELETE FROM pages WHERE id = ?", (page_db_id,))
    return len(chunk_ids)


async def index_scope(
    client: IndexerClient,
    scope: KBScope,
    conn: sqlite3.Connection,
    force: bool = False,
) -> dict:
    """Full index pass for a scope.

    Discovers pages, fetches changed ones, chunks, embeds in bulk, removes stale.
    """
    settings = get_settings()
    base_url = settings.confluence.base_url.rstrip("/")

    stats = {
        "indexed": 0,
        "updated": 0,
        "unchanged": 0,
        "removed": 0,
        "chunks": 0,
        "errors": 0,
    }

    logger.info("Discovering pages for scope '%s'...", scope.label)
    pages = await discover_pages(client, scope)
    logger.info("Found %d pages in scope '%s'", len(pages), scope.label)

    seen_page_ids: set[str] = set()
    pages_to_fetch: list[dict] = []

    for page in pages:
        seen_page_ids.add(page["page_id"])
        if force:
            pages_to_fetch.append(page)
            continue
        row = conn.execute(
            "SELECT version, content_hash FROM pages WHERE page_id = ?",
            (page["page_id"],),
        ).fetchone()
        if row is None or row["version"] < page["version"]:
            pages_to_fetch.append(page)
        else:
            stats["unchanged"] += 1

    logger.info("%d pages need fetching/indexing", len(pages_to_fetch))

    all_pending_ids: list[int] = []
    all_pending_texts: list[str] = []
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _fetch_and_index(page_info: dict) -> None:
        async with sem:
            try:
                body, _ = await fetch_page_body(client, page_info["page_id"])
                result = index_page(
                    conn,
                    page_info["page_id"],
                    page_info["title"],
                    page_info["space_key"],
                    page_info["version"],
                    body,
                    base_url,
                    settings.max_chunk_chars,
                )
                stats["chunks"] += result["chunks"]
                if result["status"] in ("indexed", "updated"):
                    stats[result["status"]] += 1
                else:
                    stats["unchanged"] += 1
                all_pending_ids.extend(result.get("pending_ids", []))
                all_pending_texts.extend(result.get("pending_texts", []))
            except Exception:
                logger.exception("Error indexing page %s", page_info["page_id"])
                stats["errors"] += 1

    tasks = [_fetch_and_index(p) for p in pages_to_fetch]
    await asyncio.gather(*tasks)

    all_indexed = conn.execute("SELECT id, page_id FROM pages").fetchall()
    for row in all_indexed:
        if row["page_id"] not in seen_page_ids:
            remove_page_from_index(conn, row["id"])
            stats["removed"] += 1

    if all_pending_ids:
        logger.info(
            "Embedding %d chunks across %d perspectives...",
            len(all_pending_ids),
            len(get_perspectives(conn)),
        )
        embed_and_store_chunks(conn, all_pending_ids, all_pending_texts)

    conn.commit()

    logger.info(
        "Scope '%s' done: %d indexed, %d updated, %d unchanged, %d removed, %d errors",
        scope.label,
        stats["indexed"],
        stats["updated"],
        stats["unchanged"],
        stats["removed"],
        stats["errors"],
    )
    return stats
