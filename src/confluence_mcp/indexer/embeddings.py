"""Embedding via remote HTTP API only."""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from confluence_mcp.config import EmbeddingConfig

logger = logging.getLogger(__name__)


class APIEmbedding:
    """Embedding via a remote HTTP API.

    Splits large batches and dispatches concurrently via a thread pool.
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._client = None
        self._url = config.api_url
        self._headers = self._parse_headers()
        self._batch_size = config.batch_size
        self._max_concurrent = config.max_concurrent

    @staticmethod
    def _parse_headers() -> dict[str, str]:
        headers: dict[str, str] = {}
        raw = os.environ.get("EMBEDDING_API_HEADERS", "")
        for pair in raw.split(","):
            if ":" in pair:
                key, value = pair.split(":", 1)
                headers[key.strip()] = value.strip()
        return headers

    def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=60.0)
            logger.info("Remote embedding client ready: %s", self._url)
        return self._client

    def _post_batch(self, texts: list[str]) -> list[list[float]]:
        client = self._get_client()
        last_exc = None
        for attempt in range(3):
            resp = client.post(self._url, json={"input": texts}, headers=self._headers)
            if resp.status_code == 422:
                lengths = [len(t) for t in texts]
                logger.warning(
                    "Embedding API 422 on batch of %d texts (max %d chars). "
                    "Truncating and retrying.",
                    len(texts),
                    max(lengths),
                )
                texts = [t[:8000] for t in texts]
                resp = client.post(self._url, json={"input": texts}, headers=self._headers)
                if resp.status_code == 422:
                    logger.error("Embedding API 422 persists after truncation.")
                    resp.raise_for_status()
            if resp.status_code >= 500:
                last_exc = Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
                wait = 2**attempt
                logger.warning("Embedding API %d, retrying in %ds...", resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            results = sorted(data["data"], key=lambda d: d["index"])
            return [r["embedding"] for r in results]
        raise last_exc  # type: ignore[misc]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        batches = [
            texts[i : i + self._batch_size]
            for i in range(0, len(texts), self._batch_size)
        ]
        if len(batches) == 1:
            return self._post_batch(batches[0])
        all_vectors: list[list[float]] = []
        with ThreadPoolExecutor(max_workers=self._max_concurrent) as pool:
            futures = [pool.submit(self._post_batch, batch) for batch in batches]
            for future in futures:
                all_vectors.extend(future.result())
        return all_vectors

    def embed_documents(self, texts: list[str], instruction: str) -> list[list[float]]:
        prefixed = [f"search_document: {instruction} {text}" for text in texts]
        return self._embed(prefixed)

    def embed_query(self, text: str, instruction: str) -> list[float]:
        prefixed = [f"search_query: {instruction} {text}"]
        return self._embed(prefixed)[0]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


_embedder: APIEmbedding | None = None


def init_embedder(config: EmbeddingConfig) -> APIEmbedding:
    global _embedder
    _embedder = APIEmbedding(config)
    return _embedder


def get_embedder() -> APIEmbedding:
    if _embedder is None:
        raise RuntimeError("Embedder not initialized — call init_embedder() first")
    return _embedder


def serialize_vector(vector: list[float]) -> bytes:
    return np.array(vector, dtype=np.float32).tobytes()
