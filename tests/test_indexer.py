"""Unit tests for the integrated Confluence indexer."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from confluence_mcp.config import Perspective
from confluence_mcp.indexer.chunker import chunk_markdown, chunk_page, content_hash
from confluence_mcp.indexer.db import (
    add_index_scope,
    add_perspective,
    close_all_dbs,
    get_perspectives,
    init_db,
    list_index_scopes,
    remove_index_scope,
    remove_perspective,
    sync_perspectives,
)
from confluence_mcp.indexer.pipeline import index_page, remove_page_from_index
from confluence_mcp.indexer.search import (
    _interval_label,
    _staleness_label,
    get_index_status,
)


@pytest.fixture()
def db():
    """Temporary SQLite DB with sqlite-vec and one test perspective."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        perspectives = [Perspective(name="test", instruction="test perspective")]
        conn = init_db(db_path, perspectives, 768)
        yield conn
        close_all_dbs()


# ---------------------------------------------------------------------------
# DB: schema and init
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_creates_tables(self, db):
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "pages" in tables
        assert "chunks" in tables
        assert "perspectives" in tables
        assert "index_scopes" in tables
        assert "schema_version" in tables

    def test_pages_has_staleness_columns(self, db):
        cols = {
            row[1] for row in db.execute("PRAGMA table_info(pages)").fetchall()
        }
        assert "last_accessed" in cols
        assert "version_date" in cols
        assert "median_update_interval_days" in cols
        assert "version_count" in cols

    def test_default_perspective_created(self, db):
        persp = get_perspectives(db)
        assert len(persp) == 1
        assert persp[0]["name"] == "test"

    def test_reinit_is_idempotent(self, db):
        """Re-initializing with the same perspectives doesn't fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            perspectives = [Perspective(name="p1", instruction="first")]
            conn = init_db(db_path, perspectives, 768)
            close_all_dbs()
            # Re-init with same config
            conn = init_db(db_path, perspectives, 768)
            persp = get_perspectives(conn)
            assert len(persp) == 1
            close_all_dbs()


# ---------------------------------------------------------------------------
# DB: perspectives
# ---------------------------------------------------------------------------


class TestPerspectives:
    def test_add_perspective(self, db):
        result = add_perspective(db, "custom", "custom focus", 768)
        assert result["name"] == "custom"
        assert result["instruction"] == "custom focus"
        persp = get_perspectives(db)
        names = [p["name"] for p in persp]
        assert "custom" in names

    def test_add_perspective_duplicate_error(self, db):
        add_perspective(db, "dup", "first", 768)
        with pytest.raises(ValueError, match="already exists"):
            add_perspective(db, "dup", "second", 768)

    def test_remove_perspective(self, db):
        add_perspective(db, "temp", "temporary", 768)
        assert remove_perspective(db, "temp") is True
        names = [p["name"] for p in get_perspectives(db)]
        assert "temp" not in names

    def test_remove_perspective_not_found(self, db):
        assert remove_perspective(db, "nonexistent") is False

    def test_get_perspectives(self, db):
        persp = get_perspectives(db)
        assert isinstance(persp, list)
        assert all("id" in p and "name" in p and "instruction" in p for p in persp)

    def test_sync_adds_new(self, db):
        new_perspectives = [
            Perspective(name="test", instruction="test perspective"),
            Perspective(name="added", instruction="newly added"),
        ]
        changed = sync_perspectives(db, new_perspectives, 768)
        assert changed is True
        names = [p["name"] for p in get_perspectives(db)]
        assert "added" in names

    def test_sync_detects_instruction_change(self, db):
        changed = sync_perspectives(
            db,
            [Perspective(name="test", instruction="CHANGED instruction")],
            768,
        )
        assert changed is True
        persp = get_perspectives(db)
        test_p = [p for p in persp if p["name"] == "test"][0]
        assert test_p["instruction"] == "CHANGED instruction"

    def test_sync_no_change(self, db):
        changed = sync_perspectives(
            db,
            [Perspective(name="test", instruction="test perspective")],
            768,
        )
        assert changed is False


# ---------------------------------------------------------------------------
# DB: index scopes
# ---------------------------------------------------------------------------


class TestIndexScopes:
    def test_add_scope(self, db):
        result = add_index_scope(db, "MLSE", "space", "MLSE")
        assert result["label"] == "MLSE"
        assert result["scope_type"] == "space"

    def test_add_scope_duplicate_error(self, db):
        add_index_scope(db, "dup", "space", "DUP")
        with pytest.raises(ValueError, match="already exists"):
            add_index_scope(db, "dup", "space", "DUP")

    def test_add_scope_invalid_type(self, db):
        with pytest.raises(ValueError, match="scope_type must be"):
            add_index_scope(db, "bad", "invalid_type", "X")

    def test_remove_scope(self, db):
        add_index_scope(db, "temp", "space", "TEMP")
        assert remove_index_scope(db, "temp") is True
        scopes = list_index_scopes(db)
        assert not any(s["label"] == "temp" for s in scopes)

    def test_remove_scope_not_found(self, db):
        assert remove_index_scope(db, "nonexistent") is False

    def test_list_scopes(self, db):
        add_index_scope(db, "s1", "space", "S1")
        add_index_scope(db, "s2", "page_tree", "12345")
        scopes = list_index_scopes(db)
        assert len(scopes) == 2
        labels = {s["label"] for s in scopes}
        assert labels == {"s1", "s2"}


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_deterministic(self):
        assert content_hash("hello") == content_hash("hello")

    def test_different_inputs(self):
        assert content_hash("hello") != content_hash("world")

    def test_non_empty(self):
        assert len(content_hash("test")) == 64  # SHA-256 hex length


class TestChunkMarkdown:
    def test_single_section(self):
        text = "# Title\n\nSome body text here."
        chunks = chunk_markdown(text)
        assert len(chunks) >= 1
        assert chunks[0].heading_path == "Title"

    def test_multiple_sections(self):
        text = "# A\n\nBody A.\n\n# B\n\nBody B."
        chunks = chunk_markdown(text)
        assert len(chunks) == 2
        assert chunks[0].heading_path == "A"
        assert chunks[1].heading_path == "B"

    def test_heading_path_nested(self):
        text = "# Top\n\nBody.\n\n## Sub\n\nSub body."
        chunks = chunk_markdown(text)
        sub_chunks = [c for c in chunks if "Sub" in c.heading_path]
        assert len(sub_chunks) == 1
        assert sub_chunks[0].heading_path == "Top > Sub"

    def test_max_chars_respected(self):
        text = "# Section\n\n" + ("word " * 1000)
        chunks = chunk_markdown(text, max_chars=200)
        for chunk in chunks:
            assert len(chunk.content) <= 200

    def test_empty_body_filtered(self):
        text = "# Heading Only\n\n# Another Heading\n\nActual content."
        chunks = chunk_markdown(text)
        headings = [c.heading_path for c in chunks]
        assert "Heading Only" not in headings

    def test_no_headings(self):
        text = "Just plain text without any headings."
        chunks = chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0].heading_path == ""


class TestChunkPage:
    def test_xhtml_to_chunks(self):
        xhtml = "<h1>Title</h1><p>Some content here.</p>"
        md, chunks = chunk_page(xhtml)
        assert "Title" in md
        assert len(chunks) >= 1

    def test_empty_xhtml(self):
        md, chunks = chunk_page("")
        assert chunks == []


# ---------------------------------------------------------------------------
# Search labels
# ---------------------------------------------------------------------------


class TestStalenessLabel:
    def test_current(self):
        now = datetime.now(timezone.utc).isoformat()
        assert _staleness_label(now, 7.0) == "current"

    def test_possibly_stale(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        assert _staleness_label(ts, 7.0) == "possibly stale"

    def test_likely_stale(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        assert _staleness_label(ts, 7.0) == "likely stale"

    def test_none_last_accessed(self):
        assert _staleness_label(None, 7.0) == "unknown"

    def test_none_median(self):
        now = datetime.now(timezone.utc).isoformat()
        assert _staleness_label(now, None) == "unknown"

    def test_both_none(self):
        assert _staleness_label(None, None) == "unknown"


class TestIntervalLabel:
    def test_daily(self):
        assert _interval_label(0.5) == "~daily"

    def test_every_few_days(self):
        assert _interval_label(2.0) == "~every few days"

    def test_weekly(self):
        assert _interval_label(7.0) == "~weekly"

    def test_monthly(self):
        assert _interval_label(30.0) == "~monthly"

    def test_quarterly(self):
        assert _interval_label(90.0) == "~quarterly"

    def test_rarely_updated(self):
        assert _interval_label(200.0) == "rarely updated"

    def test_none(self):
        assert _interval_label(None) == "unknown"


# ---------------------------------------------------------------------------
# Pipeline: index_page and remove_page
# ---------------------------------------------------------------------------


SAMPLE_XHTML = "<h1>Test Page</h1><p>This is a test page with some content.</p>"


class TestIndexPage:
    def test_new_page(self, db):
        result = index_page(
            db, "page1", "Test Page", "TEST", 1, SAMPLE_XHTML, ""
        )
        assert result["status"] == "indexed"
        assert result["chunks"] >= 1
        assert len(result["pending_ids"]) == result["chunks"]
        assert len(result["pending_texts"]) == result["chunks"]

        # Verify stored in DB
        row = db.execute(
            "SELECT page_id, title, space_key FROM pages WHERE page_id = ?",
            ("page1",),
        ).fetchone()
        assert row is not None
        assert row["title"] == "Test Page"
        assert row["space_key"] == "TEST"

    def test_unchanged_content(self, db):
        index_page(db, "page2", "Page", "T", 1, SAMPLE_XHTML, "")
        db.commit()
        result = index_page(db, "page2", "Page", "T", 2, SAMPLE_XHTML, "")
        assert result["status"] == "unchanged"
        assert result["chunks"] == 0

    def test_updated_content(self, db):
        index_page(db, "page3", "Page", "T", 1, SAMPLE_XHTML, "")
        db.commit()
        new_html = "<h1>Updated</h1><p>Different content entirely.</p>"
        result = index_page(db, "page3", "Page", "T", 2, new_html, "")
        assert result["status"] == "updated"
        assert result["chunks"] >= 1

    def test_empty_content(self, db):
        result = index_page(db, "page4", "Empty", "T", 1, "", "")
        assert result["status"] == "empty"
        assert result["chunks"] == 0

    def test_chunks_stored_in_db(self, db):
        result = index_page(db, "page5", "Page", "T", 1, SAMPLE_XHTML, "")
        db.commit()
        chunks = db.execute(
            "SELECT heading_path, content FROM chunks "
            "WHERE page_db_id = (SELECT id FROM pages WHERE page_id = ?)",
            ("page5",),
        ).fetchall()
        assert len(chunks) >= 1
        # Should have at least one [page] summary chunk
        summaries = [c for c in chunks if c["heading_path"] == "[page]"]
        assert len(summaries) == 1


class TestRemovePage:
    def test_removes_page_and_chunks(self, db):
        index_page(db, "rm1", "Remove Me", "T", 1, SAMPLE_XHTML, "")
        db.commit()
        page_row = db.execute(
            "SELECT id FROM pages WHERE page_id = ?", ("rm1",)
        ).fetchone()
        assert page_row is not None

        removed = remove_page_from_index(db, page_row["id"])
        db.commit()
        assert removed >= 1

        # Page gone
        assert db.execute(
            "SELECT id FROM pages WHERE page_id = ?", ("rm1",)
        ).fetchone() is None

        # Chunks gone
        assert db.execute(
            "SELECT id FROM chunks WHERE page_db_id = ?", (page_row["id"],)
        ).fetchone() is None


# ---------------------------------------------------------------------------
# Index status
# ---------------------------------------------------------------------------


class TestGetIndexStatus:
    def test_empty_index(self, db):
        status = get_index_status(db)
        assert status["pages_indexed"] == 0
        assert status["chunks_total"] == 0
        assert status["pages"] == []
        assert "test" in status["perspectives"]

    def test_after_indexing(self, db):
        index_page(db, "s1", "Status Test", "DEV", 1, SAMPLE_XHTML, "")
        db.commit()
        status = get_index_status(db)
        assert status["pages_indexed"] == 1
        assert status["chunks_total"] >= 1
        assert len(status["pages"]) == 1
        assert status["pages"][0]["page_id"] == "s1"
        assert status["pages"][0]["title"] == "Status Test"
