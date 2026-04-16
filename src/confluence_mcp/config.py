"""Configuration from environment variables."""

from __future__ import annotations

import os


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
