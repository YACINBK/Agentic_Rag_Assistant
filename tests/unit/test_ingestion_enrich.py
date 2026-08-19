"""Unit tests for ingestion Stage C — Enricher (`app/ingestion/enrich.py`).

The LLM is mocked with a local `BaseLLMService` double (`_RecordingLLM`) that
records every call as `(model, messages)` in order, serves responses from a FIFO
queue, and can raise on the Nth call.

Call classification keys on the template-controlled prompt PREFIX, not on a marker
word appearing anywhere in the prompt. A marker-word rule is unsafe here: real
French documentation says "Pour les autres modules ...", so a header prompt whose
excerpt contains that phrase reads as a cleaning call. The corpus gate hit exactly
that (4 of 298 header calls misclassified) before switching to prefixes.
"""

from __future__ import annotations

import copy

import pytest

from app.core.services.llm import BaseLLMService
from app.ingestion.chunk import Chunk, ImageRef
from app.ingestion.enrich import EnrichedChunk, EnrichmentError, enrich_chunks

# Template-controlled prompt prefixes (enrich.py `_cleaning_prompt` / `_header_prompt`).
_CLEANING_PREFIX = "Tu nettoies une liste de noms de modules"
_HEADER_PREFIX = "Tu écris l'OBJECTIF"

# ---------------------------------------------------------------------------
# Local test double + chunk factory
# ---------------------------------------------------------------------------


class _RecordingLLM(BaseLLMService):
    """BaseLLMService double. Matches the real ABC signature exactly, records
    `(model, messages)` per call in order, serves `responses` FIFO, and optionally
    raises `raise_exc` on the 1-based `raise_on_call`-th invocation."""

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        raise_on_call: int | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._raise_on_call = raise_on_call
        self._raise_exc = raise_exc or RuntimeError("mock LLM failure")
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
    ) -> str:
        self.calls.append((model, messages))
        if self._raise_on_call is not None and len(self.calls) == self._raise_on_call:
            raise self._raise_exc
        if self._responses:
            return self._responses.pop(0)
        return "(défaut)"


def _make_chunk(
    *,
    page_id: str,
    chunk_index: int,
    text: str = "Contenu de la section.",
    page_title: str = "Titre de la page",
    chapter_id: str = "chapter-1",
    chapter_title: str = "Chapitre 1",
    section_path: str = "Transversal > Section",
    anchor: str = "bkmrk-a",
    token_count: int = 42,
    image_refs: list[ImageRef] | None = None,
    table_count: int = 0,
    related_anchors: list[str] | None = None,
    is_generalized_procedure: bool = False,
    module_candidates: list[str] | None = None,
) -> Chunk:
    return Chunk(
        text=text,
        page_id=page_id,
        page_title=page_title,
        chapter_id=chapter_id,
        chapter_title=chapter_title,
        section_path=section_path,
        anchor=anchor,
        chunk_index=chunk_index,
        token_count=token_count,
        image_refs=image_refs if image_refs is not None else [],
        table_count=table_count,
        related_anchors=related_anchors if related_anchors is not None else [],
        is_generalized_procedure=is_generalized_procedure,
        module_candidates=module_candidates if module_candidates is not None else [],
    )


def _cleaning_calls(llm: _RecordingLLM) -> list[tuple[str, list[dict[str, str]]]]:
    return [c for c in llm.calls if c[1][0]["content"].startswith(_CLEANING_PREFIX)]


def _header_calls(llm: _RecordingLLM) -> list[tuple[str, list[dict[str, str]]]]:
    return [c for c in llm.calls if c[1][0]["content"].startswith(_HEADER_PREFIX)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_empty_input_returns_empty_output():
    """Assertion 1: empty input → `[]`, never None, never a raise, no LLM calls."""
    llm = _RecordingLLM()
    result = await enrich_chunks([], llm=llm, model="test")
    assert result == []
    assert llm.calls == []


async def test_output_mirrors_input_and_does_not_mutate_it():
    """Assertions 2, 3: one EnrichedChunk per input chunk in order, Stage-B fields
    copied unchanged, and the input list is byte-for-byte unmutated afterward."""
    chunks = [
        _make_chunk(
            page_id="page-1",
            chunk_index=0,
            text="Texte un.",
            anchor="bkmrk-un",
            token_count=11,
            image_refs=[ImageRef("img-1", "bkmrk-un", True)],
            related_anchors=["bkmrk-x", "bkmrk-y"],
            table_count=2,
            is_generalized_procedure=True,
            module_candidates=["Commandes"],
        ),
        _make_chunk(page_id="page-2", chunk_index=0, text="Texte deux."),
        _make_chunk(page_id="page-3", chunk_index=0, text="Texte trois."),
    ]
    before = copy.deepcopy(chunks)

    # page-1 has a candidate union → 1 cleaning call, then 3 header calls.
    llm = _RecordingLLM(responses=['["Commandes"]', "En-tête 0.", "En-tête 1.", "En-tête 2."])
    result = await enrich_chunks(chunks, llm=llm, model="m")

    assert len(result) == 3
    assert all(isinstance(r, EnrichedChunk) for r in result)
    for r, c in zip(result, chunks):
        assert r.page_id == c.page_id
        assert r.chunk_index == c.chunk_index
        assert r.text == c.text
        assert r.anchor == c.anchor
        assert r.token_count == c.token_count
        assert r.image_refs == c.image_refs
        assert r.table_count == c.table_count
        assert r.related_anchors == c.related_anchors
        assert r.is_generalized_procedure == c.is_generalized_procedure

    # Input unmutated — in particular module_candidates not cleaned/sorted/emptied.
    assert chunks == before
    assert chunks[0].module_candidates == ["Commandes"]


async def test_module_cleaning_once_per_page():
    """Assertions 4, 5: a page with 6 chunks (4 flagged) triggers exactly one
    cleaning call, made before any header call, and all 6 chunks carry the same
    page-wide union."""
    chunks = [
        _make_chunk(
            page_id="page-1028",
            chunk_index=i,
            text=f"Étape {i}.",
            is_generalized_procedure=i < 4,
            module_candidates=["Commandes", "Factures"] if i < 4 else [],
        )
        for i in range(6)
    ]
    llm = _RecordingLLM(
        responses=['["Commandes", "Factures"]'] + [f"En-tête {i}." for i in range(6)]
    )
    result = await enrich_chunks(chunks, llm=llm, model="m")

    assert len(_cleaning_calls(llm)) == 1
    assert len(_header_calls(llm)) == 6
    # Cleaning precedes all headers: the very first call is the cleaning call.
    assert llm.calls[0][1][0]["content"].startswith(_CLEANING_PREFIX)
    for r in result:
        assert r.applies_to == ["Commandes", "Factures"]


async def test_empty_candidate_union_skips_cleaning_call():
    """Assertion 6: neither an all-unflagged page nor a flagged-but-empty-candidate
    page triggers a cleaning call. The trigger is the empty union, not the flag —
    page-2001 is the real corpus's dominant case (14 of 16 flagged pages)."""
    chunks = [
        _make_chunk(page_id="page-2000", chunk_index=0, is_generalized_procedure=False),
        _make_chunk(page_id="page-2000", chunk_index=1, is_generalized_procedure=False),
        _make_chunk(
            page_id="page-2001",
            chunk_index=0,
            is_generalized_procedure=True,  # flagged, but no candidates
            module_candidates=[],
        ),
        _make_chunk(page_id="page-2001", chunk_index=1, is_generalized_procedure=False),
    ]
    llm = _RecordingLLM(responses=[f"En-tête {i}." for i in range(4)])
    result = await enrich_chunks(chunks, llm=llm, model="m")

    assert _cleaning_calls(llm) == []
    assert len(_header_calls(llm)) == 4
    for r in result:
        assert r.applies_to == []


async def test_contextual_header_generated_per_chunk():
    """Assertion 7: one header call per chunk (no page here has candidates, so no
    cleaning calls), and each chunk gets its own header."""
    chunks = [
        _make_chunk(page_id="page-a", chunk_index=0),
        _make_chunk(page_id="page-b", chunk_index=0),
        _make_chunk(page_id="page-c", chunk_index=0),
    ]
    llm = _RecordingLLM(responses=["Objectif A.", "Objectif B.", "Objectif C."])
    result = await enrich_chunks(chunks, llm=llm, model="m")

    assert _cleaning_calls(llm) == []
    assert len(_header_calls(llm)) == 3
    assert [r.contextual_header for r in result] == ["Objectif A.", "Objectif B.", "Objectif C."]


async def test_contextual_header_is_short_and_nonempty():
    """Assertion 8: header is non-empty, well under 200 chars (proxy for ~50
    tokens), and not a verbatim copy of the 800-token body. Also verifies the full
    body was not sent to the prompt (trap 5 / Forbidden)."""
    long_text = " ".join(f"mot{i}" for i in range(800))
    chunks = [_make_chunk(page_id="page-x", chunk_index=0, text=long_text, token_count=800)]
    header = "Cette section explique comment créer une nouvelle demande via le menu principal."
    assert len(header) < 200  # sanity on the fixture

    llm = _RecordingLLM(responses=[header])
    result = await enrich_chunks(chunks, llm=llm, model="m")

    h = result[0].contextual_header
    assert h  # non-empty
    assert len(h) < 200
    assert h != long_text

    # The header prompt received a truncated excerpt, not the whole body.
    _model, messages = llm.calls[0]
    content = messages[0]["content"]
    assert "mot0" in content
    assert "mot799" not in content


async def test_llm_failure_raises_enrichment_error():
    """Assertion 9: an LLM failure raises EnrichmentError with page/chunk context,
    chains the original exception, and returns no partial result."""
    chunks = [
        _make_chunk(page_id="page-1", chunk_index=0),
        _make_chunk(page_id="page-2", chunk_index=0),
    ]
    # No candidates → no cleaning calls; the first (header) call raises.
    llm = _RecordingLLM(
        responses=["En-tête 0.", "En-tête 1."],
        raise_on_call=1,
        raise_exc=RuntimeError("rate limit"),
    )
    with pytest.raises(EnrichmentError) as exc_info:
        await enrich_chunks(chunks, llm=llm, model="m")

    assert "page-1" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, RuntimeError)


async def test_applies_to_rejects_the_two_delegated_residuals():
    """Assertion 10 (plumbing): the raw candidate list — including both delegated
    residuals — is sent to the model, and `applies_to` reflects the cleaned RESULT
    (no duplicates, no empties, no trailing punctuation, no residuals)."""
    chunks = [
        _make_chunk(
            page_id="page-1284",
            chunk_index=0,
            is_generalized_procedure=True,
            module_candidates=["Commandes", "Commandes", "C ou Y", "AAAA", "MM", "Factures,"],
        )
    ]
    llm = _RecordingLLM(responses=['["Commandes", "Factures"]', "Objectif."])
    result = await enrich_chunks(chunks, llm=llm, model="m")

    applies_to = result[0].applies_to
    assert applies_to == ["Commandes", "Factures"]

    # The full raw candidate list — residuals included — reached the model.
    cleaning = _cleaning_calls(llm)
    assert len(cleaning) == 1
    prompt = cleaning[0][1][0]["content"]
    assert "C ou Y" in prompt
    assert "AAAA" in prompt
    assert "MM" in prompt

    # Result invariants.
    assert len(applies_to) == len(set(applies_to))  # no duplicates
    assert all(x for x in applies_to)  # no empty strings
    assert all(not x.endswith((",", ".", ";", ":")) for x in applies_to)  # no trailing punct
    assert "C ou Y" not in applies_to
    assert "AAAA" not in applies_to
    assert "MM" not in applies_to
