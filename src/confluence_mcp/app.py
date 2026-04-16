"""FastMCP server factory for Confluence MCP."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from . import tools
from .client import init_client
from .config import get_api_token, get_base_url, get_email


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    client = init_client(get_base_url(), get_email(), get_api_token())
    try:
        yield
    finally:
        await client.close()


def create_mcp_server() -> FastMCP:
    mcp = FastMCP(
        name="confluence",
        instructions=(
            "Confluence MCP provides read access to Confluence Cloud pages and spaces. "
            "Use search to find pages by text or CQL query, get_page to read full page content, "
            "get_page_outline to see a page's heading structure, "
            "get_page_section to read a specific section, "
            "get_page_by_title to find a page by name in a space, "
            "and list_spaces to discover available spaces."
        ),
        lifespan=lifespan,
    )

    mcp.add_tool(tools.search)
    mcp.add_tool(tools.get_page)
    mcp.add_tool(tools.get_page_outline)
    mcp.add_tool(tools.get_page_section)
    mcp.add_tool(tools.get_page_by_title)
    mcp.add_tool(tools.list_spaces)
    mcp.add_tool(tools.get_child_pages)

    return mcp
