"""Semantic search algorithm, shared by MCP tool and CLI."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from .db import get_perspectives
from .embeddings import get_embedder, serialize_vector

logger = logging.getLogger(__name__)


def _staleness_label(
    last_accessed: str | None,
    median_interval_days: float | None,
) -> str:
    """Estimate staleness based on time since last access vs median update interval."""
    if not last_accessed or median_interval_days is None:
        return "unknown"

    try:
        accessed_dt = datetime.fromisoformat(last_accessed).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "unknown"

    days_since = (datetime.now(timezone.utc) - accessed_dt).total_seconds() / 86400.0

    if days_since <= median_interval_days:
        return "current"
    elif days_since <= median_interval_days * 2:
        return "possibly stale"
    else:
        return "likely stale"


def _interval_label(median_days: float | None) -> str:
    """Human-readable label for median update interval."""
    if median_days is None:
        return "unknown"
    if median_days < 1:
        return "~daily"
    elif median_days < 4:
        return "~every few days"
    elif median_days < 10:
        return "~weekly"
    elif median_days < 45:
        return "~monthly"
    elif median_days < 120:
        return "~quarterly"
    else:
        return "rarely updated"


def semantic_search(
    conn: sqlite3.Connection,
    query: str,
    perspective: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Run semantic search against indexed pages.

    Returns a list of result dicts with score, page info, staleness, etc.
    """
    perspectives = get_perspectives(conn)
    if perspective:
        perspectives = [p for p in perspectives if p["name"] == perspective]
        if not perspectives:
            return []

    all_results: list[tuple[float, dict]] = []

    for p in perspectives:
        query_vec = get_embedder().embed_query(query, p["instruction"])
        vec_bytes = serialize_vector(query_vec)
        table = f"vec_p{p['id']}"

        try:
            rows = conn.execute(
                f"SELECT rowid, distance FROM [{table}] "
                f"WHERE embedding MATCH ? AND k = ?",
                (vec_bytes, limit * 2),
            ).fetchall()
        except Exception:
            logger.exception("Vec search failed on %s", table)
            continue

        for row in rows:
            chunk_id = row["rowid"]
            distance = row["distance"]
            score = 1.0 - distance

            chunk = conn.execute(
                "SELECT page_db_id, heading_path, content FROM chunks WHERE id = ?",
                (chunk_id,),
            ).fetchone()
            if not chunk:
                continue

            page = conn.execute(
                "SELECT page_id, title, space_key, last_accessed, "
                "median_update_interval_days, version_count FROM pages WHERE id = ?",
                (chunk["page_db_id"],),
            ).fetchone()
            if not page:
                continue

            last_accessed = page["last_accessed"]
            median_days = page["median_update_interval_days"]

            all_results.append((
                score,
                {
                    "page_id": page["page_id"],
                    "title": page["title"],
                    "space_key": page["space_key"],
                    "heading": chunk["heading_path"],
                    "snippet": chunk["content"][:500],
                    "last_accessed": last_accessed,
                    "staleness": _staleness_label(last_accessed, median_days),
                    "median_update_interval": _interval_label(median_days),
                },
            ))

    # Deduplicate by (page_id, heading)
    seen: set[tuple[str, str]] = set()
    deduplicated: list[tuple[float, dict]] = []
    for score, info in sorted(all_results, key=lambda x: -x[0]):
        key = (info["page_id"], info["heading"])
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append((score, info))

    return [
        {"score": round(score, 4), **info}
        for score, info in deduplicated[:limit]
    ]


def get_index_status(conn: sqlite3.Connection) -> dict:
    """Return index status: aggregate stats and per-page staleness info."""
    page_count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    last_indexed = conn.execute("SELECT MAX(last_indexed) FROM pages").fetchone()[0]

    perspectives = get_perspectives(conn)

    pages = conn.execute(
        "SELECT page_id, title, space_key, version, last_accessed, "
        "median_update_interval_days, version_count "
        "FROM pages ORDER BY last_accessed DESC"
    ).fetchall()

    page_list = []
    for p in pages:
        last_accessed = p["last_accessed"]
        median_days = p["median_update_interval_days"]
        page_list.append({
            "page_id": p["page_id"],
            "title": p["title"],
            "space_key": p["space_key"],
            "version": p["version"],
            "last_accessed": last_accessed,
            "staleness": _staleness_label(last_accessed, median_days),
            "median_update_interval": _interval_label(median_days),
            "version_count": p["version_count"],
        })

    return {
        "pages_indexed": page_count,
        "chunks_total": chunk_count,
        "last_indexed": last_indexed,
        "perspectives": [p["name"] for p in perspectives],
        "pages": page_list,
    }
