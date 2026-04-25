"""Unit tests for XHTML content transformation helpers in tools.py."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from confluence_mcp.tools import (
    _append_to_section_content,
    _collect_section_siblings,
    _delete_section_content,
    _escape_cql,
    _extract_xhtml_section,
    _find_xhtml_heading,
    _is_cql,
    _list_xhtml_headings,
    _move_section,
    _replace_section_content,
)

# ---------------------------------------------------------------------------
# Shared fixture — a page with nested headings
# ---------------------------------------------------------------------------

SAMPLE_XHTML = (
    "<h1>Title</h1>"
    "<p>Title body.</p>"
    "<h2>Overview</h2>"
    "<p>Overview text.</p>"
    "<h3>Details</h3>"
    "<p>Details text.</p>"
    "<h2>Features</h2>"
    "<p>Feature text.</p>"
    "<h3>Sub-feature A</h3>"
    "<p>Sub-feature A text.</p>"
    "<h3>Sub-feature B</h3>"
    "<p>Sub-feature B text.</p>"
    "<h2>Conclusion</h2>"
    "<p>Conclusion text.</p>"
)


# ===================================================================
# _is_cql / _escape_cql
# ===================================================================


class TestIsCql:
    def test_plain_text(self):
        assert _is_cql("how to deploy") is False

    def test_cql_space_key(self):
        assert _is_cql('space.key = "DEV"') is True

    def test_cql_and(self):
        assert _is_cql('type = page AND label = "api"') is True

    def test_cql_not(self):
        assert _is_cql("NOT label = draft") is True

    def test_cql_tilde(self):
        assert _is_cql('text ~ "deploy"') is True

    def test_cql_type_equals(self):
        assert _is_cql("type = page") is True

    def test_cql_ancestor(self):
        assert _is_cql("ancestor = 12345") is True


class TestEscapeCql:
    def test_no_special_chars(self):
        assert _escape_cql("hello world") == "hello world"

    def test_double_quotes(self):
        assert _escape_cql('say "hello"') == 'say \\"hello\\"'

    def test_backslash(self):
        assert _escape_cql("path\\to") == "path\\\\to"

    def test_backslash_then_quote(self):
        # Backslash must be escaped first to avoid double-escaping
        assert _escape_cql('a\\"b') == 'a\\\\\\"b'


# ===================================================================
# _find_xhtml_heading
# ===================================================================


class TestFindXhtmlHeading:
    def test_found(self):
        soup = BeautifulSoup(SAMPLE_XHTML, "html.parser")
        tag = _find_xhtml_heading(soup, "Overview")
        assert tag is not None
        assert tag.name == "h2"
        assert tag.get_text(strip=True) == "Overview"

    def test_not_found(self):
        soup = BeautifulSoup(SAMPLE_XHTML, "html.parser")
        assert _find_xhtml_heading(soup, "Nonexistent") is None

    def test_case_insensitive(self):
        soup = BeautifulSoup(SAMPLE_XHTML, "html.parser")
        tag = _find_xhtml_heading(soup, "OVERVIEW")
        assert tag is not None
        assert tag.get_text(strip=True) == "Overview"

    def test_whitespace_handling(self):
        soup = BeautifulSoup("<h2>  Spaced  </h2>", "html.parser")
        tag = _find_xhtml_heading(soup, "Spaced")
        assert tag is not None

    def test_returns_first_match(self):
        xhtml = "<h2>Dup</h2><p>a</p><h2>Dup</h2><p>b</p>"
        soup = BeautifulSoup(xhtml, "html.parser")
        tag = _find_xhtml_heading(soup, "Dup")
        # Should return the first one
        assert tag is not None
        assert tag.find_next_sibling("p").get_text() == "a"


# ===================================================================
# _collect_section_siblings
# ===================================================================


class TestCollectSectionSiblings:
    def test_with_subsections(self):
        soup = BeautifulSoup(SAMPLE_XHTML, "html.parser")
        heading = _find_xhtml_heading(soup, "Features")
        siblings = _collect_section_siblings(heading, include_subsections=True)
        texts = [el.get_text(strip=True) for el in siblings if hasattr(el, "get_text")]
        assert "Feature text." in texts
        assert "Sub-feature A" in texts
        assert "Sub-feature A text." in texts
        assert "Sub-feature B" in texts
        # Should stop before Conclusion (same level)
        assert "Conclusion" not in [el.get_text(strip=True) for el in siblings if hasattr(el, "get_text") and el.name and el.name.startswith("h")]

    def test_without_subsections(self):
        soup = BeautifulSoup(SAMPLE_XHTML, "html.parser")
        heading = _find_xhtml_heading(soup, "Features")
        siblings = _collect_section_siblings(heading, include_subsections=False)
        texts = [el.get_text(strip=True) for el in siblings if hasattr(el, "get_text")]
        assert "Feature text." in texts
        # Should stop at first sub-heading
        assert "Sub-feature A" not in texts

    def test_last_section(self):
        soup = BeautifulSoup(SAMPLE_XHTML, "html.parser")
        heading = _find_xhtml_heading(soup, "Conclusion")
        siblings = _collect_section_siblings(heading, include_subsections=True)
        texts = [el.get_text(strip=True) for el in siblings if hasattr(el, "get_text")]
        assert "Conclusion text." in texts

    def test_empty_section(self):
        xhtml = "<h2>Empty</h2><h2>Next</h2>"
        soup = BeautifulSoup(xhtml, "html.parser")
        heading = _find_xhtml_heading(soup, "Empty")
        siblings = _collect_section_siblings(heading)
        assert siblings == []


# ===================================================================
# _extract_xhtml_section
# ===================================================================


class TestExtractXhtmlSection:
    def test_extract_with_subsections(self):
        result = _extract_xhtml_section(SAMPLE_XHTML, "Features")
        assert result is not None
        assert "<h2>Features</h2>" in result
        assert "Feature text." in result
        assert "Sub-feature A" in result
        assert "Sub-feature B" in result
        assert "Conclusion" not in result

    def test_extract_without_subsections(self):
        result = _extract_xhtml_section(SAMPLE_XHTML, "Features", include_subsections=False)
        assert result is not None
        assert "<h2>Features</h2>" in result
        assert "Feature text." in result
        assert "Sub-feature A" not in result

    def test_missing_heading(self):
        assert _extract_xhtml_section(SAMPLE_XHTML, "Missing") is None

    def test_single_heading_document(self):
        xhtml = "<h1>Only</h1><p>Content here.</p>"
        result = _extract_xhtml_section(xhtml, "Only")
        assert result is not None
        assert "<h1>Only</h1>" in result
        assert "Content here." in result


# ===================================================================
# _list_xhtml_headings
# ===================================================================


class TestListXhtmlHeadings:
    def test_all_headings(self):
        headings = _list_xhtml_headings(SAMPLE_XHTML)
        assert headings == [
            "Title",
            "Overview",
            "Details",
            "Features",
            "Sub-feature A",
            "Sub-feature B",
            "Conclusion",
        ]

    def test_empty_document(self):
        assert _list_xhtml_headings("<p>No headings here.</p>") == []

    def test_no_content(self):
        assert _list_xhtml_headings("") == []


# ===================================================================
# _replace_section_content
# ===================================================================


class TestReplaceSectionContent:
    def test_replace_body(self):
        result = _replace_section_content(
            SAMPLE_XHTML, "Conclusion", "<p>New ending.</p>"
        )
        assert result is not None
        assert "<h2>Conclusion</h2>" in result
        assert "New ending." in result
        assert "Conclusion text." not in result

    def test_replace_with_rename(self):
        result = _replace_section_content(
            SAMPLE_XHTML, "Conclusion", "<p>New.</p>", new_heading_text="Summary"
        )
        assert result is not None
        assert "<h2>Summary</h2>" in result
        assert "Conclusion" not in result
        assert "New." in result

    def test_missing_section(self):
        assert _replace_section_content(SAMPLE_XHTML, "Missing", "<p>x</p>") is None

    def test_preserves_other_sections(self):
        result = _replace_section_content(
            SAMPLE_XHTML, "Conclusion", "<p>Replaced.</p>"
        )
        assert "Overview text." in result
        assert "Feature text." in result

    def test_replace_section_with_subsections(self):
        # Replacing Features should only replace its direct content,
        # not sub-features (since _collect_section_siblings defaults to True)
        result = _replace_section_content(
            SAMPLE_XHTML, "Overview", "<p>New overview.</p>"
        )
        assert result is not None
        assert "New overview." in result
        # Details (h3 child) should have been removed and replaced
        assert "Details text." not in result


# ===================================================================
# _delete_section_content
# ===================================================================


class TestDeleteSectionContent:
    def test_delete_with_subsections(self):
        result = _delete_section_content(SAMPLE_XHTML, "Features")
        assert result is not None
        assert "Features" not in result
        assert "Sub-feature A" not in result
        assert "Sub-feature B" not in result
        # Other sections preserved
        assert "Overview" in result
        assert "Conclusion" in result

    def test_delete_without_subsections(self):
        result = _delete_section_content(
            SAMPLE_XHTML, "Features", include_subsections=False
        )
        assert result is not None
        # Heading and direct body removed
        assert "<h2>Features</h2>" not in result
        assert "Feature text." not in result
        # Child sections preserved in place
        assert "Sub-feature A" in result
        assert "Sub-feature B" in result

    def test_missing_section(self):
        assert _delete_section_content(SAMPLE_XHTML, "Missing") is None

    def test_delete_last_section(self):
        result = _delete_section_content(SAMPLE_XHTML, "Conclusion")
        assert result is not None
        assert "Conclusion" not in result
        assert "Conclusion text." not in result
        assert "Features" in result

    def test_delete_leaf_section(self):
        result = _delete_section_content(SAMPLE_XHTML, "Sub-feature A")
        assert result is not None
        assert "Sub-feature A" not in result
        assert "Sub-feature B" in result


# ===================================================================
# _append_to_section_content
# ===================================================================


class TestAppendToSectionContent:
    def test_append(self):
        result = _append_to_section_content(
            SAMPLE_XHTML, "Conclusion", "<p>Extra note.</p>"
        )
        assert result is not None
        assert "Extra note." in result
        assert "Conclusion text." in result

    def test_append_position(self):
        # The appended content should appear after the section's existing content
        # but before the next same-level heading
        xhtml = "<h2>A</h2><p>A text.</p><h2>B</h2><p>B text.</p>"
        result = _append_to_section_content(xhtml, "A", "<p>Appended.</p>")
        assert result is not None
        # Check ordering: A text, Appended, then B
        a_pos = result.index("A text.")
        appended_pos = result.index("Appended.")
        b_pos = result.index("<h2>B</h2>")
        assert a_pos < appended_pos < b_pos

    def test_append_to_empty_section(self):
        xhtml = "<h2>Empty</h2><h2>Next</h2>"
        result = _append_to_section_content(xhtml, "Empty", "<p>Now has content.</p>")
        assert result is not None
        assert "Now has content." in result
        empty_pos = result.index("<h2>Empty</h2>")
        content_pos = result.index("Now has content.")
        next_pos = result.index("<h2>Next</h2>")
        assert empty_pos < content_pos < next_pos

    def test_missing_section(self):
        assert _append_to_section_content(SAMPLE_XHTML, "Missing", "<p>x</p>") is None


# ===================================================================
# _move_section
# ===================================================================


class TestMoveSection:
    def test_move_after(self):
        xhtml = "<h2>A</h2><p>A text.</p><h2>B</h2><p>B text.</p><h2>C</h2><p>C text.</p>"
        result, err = _move_section(xhtml, "A", after="C")
        assert err is None
        assert result is not None
        # A should now appear after C
        c_pos = result.index("<h2>C</h2>")
        a_pos = result.index("<h2>A</h2>")
        assert c_pos < a_pos

    def test_move_before(self):
        xhtml = "<h2>A</h2><p>A text.</p><h2>B</h2><p>B text.</p><h2>C</h2><p>C text.</p>"
        result, err = _move_section(xhtml, "C", before="A")
        assert err is None
        assert result is not None
        # C should now appear before A
        c_pos = result.index("<h2>C</h2>")
        a_pos = result.index("<h2>A</h2>")
        assert c_pos < a_pos

    def test_move_with_subsections(self):
        result, err = _move_section(SAMPLE_XHTML, "Features", after="Conclusion")
        assert err is None
        assert result is not None
        conclusion_pos = result.index("<h2>Conclusion</h2>")
        features_pos = result.index("<h2>Features</h2>")
        assert conclusion_pos < features_pos
        # Sub-features should move with Features
        assert "Sub-feature A" in result
        assert "Sub-feature B" in result
        sub_a_pos = result.index("Sub-feature A")
        assert sub_a_pos > features_pos

    def test_rename(self):
        xhtml = "<h2>A</h2><p>Text.</p><h2>B</h2><p>B text.</p>"
        result, err = _move_section(xhtml, "A", after="B", new_heading_text="Alpha")
        assert err is None
        assert "Alpha" in result
        assert "<h2>A</h2>" not in result

    def test_relevel(self):
        xhtml = "<h2>A</h2><p>Text.</p><h3>A-child</h3><p>Child.</p><h2>B</h2><p>B text.</p>"
        result, err = _move_section(xhtml, "A", after="B", new_level=1)
        assert err is None
        assert "<h1>A</h1>" in result
        # Child shifts by same delta: h3 → h2
        assert "<h2>A-child</h2>" in result

    def test_relevel_clamp(self):
        # Level clamped to 1-6
        xhtml = "<h1>A</h1><p>Text.</p><h2>B</h2><p>B text.</p>"
        result, err = _move_section(xhtml, "B", after="A", new_level=1)
        assert err is None
        # h2 → h1 (delta -1), that's fine
        assert "<h1>B</h1>" in result

    def test_error_missing_source(self):
        xhtml = "<h2>A</h2><p>Text.</p>"
        result, err = _move_section(xhtml, "Missing", after="A")
        assert result is None
        assert "not found" in err.lower()

    def test_error_missing_target(self):
        xhtml = "<h2>A</h2><p>Text.</p>"
        result, err = _move_section(xhtml, "A", after="Missing")
        assert result is None
        assert "not found" in err.lower()

    def test_error_target_inside_source(self):
        result, err = _move_section(SAMPLE_XHTML, "Features", after="Sub-feature A")
        assert result is None
        assert "inside" in err.lower()

    def test_error_no_direction(self):
        result, err = _move_section(SAMPLE_XHTML, "Features")
        assert result is None
        assert err is not None
