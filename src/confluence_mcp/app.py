"""FastMCP server factory for Confluence MCP."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from . import tools
from .client import init_client
from .config import get_api_token, get_base_url, get_email, get_indexer_url
from .indexer_client import get_indexer_client, init_indexer_client


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    client = init_client(get_base_url(), get_email(), get_api_token())
    indexer_url = get_indexer_url()
    if indexer_url:
        init_indexer_client(indexer_url, get_base_url(), get_email(), get_api_token())
    try:
        yield
    finally:
        ic = get_indexer_client()
        if ic:
            await ic.close()
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
    # Comments, labels, delete
    mcp.add_tool(tools.get_comments)
    mcp.add_tool(tools.add_comment)
    mcp.add_tool(tools.add_label)
    mcp.add_tool(tools.delete_page)
    # Semantic search (optional, requires indexer service)
    mcp.add_tool(tools.semantic_search)

    return mcp
