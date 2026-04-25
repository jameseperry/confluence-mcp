"""Unit tests for Confluence storage format conversion in converter.py."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from confluence_mcp.converter import (
    Section,
    _clean_images,
    _preprocess_macros,
    extract_outline,
    extract_section,
    slice_content,
    storage_to_markdown,
)


# ===================================================================
# storage_to_markdown — end-to-end
# ===================================================================


class TestStorageToMarkdown:
    def test_empty_input(self):
        assert storage_to_markdown("") == ""
        assert storage_to_markdown("   ") == ""

    def test_basic_paragraph(self):
        md = storage_to_markdown("<p>Hello world.</p>")
        assert "Hello world." in md

    def test_headings(self):
        md = storage_to_markdown("<h1>Title</h1><h2>Sub</h2><p>Body.</p>")
        assert "# Title" in md
        assert "## Sub" in md
        assert "Body." in md

    def test_bold_italic(self):
        md = storage_to_markdown("<p><strong>bold</strong> and <em>italic</em></p>")
        assert "**bold**" in md
        assert "*italic*" in md

    def test_unordered_list(self):
        md = storage_to_markdown("<ul><li>one</li><li>two</li></ul>")
        assert "one" in md
        assert "two" in md

    def test_table(self):
        html = (
            "<table><tr><th>Name</th><th>Value</th></tr>"
            "<tr><td>foo</td><td>bar</td></tr></table>"
        )
        md = storage_to_markdown(html)
        assert "Name" in md
        assert "foo" in md

    def test_collapses_blank_lines(self):
        html = "<p>A</p><p></p><p></p><p></p><p>B</p>"
        md = storage_to_markdown(html)
        assert "\n\n\n" not in md


# ===================================================================
# _preprocess_macros — individual macro types
# ===================================================================


class TestPreprocessMacrosCodeBlocks:
    def test_code_block_with_language(self):
        xhtml = (
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">python</ac:parameter>'
            "<ac:plain-text-body>print('hi')</ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        pre = soup.find("pre")
        assert pre is not None
        code = pre.find("code")
        assert code is not None
        assert "language-python" in code.get("class", [])
        assert "print('hi')" in code.get_text()

    def test_code_block_without_language(self):
        xhtml = (
            '<ac:structured-macro ac:name="code">'
            "<ac:plain-text-body>hello</ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        code = soup.find("code")
        assert code is not None
        assert code.get("class") is None or code.get("class") == []
        assert "hello" in code.get_text()


class TestPreprocessMacrosPanels:
    @pytest.mark.parametrize("panel_type", ["info", "warning", "note", "tip", "panel"])
    def test_panel(self, panel_type):
        xhtml = (
            f'<ac:structured-macro ac:name="{panel_type}">'
            f"<ac:rich-text-body><p>Panel content.</p></ac:rich-text-body>"
            f"</ac:structured-macro>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        bq = soup.find("blockquote")
        assert bq is not None
        text = bq.get_text()
        assert panel_type.capitalize() in text
        assert "Panel content." in text


class TestPreprocessMacrosLinks:
    def test_page_link_with_base_url(self):
        xhtml = (
            "<ac:link>"
            '<ri:page ri:content-title="My Page" ri:content-id="12345"/>'
            "</ac:link>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup, base_url="https://example.atlassian.net")
        a = soup.find("a")
        assert a is not None
        assert a["href"] == "https://example.atlassian.net/wiki/pages/12345"
        assert a.get_text() == "My Page"

    def test_page_link_without_base_url(self):
        xhtml = (
            "<ac:link>"
            '<ri:page ri:content-title="My Page" ri:content-id="12345"/>'
            "</ac:link>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        # Without base_url, should fall back to text replacement
        text = soup.get_text()
        assert "My Page" in text

    def test_page_link_custom_display_text(self):
        xhtml = (
            "<ac:link>"
            '<ri:page ri:content-title="My Page" ri:content-id="12345"/>'
            "<ac:plain-text-link-body>Custom Text</ac:plain-text-link-body>"
            "</ac:link>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup, base_url="https://x.atlassian.net")
        a = soup.find("a")
        assert a.get_text() == "Custom Text"

    def test_user_mention(self):
        xhtml = '<ac:link><ri:user ri:account-id="abc123"/></ac:link>'
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "@user" in soup.get_text()

    def test_url_link(self):
        xhtml = (
            "<ac:link>"
            '<ri:url ri:value="https://example.com"/>'
            "<ac:plain-text-link-body>Example</ac:plain-text-link-body>"
            "</ac:link>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        a = soup.find("a")
        assert a is not None
        assert a["href"] == "https://example.com"
        assert a.get_text() == "Example"

    def test_link_fallback(self):
        xhtml = "<ac:link>Some text</ac:link>"
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "Some text" in soup.get_text()


class TestPreprocessMacrosImages:
    def test_image_with_attachment(self):
        xhtml = '<ac:image><ri:attachment ri:filename="diagram.png"/></ac:image>'
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        img = soup.find("img")
        assert img is not None
        assert img["src"] == "diagram.png"
        assert img["alt"] == "diagram.png"

    def test_image_strips_query_params(self):
        xhtml = '<ac:image><ri:attachment ri:filename="img.png?version=1"/></ac:image>'
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        img = soup.find("img")
        assert img["src"] == "img.png"

    def test_image_without_attachment(self):
        xhtml = "<ac:image></ac:image>"
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "[image]" in soup.get_text()


class TestPreprocessMacrosRemovals:
    def test_toc_removed(self):
        xhtml = '<ac:structured-macro ac:name="toc"></ac:structured-macro><p>Content.</p>'
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert soup.find("ac:structured-macro") is None
        assert "Content." in soup.get_text()

    def test_emoticon_removed(self):
        xhtml = '<p>Hello <ac:emoticon ac:name="smile"/> world</p>'
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert soup.find("ac:emoticon") is None

    def test_placeholder_removed(self):
        xhtml = "<p>Before <ac:placeholder>Type here</ac:placeholder> after</p>"
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "Type here" not in soup.get_text()


class TestPreprocessMacrosDates:
    def test_date(self):
        xhtml = '<time datetime="2025-05-21" />'
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "2025-05-21" in soup.get_text()


class TestPreprocessMacrosDecisions:
    def test_decision_list(self):
        xhtml = (
            "<ac:adf-extension>"
            '<ac:adf-node type="decision-list">'
            '<ac:adf-node type="decision-item">'
            '<ac:adf-attribute key="state">DECIDED</ac:adf-attribute>'
            "<ac:adf-content>Use Python</ac:adf-content>"
            "</ac:adf-node>"
            "</ac:adf-node>"
            "</ac:adf-extension>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        text = soup.get_text()
        assert "Decisions" in text
        assert "DECIDED" in text
        assert "Use Python" in text

    def test_empty_decision_list(self):
        xhtml = (
            "<ac:adf-extension>"
            '<ac:adf-node type="decision-list">'
            "</ac:adf-node>"
            "</ac:adf-extension>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "[Decisions]" in soup.get_text()

    def test_generic_adf_extension(self):
        xhtml = "<ac:adf-extension>Some ADF content</ac:adf-extension>"
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "Some ADF content" in soup.get_text()

    def test_empty_adf_extension(self):
        xhtml = "<ac:adf-extension></ac:adf-extension>"
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "[ADF extension]" in soup.get_text()


class TestPreprocessMacrosJira:
    def test_jira_link_with_key(self):
        xhtml = (
            '<ac:structured-macro ac:name="jira">'
            '<ac:parameter ac:name="key">PROJ-123</ac:parameter>'
            "</ac:structured-macro>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "PROJ-123" in soup.get_text()

    def test_jira_link_with_server(self):
        xhtml = (
            '<ac:structured-macro ac:name="jira">'
            '<ac:parameter ac:name="key">PROJ-456</ac:parameter>'
            '<ac:parameter ac:name="server">Jira Cloud AMD-Hub</ac:parameter>'
            "</ac:structured-macro>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        text = soup.get_text()
        assert "PROJ-456" in text
        assert "Jira: AMD-Hub" in text
        # "Jira Cloud " prefix stripped
        assert "Jira Cloud" not in text

    def test_jira_link_without_key(self):
        xhtml = '<ac:structured-macro ac:name="jira"></ac:structured-macro>'
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "[JIRA]" in soup.get_text()

    def test_jira_datasource_table(self):
        import json

        ds = json.dumps({"parameters": {"jql": "project = DEV"}})
        xhtml = (
            f'<a data-datasource=\'{ds}\' '
            f'href="https://amd-hub.atlassian.net/issues">table</a>'
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        text = soup.get_text()
        assert "Jira table" in text
        assert "amd-hub" in text
        assert "project = DEV" in text


class TestPreprocessMacrosTaskLists:
    def test_task_list(self):
        xhtml = (
            "<ac:task-list>"
            "<ac:task>"
            "<ac:task-id>1</ac:task-id>"
            "<ac:task-status>complete</ac:task-status>"
            "<ac:task-body>Done item</ac:task-body>"
            "</ac:task>"
            "<ac:task>"
            "<ac:task-id>2</ac:task-id>"
            "<ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>Todo item</ac:task-body>"
            "</ac:task>"
            "</ac:task-list>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        ul = soup.find("ul")
        assert ul is not None
        items = ul.find_all("li")
        assert len(items) == 2
        assert "[x]" in items[0].get_text()
        assert "Done item" in items[0].get_text()
        assert "[ ]" in items[1].get_text()
        assert "Todo item" in items[1].get_text()


class TestPreprocessMacrosStatus:
    def test_status_lozenge(self):
        xhtml = (
            '<ac:structured-macro ac:name="status">'
            '<ac:parameter ac:name="title">IN PROGRESS</ac:parameter>'
            "</ac:structured-macro>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "[IN PROGRESS]" in soup.get_text()

    def test_status_lozenge_no_title(self):
        xhtml = '<ac:structured-macro ac:name="status"></ac:structured-macro>'
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "[STATUS]" in soup.get_text()


class TestPreprocessMacrosCatchAll:
    def test_macro_with_body_unwraps(self):
        xhtml = (
            '<ac:structured-macro ac:name="expand">'
            '<ac:parameter ac:name="title">Click me</ac:parameter>'
            "<ac:rich-text-body><p>Hidden content.</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        text = soup.get_text()
        assert "Hidden content." in text
        # Parameter should be removed
        assert "Click me" not in text

    def test_macro_without_body_placeholder(self):
        xhtml = '<ac:structured-macro ac:name="drawio"></ac:structured-macro>'
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "[Confluence macro: drawio]" in soup.get_text()


class TestPreprocessMacrosAdfSmartLinks:
    def test_inline_card_jira(self):
        import json

        adf = json.dumps(
            {
                "content": [
                    {
                        "content": [
                            {
                                "type": "inlineCard",
                                "attrs": {
                                    "url": "https://jira.example.com/browse/PROJ-789"
                                },
                            }
                        ]
                    }
                ]
            }
        )
        xhtml = f"<fab:adf>{adf}</fab:adf>"
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "PROJ-789" in soup.get_text()

    def test_inline_card_non_jira_url(self):
        import json

        adf = json.dumps(
            {
                "content": [
                    {
                        "content": [
                            {
                                "type": "inlineCard",
                                "attrs": {"url": "https://example.com/page"},
                            }
                        ]
                    }
                ]
            }
        )
        xhtml = f"<fab:adf>{adf}</fab:adf>"
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        assert "https://example.com/page" in soup.get_text()

    def test_malformed_adf_json(self):
        xhtml = "<fab:adf>not valid json</fab:adf>"
        soup = BeautifulSoup(xhtml, "html.parser")
        _preprocess_macros(soup)
        # Should fall back to text extraction
        assert "not valid json" in soup.get_text()


# ===================================================================
# _clean_images
# ===================================================================


class TestCleanImages:
    def test_strips_src_query_params(self):
        soup = BeautifulSoup('<img src="photo.png?v=2" alt="photo"/>', "html.parser")
        _clean_images(soup)
        assert soup.find("img")["src"] == "photo.png"

    def test_strips_alt_query_params(self):
        soup = BeautifulSoup(
            '<img src="photo.png" alt="photo.png?v=2"/>', "html.parser"
        )
        _clean_images(soup)
        assert soup.find("img")["alt"] == "photo.png"

    def test_no_query_params_unchanged(self):
        soup = BeautifulSoup('<img src="photo.png" alt="photo"/>', "html.parser")
        _clean_images(soup)
        assert soup.find("img")["src"] == "photo.png"
        assert soup.find("img")["alt"] == "photo"

    def test_no_images(self):
        soup = BeautifulSoup("<p>No images.</p>", "html.parser")
        _clean_images(soup)
        assert "No images." in soup.get_text()


# ===================================================================
# extract_outline
# ===================================================================


class TestExtractOutline:
    def test_basic(self):
        md = "# Title\n\nIntro.\n\n## Section\n\nBody.\n\n### Sub\n\nDetail."
        sections = extract_outline(md)
        assert len(sections) == 3
        assert sections[0] == Section(level=1, title="Title", char_offset=0, line_number=1)
        assert sections[1].level == 2
        assert sections[1].title == "Section"
        assert sections[2].level == 3
        assert sections[2].title == "Sub"

    def test_empty_document(self):
        assert extract_outline("") == []

    def test_no_headings(self):
        assert extract_outline("Just a paragraph.") == []

    def test_line_numbers(self):
        md = "line1\nline2\n# Heading\nline4"
        sections = extract_outline(md)
        assert len(sections) == 1
        assert sections[0].line_number == 3

    def test_consecutive_headings(self):
        md = "# A\n## B\n### C"
        sections = extract_outline(md)
        assert len(sections) == 3
        assert [s.title for s in sections] == ["A", "B", "C"]


# ===================================================================
# extract_section (markdown)
# ===================================================================


class TestExtractSection:
    MARKDOWN = (
        "# Title\n\nIntro.\n\n"
        "## Overview\n\nOverview text.\n\n"
        "### Details\n\nDetails text.\n\n"
        "## Features\n\nFeature text.\n\n"
        "## Conclusion\n\nEnd."
    )

    def test_extract_with_subsections(self):
        result = extract_section(self.MARKDOWN, "Overview")
        assert result is not None
        assert "## Overview" in result
        assert "Overview text." in result
        assert "### Details" in result
        assert "Details text." in result
        # Should stop before next h2
        assert "Features" not in result

    def test_extract_without_subsections(self):
        result = extract_section(self.MARKDOWN, "Overview", include_subsections=False)
        assert result is not None
        assert "## Overview" in result
        assert "Overview text." in result
        assert "### Details" not in result

    def test_case_insensitive(self):
        result = extract_section(self.MARKDOWN, "OVERVIEW")
        assert result is not None
        assert "Overview text." in result

    def test_missing_heading(self):
        assert extract_section(self.MARKDOWN, "Missing") is None

    def test_last_section(self):
        result = extract_section(self.MARKDOWN, "Conclusion")
        assert result is not None
        assert "End." in result


# ===================================================================
# slice_content
# ===================================================================


class TestSliceContent:
    def test_within_limit(self):
        text = "Short text."
        content, total, has_more = slice_content(text, 100)
        assert content == text
        assert total == len(text)
        assert has_more is False

    def test_exact_limit(self):
        text = "12345"
        content, total, has_more = slice_content(text, 5)
        assert content == text
        assert has_more is False

    def test_over_limit_with_paragraph_break(self):
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        content, total, has_more = slice_content(text, 40)
        assert has_more is True
        assert total == len(text)
        # Should break at paragraph boundary
        assert content.endswith("Second paragraph.")

    def test_over_limit_no_good_break(self):
        text = "a" * 200
        content, total, has_more = slice_content(text, 100)
        assert has_more is True
        assert len(content) == 100

    def test_offset(self):
        text = "AAAA\n\nBBBB\n\nCCCC"
        content, total, has_more = slice_content(text, 100, start_offset=6)
        assert content == "BBBB\n\nCCCC"
        assert has_more is False

    def test_offset_past_end(self):
        text = "Hello"
        content, total, has_more = slice_content(text, 100, start_offset=999)
        assert content == ""
        assert has_more is False

    def test_paragraph_break_before_midpoint_ignored(self):
        # A paragraph break in the first half should be ignored
        # (to avoid breaking too early)
        text = "AB\n\n" + "C" * 200
        content, total, has_more = slice_content(text, 100)
        assert has_more is True
        # The \n\n is at position 2, which is before midpoint (50),
        # so it should use the full max_length
        assert len(content) == 100
