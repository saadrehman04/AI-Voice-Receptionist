"""Per-tenant knowledge base: markdown files searched by meaning, with a keyword fallback.

Answer order for `lookup_business_info`:
1. Facts from the tenant YAML (prices, hours, policies, FAQ): exact, so they always win.
2. The tenant's markdown files in `<KB_DIR>/<slug>/*.md`, split into sections by heading and
   searched with a small multilingual embedding model (fastembed, ONNX on CPU, ~0.2 GB).
3. If the model is disabled or unavailable, sections are ranked by word overlap instead.

If nothing scores high enough the result is `found=False`, and the agent says it will check.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from receptionist_shared.knowledge import KnowledgeResult, lookup
from receptionist_shared.tenant import TenantConfig

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "do",
        "does",
        "you",
        "your",
        "i",
        "we",
        "can",
        "to",
        "of",
        "for",
        "in",
        "on",
        "at",
        "it",
        "there",
        "any",
        "my",
        "me",
        "what",
        "how",
        "and",
        "or",
        "with",
        "have",
        "has",
        "will",
        "be",
    ]
)
SEMANTIC_THRESHOLD = 0.5
KEYWORD_THRESHOLD = 2


@dataclass(frozen=True)
class Chunk:
    source: str  # "file.md#Heading"
    text: str


def split_markdown(text: str, source: str) -> list[Chunk]:
    """One chunk per `#`/`##`/`###` section (heading + body). Text before the first heading
    becomes its own chunk."""
    chunks: list[Chunk] = []
    heading = ""
    body: list[str] = []

    def flush() -> None:
        content = "\n".join(body).strip()
        if content:
            label = heading or "intro"
            chunks.append(Chunk(f"{source}#{label}", f"{heading}\n{content}".strip()))

    for line in text.splitlines():
        m = re.match(r"^#{1,3}\s+(.*)", line)
        if m:
            flush()
            heading, body = m.group(1).strip(), []
        else:
            body.append(line)
    flush()
    return chunks


def load_chunks(kb_dir: Path, slug: str) -> list[Chunk]:
    folder = kb_dir / slug
    if not folder.is_dir():
        return []
    chunks: list[Chunk] = []
    for path in sorted(folder.glob("*.md")):
        chunks += split_markdown(path.read_text(encoding="utf-8"), path.name)
    return chunks


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall(text.lower()) if t not in _STOP and len(t) > 2}


def keyword_rank(question: str, chunks: list[Chunk]) -> list[tuple[float, Chunk]]:
    q = _tokens(question)
    scored = [(float(len(q & _tokens(c.text))), c) for c in chunks]
    return sorted(scored, key=lambda x: -x[0])


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """fastembed's ONNX model. Loading downloads it once (~0.2 GB) to the HF cache."""

    def __init__(self, model_name: str) -> None:
        from fastembed import TextEmbedding

        self._model: Any = TextEmbedding(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]


@dataclass
class _TenantIndex:
    chunks: list[Chunk]
    vectors: list[list[float]] = field(default_factory=list)


class KnowledgeBase:
    def __init__(self, kb_dir: Path, embedder: Embedder | None = None) -> None:
        self._kb_dir = kb_dir
        self._embedder = embedder
        self._indexes: dict[str, _TenantIndex] = {}
        self._lock = asyncio.Lock()

    @property
    def semantic(self) -> bool:
        return self._embedder is not None

    async def _index(self, slug: str) -> _TenantIndex:
        async with self._lock:
            if slug not in self._indexes:
                chunks = await asyncio.to_thread(load_chunks, self._kb_dir, slug)
                index = _TenantIndex(chunks)
                if self._embedder is not None and chunks:
                    index.vectors = await asyncio.to_thread(
                        self._embedder.embed, [c.text for c in chunks]
                    )
                self._indexes[slug] = index
                logger.info("kb indexed", tenant=slug, chunks=len(chunks), semantic=self.semantic)
            return self._indexes[slug]

    async def search(self, slug: str, question: str) -> Chunk | None:
        index = await self._index(slug)
        if not index.chunks:
            return None
        if self._embedder is not None and index.vectors:
            (qv,) = await asyncio.to_thread(self._embedder.embed, [question])
            best = max(
                zip((_cosine(qv, v) for v in index.vectors), index.chunks, strict=True),
                key=lambda x: x[0],
            )
            return best[1] if best[0] >= SEMANTIC_THRESHOLD else None
        score, chunk = keyword_rank(question, index.chunks)[0]
        return chunk if score >= KEYWORD_THRESHOLD else None

    async def lookup(self, tenant: TenantConfig, question: str, language: str) -> KnowledgeResult:
        config = lookup(tenant, question, language=language)
        chunk = await self.search(tenant.slug, question)
        if chunk is None:
            return config
        answer = f"{config.answer} {chunk.text}".strip() if config.found else chunk.text
        return KnowledgeResult(found=True, answer=answer, sources=[*config.sources, chunk.source])


def make_embedder(enabled: bool, model_name: str) -> Embedder | None:
    """The embedding model, or None (keyword fallback) if disabled or it fails to load."""
    if not enabled:
        return None
    try:
        return FastEmbedder(model_name)
    except Exception as exc:  # no network on first run, corrupt cache, ...
        logger.warning("embedding model unavailable, using keyword search", error=str(exc))
        return None
