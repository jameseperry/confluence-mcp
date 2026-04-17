"""Configuration models and singleton for the Confluence semantic indexer."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_PERSPECTIVES = [
    ("general", "general knowledge, concepts, and overview information"),
    ("technical", "technical specifications, parameters, data types, and implementation details"),
    ("procedural", "procedures, workflows, steps, and how-to instructions"),
]


class ConfluenceCredentials(BaseModel):
    base_url: str
    email: str
    api_token: str


class Perspective(BaseModel):
    name: str
    instruction: str


class KBScope(BaseModel):
    label: str
    scope_type: Literal["space", "page_tree"]
    scope_id: str
    perspectives: list[Perspective] = []
    db_path: str = ""


class EmbeddingConfig(BaseModel):
    backend: str = "local"
    model_name: str = "nomic-ai/nomic-embed-text-v1.5"
    dimensions: int = 768
    api_url: str = ""
    batch_size: int = 128
    max_concurrent: int = 4


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8400
    data_dir: str = "data"
    reindex_interval_hours: int = 24
    token_ttl_hours: int = 24


class Settings(BaseModel):
    confluence: ConfluenceCredentials
    scopes: list[KBScope]
    embedding: EmbeddingConfig = EmbeddingConfig()
    server: ServerConfig = ServerConfig()
    max_chunk_chars: int = 2000

    @property
    def embed_dimensions(self) -> int:
        return self.embedding.dimensions


def _resolve_settings(settings: Settings) -> Settings:
    """Fill in defaults for empty fields."""
    for scope in settings.scopes:
        if not scope.perspectives:
            scope.perspectives = [
                Perspective(name=n, instruction=i) for n, i in DEFAULT_PERSPECTIVES
            ]
        if not scope.db_path:
            scope.db_path = str(
                Path(settings.server.data_dir) / f"{scope.label}.db"
            )
    return settings


def load_settings(config_path: str = "indexer_config.json") -> Settings:
    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(p) as f:
        data = json.load(f)
    settings = Settings(**data)
    return _resolve_settings(settings)


_settings: Settings | None = None


def init_settings(config_path: str = "indexer_config.json") -> Settings:
    global _settings
    _settings = load_settings(config_path)
    return _settings


def get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("Settings not initialized — call init_settings() first")
    return _settings
