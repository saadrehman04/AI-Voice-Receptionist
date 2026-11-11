from pathlib import Path

from receptionist_api.knowledge_base import (
    KnowledgeBase,
    keyword_rank,
    load_chunks,
    split_markdown,
)
from receptionist_shared.tenant import TenantConfig

KB = Path(__file__).resolve().parents[3] / "tenants/kb"


def test_split_markdown_by_headings() -> None:
    chunks = split_markdown("intro text\n# A\nalpha\n## B\nbeta\n\n### C\n", "f.md")
    assert [c.source for c in chunks] == ["f.md#intro", "f.md#A", "f.md#B"]
    assert chunks[1].text == "A\nalpha"


def test_example_kb_loads() -> None:
    chunks = load_chunks(KB, "demo-salon")
    assert any(c.source == "hair-and-colour.md#Colour aftercare" for c in chunks)
    assert load_chunks(KB, "no-such-tenant") == []


def test_keyword_rank_prefers_overlap() -> None:
    chunks = load_chunks(KB, "demo-workshop")
    score, best = keyword_rank("is there a waiting area with wifi", chunks)[0]
    assert best.source.endswith("#Waiting area")
    assert score >= 2


class FakeEmbedder:
    """Bag-of-words vectors: enough to test the semantic code path deterministically."""

    VOCAB = ("colour", "wash", "children", "bridal", "trial", "oil", "wifi")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(w in t.lower()) for w in self.VOCAB] for t in texts]


async def test_semantic_path_and_threshold(salon: TenantConfig) -> None:
    kb = KnowledgeBase(KB, FakeEmbedder())
    hits = await kb.search("demo-salon", "when can I wash after colour")
    assert hits and "Colour aftercare" in hits[0].source
    assert await kb.search("demo-salon", "do you sell gift vouchers") == []


async def test_config_facts_come_first(salon: TenantConfig) -> None:
    kb = KnowledgeBase(KB, FakeEmbedder())
    result = await kb.lookup(salon, "how much is bridal makeup and is there a trial", "en")
    assert result.found
    assert result.answer.startswith("Bridal makeup: about 180 minutes, price on request")
    assert "trial" in result.answer.lower()
    assert result.sources[0] == "services"
