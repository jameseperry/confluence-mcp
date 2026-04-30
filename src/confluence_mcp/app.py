"""FastMCP server factory for Confluence MCP."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from . import tools
from .client import init_client
from .config import get_api_token, get_base_url, get_email, get_embedding_config

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    client = init_client(get_base_url(), get_email(), get_api_token())

    # Init indexer if EMBEDDING_API_URL is set
    embed_config = get_embedding_config()
    if embed_config:
        from .config import get_db_path, get_default_perspectives
        from .indexer.db import init_db, sync_perspectives
        from .indexer.embeddings import init_embedder

        init_embedder(embed_config)
        perspectives = get_default_perspectives()
        db_path = get_db_path()
        conn = init_db(db_path, perspectives, embed_config.dimensions)
        sync_perspectives(conn, perspectives, embed_config.dimensions)
        logger.info("Indexer initialized: %s", db_path)
    else:
        logger.info("Indexer not configured — set EMBEDDING_API_URL to enable semantic search")

    try:
        yield
    finally:
        try:
            from .indexer.db import close_all_dbs
            close_all_dbs()
        except Exception:
            pass
        await client.close()


def create_mcp_server() -> FastMCP:
    mcp = FastMCP(
        name="confluence",
        instructions=(
            "Confluence MCP provides read and write access to Confluence Cloud pages and spaces. "
            "Reading: use get_page (format='md' or 'xhtml'), get_page_section, get_page_outline, "
            "get_page_by_title, search, list_spaces, get_space_pages, get_child_pages. "
            "Writing: use create_page (XHTML), update_page (full page XHTML), "
            "update_page_section (replace one section), append_to_page, append_to_section. "
            "For editing, read with format='xhtml', modify the XHTML, then write back."
        ),
        lifespan=lifespan,
    )

    # Read
    mcp.add_tool(tools.search)
    mcp.add_tool(tools.get_page)
    mcp.add_tool(tools.get_page_outline)
    mcp.add_tool(tools.get_page_section)
    mcp.add_tool(tools.get_page_by_title)
    # Browse
    mcp.add_tool(tools.list_spaces)
    mcp.add_tool(tools.get_space_pages)
    mcp.add_tool(tools.get_child_pages)
    # Write
    mcp.add_tool(tools.create_page)
    mcp.add_tool(tools.update_page)
    mcp.add_tool(tools.update_page_section)
    mcp.add_tool(tools.append_to_page)
    mcp.add_tool(tools.append_to_section)
    mcp.add_tool(tools.move_section)
    mcp.add_tool(tools.delete_section)
    # Comments, labels, delete
    mcp.add_tool(tools.get_comments)
    mcp.add_tool(tools.add_comment)
    mcp.add_tool(tools.add_label)
    mcp.add_tool(tools.delete_page)
    # Semantic search
    mcp.add_tool(tools.semantic_search)
    # Index management
    mcp.add_tool(tools.index_status)
    mcp.add_tool(tools.list_perspectives)
    mcp.add_tool(tools.add_perspective)
    mcp.add_tool(tools.remove_perspective)
    mcp.add_tool(tools.list_index_scopes)
    mcp.add_tool(tools.add_index_scope)
    mcp.add_tool(tools.remove_index_scope)
    mcp.add_tool(tools.index_now)

    return mcp
