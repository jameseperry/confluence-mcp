"""MCP tool implementations for Confluence Cloud."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from typing import Annotated

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import Field

from .client import get_client
from .config import get_base_url, get_embedding_config, get_large_page_threshold, get_max_length
from .converter import extract_outline, extract_section, slice_content, storage_to_markdown

logger = logging.getLogger(__name__)

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


async def _fetch_page_markdown(page_id: str, no_cache: bool = False) -> tuple[dict, str] | dict:
    """Fetch a page and convert to markdown, with LRU caching (5 min TTL).

    Returns (page_dict, markdown) or an error dict.
    """
    if not no_cache and page_id in _page_cache:
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

    # Trigger on-demand indexing (non-blocking)
    _schedule_index(page_id, storage_body, page)

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


# ---------------------------------------------------------------------------
# On-demand indexing
# ---------------------------------------------------------------------------

_index_tasks: set[asyncio.Task] = set()


def _schedule_index(page_id: str, storage_html: str, page_data: dict) -> None:
    """Schedule on-demand indexing as a background task (non-blocking)."""
    if get_embedding_config() is None:
        return
    task = asyncio.create_task(_maybe_index_page(page_id, storage_html, page_data))
    _index_tasks.add(task)
    task.add_done_callback(_index_tasks.discard)


_space_key_cache: dict[str, str] = {}


async def _resolve_space_key(client, space_id: str) -> str:
    """Resolve a numeric space ID to a space key, with caching."""
    if space_id in _space_key_cache:
        return _space_key_cache[space_id]
    space = await client.get_space_by_id(space_id)
    if space:
        key = space.get("key", space_id)
        _space_key_cache[space_id] = key
        return key
    return space_id


async def _maybe_index_page(page_id: str, storage_html: str, page_data: dict) -> None:
    """Opportunistically index a fetched page."""
    try:
        from .config import get_max_chunk_chars
        from .indexer.db import get_conn
        from .indexer.pipeline import index_page_from_fetch

        conn = get_conn()
        client = get_client()

        title = page_data.get("title", "")
        # v2 API returns numeric spaceId — resolve to space key
        space_id = page_data.get("spaceId", "")
        space_key = await _resolve_space_key(client, space_id) if space_id else ""
        version = page_data.get("version", {}).get("number", 1)

        base_url = ""
        try:
            base_url = get_base_url()
        except RuntimeError:
            pass

        await index_page_from_fetch(
            conn, client, page_id, title, space_key, version,
            storage_html, base_url, get_max_chunk_chars(),
        )
    except Exception:
        logger.debug("On-demand indexing failed for page %s", page_id, exc_info=True)


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
    xhtml: str, heading_text: str, new_content: str,
    new_heading_text: str | None = None,
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
    if new_heading_text is not None:
        heading.clear()
        heading.string = new_heading_text
    return str(soup)


def _delete_section_content(
    xhtml: str, heading_text: str, include_subsections: bool = True,
) -> str | None:
    soup = BeautifulSoup(xhtml, "html.parser")
    heading = _find_xhtml_heading(soup, heading_text)
    if heading is None:
        return None
    for el in _collect_section_siblings(heading, include_subsections=include_subsections):
        el.extract()
    heading.extract()
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


def _move_section(
    xhtml: str,
    heading_text: str,
    after: str | None = None,
    before: str | None = None,
    new_heading_text: str | None = None,
    new_level: int | None = None,
) -> tuple[str | None, str | None]:
    """Move a section (with subsections) to a new position.

    Returns (new_xhtml, None) on success or (None, error_message) on failure.
    """
    soup = BeautifulSoup(xhtml, "html.parser")

    # Find source heading
    source_heading = _find_xhtml_heading(soup, heading_text)
    if source_heading is None:
        return None, f"Source section '{heading_text}' not found"

    source_level = int(source_heading.name[1])

    # Collect source section: heading + content + subsections
    source_siblings = _collect_section_siblings(source_heading, include_subsections=True)
    source_elements = [source_heading] + source_siblings

    # Find target heading
    target_text = after or before
    if target_text is None:
        return None, "Specify either 'after' or 'before'"
    target_heading = _find_xhtml_heading(soup, target_text)
    if target_heading is None:
        return None, f"Target section '{target_text}' not found"

    # Target must not be within the source section
    if target_heading in source_elements:
        return None, "Target heading is inside the section being moved"

    # Extract source elements from DOM
    for el in source_elements:
        el.extract()

    # Adjust heading levels if new_level specified (preserve relative depth)
    if new_level is not None:
        delta = new_level - source_level
        if delta != 0:
            for el in source_elements:
                if isinstance(el, Tag) and _HEADING_TAG_RE.match(el.name):
                    old_level = int(el.name[1])
                    el.name = f"h{max(1, min(6, old_level + delta))}"

    # Rename heading if specified
    if new_heading_text is not None:
        source_heading.clear()
        source_heading.string = new_heading_text

    # Insert at target position
    if after:
        # Place after the target section's last element
        target_siblings = _collect_section_siblings(target_heading, include_subsections=True)
        insert_point = target_siblings[-1] if target_siblings else target_heading
        for el in reversed(source_elements):
            insert_point.insert_after(el)
    else:
        # Place before the target heading (forward order — each insert_before
        # shifts earlier elements left, so sequential order is correct)
        for el in source_elements:
            target_heading.insert_before(el)

    return str(soup), None


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
    allow_large: Annotated[
        bool,
        Field(
            description=(
                "If false (default), pages larger than ~10KB return an outline "
                "instead of full content, with a hint to use get_page_section. "
                "Set to true to force returning full content regardless of size."
            )
        ),
    ] = False,
    no_cache: Annotated[
        bool,
        Field(
            description=(
                "Bypass the page cache and fetch fresh content from Confluence. "
                "Use this after external edits (edits made outside this MCP server)."
            )
        ),
    ] = False,
) -> dict:
    """Get a Confluence page by ID, returning its content as Markdown or raw XHTML."""
    max_length = get_max_length()
    large_threshold = get_large_page_threshold()

    if no_cache:
        _page_cache.pop(page_id, None)

    if format == "xhtml":
        fetched = await _fetch_page_xhtml(page_id)
        if isinstance(fetched, dict):
            return fetched
        page, xhtml = fetched

        # Size gate: return outline instead of full content for large pages
        if not allow_large and start_offset == 0 and len(xhtml) > large_threshold:
            version = page.get("version", {})
            headings = _list_xhtml_headings(xhtml)
            size_kb = len(xhtml) / 1024
            return {
                "id": str(page.get("id", "")),
                "title": page.get("title", ""),
                "format": "xhtml",
                "version": version.get("number"),
                "total_length": len(xhtml),
                "large_page": True,
                "sections": headings,
                "url": f"{get_base_url()}/wiki/pages/{page_id}",
                "hint": (
                    f"Page is {size_kb:.0f}KB ({len(xhtml)} chars). "
                    f"Use get_page_section(\"{page_id}\", heading=\"...\", format=\"xhtml\") "
                    f"to read specific sections, or call get_page with allow_large=true "
                    f"to retrieve the full content."
                ),
            }

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
    fetched = await _fetch_page_markdown(page_id, no_cache=no_cache)
    if isinstance(fetched, dict):
        return fetched
    page, full_markdown = fetched

    # Size gate: return outline instead of full content for large pages
    if not allow_large and start_offset == 0 and len(full_markdown) > large_threshold:
        sections = extract_outline(full_markdown)
        version = page.get("version", {})
        size_kb = len(full_markdown) / 1024
        return {
            "id": str(page.get("id", "")),
            "title": page.get("title", ""),
            "version": version.get("number"),
            "total_length": len(full_markdown),
            "large_page": True,
            "sections": [
                {"level": sec.level, "title": sec.title, "line": sec.line_number}
                for sec in sections
            ],
            "url": f"{get_base_url()}/wiki/pages/{page_id}",
            "hint": (
                f"Page is {size_kb:.0f}KB ({len(full_markdown)} chars). "
                f"Use get_page_section(\"{page_id}\", heading=\"...\") to read specific "
                f"sections, or call get_page with allow_large=true to retrieve the full content."
            ),
        }

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
    no_cache: Annotated[
        bool,
        Field(
            description=(
                "Bypass the page cache and fetch fresh content from Confluence. "
                "Use this after external edits (edits made outside this MCP server)."
            )
        ),
    ] = False,
) -> dict:
    """Get the heading structure of a Confluence page as a table of contents."""
    fetched = await _fetch_page_markdown(page_id, no_cache=no_cache)
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
    no_cache: Annotated[
        bool,
        Field(
            description=(
                "Bypass the page cache and fetch fresh content from Confluence. "
                "Use this after external edits (edits made outside this MCP server)."
            )
        ),
    ] = False,
) -> dict:
    """Get the content of a specific section from a Confluence page."""
    max_length = get_max_length()

    if no_cache:
        _page_cache.pop(page_id, None)

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
    fetched = await _fetch_page_markdown(page_id, no_cache=no_cache)
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
    content: Annotated[
        str,
        Field(
            description=(
                "New page body in Confluence storage format (XHTML). "
                "Use get_page(format='xhtml') to fetch the current XHTML, edit it, and pass it here."
            )
        ),
    ],
    title: Annotated[
        str,
        Field(
            description="New page title. If omitted, the current title is kept."
        ),
    ] = "",
    version_message: Annotated[
        str,
        Field(description="Optional message describing this edit"),
    ] = "",
) -> dict:
    """Update an existing Confluence page using storage format (XHTML). Handles versioning automatically."""
    fetched = await _fetch_page_xhtml(page_id)
    if isinstance(fetched, dict):
        return fetched
    page, _ = fetched

    version = page.get("version", {})
    return await _push_page_update(
        page_id,
        title or page.get("title", ""),
        content,
        version.get("number", 0) + 1,
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
    new_heading: Annotated[
        str,
        Field(
            description=(
                "Optional new heading text to rename the section. "
                "The heading level (h1-h6) is preserved; only the text changes."
            )
        ),
    ] = "",
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

    new_xhtml = _replace_section_content(
        xhtml, heading, content,
        new_heading_text=new_heading or None,
    )
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


async def move_section(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID to update"),
    ],
    heading: Annotated[
        str,
        Field(description="Heading text identifying the section to move (case-insensitive)"),
    ],
    after: Annotated[
        str,
        Field(
            description=(
                "Place the section after this heading's section (including its subsections). "
                "Mutually exclusive with 'before'."
            )
        ),
    ] = "",
    before: Annotated[
        str,
        Field(
            description=(
                "Place the section before this heading. "
                "Mutually exclusive with 'after'."
            )
        ),
    ] = "",
    new_heading: Annotated[
        str,
        Field(description="Optional new heading text to rename the section."),
    ] = "",
    new_level: Annotated[
        int,
        Field(
            description=(
                "Optional new heading level (1-6). Child headings shift by the same "
                "delta to preserve relative depth (e.g. H2→H1 makes H3 children become H2)."
            )
        ),
    ] = 0,
    version_message: Annotated[
        str,
        Field(description="Optional message describing this edit"),
    ] = "",
) -> dict:
    """Move a section (with all subsections) to a new position on the page. Handles versioning automatically."""
    has_after = bool(after)
    has_before = bool(before)
    if has_after == has_before:
        return {"error": "Specify exactly one of 'after' or 'before', not both or neither."}

    fetched = await _fetch_page_xhtml(page_id)
    if isinstance(fetched, dict):
        return fetched
    page, xhtml = fetched

    new_xhtml, error = _move_section(
        xhtml,
        heading,
        after=after or None,
        before=before or None,
        new_heading_text=new_heading or None,
        new_level=new_level if new_level >= 1 else None,
    )
    if new_xhtml is None:
        result: dict = {
            "error": error,
            "page_id": page_id,
            "available_sections": _list_xhtml_headings(xhtml),
        }
        return result

    version = page.get("version", {})
    return await _push_page_update(
        page_id,
        page.get("title", ""),
        new_xhtml,
        version.get("number", 0) + 1,
        version_message=version_message or None,
    )


async def delete_section(
    page_id: Annotated[
        str,
        Field(description="The Confluence page ID to update"),
    ],
    heading: Annotated[
        str,
        Field(description="Heading text identifying the section to delete (case-insensitive)"),
    ],
    include_subsections: Annotated[
        bool,
        Field(
            description=(
                "If true (default), delete the heading and all nested sub-headings. "
                "If false, delete only the content directly under the heading, "
                "preserving child sections which get promoted in place."
            )
        ),
    ] = True,
    version_message: Annotated[
        str,
        Field(description="Optional message describing this edit"),
    ] = "",
) -> dict:
    """Delete a section (heading + body) from a Confluence page. Handles versioning automatically."""
    fetched = await _fetch_page_xhtml(page_id)
    if isinstance(fetched, dict):
        return fetched
    page, xhtml = fetched

    new_xhtml = _delete_section_content(xhtml, heading, include_subsections=include_subsections)
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
# Semantic search (integrated indexer)
# ---------------------------------------------------------------------------


async def semantic_search(
    query: Annotated[
        str,
        Field(description="Natural language search query"),
    ],
    perspective: Annotated[
        str,
        Field(
            description=(
                "Search perspective (e.g. 'technical', 'project'). "
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
    Requires a configured embedding service (set EMBEDDING_API_URL).
    """
    if get_embedding_config() is None:
        return {
            "error": "Semantic search not available — set EMBEDDING_API_URL to enable",
        }

    limit = max(1, min(limit, 50))
    try:
        from .indexer.db import get_conn
        from .indexer.search import semantic_search as _search

        conn = get_conn()
        results = _search(
            conn,
            query,
            perspective=perspective or None,
            limit=limit,
        )
    except Exception as exc:
        return {"error": f"Semantic search failed: {exc}"}

    base_url = get_base_url()
    for r in results:
        r["url"] = f"{base_url}/wiki/pages/{r.get('page_id', '')}"

    return {"query": query, "results": results}


# ---------------------------------------------------------------------------
# Perspective management
# ---------------------------------------------------------------------------


def _require_indexer() -> dict | None:
    """Returns an error dict if the indexer is not configured, else None."""
    if get_embedding_config() is None:
        return {"error": "Indexer not configured — set EMBEDDING_API_URL to enable"}
    return None


async def list_perspectives() -> dict:
    """Returns current perspectives with names and instructions."""
    if err := _require_indexer():
        return err
    try:
        from .indexer.db import get_conn, get_perspectives as _get_perspectives

        conn = get_conn()
        perspectives = _get_perspectives(conn)
        return {"perspectives": perspectives}
    except Exception as exc:
        return {"error": f"Failed to list perspectives: {exc}"}


async def add_perspective(
    name: Annotated[
        str,
        Field(description="Name for the new perspective"),
    ],
    instruction: Annotated[
        str,
        Field(description="Instruction describing what this perspective focuses on"),
    ],
) -> dict:
    """Adds a new embedding perspective. Existing chunks are lazily re-embedded on next page access."""
    if err := _require_indexer():
        return err
    embed_config = get_embedding_config()
    try:
        from .indexer.db import add_perspective as _add_perspective, get_conn

        conn = get_conn()
        result = _add_perspective(conn, name, instruction, embed_config.dimensions)
        return {"added": True, **result}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Failed to add perspective: {exc}"}


async def remove_perspective(
    name: Annotated[
        str,
        Field(description="Name of the perspective to remove"),
    ],
) -> dict:
    """Removes a perspective and drops its vector table."""
    if err := _require_indexer():
        return err
    try:
        from .indexer.db import get_conn, remove_perspective as _remove_perspective

        conn = get_conn()
        removed = _remove_perspective(conn, name)
        if removed:
            return {"removed": True, "name": name}
        return {"error": f"Perspective '{name}' not found"}
    except Exception as exc:
        return {"error": f"Failed to remove perspective: {exc}"}


# ---------------------------------------------------------------------------
# Index status and scope management
# ---------------------------------------------------------------------------


async def index_status() -> dict:
    """Show the current state of the documentation index.

    Reports per-KB indexed files, chunks, stale counts, and perspectives.
    """
    if err := _require_indexer():
        return err
    try:
        from .indexer.db import get_conn
        from .indexer.search import get_index_status as _get_status

        conn = get_conn()
        return _get_status(conn)
    except Exception as exc:
        return {"error": f"Failed to get index status: {exc}"}


async def list_index_scopes() -> dict:
    """Returns configured index scopes (spaces or page trees for bulk indexing)."""
    if err := _require_indexer():
        return err
    try:
        from .indexer.db import get_conn, list_index_scopes as _list_scopes

        conn = get_conn()
        scopes = _list_scopes(conn)
        return {"scopes": scopes}
    except Exception as exc:
        return {"error": f"Failed to list scopes: {exc}"}


async def add_index_scope(
    label: Annotated[
        str,
        Field(description="Unique label for this scope (e.g. the space key)"),
    ],
    scope_type: Annotated[
        str,
        Field(description="Type: 'space' for a whole Confluence space, 'page_tree' for a page and its descendants"),
    ] = "space",
    scope_id: Annotated[
        str,
        Field(description="The space key (for space) or root page ID (for page_tree). Defaults to label if omitted."),
    ] = "",
) -> dict:
    """Add an index scope for bulk indexing. Use index_now to trigger indexing."""
    if err := _require_indexer():
        return err
    if not scope_id:
        scope_id = label
    try:
        from .indexer.db import add_index_scope as _add_scope, get_conn

        conn = get_conn()
        result = _add_scope(conn, label, scope_type, scope_id)
        return {"added": True, **result}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Failed to add scope: {exc}"}


async def remove_index_scope(
    label: Annotated[
        str,
        Field(description="Label of the scope to remove"),
    ],
) -> dict:
    """Remove an index scope."""
    if err := _require_indexer():
        return err
    try:
        from .indexer.db import get_conn, remove_index_scope as _remove_scope

        conn = get_conn()
        removed = _remove_scope(conn, label)
        if removed:
            return {"removed": True, "label": label}
        return {"error": f"Scope '{label}' not found"}
    except Exception as exc:
        return {"error": f"Failed to remove scope: {exc}"}


async def index_now(
    label: Annotated[
        str,
        Field(description="Label of the scope to index. Must be added via add_index_scope first."),
    ] = "",
    force: Annotated[
        bool,
        Field(description="Force full re-index even if pages haven't changed"),
    ] = False,
) -> dict:
    """Trigger bulk indexing of a scope. Runs as a background task."""
    if err := _require_indexer():
        return err
    try:
        from .config import get_max_chunk_chars
        from .indexer.db import get_conn, list_index_scopes as _list_scopes
        from .indexer.pipeline import index_scope as _index_scope

        conn = get_conn()
        scopes = _list_scopes(conn)

        if label:
            scopes = [s for s in scopes if s["label"] == label]
            if not scopes:
                return {"error": f"Scope '{label}' not found. Add it with add_index_scope first."}
        if not scopes:
            return {"error": "No scopes configured. Add one with add_index_scope first."}

        client = get_client()
        results = {}
        for scope in scopes:
            stats = await _index_scope(
                client, conn,
                scope_type=scope["scope_type"],
                scope_id=scope["scope_id"],
                label=scope["label"],
                max_chunk_chars=get_max_chunk_chars(),
                force=force,
            )
            results[scope["label"]] = stats
        return {"indexed": results}
    except Exception as exc:
        return {"error": f"Indexing failed: {exc}"}
