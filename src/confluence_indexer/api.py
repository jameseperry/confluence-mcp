"""FastAPI application for the Confluence semantic indexer."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Header
from pydantic import BaseModel

from confluence_indexer.auth import (
    BODY_PREFIX_LEN,
    TokenStore,
    compute_challenge_hash,
    pick_challenge_page,
)
from confluence_indexer.config import Settings, get_settings, init_settings
from confluence_indexer.db import (
    close_all_dbs,
    get_all_connections,
    get_conn,
    get_perspectives,
    init_db,
    sync_perspectives,
)
from confluence_indexer.embeddings import get_embedder, serialize_vector
from confluence_indexer.indexer import IndexerClient, index_scope

logger = logging.getLogger(__name__)

_client: IndexerClient | None = None
_token_store: TokenStore | None = None
_reindex_task: asyncio.Task | None = None


async def _periodic_reindex(settings: Settings, client: IndexerClient) -> None:
    interval = settings.server.reindex_interval_hours * 3600
    while True:
        await asyncio.sleep(interval)
        logger.info("Starting periodic re-index...")
        for scope in settings.scopes:
            try:
                conn = get_conn(scope.label)
                await index_scope(client, scope, conn)
            except Exception:
                logger.exception("Periodic re-index failed for scope '%s'", scope.label)
        if _token_store:
            _token_store.cleanup_expired()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _client, _token_store, _reindex_task

    settings = get_settings()

    _client = IndexerClient(
        settings.confluence.base_url,
        settings.confluence.email,
        settings.confluence.api_token,
    )

    for scope in settings.scopes:
        conn = init_db(scope.label, scope.db_path, scope.perspectives)
        sync_perspectives(conn, scope.perspectives)

    _token_store = TokenStore(ttl_hours=settings.server.token_ttl_hours)

    _reindex_task = asyncio.create_task(_periodic_reindex(settings, _client))

    try:
        yield
    finally:
        if _reindex_task:
            _reindex_task.cancel()
            try:
                await _reindex_task
            except asyncio.CancelledError:
                pass
        close_all_dbs()
        await _client.close()


app = FastAPI(title="Confluence Semantic Indexer", lifespan=lifespan)


# --- Auth dependency ---

async def require_auth(authorization: str = Header(...)) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    if _token_store is None:
        raise HTTPException(status_code=500, detail="Auth not initialized")
    result = _token_store.is_valid(token)
    if result == "expired":
        raise HTTPException(
            status_code=401,
            detail={"error": "token_expired", "message": "Token has expired. Please re-authenticate."},
        )
    if not result:
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})
    return token


# --- Request/Response models ---

class ChallengeResponse(BaseModel):
    token: str
    challenge_page_id: str
    challenge_version: int


class VerifyRequest(BaseModel):
    token: str
    hash: str


class VerifyResponse(BaseModel):
    verified: bool
    expires_in_hours: int = 0


class SearchRequest(BaseModel):
    query: str
    scope: str | None = None
    perspective: str | None = None
    limit: int = 10


class SearchResult(BaseModel):
    score: float
    page_id: str
    page_title: str
    space_key: str
    heading: str
    snippet: str
    scope: str


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]


class ReindexRequest(BaseModel):
    scope: str | None = None
    force: bool = False


class StatusScope(BaseModel):
    label: str
    scope_type: str
    scope_id: str
    pages_indexed: int
    chunks_total: int
    last_indexed: str | None
    perspectives: list[str]
    indexing_in_progress: bool = False


# --- Endpoints ---

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/auth/challenge")
async def auth_challenge():
    if _client is None or _token_store is None:
        raise HTTPException(status_code=500, detail="Service not initialized")

    connections = get_all_connections()
    challenge = pick_challenge_page(connections)
    if challenge is None:
        raise HTTPException(
            status_code=503,
            detail="No indexed pages available for challenge. Index some pages first.",
        )

    page_id, version = challenge

    try:
        page_data = await _client.get_page_version(page_id, version)
        body = page_data.get("body", {}).get("storage", {}).get("value", "")
    except Exception:
        logger.exception("Failed to fetch challenge page %s v%d", page_id, version)
        raise HTTPException(status_code=503, detail="Failed to generate challenge")

    body_prefix = body[:BODY_PREFIX_LEN]
    auth_token = _token_store.create_challenge(page_id, version, body_prefix)

    return ChallengeResponse(
        token=auth_token.token,
        challenge_page_id=page_id,
        challenge_version=version,
    )


@app.put("/auth/verify")
async def auth_verify(req: VerifyRequest):
    if _token_store is None:
        raise HTTPException(status_code=500, detail="Auth not initialized")

    if _token_store.verify(req.token, req.hash):
        settings = get_settings()
        return VerifyResponse(
            verified=True,
            expires_in_hours=settings.server.token_ttl_hours,
        )

    raise HTTPException(status_code=401, detail={"error": "verification_failed"})


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest, _token: str = Depends(require_auth)):
    settings = get_settings()
    scopes = settings.scopes
    if req.scope:
        scopes = [s for s in scopes if s.label == req.scope]
        if not scopes:
            raise HTTPException(status_code=404, detail=f"Scope '{req.scope}' not found")

    all_results: list[tuple[float, dict, str]] = []

    for scope in scopes:
        conn = get_conn(scope.label)
        perspectives = get_perspectives(conn)

        if req.perspective:
            perspectives = [p for p in perspectives if p["name"] == req.perspective]
            if not perspectives:
                continue

        for p in perspectives:
            query_vec = get_embedder().embed_query(req.query, p["instruction"])
            vec_bytes = serialize_vector(query_vec)
            table = f"vec_p{p['id']}"

            try:
                rows = conn.execute(
                    f"SELECT rowid, distance FROM [{table}] "
                    f"WHERE embedding MATCH ? AND k = ?",
                    (vec_bytes, req.limit * 2),
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
                    "SELECT page_id, title, space_key FROM pages WHERE id = ?",
                    (chunk["page_db_id"],),
                ).fetchone()
                if not page:
                    continue

                snippet = chunk["content"][:500]
                all_results.append((
                    score,
                    {
                        "page_id": page["page_id"],
                        "page_title": page["title"],
                        "space_key": page["space_key"],
                        "heading": chunk["heading_path"],
                        "snippet": snippet,
                    },
                    scope.label,
                ))

    seen_chunks: set[tuple[str, str]] = set()
    deduplicated: list[tuple[float, dict, str]] = []
    for score, info, scope_label in sorted(all_results, key=lambda x: -x[0]):
        key = (info["page_id"], info["heading"])
        if key in seen_chunks:
            continue
        seen_chunks.add(key)
        deduplicated.append((score, info, scope_label))

    top = deduplicated[: req.limit]

    return SearchResponse(
        query=req.query,
        results=[
            SearchResult(score=score, scope=scope_label, **info)
            for score, info, scope_label in top
        ],
    )


_indexing_scopes: set[str] = set()


@app.post("/reindex", status_code=202)
async def reindex(
    req: ReindexRequest,
    background_tasks: BackgroundTasks,
    _token: str = Depends(require_auth),
):
    settings = get_settings()
    scopes = settings.scopes
    if req.scope:
        scopes = [s for s in scopes if s.label == req.scope]
        if not scopes:
            raise HTTPException(status_code=404, detail=f"Scope '{req.scope}' not found")

    started: list[str] = []
    for scope in scopes:
        if scope.label in _indexing_scopes:
            continue
        _indexing_scopes.add(scope.label)
        started.append(scope.label)

        async def _run(s=scope):
            try:
                conn = get_conn(s.label)
                await index_scope(_client, s, conn, force=req.force)
            except Exception:
                logger.exception("Re-index failed for scope '%s'", s.label)
            finally:
                _indexing_scopes.discard(s.label)

        background_tasks.add_task(_run)

    return {"status": "started", "scopes": started}


@app.get("/status")
async def status(_token: str = Depends(require_auth)):
    settings = get_settings()
    scope_statuses: list[StatusScope] = []

    for scope in settings.scopes:
        conn = get_conn(scope.label)
        page_count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        last = conn.execute(
            "SELECT MAX(last_indexed) FROM pages"
        ).fetchone()[0]
        persp_names = [p["name"] for p in get_perspectives(conn)]

        scope_statuses.append(StatusScope(
            label=scope.label,
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            pages_indexed=page_count,
            chunks_total=chunk_count,
            last_indexed=last,
            perspectives=persp_names,
            indexing_in_progress=scope.label in _indexing_scopes,
        ))

    return {"scopes": scope_statuses}
