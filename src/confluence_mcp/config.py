"""Configuration from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel


def _require_env(name: str, help_text: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} environment variable is not set. {help_text}")
    return value


def get_base_url() -> str:
    url = _require_env(
        "CONFLUENCE_BASE_URL",
        "Set to your Atlassian URL, e.g. https://yourorg.atlassian.net",
    )
    return url.rstrip("/")


def get_email() -> str:
    return _require_env("CONFLUENCE_EMAIL", "Set to your Atlassian account email.")


def get_api_token() -> str:
    return _require_env(
        "CONFLUENCE_API_TOKEN",
        "Create an API token at https://id.atlassian.com/manage-profile/security/api-tokens",
    )


def get_max_length() -> int:
    raw = os.environ.get("CONFLUENCE_MAX_LENGTH", "50000").strip()
    try:
        return int(raw)
    except ValueError:
        return 50000


def get_large_page_threshold() -> int:
    """Content size (bytes) above which get_page returns an outline instead of full content."""
    raw = os.environ.get("CONFLUENCE_LARGE_PAGE_THRESHOLD", "10000").strip()
    try:
        return int(raw)
    except ValueError:
        return 10000


# ---------------------------------------------------------------------------
# Indexer configuration — all from env vars
# ---------------------------------------------------------------------------

DEFAULT_PERSPECTIVES = [
    ("technical", "technical specifications, architecture, ISA details, implementation, and engineering concepts"),
    ("project", "schedules, milestones, project status, team assignments, and deliverables"),
]

DEFAULT_DB_PATH = str(Path.home() / ".confluence-mcp" / "index.db")


class Perspective(BaseModel):
    name: str
    instruction: str


class EmbeddingConfig(BaseModel):
    api_url: str
    dimensions: int = 768
    batch_size: int = 128
    max_concurrent: int = 4


def get_embedding_config() -> EmbeddingConfig | None:
    """Build embedding config from env vars. Returns None if not configured."""
    api_url = os.environ.get("EMBEDDING_API_URL", "").strip()
    if not api_url:
        return None

    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    return EmbeddingConfig(
        api_url=api_url,
        dimensions=_int_env("EMBEDDING_API_DIMENSIONS", 768),
        batch_size=_int_env("EMBEDDING_API_BATCH_SIZE", 128),
        max_concurrent=_int_env("EMBEDDING_API_MAX_CONCURRENT", 4),
    )


def get_db_path() -> str:
    return DEFAULT_DB_PATH


def get_default_perspectives() -> list[Perspective]:
    return [Perspective(name=n, instruction=i) for n, i in DEFAULT_PERSPECTIVES]


def get_max_chunk_chars() -> int:
    raw = os.environ.get("EMBEDDING_MAX_CHUNK_CHARS", "2000").strip()
    try:
        return int(raw)
    except ValueError:
        return 2000
