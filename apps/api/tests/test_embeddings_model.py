"""Opt-in: the real multilingual embedding model (downloads ~0.2 GB once). `make test-models`."""

from pathlib import Path

import pytest

from receptionist_api.knowledge_base import FastEmbedder, KnowledgeBase, _cosine, load_chunks
from receptionist_shared import Settings

pytestmark = pytest.mark.models
KB = Path(__file__).resolve().parents[3] / "tenants/kb"


@pytest.fixture(scope="module")
def embedder() -> FastEmbedder:
    return FastEmbedder(Settings(_env_file=None).embeddings_model)


@pytest.mark.parametrize(
    ("question", "expected_section"),
    [
        (
            "do I need a test before colouring my hair for the first time?",
            "Patch test before colour",
        ),
        ("when can I shampoo after getting my hair dyed", "Colour aftercare"),
        ("can my daughter get a haircut", "Children"),
        ("رنگ کروانے کے بعد بال کب دھو سکتی ہوں؟", "Colour aftercare"),  # Urdu question
    ],
)
async def test_semantic_hits(embedder: FastEmbedder, question: str, expected_section: str) -> None:
    kb = KnowledgeBase(KB, embedder)
    hits = await kb.search("demo-salon", question)
    assert any(h.source.endswith(expected_section) for h in hits), (question, hits)


async def test_unrelated_questions_miss(embedder: FastEmbedder) -> None:
    kb = KnowledgeBase(KB, embedder)
    for question in ("do you sell gift vouchers?", "what is the capital of France?"):
        assert await kb.search("demo-salon", question) == [], question


def test_print_score_distribution(embedder: FastEmbedder) -> None:
    """Not an assertion: prints best scores so the threshold choice is documented."""
    chunks = load_chunks(KB, "demo-salon")
    vectors = embedder.embed([c.text for c in chunks])
    for q in ("patch test before colour?", "gift vouchers?", "رنگ کے بعد بال کب دھوئیں؟"):
        (qv,) = embedder.embed([q])
        best = max(_cosine(qv, v) for v in vectors)
        print(f"\n{best:.3f}  {q}")
