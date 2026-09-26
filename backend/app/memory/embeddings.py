"""Embeddings.

Live path: the Nebius embedding model. Fallback: a deterministic hashed bag-of-words embedding so
retrieval keeps working (and stays reproducible in tests) without network access.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import httpx

from app.config import Settings, get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9']+")
DEFAULT_DIM = 512


class Embedder(Protocol):
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


class HashingEmbedder:
    """Deterministic hashed bag-of-words embedding (no dependencies, no network)."""

    name = "local-hash"

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = dim

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        tokens = _TOKEN_RE.findall(text.lower())
        if not tokens:
            return vector
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            counts_weight = 1.0 + math.log(count)
            # Character trigram signal gives partial robustness to typos and morphology.
            grams = {token[i : i + 3] for i in range(max(1, len(token) - 2))}
            gram_weight = sum(
                1.0 / self.dim for gram in grams
            )
            vector[index] += sign * counts_weight * (1.0 + gram_weight)
        return _normalise(vector)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


class NebiusEmbedder:
    """OpenAI-compatible embeddings endpoint served by Nebius Token Factory."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.name = f"nebius:{settings.embedding_model}"
        self.dim = DEFAULT_DIM  # updated on first successful response
        self._client = httpx.AsyncClient(
            base_url=settings.nebius_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.nebius_api_key}"},
            timeout=httpx.Timeout(30.0),
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.post(
            "/embeddings",
            json={"model": self._settings.embedding_model, "input": texts},
        )
        response.raise_for_status()
        payload = response.json()
        vectors = [item["embedding"] for item in payload["data"]]
        if vectors:
            self.dim = len(vectors[0])
        return [_normalise([float(value) for value in vector]) for vector in vectors]

    async def aclose(self) -> None:
        await self._client.aclose()


class FallbackEmbedder:
    """Wraps a primary embedder and transparently degrades to hashing on failure."""

    def __init__(self, primary: Embedder, fallback: Embedder | None = None) -> None:
        self._primary = primary
        self._fallback = fallback or HashingEmbedder()
        self.name = primary.name
        self.dim = primary.dim
        self._degraded = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._degraded:
            try:
                return await self._primary.embed(texts)
            except Exception as exc:  # noqa: BLE001 - any failure degrades, never crashes the loop
                logger.warning("embedding provider failed (%s); using local hashing embedder", exc)
                self._degraded = True
                self.name = f"{self._primary.name}->{self._fallback.name}"
                self.dim = self._fallback.dim
        return await self._fallback.embed(texts)

    @property
    def degraded(self) -> bool:
        return self._degraded


def build_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    fallback = HashingEmbedder()
    if settings.has_nebius:
        return FallbackEmbedder(NebiusEmbedder(settings), fallback)
    return fallback
