"""MCP tool implementations for Confluence Cloud."""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Annotated

import httpx
from pydantic import Field

from .client import get_client
from .config import get_base_url, get_max_length
from .converter import extract_outline, extract_section, slice_content, storage_to_markdown

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CQL_INDICATORS = re.compile(
    r"""(?:^|\s)(?:
        type\s*=|space\s*=|space\.key\s*=|ancestor\s*=|
        label\s*=|creator\s*=|contributor\s*=|
        \bAND\b|\bOR\b|\bNOT\b|\bIN\b|
        ~|!=|>=|<=
    )""",
    re.VERBOSE | re.IGNORECASE,
)

_CACHE_MAX = 20
_page_cache: OrderedDict[str, tuple[dict, str]] = OrderedDict()


def _is_cql(query: str) -> bool:
    """Heuristic: does the query look like raw CQL?"""
    return bool(_CQL_INDICATORS.search(query))


def _escape_cql(text: str) -> str:
    """Escape double quotes for CQL string literals."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


async def _fetch_page_markdown(page_id: str) -> tuple[dict, str] | dict:
    """Fetch a page and convert to markdown, with LRU caching.

    Returns (page_dict, markdown) or an error dict.
    """
    if page_id in _page_cache:
        _page_cache.move_to_end(page_id)
        return _page_cache[page_id]

    client = get_client()
    try:
        page = await client.get_page(page_id, body_format="storage")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return {"error": "Page not found", "page_id": page_id}
        if exc.response.status_code == 401:
            return {"error": "Authentication failed — check API token and username"}
        return {"error": f"Failed to get page: {exc.response.status_code}", "page_id": page_id}

    storage_body = page.get("body", {}).get("storage", {}).get("value", "")
    markdown = storage_to_markdown(storage_body, base_url=get_base_url())

    _page_cache[page_id] = (page, markdown)
    if len(_page_cache) > _CACHE_MAX:
        _page_cache.popitem(last=False)

    return page, markdown


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def search(
    query: Annotated[
        str,
        Field(
            description=(
                "Search query. Plain text is auto-wrapped as a CQL text search. "
                'Raw CQL (e.g. \'space.key = "DEV" AND label = "api"\') is passed through directly.'
            )
        ),
    ],
    space_key: Annotated[
        str,
        Field(
            description="Restrict search to a space key (e.g. 'DEV'). Omit to search all spaces."
        ),
    ] = "",
    limit: Annotated[
        int,
        Field(description="Maximum results to return (default 10, max 50)"),
    ] = 10,
) -> dict:
    """Search Confluence pages using text or CQL."""
    client = get_client()
    limit = max(1, min(limit, 50))

    if _is_cql(query):
        cql = query
    else:
        cql = f'type = "page" AND text ~ "{_escape_cql(query)}"'
        if space_key:
            cql = f'space.key = "{_escape_cql(space_key)}" AND {cql}'

    try:
        data = await client.search_cql(cql, limit=limit)
    except httpx.HTTPStatusError as exc:
        return {"error": f"Search failed: {exc.response.status_code}", "cql": cql}

    results = []
    for item in data.get("results", []):
        content = item.get("content", item)
        space = content.get("space", {})
        version = content.get("version", {})

        # Excerpt can be at item level or nested; may contain HTML highlight markers
        excerpt = (
            item.get("excerpt")
            or item.get("resultExcerpt")
            or content.get("excerpt")
            or ""
        )
        if excerpt:
            excerpt = re.sub(r"<[^>]+>", "", excerpt).strip()

        results.append(
            {
                "id": str(content.get("id", "")),
                "title": content.get("title", ""),
                "space_key": space.get("key", ""),
                "space_name": space.get("name", ""),
                "last_modified": version.get("when", ""),
                "excerpt": excerpt,
            }
        )

    return {"cql": cql, "total_size": data.get("totalSize", len(results)), "results": results}


async def get_page(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID (numeric string)"),
    ],
    start_offset: Annotated[
        int,
        Field(
            description=(
                "Character offset to start reading from. "
                "Use this to continue reading a page that was truncated. Default 0."
            )
        ),
    ] = 0,
) -> dict:
    """Get a Confluence page by ID, returning its content as Markdown."""
    fetched = await _fetch_page_markdown(page_id)
    if isinstance(fetched, dict):
        return fetched
    page, full_markdown = fetched

    max_length = get_max_length()
    content, total_length, has_more = slice_content(full_markdown, max_length, start_offset)

    client = get_client()
    try:
        labels = await client.get_labels(page_id)
        label_names = [lb.get("name", "") for lb in labels]
    except httpx.HTTPStatusError:
        label_names = []

    try:
        ancestors = await client.get_ancestors(page_id)
        breadcrumbs = [
            {"id": str(a.get("id", "")), "title": a.get("title", "")}
            for a in ancestors
        ]
    except httpx.HTTPStatusError:
        breadcrumbs = []

    version = page.get("version", {})
    result: dict = {
        "id": str(page.get("id", "")),
        "title": page.get("title", ""),
        "space_id": page.get("spaceId", ""),
        "breadcrumbs": breadcrumbs,
        "content": content,
        "total_length": total_length,
        "labels": label_names,
        "last_modified": version.get("createdAt", ""),
        "last_modified_by": version.get("authorId", ""),
        "url": f"{get_base_url()}/wiki/pages/{page_id}",
    }

    if has_more:
        next_offset = start_offset + len(content)
        result["truncated"] = True
        result["next_offset"] = next_offset
        result["continuation_hint"] = (
            f"Content truncated. Call get_page(\"{page_id}\", start_offset={next_offset}) "
            f"to read more ({total_length - next_offset} characters remaining)."
        )

    return result


async def get_page_outline(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID (numeric string)"),
    ],
) -> dict:
    """Get the heading structure of a Confluence page as a table of contents."""
    fetched = await _fetch_page_markdown(page_id)
    if isinstance(fetched, dict):
        return fetched
    page, markdown = fetched

    sections = extract_outline(markdown)

    return {
        "id": str(page.get("id", "")),
        "title": page.get("title", ""),
        "sections": [
            {
                "level": sec.level,
                "title": sec.title,
                "line": sec.line_number,
            }
            for sec in sections
        ],
    }


async def get_page_section(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID (numeric string)"),
    ],
    heading: Annotated[
        str,
        Field(description="Heading text to extract (case-insensitive match)"),
    ],
    include_subsections: Annotated[
        bool,
        Field(
            description=(
                "If true (default), include all nested sub-headings. "
                "If false, return only the content directly under the heading, "
                "stopping at the next heading of any level."
            )
        ),
    ] = True,
    start_offset: Annotated[
        int,
        Field(
            description=(
                "Character offset within the section to start reading from. "
                "Use this to continue reading a section that was truncated. Default 0."
            )
        ),
    ] = 0,
) -> dict:
    """Get the content of a specific section from a Confluence page."""
    fetched = await _fetch_page_markdown(page_id)
    if isinstance(fetched, dict):
        return fetched
    page, markdown = fetched

    section_text = extract_section(markdown, heading, include_subsections)
    if section_text is None:
        available = [sec.title for sec in extract_outline(markdown)]
        return {
            "error": f"Section '{heading}' not found",
            "page_id": page_id,
            "available_sections": available,
        }

    max_length = get_max_length()
    content, total_length, has_more = slice_content(section_text, max_length, start_offset)

    result: dict = {
        "id": str(page.get("id", "")),
        "title": page.get("title", ""),
        "section": heading,
        "content": content,
        "total_length": total_length,
    }

    if has_more:
        next_offset = start_offset + len(content)
        result["truncated"] = True
        result["next_offset"] = next_offset
        result["continuation_hint"] = (
            f"Section truncated. Call get_page_section(\"{page_id}\", \"{heading}\", "
            f"start_offset={next_offset}) to read more "
            f"({total_length - next_offset} characters remaining)."
        )

    return result


async def get_page_by_title(
    title: Annotated[
        str,
        Field(description="Page title to search for"),
    ],
    space_key: Annotated[
        str,
        Field(description="Space key to search within (e.g. 'DEV')"),
    ],
) -> dict:
    """Find a Confluence page by title within a space. Returns page metadata (use get_page to read content)."""
    client = get_client()

    # Exact match first
    cql = f'type = "page" AND space.key = "{_escape_cql(space_key)}" AND title = "{_escape_cql(title)}"'
    try:
        data = await client.search_cql(cql, limit=5)
    except httpx.HTTPStatusError as exc:
        return {"error": f"Search failed: {exc.response.status_code}", "title": title, "space_key": space_key}

    results = data.get("results", [])

    # Fuzzy fallback
    if not results:
        cql = f'type = "page" AND space.key = "{_escape_cql(space_key)}" AND title ~ "{_escape_cql(title)}"'
        try:
            data = await client.search_cql(cql, limit=5)
        except httpx.HTTPStatusError as exc:
            return {"error": f"Search failed: {exc.response.status_code}", "title": title, "space_key": space_key}
        results = data.get("results", [])

    if not results:
        return {"error": "No page found", "title": title, "space_key": space_key}

    matches = []
    for item in results:
        content = item.get("content", item)
        space = content.get("space", {})
        version = content.get("version", {})
        matches.append(
            {
                "id": str(content.get("id", "")),
                "title": content.get("title", ""),
                "space_key": space.get("key", ""),
                "space_name": space.get("name", ""),
                "last_modified": version.get("when", ""),
            }
        )

    return {"results": matches}


async def list_spaces(
    limit: Annotated[
        int,
        Field(description="Maximum number of spaces to return (default 25, max 250)"),
    ] = 25,
) -> dict:
    """List available Confluence spaces."""
    client = get_client()
    limit = max(1, min(limit, 250))

    try:
        data = await client.list_spaces(limit=limit)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return {"error": "Authentication failed — check API token and username"}
        return {"error": f"Failed to list spaces: {exc.response.status_code}"}

    spaces = []
    for space in data.get("results", []):
        spaces.append(
            {
                "id": str(space.get("id", "")),
                "key": space.get("key", ""),
                "name": space.get("name", ""),
                "type": space.get("type", ""),
                "status": space.get("status", ""),
            }
        )

    return {"spaces": spaces}


async def get_space_pages(
    space_key: Annotated[
        str,
        Field(description="Space key (e.g. 'DEV'). Use list_spaces to find available keys."),
    ],
    limit: Annotated[
        int,
        Field(description="Maximum number of pages to return (default 25)"),
    ] = 25,
) -> dict:
    """Get the top-level pages in a Confluence space. Use get_child_pages to navigate deeper."""
    client = get_client()
    limit = max(1, min(limit, 100))

    try:
        space = await client.get_space_by_key(space_key)
    except httpx.HTTPStatusError as exc:
        return {"error": f"Failed to look up space: {exc.response.status_code}", "space_key": space_key}

    if space is None:
        return {"error": f"Space '{space_key}' not found", "space_key": space_key}

    space_id = str(space.get("id", ""))
    try:
        data = await client.get_space_pages(space_id, depth="root", limit=limit)
    except httpx.HTTPStatusError as exc:
        return {"error": f"Failed to get pages: {exc.response.status_code}", "space_key": space_key}

    pages = []
    for pg in data.get("results", []):
        pages.append(
            {
                "id": str(pg.get("id", "")),
                "title": pg.get("title", ""),
                "status": pg.get("status", ""),
            }
        )

    return {
        "space_key": space_key,
        "space_name": space.get("name", ""),
        "pages": pages,
    }


async def get_child_pages(
    page_id: Annotated[
        str,
        Field(description="Parent page ID to get children of"),
    ],
    limit: Annotated[
        int,
        Field(description="Maximum number of children to return (default 25)"),
    ] = 25,
) -> dict:
    """Get child pages of a Confluence page."""
    client = get_client()
    limit = max(1, min(limit, 100))

    try:
        data = await client.get_child_pages(page_id, limit=limit)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return {"error": "Page not found", "page_id": page_id}
        return {"error": f"Failed to get children: {exc.response.status_code}", "page_id": page_id}

    children = []
    for child in data.get("results", []):
        children.append(
            {
                "id": str(child.get("id", "")),
                "title": child.get("title", ""),
                "status": child.get("status", ""),
            }
        )

    return {"parent_id": page_id, "children": children}
