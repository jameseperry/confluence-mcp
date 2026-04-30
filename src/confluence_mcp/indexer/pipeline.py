"""Page discovery, fetching, chunking, embedding, and index management."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from confluence_mcp.client import ConfluenceClient

from .chunker import chunk_page, content_hash
from .db import get_perspectives
from .embeddings import get_embedder, serialize_vector

logger = logging.getLogger(__name__)

FETCH_CONCURRENCY = 5


async def index_page_from_fetch(
    conn: sqlite3.Connection,
    client: ConfluenceClient,
    page_id: str,
    title: str,
    space_key: str,
    version: int,
    storage_html: str,
    base_url: str,
    max_chunk_chars: int = 2000,
) -> dict:
    """On-demand indexing: called after an MCP tool fetches a page.

    1. Check if page is already indexed at this version → just update last_accessed
    2. If new or newer → chunk, embed, store
    3. Fetch version history → compute median update interval

    Returns {status, chunks}.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    new_hash = content_hash(storage_html)

    row = conn.execute(
        "SELECT id, content_hash, version FROM pages WHERE page_id = ?", (page_id,)
    ).fetchone()

    if row and row["content_hash"] == new_hash:
        # Same content — update metadata
        conn.execute(
            "UPDATE pages SET last_accessed = ?, title = ?, space_key = ? WHERE id = ?",
            (now, title, space_key, row["id"]),
        )
        conn.commit()
        # Still fetch version history to update staleness info
        await _update_version_info(conn, client, page_id, row["id"])
        return {"status": "unchanged", "chunks": 0}

    # New or changed — full index
    if row:
        remove_page_from_index(conn, row["id"])

    result = index_page(
        conn, page_id, title, space_key, version, storage_html, base_url, max_chunk_chars
    )

    if result.get("pending_ids"):
        embed_and_store_chunks(conn, result["pending_ids"], result["pending_texts"])
        conn.commit()

    # Update version history info
    page_row = conn.execute(
        "SELECT id FROM pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    if page_row:
        await _update_version_info(conn, client, page_id, page_row["id"])

    return {"status": result["status"], "chunks": result["chunks"]}


async def _update_version_info(
    conn: sqlite3.Connection,
    client: ConfluenceClient,
    page_id: str,
    db_id: int,
) -> None:
    """Fetch version history and update staleness columns."""
    try:
        timestamps = await client.get_version_history(page_id)
        version_count = len(timestamps)
        median_days = client.compute_median_update_interval(timestamps)
        version_date = timestamps[0] if timestamps else None

        conn.execute(
            "UPDATE pages SET version_count = ?, median_update_interval_days = ?, "
            "version_date = ? WHERE id = ?",
            (version_count, median_days, version_date, db_id),
        )
        conn.commit()
    except Exception:
        logger.debug("Could not fetch version history for page %s", page_id, exc_info=True)


def index_page(
    conn: sqlite3.Connection,
    page_id: str,
    title: str,
    space_key: str,
    version: int,
    storage_html: str,
    base_url: str,
    max_chars: int = 2000,
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

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        "INSERT INTO pages (page_id, title, space_key, version, content_hash, "
        "last_indexed, last_accessed) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (page_id, title, space_key, version, new_hash, now, now),
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

    # Summary chunk
    full_text = "\n\n".join(c.content for c in chunks)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars]
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


async def discover_pages(
    client: ConfluenceClient, scope_type: str, scope_id: str
) -> list[dict]:
    """Discover all pages in a scope. Returns [{page_id, title, version, space_key}]."""
    if scope_type == "space":
        cql = f'type=page AND space.key="{scope_id}"'
    else:
        cql = f"type=page AND ancestor={scope_id}"

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

    if scope_type == "page_tree":
        root_ids = {p["page_id"] for p in pages}
        if scope_id not in root_ids:
            try:
                root_data = await client.get_page(scope_id)
                pages.append({
                    "page_id": scope_id,
                    "title": root_data.get("title", ""),
                    "version": root_data.get("version", {}).get("number", 1),
                    "space_key": root_data.get("spaceId", ""),
                })
            except Exception:
                logger.warning("Could not fetch root page %s", scope_id)

    return pages


async def fetch_page_body(
    client: ConfluenceClient, page_id: str
) -> tuple[str, dict]:
    """Fetch a page's storage body and metadata."""
    data = await client.get_page(page_id, body_format="storage")
    body = data.get("body", {}).get("storage", {}).get("value", "")
    return body, data


async def index_scope(
    client: ConfluenceClient,
    conn: sqlite3.Connection,
    scope_type: str,
    scope_id: str,
    label: str = "",
    max_chunk_chars: int = 2000,
    force: bool = False,
) -> dict:
    """Full index pass for a scope.

    Discovers pages, fetches changed ones, chunks, embeds in bulk, removes stale.
    """
    base_url = ""
    try:
        from confluence_mcp.config import get_base_url
        base_url = get_base_url()
    except RuntimeError:
        pass

    display_label = label or f"{scope_type}:{scope_id}"
    stats = {
        "indexed": 0,
        "updated": 0,
        "unchanged": 0,
        "removed": 0,
        "chunks": 0,
        "errors": 0,
    }

    logger.info("Discovering pages for scope '%s'...", display_label)
    pages = await discover_pages(client, scope_type, scope_id)
    logger.info("Found %d pages in scope '%s'", len(pages), display_label)

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
                    max_chunk_chars,
                )
                stats["chunks"] += result["chunks"]
                if result["status"] in ("indexed", "updated"):
                    stats[result["status"]] += 1
                else:
                    stats["unchanged"] += 1
                all_pending_ids.extend(result.get("pending_ids", []))
                all_pending_texts.extend(result.get("pending_texts", []))

                # Always update metadata and version history
                page_row = conn.execute(
                    "SELECT id FROM pages WHERE page_id = ?",
                    (page_info["page_id"],),
                ).fetchone()
                if page_row:
                    conn.execute(
                        "UPDATE pages SET title = ?, space_key = ?, "
                        "last_accessed = datetime('now') WHERE id = ?",
                        (page_info["title"], page_info["space_key"], page_row["id"]),
                    )
                    await _update_version_info(
                        conn, client, page_info["page_id"], page_row["id"]
                    )
            except Exception:
                logger.exception("Error indexing page %s", page_info["page_id"])
                stats["errors"] += 1

    tasks = [_fetch_and_index(p) for p in pages_to_fetch]
    await asyncio.gather(*tasks)

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
        display_label,
        stats["indexed"],
        stats["updated"],
        stats["unchanged"],
        stats["removed"],
        stats["errors"],
    )
    return stats
