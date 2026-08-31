"""Ingestion Stage C — Enricher.

Takes Stage B `Chunk` output and produces `EnrichedChunk` objects ready for Stage E
(Qdrant upsert). It does three things, in this order:

1. **Module cleaning** — one LLM call per *candidate-bearing* page (a page whose
   flagged chunks contribute a non-empty `module_candidates` union). The raw
   candidate list is handed to the model verbatim; the model drops the two
   residuals Stage B deliberately delegated (French function-word junk such as
   ``C ou Y``, and all-caps format placeholders such as ``AAAA``/``MM``/``JJ``).
   This stage does the *structural* cleanup of the result (dedupe, strip empties
   and trailing punctuation); the model does the *semantic* judgement.
2. **Page-wide union** — every chunk on a page carries the same cleaned
   ``applies_to`` list, so a search filtered by any module mentioned anywhere on
   the page retrieves all of that page's chunks, not only the flagged one.
3. **Contextual header** — one LLM call per chunk (all of them, including
   unflagged chunks and chunks whose ``applies_to`` is empty). A one-sentence
   French *purpose* statement, embedded as a prefix to ``text`` in Stage E as a
   ranking signal. Never shown to the user.

Measured against the real 2.62 MB Ask&Go export (post Stage-B fix), the call
budget is lopsided: 240 pages, 298 chunks → **298 header calls** but only
**2 module-cleaning calls** (2 of 240 pages carry any candidate union). 99.3% of
this stage's cost is contextual headers, and that half applies to every chunk.

Constraints (see ``contracts/ingestion_enrich.md`` and CLAUDE.md §4/§12):
- All LLM access goes through the injected ``BaseLLMService`` — no direct SDK use.
- Input ``Chunk`` objects are never mutated; enrichment is returned on the new
  ``EnrichedChunk`` type.
- No Qdrant, no embeddings, no tokenizer, no network here — those are Stage E.
- One attempt per LLM call; a failure raises ``EnrichmentError`` (no retry).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.core.logging import get_logger
from app.core.services.llm import BaseLLMService
from app.ingestion.chunk import Chunk, ImageRef

logger = get_logger(__name__)

# First ~N whitespace tokens of `text` handed to the header prompt. A soft budget:
# the full 800-token body must never be sent (Forbidden clause / trap 5).
_HEADER_EXCERPT_WORDS = 200


class EnrichmentError(Exception):
    """Raised when an LLM call in Stage C fails.

    Carries the page/chunk context of the failing call so the orchestrator (M6)
    can log which unit was in flight. The underlying exception is chained via
    ``raise ... from`` — this stage never swallows it and never returns a partial
    result. Retry is the orchestrator's job, not this stage's.
    """


@dataclass(frozen=True)
class EnrichedChunk:
    # --- from Stage B, copied unchanged ---
    text: str
    page_id: str
    page_title: str
    chapter_id: str
    chapter_title: str
    section_path: str
    anchor: str
    chunk_index: int
    token_count: int
    image_refs: list[ImageRef]
    table_count: int
    related_anchors: list[str]
    is_generalized_procedure: bool

    # --- added by this stage ---
    applies_to: list[str]  # cleaned module names, page-wide union
    contextual_header: str  # 1-sentence purpose statement, ~50 tokens


# ---------------------------------------------------------------------------
# Prompt builders — French, per the corpus. The marker words "modules"
# (cleaning) and "phrase" (header) are stable and do not cross-contaminate;
# the tests key call classification on them.
# ---------------------------------------------------------------------------


def _cleaning_prompt(candidates: list[str]) -> str:
    """Module-cleaning prompt. Sends the RAW candidate list so the model — not this
    code — decides which entries are real modules and which are the delegated
    residuals (French function-word junk, all-caps format masks)."""
    candidates_json = json.dumps(candidates, ensure_ascii=False)
    return (
        "Tu nettoies une liste de noms de modules extraits d'une documentation "
        "logicielle en français. On te fournit une liste brute de candidats. Ne "
        "garde que les vrais noms de modules de l'application (libellés de menu "
        "ou d'écran).\n\n"
        "Supprime notamment :\n"
        "- les expressions contenant un mot de fonction français comme « ou », "
        "« et », « de » (par exemple « C ou Y » est un choix de valeur, pas un "
        "module) ;\n"
        "- les masques de format en majuscules (par exemple AAAA, MM, JJ pour "
        "des dates) ;\n"
        "- les doublons et la ponctuation superflue.\n\n"
        "Réponds UNIQUEMENT par une liste JSON de chaînes de caractères, sans "
        "aucun autre texte.\n\n"
        f"Candidats : {candidates_json}"
    )


def _header_prompt(page_title: str, section_path: str, excerpt: str) -> str:
    """Contextual-header prompt. Requests a one-sentence PURPOSE statement, and is
    fed only the page title, section path and a truncated excerpt — never the full
    body (trap 5 / Forbidden clause)."""
    return (
        "Tu écris l'OBJECTIF d'une section de documentation en UNE seule phrase "
        "courte (environ 50 tokens au maximum), en français. La phrase doit "
        "décrire le BUT de la section — à quelle question elle répond — et non "
        "énumérer ses étapes. Ne recopie pas le texte.\n\n"
        f"Titre de la page : {page_title}\n"
        f"Chemin : {section_path}\n"
        f"Extrait : {excerpt}\n\n"
        "Réponds uniquement par cette phrase."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _excerpt(text: str, max_words: int = _HEADER_EXCERPT_WORDS) -> str:
    """First ~`max_words` whitespace tokens of `text`. Whitespace split only — no
    tokenizer is imported in this stage (Environment clause)."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def _parse_cleaning_response(raw: str) -> list[str]:
    """Parse the model's cleaning response into a list of strings. Robust to a
    JSON list, a fenced JSON block, or a bare comma/newline-delimited list. Never
    raises — a malformed response degrades `applies_to`, it does not crash the
    pipeline (Stage E filter tolerates junk; it can never widen access)."""
    text = raw.strip()
    if text.startswith("```"):
        # strip a ```json ... ``` fence
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str):
        return [value]
    # Fallback: split on commas / newlines, drop bracket and quote noise.
    parts = re.split(r"[,\n]", text)
    return [p for p in (part.strip().strip("[]\"'") for part in parts) if p]


def _clean_result(names: list[str]) -> list[str]:
    """Structural cleanup of the model's cleaned list: strip surrounding
    whitespace and punctuation, drop empties, dedupe preserving first-seen order.
    Semantic rejection (dropping `C ou Y`, `AAAA`) is the model's job, done in the
    prompt — this only guarantees assertion 10's *shape* invariants."""
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        cleaned = name.strip().strip(",;:.").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


# ---------------------------------------------------------------------------
# Stage C entry point
# ---------------------------------------------------------------------------


async def enrich_chunks(
    chunks: list[Chunk],
    *,
    llm: BaseLLMService,
    model: str,
) -> list[EnrichedChunk]:
    """Enrich Stage B chunks with a page-wide cleaned module list and a per-chunk
    contextual header. See module docstring and the contract for full semantics.

    Runs in two passes so that all module-cleaning calls precede all header calls
    (assertions 4/5 order): pass 1 cleans one call per candidate-bearing page,
    pass 2 generates one header per chunk in input order.
    """
    if not chunks:
        return []

    # Group by page, preserving first-seen page order.
    pages: dict[str, list[Chunk]] = {}
    page_order: list[str] = []
    for chunk in chunks:
        if chunk.page_id not in pages:
            pages[chunk.page_id] = []
            page_order.append(chunk.page_id)
        pages[chunk.page_id].append(chunk)

    # --- Pass 1: module cleaning, one call per candidate-bearing page ---
    applies_by_page: dict[str, list[str]] = {}
    cleaning_calls = 0
    for page_id in page_order:
        # Raw union from flagged chunks only (assertion 5). Duplicates preserved —
        # the RESULT is deduped, not the input (trap 3).
        raw_candidates: list[str] = []
        for chunk in pages[page_id]:
            if chunk.is_generalized_procedure:
                raw_candidates.extend(chunk.module_candidates)

        # Empty union → no call, empty applies_to. The trigger is the empty union,
        # not the generalization flag (assertion 6).
        if not raw_candidates:
            applies_by_page[page_id] = []
            continue

        try:
            response = await llm.complete(
                model=model,
                messages=[{"role": "user", "content": _cleaning_prompt(raw_candidates)}],
            )
        except Exception as exc:  # one attempt, no retry
            raise EnrichmentError(f"Stage C module cleaning failed for page_id={page_id}") from exc

        applies_by_page[page_id] = _clean_result(_parse_cleaning_response(response))
        cleaning_calls += 1

    # --- Pass 2: contextual header, one call per chunk, input order ---
    enriched: list[EnrichedChunk] = []
    for chunk in chunks:
        try:
            header = await llm.complete(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": _header_prompt(
                            chunk.page_title,
                            chunk.section_path,
                            _excerpt(chunk.text),
                        ),
                    }
                ],
            )
        except Exception as exc:  # one attempt, no retry
            raise EnrichmentError(
                f"Stage C header generation failed for "
                f"page_id={chunk.page_id} chunk_index={chunk.chunk_index}"
            ) from exc

        enriched.append(
            EnrichedChunk(
                text=chunk.text,
                page_id=chunk.page_id,
                page_title=chunk.page_title,
                chapter_id=chunk.chapter_id,
                chapter_title=chunk.chapter_title,
                section_path=chunk.section_path,
                anchor=chunk.anchor,
                chunk_index=chunk.chunk_index,
                token_count=chunk.token_count,
                # Copied, not aliased. `Chunk` is a plain (mutable) dataclass, so
                # handing its own list objects to the EnrichedChunk would let a
                # later stage's `.append()` write backwards into Stage B's output —
                # which Stage D and E may still be holding. `ImageRef` is frozen so
                # the elements are safe; only the list itself needs the copy.
                image_refs=list(chunk.image_refs),
                table_count=chunk.table_count,
                related_anchors=list(chunk.related_anchors),
                is_generalized_procedure=chunk.is_generalized_procedure,
                # Per-chunk copy for the same reason, plus so the page-wide union is
                # never shared across the chunks of one page.
                applies_to=list(applies_by_page.get(chunk.page_id, [])),
                contextual_header=header.strip(),
            )
        )

    logger.info(
        "stage_c_enrich_complete",
        chunks=len(chunks),
        pages=len(page_order),
        cleaning_calls=cleaning_calls,
        header_calls=len(chunks),
    )
    return enriched
