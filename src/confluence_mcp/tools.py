"""MCP tool implementations for Confluence Cloud."""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Annotated

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import Field

from .client import get_client
from .config import get_base_url, get_max_length
from .converter import extract_outline, extract_section, slice_content, storage_to_markdown
from .indexer_client import get_indexer_client

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
_CACHE_TTL = 300  # 5 minutes
_page_cache: OrderedDict[str, tuple[dict, str, float]] = OrderedDict()

_HEADING_TAG_RE = re.compile(r"^h[1-6]$")


def _is_cql(query: str) -> bool:
    """Heuristic: does the query look like raw CQL?"""
    return bool(_CQL_INDICATORS.search(query))


def _escape_cql(text: str) -> str:
    """Escape double quotes for CQL string literals."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


async def _fetch_page_markdown(page_id: str) -> tuple[dict, str] | dict:
    """Fetch a page and convert to markdown, with LRU caching (5 min TTL).

    Returns (page_dict, markdown) or an error dict.
    """
    if page_id in _page_cache:
        page, markdown, cached_at = _page_cache[page_id]
        if time.monotonic() - cached_at < _CACHE_TTL:
            _page_cache.move_to_end(page_id)
            return page, markdown
        del _page_cache[page_id]

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

    _page_cache[page_id] = (page, markdown, time.monotonic())
    if len(_page_cache) > _CACHE_MAX:
        _page_cache.popitem(last=False)

    return page, markdown


async def _fetch_page_xhtml(page_id: str) -> tuple[dict, str] | dict:
    """Fetch page XHTML fresh (uncached) for editing workflows."""
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
    return page, storage_body


# -- XHTML section helpers --------------------------------------------------


def _find_xhtml_heading(soup: BeautifulSoup, heading_text: str) -> Tag | None:
    for tag in soup.find_all(_HEADING_TAG_RE):
        if tag.get_text(strip=True).lower() == heading_text.strip().lower():
            return tag
    return None


def _collect_section_siblings(
    heading_tag: Tag, include_subsections: bool = True
) -> list:
    level = int(heading_tag.name[1])
    elements: list = []
    for sibling in heading_tag.next_siblings:
        if isinstance(sibling, Tag) and _HEADING_TAG_RE.match(sibling.name):
            sibling_level = int(sibling.name[1])
            if include_subsections:
                if sibling_level <= level:
                    break
            else:
                break
        elements.append(sibling)
    return elements


def _extract_xhtml_section(
    xhtml: str, heading_text: str, include_subsections: bool = True
) -> str | None:
    soup = BeautifulSoup(xhtml, "html.parser")
    heading = _find_xhtml_heading(soup, heading_text)
    if heading is None:
        return None
    siblings = _collect_section_siblings(heading, include_subsections)
    parts = [str(heading)]
    for el in siblings:
        parts.append(str(el))
    return "".join(parts)


def _list_xhtml_headings(xhtml: str) -> list[str]:
    soup = BeautifulSoup(xhtml, "html.parser")
    return [tag.get_text(strip=True) for tag in soup.find_all(_HEADING_TAG_RE)]


def _replace_section_content(
    xhtml: str, heading_text: str, new_content: str
) -> str | None:
    soup = BeautifulSoup(xhtml, "html.parser")
    heading = _find_xhtml_heading(soup, heading_text)
    if heading is None:
        return None
    for el in _collect_section_siblings(heading):
        el.extract()
    new_elements = list(BeautifulSoup(new_content, "html.parser").children)
    for el in reversed(new_elements):
        heading.insert_after(el)
    return str(soup)


def _append_to_section_content(
    xhtml: str, heading_text: str, new_content: str
) -> str | None:
    soup = BeautifulSoup(xhtml, "html.parser")
    heading = _find_xhtml_heading(soup, heading_text)
    if heading is None:
        return None
    siblings = _collect_section_siblings(heading)
    insert_after = siblings[-1] if siblings else heading
    new_elements = list(BeautifulSoup(new_content, "html.parser").children)
    for el in reversed(new_elements):
        insert_after.insert_after(el)
    return str(soup)


async def _push_page_update(
    page_id: str,
    title: str,
    xhtml: str,
    version_number: int,
    version_message: str | None = None,
) -> dict:
    """Push updated XHTML content and evict cache."""
    client = get_client()
    try:
        page = await client.update_page(
            page_id, title, xhtml, version_number,
            version_message=version_message,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return {
                "error": "Version conflict — the page was modified since you last read it. "
                "Re-fetch the page and try again.",
                "page_id": page_id,
            }
        return {
            "error": f"Failed to update page: {exc.response.status_code}",
            "detail": exc.response.text,
        }
    _page_cache.pop(page_id, None)
    version = page.get("version", {})
    return {
        "id": str(page.get("id", "")),
        "title": page.get("title", ""),
        "version": version.get("number"),
        "url": f"{get_base_url()}/wiki/pages/{page_id}",
    }


# ---------------------------------------------------------------------------
# Read tools
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
    format: Annotated[
        str,
        Field(
            description=(
                "Output format: 'md' for Markdown (default) or 'xhtml' for raw "
                "Confluence storage format. Use 'xhtml' when you plan to edit the page."
            )
        ),
    ] = "md",
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
    """Get a Confluence page by ID, returning its content as Markdown or raw XHTML."""
    max_length = get_max_length()

    if format == "xhtml":
        fetched = await _fetch_page_xhtml(page_id)
        if isinstance(fetched, dict):
            return fetched
        page, xhtml = fetched

        content, total_length, has_more = slice_content(xhtml, max_length, start_offset)
        version = page.get("version", {})
        result: dict = {
            "id": str(page.get("id", "")),
            "title": page.get("title", ""),
            "format": "xhtml",
            "version": version.get("number"),
            "content": content,
            "total_length": total_length,
            "url": f"{get_base_url()}/wiki/pages/{page_id}",
        }
        if has_more:
            next_offset = start_offset + len(content)
            result["truncated"] = True
            result["next_offset"] = next_offset
            result["continuation_hint"] = (
                f'Content truncated. Call get_page("{page_id}", format="xhtml", start_offset={next_offset}) '
                f"to read more ({total_length - next_offset} characters remaining)."
            )
        return result

    # Markdown format (default)
    fetched = await _fetch_page_markdown(page_id)
    if isinstance(fetched, dict):
        return fetched
    page, full_markdown = fetched

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
    result = {
        "id": str(page.get("id", "")),
        "title": page.get("title", ""),
        "space_id": page.get("spaceId", ""),
        "version": version.get("number"),
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
            f'Content truncated. Call get_page("{page_id}", start_offset={next_offset}) '
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
    format: Annotated[
        str,
        Field(
            description=(
                "Output format: 'md' for Markdown (default) or 'xhtml' for raw "
                "Confluence storage format. Use 'xhtml' when you plan to edit the section."
            )
        ),
    ] = "md",
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
    max_length = get_max_length()

    if format == "xhtml":
        fetched = await _fetch_page_xhtml(page_id)
        if isinstance(fetched, dict):
            return fetched
        page, xhtml = fetched

        section_xhtml = _extract_xhtml_section(xhtml, heading, include_subsections)
        if section_xhtml is None:
            return {
                "error": f"Section '{heading}' not found",
                "page_id": page_id,
                "available_sections": _list_xhtml_headings(xhtml),
            }

        content, total_length, has_more = slice_content(section_xhtml, max_length, start_offset)
        version = page.get("version", {})
        result: dict = {
            "id": str(page.get("id", "")),
            "title": page.get("title", ""),
            "section": heading,
            "format": "xhtml",
            "version": version.get("number"),
            "content": content,
            "total_length": total_length,
        }
        if has_more:
            next_offset = start_offset + len(content)
            result["truncated"] = True
            result["next_offset"] = next_offset
            result["continuation_hint"] = (
                f'Section truncated. Call get_page_section("{page_id}", "{heading}", '
                f'format="xhtml", start_offset={next_offset}) to read more '
                f"({total_length - next_offset} characters remaining)."
            )
        return result

    # Markdown format (default)
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

    content, total_length, has_more = slice_content(section_text, max_length, start_offset)

    result = {
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
            f'Section truncated. Call get_page_section("{page_id}", "{heading}", '
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

    cql = f'type = "page" AND space.key = "{_escape_cql(space_key)}" AND title = "{_escape_cql(title)}"'
    try:
        data = await client.search_cql(cql, limit=5)
    except httpx.HTTPStatusError as exc:
        return {"error": f"Search failed: {exc.response.status_code}", "title": title, "space_key": space_key}

    results = data.get("results", [])

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


# ---------------------------------------------------------------------------
# Browse tools
# ---------------------------------------------------------------------------


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
    fallback = False
    try:
        data = await client.get_space_pages(space_id, depth="root", limit=limit)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code >= 500:
            fallback = True
        else:
            return {"error": f"Failed to get pages: {exc.response.status_code}", "space_key": space_key}

    if fallback:
        cql = f'space.key = "{_escape_cql(space_key)}" AND type = "page" ORDER BY title ASC'
        try:
            data = await client.search_cql(cql, limit=limit)
        except httpx.HTTPStatusError as exc:
            return {"error": f"Failed to get pages: {exc.response.status_code}", "space_key": space_key}
        pages = []
        for item in data.get("results", []):
            content = item.get("content", item)
            pages.append(
                {
                    "id": str(content.get("id", "")),
                    "title": content.get("title", ""),
                    "status": content.get("status", "current"),
                }
            )
        result: dict = {
            "space_key": space_key,
            "space_name": space.get("name", ""),
            "pages": pages,
            "note": "Space too large for tree listing; showing pages by title. Use search to find specific pages.",
        }
        return result

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


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


async def create_page(
    space_key: Annotated[
        str,
        Field(description="Space key to create the page in (e.g. 'DEV')"),
    ],
    title: Annotated[
        str,
        Field(description="Page title"),
    ],
    content: Annotated[
        str,
        Field(
            description=(
                "Page body in Confluence storage format (XHTML). "
                "Example: '<p>Hello <strong>world</strong></p>'"
            )
        ),
    ],
    parent_id: Annotated[
        str,
        Field(description="Parent page ID. If omitted, the page is created at the space root."),
    ] = "",
) -> dict:
    """Create a new Confluence page using storage format (XHTML)."""
    client = get_client()

    try:
        space = await client.get_space_by_key(space_key)
    except httpx.HTTPStatusError as exc:
        return {"error": f"Failed to look up space: {exc.response.status_code}", "space_key": space_key}

    if space is None:
        return {"error": f"Space '{space_key}' not found", "space_key": space_key}

    space_id = str(space.get("id", ""))

    try:
        page = await client.create_page(
            space_id, title, content, parent_id=parent_id or None
        )
    except httpx.HTTPStatusError as exc:
        return {"error": f"Failed to create page: {exc.response.status_code}", "detail": exc.response.text}

    page_id = str(page.get("id", ""))
    return {
        "id": page_id,
        "title": page.get("title", ""),
        "url": f"{get_base_url()}/wiki/pages/{page_id}",
        "space_key": space_key,
    }


async def update_page(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID to update"),
    ],
    title: Annotated[
        str,
        Field(description="Page title (required even if unchanged)"),
    ],
    content: Annotated[
        str,
        Field(
            description=(
                "New page body in Confluence storage format (XHTML). "
                "Use get_page(format='xhtml') to fetch the current XHTML, edit it, and pass it here."
            )
        ),
    ],
    version_number: Annotated[
        int,
        Field(
            description=(
                "New version number (current version + 1). "
                "Get the current version from the get_page response."
            )
        ),
    ],
    version_message: Annotated[
        str,
        Field(description="Optional message describing this edit"),
    ] = "",
) -> dict:
    """Update an existing Confluence page using storage format (XHTML)."""
    return await _push_page_update(
        page_id, title, content, version_number,
        version_message=version_message or None,
    )


async def update_page_section(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID to update"),
    ],
    heading: Annotated[
        str,
        Field(description="Heading text identifying the section to replace (case-insensitive)"),
    ],
    content: Annotated[
        str,
        Field(
            description=(
                "New XHTML content for the section body (everything under the heading). "
                "The heading itself is preserved; do not include it in the content. "
                "Sub-headings in the content are allowed."
            )
        ),
    ],
    version_message: Annotated[
        str,
        Field(description="Optional message describing this edit"),
    ] = "",
) -> dict:
    """Replace the content of a specific section, identified by heading. Handles versioning automatically."""
    fetched = await _fetch_page_xhtml(page_id)
    if isinstance(fetched, dict):
        return fetched
    page, xhtml = fetched

    new_xhtml = _replace_section_content(xhtml, heading, content)
    if new_xhtml is None:
        return {
            "error": f"Section '{heading}' not found",
            "page_id": page_id,
            "available_sections": _list_xhtml_headings(xhtml),
        }

    version = page.get("version", {})
    return await _push_page_update(
        page_id,
        page.get("title", ""),
        new_xhtml,
        version.get("number", 0) + 1,
        version_message=version_message or None,
    )


async def append_to_page(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID to append to"),
    ],
    content: Annotated[
        str,
        Field(
            description=(
                "XHTML content to append at the end of the page. "
                "No need to read the page first."
            )
        ),
    ],
    version_message: Annotated[
        str,
        Field(description="Optional message describing this edit"),
    ] = "",
) -> dict:
    """Append content at the end of a Confluence page. Handles versioning automatically."""
    fetched = await _fetch_page_xhtml(page_id)
    if isinstance(fetched, dict):
        return fetched
    page, xhtml = fetched

    new_xhtml = xhtml + content

    version = page.get("version", {})
    return await _push_page_update(
        page_id,
        page.get("title", ""),
        new_xhtml,
        version.get("number", 0) + 1,
        version_message=version_message or None,
    )


async def append_to_section(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID to update"),
    ],
    heading: Annotated[
        str,
        Field(description="Heading text identifying the section to append to (case-insensitive)"),
    ],
    content: Annotated[
        str,
        Field(
            description=(
                "XHTML content to append at the end of the section, "
                "before the next same-or-higher-level heading."
            )
        ),
    ],
    version_message: Annotated[
        str,
        Field(description="Optional message describing this edit"),
    ] = "",
) -> dict:
    """Append content at the end of a specific section. Handles versioning automatically."""
    fetched = await _fetch_page_xhtml(page_id)
    if isinstance(fetched, dict):
        return fetched
    page, xhtml = fetched

    new_xhtml = _append_to_section_content(xhtml, heading, content)
    if new_xhtml is None:
        return {
            "error": f"Section '{heading}' not found",
            "page_id": page_id,
            "available_sections": _list_xhtml_headings(xhtml),
        }

    version = page.get("version", {})
    return await _push_page_update(
        page_id,
        page.get("title", ""),
        new_xhtml,
        version.get("number", 0) + 1,
        version_message=version_message or None,
    )


# ---------------------------------------------------------------------------
# Comments, labels, delete
# ---------------------------------------------------------------------------


async def get_comments(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID"),
    ],
    limit: Annotated[
        int,
        Field(description="Maximum number of comments to return (default 25)"),
    ] = 25,
) -> dict:
    """Get comments on a Confluence page."""
    client = get_client()
    limit = max(1, min(limit, 100))

    try:
        data = await client.get_comments(page_id, limit=limit)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return {"error": "Page not found", "page_id": page_id}
        return {"error": f"Failed to get comments: {exc.response.status_code}", "page_id": page_id}

    comments = []
    for comment in data.get("results", []):
        body_html = comment.get("body", {}).get("storage", {}).get("value", "")
        body_text = storage_to_markdown(body_html) if body_html else ""
        version = comment.get("version", {})
        comments.append(
            {
                "id": str(comment.get("id", "")),
                "author_id": version.get("authorId", ""),
                "created": version.get("createdAt", ""),
                "body": body_text,
            }
        )

    return {"page_id": page_id, "comments": comments}


async def add_comment(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID to comment on"),
    ],
    content: Annotated[
        str,
        Field(
            description=(
                "Comment body in Confluence storage format (XHTML). "
                "Example: '<p>Looks good!</p>'"
            )
        ),
    ],
) -> dict:
    """Add a comment to a Confluence page."""
    client = get_client()

    try:
        comment = await client.create_comment(page_id, content)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return {"error": "Page not found", "page_id": page_id}
        return {"error": f"Failed to add comment: {exc.response.status_code}", "detail": exc.response.text}

    return {
        "id": str(comment.get("id", "")),
        "page_id": page_id,
    }


async def add_label(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID to label"),
    ],
    label: Annotated[
        str,
        Field(description="Label name to add (e.g. 'documentation')"),
    ],
) -> dict:
    """Add a label to a Confluence page."""
    client = get_client()

    try:
        await client.add_label(page_id, label)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return {"error": "Page not found", "page_id": page_id}
        return {"error": f"Failed to add label: {exc.response.status_code}", "detail": exc.response.text}

    return {"page_id": page_id, "label": label}


async def delete_page(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID to delete"),
    ],
) -> dict:
    """Delete a Confluence page. This action cannot be undone."""
    client = get_client()

    try:
        await client.delete_page(page_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return {"error": "Page not found", "page_id": page_id}
        return {"error": f"Failed to delete page: {exc.response.status_code}", "detail": exc.response.text}

    _page_cache.pop(page_id, None)
    return {"deleted": True, "page_id": page_id}


# ---------------------------------------------------------------------------
# Semantic search (via indexer service)
# ---------------------------------------------------------------------------


async def semantic_search(
    query: Annotated[
        str,
        Field(description="Natural language search query"),
    ],
    scope: Annotated[
        str,
        Field(description="Restrict search to a specific index scope. Omit to search all."),
    ] = "",
    perspective: Annotated[
        str,
        Field(
            description=(
                "Search perspective (e.g. 'general', 'technical', 'procedural'). "
                "Omit for best results across all perspectives."
            )
        ),
    ] = "",
    limit: Annotated[
        int,
        Field(description="Maximum results to return (default 10, max 50)"),
    ] = 10,
) -> dict:
    """Semantic search across indexed Confluence pages using vector embeddings.

    Returns ranked results with relevance scores, page titles, headings, and content snippets.
    Requires a running indexer service (set CONFLUENCE_INDEXER_URL).
    """
    client = get_indexer_client()
    if client is None:
        return {
            "error": "Semantic search not available — CONFLUENCE_INDEXER_URL not configured",
        }

    limit = max(1, min(limit, 50))
    try:
        data = await client.search(
            query,
            scope=scope or None,
            perspective=perspective or None,
            limit=limit,
        )
    except Exception as exc:
        return {"error": f"Semantic search failed: {exc}"}

    results = []
    base_url = get_base_url()
    for item in data.get("results", []):
        results.append({
            "score": round(item.get("score", 0), 4),
            "page_id": item.get("page_id", ""),
            "title": item.get("page_title", ""),
            "space_key": item.get("space_key", ""),
            "heading": item.get("heading", ""),
            "snippet": item.get("snippet", ""),
            "scope": item.get("scope", ""),
            "url": f"{base_url}/wiki/pages/{item.get('page_id', '')}",
        })

    return {"query": query, "results": results}
