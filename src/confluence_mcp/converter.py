"""Convert Confluence storage format (XHTML) to Markdown."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify


def _preprocess_macros(soup: BeautifulSoup, base_url: str = "") -> None:
    """Replace Confluence-specific XML elements with HTML that markdownify handles."""

    # Code blocks: <ac:structured-macro ac:name="code">
    for macro in soup.find_all("ac:structured-macro", attrs={"ac:name": "code"}):
        lang_param = macro.find("ac:parameter", attrs={"ac:name": "language"})
        lang = lang_param.get_text(strip=True) if lang_param else ""
        body = macro.find("ac:plain-text-body")
        code_text = body.get_text() if body else ""
        pre = soup.new_tag("pre")
        code = soup.new_tag("code", attrs={"class": f"language-{lang}"} if lang else {})
        code.string = code_text
        pre.append(code)
        macro.replace_with(pre)

    # Info/warning/note/tip panels → blockquotes
    for panel_type in ("info", "warning", "note", "tip", "panel"):
        for macro in soup.find_all(
            "ac:structured-macro", attrs={"ac:name": panel_type}
        ):
            body = macro.find("ac:rich-text-body")
            inner = body.decode_contents() if body else ""
            label = panel_type.capitalize()
            bq = soup.new_tag("blockquote")
            bq.append(BeautifulSoup(f"<p><strong>{label}:</strong></p>{inner}", "html.parser"))
            macro.replace_with(bq)

    # Links: <ac:link><ri:page ri:content-title="..." ri:content-id="..."/></ac:link>
    for link in soup.find_all("ac:link"):
        page_ref = link.find("ri:page")
        if page_ref:
            title = page_ref.get("ri:content-title", "")
            page_id = page_ref.get("ri:content-id", "")
            link_body = link.find("ac:plain-text-link-body") or link.find(
                "ac:link-body"
            )
            display = link_body.get_text(strip=True) if link_body else title
            if page_id and base_url:
                a_tag = soup.new_tag("a", href=f"{base_url}/wiki/pages/{page_id}")
                a_tag.string = display or f"page:{page_id}"
                link.replace_with(a_tag)
            else:
                link.replace_with(soup.new_string(display or "[link]"))
            continue
        # URL links
        url_ref = link.find("ri:url")
        if url_ref:
            href = url_ref.get("ri:value", "")
            link_body = link.find("ac:plain-text-link-body") or link.find(
                "ac:link-body"
            )
            display = link_body.get_text(strip=True) if link_body else href
            a_tag = soup.new_tag("a", href=href)
            a_tag.string = display
            link.replace_with(a_tag)
            continue
        # Fallback: extract any text
        link.replace_with(soup.new_string(link.get_text(strip=True) or "[link]"))

    # Images: <ac:image><ri:attachment ri:filename="..."/></ac:image>
    for img in soup.find_all("ac:image"):
        attachment = img.find("ri:attachment")
        if attachment:
            filename = attachment.get("ri:filename", "image")
            img_tag = soup.new_tag("img", alt=filename, src=filename)
            img.replace_with(img_tag)
        else:
            img.replace_with(soup.new_string("[image]"))

    # TOC macro — strip entirely
    for macro in soup.find_all("ac:structured-macro", attrs={"ac:name": "toc"}):
        macro.decompose()

    # Emoticons — strip
    for emoticon in soup.find_all("ac:emoticon"):
        emoticon.decompose()

    # Jira issue links: <ac:structured-macro ac:name="jira">
    for macro in soup.find_all(
        "ac:structured-macro", attrs={"ac:name": "jira"}
    ):
        key_param = macro.find("ac:parameter", attrs={"ac:name": "key"})
        if key_param:
            key = key_param.get_text(strip=True)
            macro.replace_with(soup.new_string(key))
        else:
            macro.replace_with(soup.new_string("[JIRA]"))

    # ADF smart links (fab:adf) — newer Jira/Confluence inline cards
    for adf in soup.find_all("fab:adf"):
        try:
            adf_json = json.loads(adf.get_text())
            texts = []
            for node in adf_json.get("content", []):
                for inline in node.get("content", []):
                    if inline.get("type") == "inlineCard":
                        url = inline.get("attrs", {}).get("url", "")
                        # Extract Jira issue key from URL if possible
                        jira_match = re.search(r"/browse/([A-Z]+-\d+)", url)
                        if jira_match:
                            texts.append(jira_match.group(1))
                        elif url:
                            texts.append(url)
                    elif inline.get("type") == "text":
                        texts.append(inline.get("text", ""))
            if texts:
                adf.replace_with(soup.new_string(" ".join(texts)))
            else:
                adf.replace_with(soup.new_string(adf.get_text(strip=True) or ""))
        except (json.JSONDecodeError, AttributeError):
            adf.replace_with(soup.new_string(adf.get_text(strip=True) or ""))

    # Status lozenges: <ac:structured-macro ac:name="status">
    for macro in soup.find_all(
        "ac:structured-macro", attrs={"ac:name": "status"}
    ):
        title_param = macro.find("ac:parameter", attrs={"ac:name": "title"})
        text = title_param.get_text(strip=True) if title_param else "STATUS"
        macro.replace_with(soup.new_string(f"[{text}]"))

    # Catch-all: any remaining ac:structured-macro → extract inner text
    for macro in soup.find_all("ac:structured-macro"):
        body = macro.find("ac:rich-text-body")
        if body:
            body.unwrap()
        macro.unwrap()


def storage_to_markdown(storage_html: str, base_url: str = "") -> str:
    """Convert Confluence storage format XHTML to readable Markdown."""
    if not storage_html or not storage_html.strip():
        return ""

    soup = BeautifulSoup(storage_html, "html.parser")
    _preprocess_macros(soup, base_url)

    md = markdownify(
        str(soup),
        heading_style="ATX",
        strip=["script", "style"],
    )

    # Collapse runs of 3+ blank lines to 2
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


@dataclass
class Section:
    """A heading and its position in the markdown text."""

    level: int
    title: str
    char_offset: int
    line_number: int


def extract_outline(markdown: str) -> list[Section]:
    """Return the heading structure of a markdown document."""
    sections: list[Section] = []
    line_num = 1
    last_end = 0
    for m in _HEADING_RE.finditer(markdown):
        # Count newlines between last match end and this match start
        line_num += markdown[last_end : m.start()].count("\n")
        last_end = m.start()
        sections.append(
            Section(
                level=len(m.group(1)),
                title=m.group(2).strip(),
                char_offset=m.start(),
                line_number=line_num,
            )
        )
    return sections


def extract_section(
    markdown: str, heading: str, include_subsections: bool = True
) -> str | None:
    """Extract content under a specific heading.

    Args:
        markdown: Full markdown text.
        heading: Heading text to match (case-insensitive).
        include_subsections: If True, include all nested sub-headings.
            If False, stop at the next heading of any level.

    Returns:
        The section text (including the heading line), or None if not found.
    """
    sections = extract_outline(markdown)
    target = None
    for sec in sections:
        if sec.title.lower() == heading.lower():
            target = sec
            break

    if target is None:
        return None

    start = target.char_offset

    # Find the end: next heading at same or higher level (include_subsections=True)
    # or next heading at any level (include_subsections=False)
    end = len(markdown)
    for sec in sections:
        if sec.char_offset <= start:
            continue
        if include_subsections:
            if sec.level <= target.level:
                end = sec.char_offset
                break
        else:
            # Stop at any heading
            end = sec.char_offset
            break

    return markdown[start:end].strip()


def slice_content(
    text: str, max_length: int, start_offset: int = 0
) -> tuple[str, int, bool]:
    """Return a slice of text with paragraph-boundary truncation.

    Returns (content, total_length, has_more).
    """
    total = len(text)
    if start_offset >= total:
        return "", total, False

    remaining = text[start_offset:]
    if len(remaining) <= max_length:
        return remaining, total, False

    # Try to break at last paragraph boundary before limit
    truncated = remaining[:max_length]
    last_break = truncated.rfind("\n\n")
    if last_break > max_length // 2:
        truncated = truncated[:last_break]

    return truncated, total, True
