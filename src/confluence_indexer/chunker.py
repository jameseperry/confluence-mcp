"""Heading-aware markdown chunking for Confluence pages."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from confluence_mcp.converter import storage_to_markdown

from confluence_indexer.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class Chunk:
    heading_path: str
    content: str
    char_start: int
    char_end: int


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def chunk_page(
    storage_html: str, base_url: str = "", max_chars: int | None = None
) -> tuple[str, list[Chunk]]:
    """Convert Confluence XHTML to markdown, then chunk.

    Returns (markdown_text, chunks).
    """
    md = storage_to_markdown(storage_html, base_url)
    chunks = chunk_markdown(md, max_chars)
    return md, chunks


def chunk_markdown(text: str, max_chars: int | None = None) -> list[Chunk]:
    """Split markdown text into heading-aware chunks."""
    if max_chars is None:
        max_chars = get_settings().max_chunk_chars

    lines = text.split("\n")
    raw_chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []
    current_lines: list[str] = []
    current_start = 0

    def _flush(end_offset: int) -> None:
        if not current_lines:
            return
        content = "\n".join(current_lines).strip()
        if not content:
            return
        path = " > ".join(h[1] for h in heading_stack)
        start_char = _line_to_char(text, current_start)
        end_char = _line_to_char(text, end_offset + 1) if end_offset + 1 < len(lines) else len(text)
        raw_chunks.append(Chunk(
            heading_path=path,
            content=content,
            char_start=start_char,
            char_end=end_char,
        ))

    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.+)", line)
        if m:
            _flush(i - 1 if i > 0 else 0)
            level = len(m.group(1))
            title = m.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            current_lines = [line]
            current_start = i
        else:
            current_lines.append(line)

    _flush(len(lines) - 1)

    result: list[Chunk] = []
    for chunk in raw_chunks:
        body = re.sub(r"^#{1,6}\s+.+\n?", "", chunk.content, count=1).strip()
        if not body:
            continue
        if len(chunk.content) <= max_chars:
            result.append(chunk)
        else:
            result.extend(_split_chunk(chunk, max_chars))

    return result


def _line_to_char(text: str, line_num: int) -> int:
    """Convert a 0-based line number to a character offset."""
    offset = 0
    for i, line in enumerate(text.split("\n")):
        if i == line_num:
            return offset
        offset += len(line) + 1
    return len(text)


def _split_chunk(chunk: Chunk, max_chars: int) -> list[Chunk]:
    """Split an oversized chunk by paragraph breaks, table rows, or lines."""
    paragraphs = re.split(r"\n\n+", chunk.content)

    refined: list[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            refined.append(para)
        else:
            _, parts = _split_oversized_paragraph(para, max_chars)
            refined.extend(parts)

    sub_chunks: list[Chunk] = []
    current_parts: list[str] = []
    current_len = 0

    for para in refined:
        para_len = len(para)
        if current_parts and current_len + para_len + 2 > max_chars:
            sub_chunks.append(Chunk(
                heading_path=chunk.heading_path,
                content="\n\n".join(current_parts),
                char_start=chunk.char_start,
                char_end=chunk.char_end,
            ))
            current_parts = []
            current_len = 0
        current_parts.append(para)
        current_len += para_len + 2

    if current_parts:
        sub_chunks.append(Chunk(
            heading_path=chunk.heading_path,
            content="\n\n".join(current_parts),
            char_start=chunk.char_start,
            char_end=chunk.char_end,
        ))

    return sub_chunks if sub_chunks else [chunk]


def _split_oversized_paragraph(
    text: str, max_chars: int
) -> tuple[str, list[str]]:
    parts = re.split(r"(?<=\n)(?=\+[-+]+\+)", text)
    if len(parts) > 1:
        accumulated = _accumulate_parts(parts, max_chars, "\n")
        result: list[str] = []
        for part in accumulated:
            if len(part) > max_chars:
                lines = part.split("\n")
                result.extend(_accumulate_parts(lines, max_chars, "\n"))
            else:
                result.append(part)
        return "table", result

    lines = text.split("\n")
    return "lines", _accumulate_parts(lines, max_chars, "\n")


def _accumulate_parts(parts: list[str], max_chars: int, sep: str) -> list[str]:
    result: list[str] = []
    current: list[str] = []
    current_len = 0
    sep_len = len(sep)

    for part in parts:
        part_len = len(part)
        if part_len > max_chars:
            if current:
                result.append(sep.join(current))
                current = []
                current_len = 0
            for i in range(0, part_len, max_chars):
                result.append(part[i : i + max_chars])
            continue
        if current and current_len + part_len + sep_len > max_chars:
            result.append(sep.join(current))
            current = []
            current_len = 0
        current.append(part)
        current_len += part_len + sep_len

    if current:
        result.append(sep.join(current))

    return result
