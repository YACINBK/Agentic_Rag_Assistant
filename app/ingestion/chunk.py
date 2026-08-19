"""Ingestion Stage B — BookStack chunker (M4).

Turns Stage A's *cleaned* HTML (base64 images already stripped; every ``<img>``
carries a ``data-image-id`` and keeps its ``bkmrk-*`` position) into a list of
structural :class:`Chunk` objects. Pure and deterministic: lxml plus a
caller-supplied ``length_fn``. No network, no LLM, no Qdrant, no storage, no
Docling — the one ingestion stage whose tests run with no service up.

The export is a **BookStack** dump: heading *levels* are cosmetic, so structure
is read from ids, never from ``h1``–``h6``:

- ``id="chapter-*"`` and ``id="page-*"`` are flat ``<h1>`` markers, siblings in
  document order (not wrappers). A page's content is the sibling run from its
  ``page-*`` marker up to the next ``page-*`` **or** ``chapter-*`` marker.
- ``id="bkmrk-*"`` sub-anchors mark deep-link targets inside a page.

Five things the obvious implementation gets wrong on the real 2.62 MB artifact,
each pinned by an assertion/test (see ``contracts/ingestion_chunking.md``):

1. ``bkmrk-*`` ids are unique only *within* a page — 588 ids repeat across pages
   (``bkmrk-objectifs`` on 208 of them). The deep-link key is ``(page_id,
   anchor)``; ``anchor`` alone mis-navigates the majority. (assertion 6 / case 8)
2. ``<style>``/``<script>`` are dropped from the tree *before* any text
   extraction — the export's one 48.1 KB ``<style>`` block holds 4 base64
   payloads Stage A leaves behind, and ``.itertext()`` would pull them into a
   chunk. Structural, not positional. (Forbidden / case 8)
3. Chapter attribution is nearest-preceding in **document order**, never sorted
   on the numeric id: the export emits chapters ``95, 104, 111, 110, …``.
   (assertion 2 / case 7)
4. ``GENERALIZATION_PHRASES`` is exactly six narrow regexes; ``toutes les`` is
   deliberately excluded (84 raw hits, 4× false-positive inflation). (assertion 10)
5. ``related_anchors`` is empty for every chunk this corpus produces (all 259
   internal hrefs live in the TOC, which assertion 1 drops). Implemented anyway;
   proven by a synthetic fixture (case 7).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from lxml import etree

from app.core.logging import get_logger

logger = get_logger(__name__)


# The narrow generalization set (assertion 10). Compiled once at import,
# case-insensitive; none is built per call. `toutes les` is intentionally NOT
# here: on the real corpus it fires on ordinary prose ("toutes les opérations…",
# "toutes les grilles") and quadruples the flag rate almost entirely with false
# positives, each of which costs a Stage C enrichment call. `s'applique` allows
# both the straight (U+0027) and typographic (U+2019) apostrophe the export mixes.
GENERALIZATION_PHRASES: tuple[str, ...] = (
    r"même démarche",
    r"même procédure",
    r"même principe",
    r"même affichage",
    r"pour les autres",
    r"s['’]applique",
)
_GENERALIZATION_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in GENERALIZATION_PHRASES
)

# Module-name lists are surfaced from parenthesized groups inside a
# generalization sentence ("Pour les autres modules (Commandes, Réceptions,
# Factures etc..), la même démarche s'applique"). Raw output — Stage C cleans it.
_PAREN_RE = re.compile(r"\(([^)]*)\)")

# Trailing enumeration filler in a list item: "Factures etc..", "Visa etc.".
# Not part of a module name, and assertion 10 forbids trailing punctuation.
_ETC_RE = re.compile(r"\s*\betc\b\.*\s*$", re.IGNORECASE)

# Longest module name accepted, in words. "Sorties de stock" and "Demandes
# d'achat" are 3 and 2; a parenthetical clause ("(Le même principe que le
# bouton supprimer)") is 7. Raw output is Stage C's to clean, but a clause is
# not a name at all, and this also bounds what a future extraction bug can emit.
MAX_MODULE_WORDS = 4

# One word of a module name: letters only, optionally joined by an apostrophe
# or hyphen ("Demandes d'achat", "Notes de crédit"). Rejects digits and symbols,
# so "(19)", "(P2P)" and "($int32)" are not mistaken for names.
_MODULE_WORD_RE = re.compile(r"^[^\W\d_]+(?:['’\-][^\W\d_]+)*$")

# Sentence-final punctuation, honoured only at parenthesis depth 0 — see
# _generalization_sentences.
_SENTENCE_END = ".:;!?"

# Stable image placeholder emitted into text; carries the Stage-A data-image-id
# so per-chunk ImageRefs can be rebuilt by scanning the (possibly window-split)
# text — never the src, so no base64 ever reaches Chunk.text.
_IMG_TOKEN_RE = re.compile(r"\[\[image(?::([^\]]*))?\]\]")

# Separator hierarchy for recursive token-window splitting of an oversized
# single section (assertion 4, second half). Finest is a space, so overlap is
# always expressible in whole words and can be non-empty yet bounded (assertion 5).
_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ". ", " ")

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


@dataclass(frozen=True)
class ImageRef:
    """One image occurrence inside a chunk, in document order (assertion 8)."""

    image_id: str  # Stage-A sha256 (data-image-id); "" only in a pre-Stage-A run
    anchor: str  # the owning chunk's deep-link anchor
    needs_caption: bool  # Stage-A data-needs-caption


@dataclass
class Chunk:
    """One embed-ready segment with full page/chapter provenance."""

    text: str  # tables → markdown; images → placeholder; never base64/CSS
    page_id: str  # "page-1028"
    page_title: str
    chapter_id: str  # nearest preceding chapter marker in document order; "" if none
    chapter_title: str
    section_path: str  # "Chapter > Page[ > sub-section]"
    anchor: str  # bkmrk-* leading the chunk, else page-* — never empty
    chunk_index: int  # 0-based within its page
    token_count: int  # length_fn(text), never len(text)
    image_refs: list[ImageRef] = field(default_factory=list)
    table_count: int = 0
    related_anchors: list[str] = field(default_factory=list)
    is_generalized_procedure: bool = False
    module_candidates: list[str] = field(default_factory=list)


def chunk_bookstack_html(
    cleaned_html: str,
    *,
    length_fn: Callable[[str], int],
    max_tokens: int = 800,
    overlap_tokens: int = 200,
    embed_max_tokens: int = 8192,
) -> list[Chunk]:
    """Segment cleaned BookStack HTML into token-bounded, structure-aware chunks.

    Args:
        cleaned_html: Stage A output. Empty string → ``[]``.
        length_fn: str→int token counter (BGE-M3 encode-length in production).
            If it raises, the exception propagates unchanged — never swallowed.
        max_tokens: per-chunk budget **and packing target**; must be ``>= 1``.
            Sections are accumulated toward it, not just kept under it (assertion 4).
        overlap_tokens: recursive-split overlap ceiling; must be in
            ``[0, max_tokens)``.
        embed_max_tokens: the embedder's own hard ceiling (BGE-M3 max_seq_length
            = 8192) — the one length no chunk may cross, tables included. Must be
            ``>= max_tokens``. An atomic table (assertion 7) may exceed
            ``max_tokens`` but is row-group-split before it exceeds this.

    Returns:
        ``list[Chunk]`` ordered by (page order in document, ``chunk_index``).
        Never ``None``. Every chunk has non-empty ``text`` or non-empty
        ``image_refs``.

    Raises:
        ValueError: if ``max_tokens``/``overlap_tokens``/``embed_max_tokens`` are
            out of range.
    """
    if max_tokens < 1:
        raise ValueError(f"max_tokens must be >= 1, got {max_tokens}")
    if not 0 <= overlap_tokens < max_tokens:
        raise ValueError(f"overlap_tokens must be in [0, {max_tokens}), got {overlap_tokens}")
    if embed_max_tokens < max_tokens:
        raise ValueError(
            f"embed_max_tokens must be >= max_tokens ({max_tokens}), got {embed_max_tokens}"
        )

    if not cleaned_html:
        return []

    root = etree.HTML(cleaned_html)
    if root is None:
        return []

    # Correction #2: strip <style>/<script> from the tree BEFORE any text
    # extraction, so the guarantee "no CSS/base64 in Chunk.text" holds by
    # construction rather than by where the block happens to sit. with_tail=False
    # keeps the text that follows the element — only the element's own subtree goes.
    etree.strip_elements(root, "style", "script", with_tail=False)

    # Assertion 8: an <img> with no data-image-id is an external image whose bytes
    # Stage A never held (the 142 `buildsrv2:8090` hosts) — it is dropped in
    # _render, never emitted as ImageRef(image_id=""). Count it once here, over the
    # whole tree, rather than in _render: a page that gets split re-serializes its
    # sections, so a per-render counter would multiply the count by the split fan-out.
    external_images = sum(1 for img in root.iter("img") if not img.get("data-image-id"))
    if external_images:
        logger.warning("chunk_dropped_external_images", count=external_images)

    markers = root.xpath('.//*[starts-with(@id,"page-") or starts-with(@id,"chapter-")]')
    if not markers:
        logger.warning("chunk_no_page_markers")
        return []

    # Markers and content are flat siblings under a single container (the export's
    # wrapping <div>; <body> for hand-written fixtures). Iterate that container's
    # children in document order.
    container = markers[0].getparent()
    if container is None:
        return []

    pages = _collect_pages(list(container))

    chunks: list[Chunk] = []
    for page in pages:
        chunks.extend(_chunk_page(page, length_fn, max_tokens, overlap_tokens, embed_max_tokens))
    return chunks


def _collect_pages(blocks: list[etree._Element]) -> list[dict]:
    """Group the flat sibling stream into pages, attributing chapters in document order.

    Content that falls under no ``page-*`` run (before the first page, between a
    chapter marker and its first page, after the last page) is dropped with a
    warning — never merged into a neighbor (assertion 1 / Forbidden). This is what
    keeps the TOC, the ``<style>`` block, and the trailing region out of chunks.
    """
    pages: list[dict] = []
    cur_chapter_id = ""
    cur_chapter_title = ""
    current: dict | None = None
    dropped = 0

    for block in blocks:
        bid = block.get("id") or ""
        if bid.startswith("chapter-"):
            cur_chapter_id = bid
            cur_chapter_title = _text_of(block)
            current = None  # a chapter marker closes any open page run
            continue
        if bid.startswith("page-"):
            current = {
                "page_id": bid,
                "page_title": _text_of(block),
                "chapter_id": cur_chapter_id,
                "chapter_title": cur_chapter_title,
                "blocks": [],
            }
            pages.append(current)
            continue
        # A content block. Correction #3's sibling: keep it only if it lives inside
        # an open page run; otherwise it is marker-less and dropped.
        if current is None:
            if _has_content(block):
                dropped += 1
            continue
        current["blocks"].append(block)

    if dropped:
        logger.warning("chunk_dropped_markerless_blocks", count=dropped)
    return pages


def _chunk_page(
    page: dict,
    length_fn: Callable[[str], int],
    max_tokens: int,
    overlap_tokens: int,
    embed_max_tokens: int,
) -> list[Chunk]:
    blocks = page["blocks"]
    page_text, page_images, page_hrefs, page_tables = _serialize(blocks)
    # Module candidates are computed per chunk from the chunk's own
    # generalization sentence (assertion 10). Never page-wide: a parenthesized
    # group elsewhere on the page belongs to a different statement.
    result: list[Chunk] = []

    def emit(
        text: str,
        anchor: str,
        subsection: str,
        related: list[str],
        table_count: int,
        image_meta: list[tuple[str, bool]],
    ) -> None:
        text = text.strip()
        image_refs = _build_image_refs(text, image_meta, anchor)
        # Forbidden: a chunk that is both text-empty and image-empty.
        if not text and not image_refs:
            return
        token_count = length_fn(text)
        generalized = _is_generalized(text)
        chunk = Chunk(
            text=text,
            page_id=page["page_id"],
            page_title=page["page_title"],
            chapter_id=page["chapter_id"],
            chapter_title=page["chapter_title"],
            section_path=_section_path(page, subsection),
            anchor=anchor,
            chunk_index=len(result),
            token_count=token_count,
            image_refs=image_refs,
            table_count=table_count,
            related_anchors=related,
            is_generalized_procedure=generalized,
            module_candidates=_module_candidates(text) if generalized else [],
        )
        if token_count > max_tokens:
            # Reachable only for a chunk holding an atomic table (assertion 7):
            # flagged, never truncated, and bounded above by embed_max_tokens.
            logger.warning(
                "chunk_exceeds_max_tokens",
                page_id=page["page_id"],
                anchor=anchor,
                token_count=token_count,
                table_count=table_count,
            )
        result.append(chunk)

    # Assertion 3: a page that fits the budget is exactly one chunk, regardless of
    # how many bkmrk sub-anchors it holds.
    if length_fn(page_text) <= max_tokens:
        emit(
            page_text,
            _leading_anchor(blocks, page["page_id"]),
            _leading_subsection(blocks),
            _dedup(page_hrefs),
            page_tables,
            page_images,
        )
        return result

    # Assertion 4: an oversized page splits at bkmrk-* boundaries, and the sections
    # are then PACKED toward max_tokens — consecutive fitting sections accumulate
    # into one chunk until the next would cross the budget. Emitting one chunk per
    # bkmrk-* section is the defect this corpus punishes hardest (3570 chunks,
    # median 8 tokens); max_tokens is a target to fill, not only a ceiling.
    pack_texts: list[str] = []
    pack_images: list[tuple[str, bool]] = []
    pack_hrefs: list[str] = []
    pack_tables = 0
    pack_anchor = ""
    pack_subsection = ""

    def flush_pack() -> None:
        nonlocal pack_texts, pack_images, pack_hrefs, pack_tables
        nonlocal pack_anchor, pack_subsection
        if not pack_texts:
            return
        # anchor/subsection come from the FIRST section in the pack (assertion 4).
        # pack_tables carries the count of any small (fits-max_tokens) tables that
        # packed in with prose — a table that fits is not routed to the atomic
        # branch, so its table_count must ride along here or it is lost.
        emit(
            "\n\n".join(pack_texts),
            pack_anchor,
            pack_subsection,
            _dedup(pack_hrefs),
            pack_tables,
            pack_images,
        )
        pack_texts = []
        pack_images = []
        pack_hrefs = []
        pack_tables = 0
        pack_anchor = ""
        pack_subsection = ""

    for section in _sections(blocks, page["page_id"]):
        s_blocks = section["blocks"]
        s_text, s_images, s_hrefs, s_tables = _serialize(s_blocks)
        anchor = section["anchor"]
        subsection = section["subsection"]
        s_tokens = length_fn(s_text)

        if s_tables > 0 and s_tokens > max_tokens:
            # A table is atomic (assertion 7): never window-split. It may exceed
            # max_tokens, but is row-group-split before it exceeds embed_max_tokens.
            # Flush the open pack first so document order is preserved.
            flush_pack()
            for part_text, part_tables in _table_section_parts(
                s_blocks, length_fn, embed_max_tokens
            ):
                emit(part_text, anchor, subsection, _dedup(s_hrefs), part_tables, s_images)
            continue

        if s_tokens > max_tokens:
            # A single table-free section that alone busts the budget: flush the
            # pack, then recursive token-window split with separators PRESERVED
            # (assertion 5).
            flush_pack()
            pieces = _atomic_pieces(s_text, length_fn, max_tokens, _SEPARATORS)
            windows = _merge_windows(pieces, length_fn, max_tokens, overlap_tokens)
            for w_index, window in enumerate(windows):
                emit(
                    window,
                    anchor,
                    subsection,
                    _dedup(s_hrefs) if w_index == 0 else [],
                    0,
                    s_images,
                )
            continue

        # A section that fits alone: pack it. If adding it to the open pack would
        # cross max_tokens, emit the pack first and start a new one at this section.
        if pack_texts and length_fn("\n\n".join([*pack_texts, s_text])) > max_tokens:
            flush_pack()
        if not pack_texts:
            pack_anchor = anchor
            pack_subsection = subsection
        pack_texts.append(s_text)
        pack_images.extend(s_images)
        pack_hrefs.extend(s_hrefs)
        pack_tables += s_tables

    flush_pack()
    return result


def _sections(blocks: list[etree._Element], page_id: str) -> list[dict]:
    """Split a page's blocks into bkmrk-* sections (assertion 4).

    A new section begins at each block carrying a ``bkmrk-*`` id; blocks with no
    such id join the current section. Content before the first ``bkmrk-*`` is a
    section anchored to the ``page-*`` id (assertion 6's fallback).
    """
    sections: list[dict] = []
    current: dict | None = None
    for block in blocks:
        bid = block.get("id") or ""
        if bid.startswith("bkmrk-"):
            current = {
                "anchor": bid,
                "subsection": _text_of(block) if _is_heading(block) else "",
                "blocks": [block],
            }
            sections.append(current)
        elif current is None:
            current = {
                "anchor": page_id,
                "subsection": _text_of(block) if _is_heading(block) else "",
                "blocks": [block],
            }
            sections.append(current)
        else:
            if _is_heading(block) and not current["subsection"]:
                current["subsection"] = _text_of(block)
            current["blocks"].append(block)
    return sections


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class _Ctx:
    """Accumulator threaded through the recursive block walk."""

    __slots__ = ("hrefs", "images", "parts", "table_count")

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.images: list[tuple[str, bool]] = []
        self.hrefs: list[str] = []
        self.table_count = 0


def _serialize(blocks: list[etree._Element]) -> tuple[str, list[tuple[str, bool]], list[str], int]:
    """Render a list of blocks to (text, image_meta, hrefs, table_count).

    ``image_meta`` is ``[(image_id, needs_caption), …]`` in document order;
    per-chunk ImageRefs are rebuilt from the final text so they stay consistent
    with the placeholders even after window splitting.
    """
    ctx = _Ctx()
    for block in blocks:
        _render(block, ctx, is_root=True)
        ctx.parts.append("\n\n")
    return _normalize("".join(ctx.parts)), ctx.images, ctx.hrefs, ctx.table_count


def _render(el: etree._Element, ctx: _Ctx, *, is_root: bool = False) -> None:
    tag = el.tag
    if not isinstance(tag, str):  # comments / processing instructions
        if not is_root and el.tail:
            ctx.parts.append(el.tail)
        return
    tag = tag.lower()

    if tag == "table":
        markdown = _table_to_markdown(el)
        if markdown:
            ctx.parts.append("\n\n" + markdown + "\n\n")
            ctx.table_count += 1
        if not is_root and el.tail:
            ctx.parts.append(el.tail)
        return

    if tag == "img":
        # Assertion 8: an <img> with no data-image-id is an external image whose
        # bytes Stage A never held — drop it entirely, never emit a [[image:]]
        # placeholder or an ImageRef(image_id=""). The document-level count in
        # chunk_bookstack_html reports how many were dropped.
        image_id = el.get("data-image-id")
        if image_id:
            ctx.parts.append(f" [[image:{image_id}]] ")
            ctx.images.append((image_id, el.get("data-needs-caption") == "true"))
        if not is_root and el.tail:
            ctx.parts.append(el.tail)
        return

    if tag == "a":
        href = el.get("href", "")
        if href.startswith("#") and len(href) > 1:
            ctx.hrefs.append(href[1:])

    if el.text:
        ctx.parts.append(el.text)
    for child in el:
        _render(child, ctx)
    if not is_root and el.tail:
        ctx.parts.append(el.tail)


def _cell_text(cell: etree._Element) -> str:
    """Cell text EXCLUDING any nested ``<table>`` content.

    A nested table's rows are flattened in as rows of their own (see
    ``_table_rows``); pulling their text into the enclosing cell as well would
    emit that content a second time, concatenated into one unreadable token —
    ``<td><table>…IH…INNERROW0…</table></td>`` became the single cell
    ``"IHINNERROW0"``. The nested table's *tail* text still belongs to this cell.
    """
    parts: list[str] = []

    def walk(el: etree._Element) -> None:
        if el.text:
            parts.append(el.text)
        for child in el:
            if child.tag != "table":
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(cell)
    return " ".join("".join(parts).split())


def _table_rows(table: etree._Element) -> list[list[str]]:
    """Extract a table's rows as padded cell-text lists (nested tables flattened).

    ``.iter("tr")`` walks descendant rows, so the 12 nested tables in the export
    flatten into their parent — this is why ``table_count`` is 24, not the profile's
    36, and it is deliberate (Pass notes). Rows are right-padded to a common width.

    Flattening means each nested row appears **once**, as a row in its own right.
    Two things must therefore be excluded from the *enclosing* row, or the same
    content lands three times: a nested ``<td>`` is not a cell of the outer row
    (``tr.iter`` walks into it), and nested text is not part of the outer cell
    (``itertext`` walks into it). All 12 nested tables in the export sit inside a
    cell, but only 6 have ``<td>`` as their direct parent — the other 6 are wrapped
    in a ``<div>`` — so this is keyed on the nearest ``<tr>``/``<table>`` ancestor,
    never on the parent tag.
    """
    rows: list[list[str]] = []
    for tr in table.iter("tr"):
        cells = [
            _cell_text(cell)
            for cell in tr.iter("td", "th")
            if next(cell.iterancestors("tr"), None) is tr
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return []
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]


def _table_to_markdown(table: etree._Element) -> str:
    """Serialize a ``<table>`` to one Markdown block (assertion 7 — atomic)."""
    rows = _table_rows(table)
    if not rows:
        return ""
    width = len(rows[0])
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(r) + " |" for r in rows[1:])
    return "\n".join(lines)


def _table_markdown_parts(
    table: etree._Element, length_fn: Callable[[str], int], budget: int
) -> list[str]:
    """Split one table into Markdown parts, each ``<= budget`` tokens (assertion 7).

    Never splits a row; repeats the header row + separator in every part so each
    stays independently readable. A single row that alone busts ``budget`` is still
    emitted whole (a row is the atomic unit — the alternative is truncation).
    """
    rows = _table_rows(table)
    if not rows:
        return []
    width = len(rows[0])
    header_line = "| " + " | ".join(rows[0]) + " |"
    sep_line = "| " + " | ".join(["---"] * width) + " |"
    prefix = header_line + "\n" + sep_line
    body = ["| " + " | ".join(r) + " |" for r in rows[1:]]
    if not body:
        return [prefix]

    parts: list[str] = []
    current: list[str] = []
    for row_line in body:
        if current and length_fn("\n".join([prefix, *current, row_line])) > budget:
            parts.append("\n".join([prefix, *current]))
            current = [row_line]
        else:
            current.append(row_line)
    if current:
        parts.append("\n".join([prefix, *current]))
    return parts


def _table_section_parts(
    blocks: list[etree._Element],
    length_fn: Callable[[str], int],
    embed_max_tokens: int,
) -> list[tuple[str, int]]:
    """Split an oversized table-bearing section into ``(text, table_count)`` parts.

    A table is atomic and may exceed ``max_tokens`` (assertion 7) — but never
    ``embed_max_tokens``, above which BGE-M3 truncates silently. When the whole
    section fits ``embed_max_tokens`` it is one atomic part (the licensed
    over-``max_tokens`` table); otherwise each ``<table>`` too large to embed is
    row-group split (header repeated, never mid-row) and surrounding prose is
    emitted in its own parts. ``table_count`` counts a table once per part.
    """
    whole_text, _imgs, _hrefs, whole_tables = _serialize(blocks)
    if length_fn(whole_text) <= embed_max_tokens:
        return [(whole_text, whole_tables)]

    parts: list[tuple[str, int]] = []
    buf: list[str] = []
    buf_tables = 0

    def flush_buf() -> None:
        nonlocal buf, buf_tables
        text = _normalize("\n\n".join(buf)).strip()
        if text:
            parts.append((text, buf_tables))
        buf = []
        buf_tables = 0

    for block in blocks:
        b_text, _bi, _bh, b_tables = _serialize([block])
        if not b_text.strip() and b_tables == 0:
            continue
        oversized = length_fn(b_text) > embed_max_tokens
        if b_tables > 0 and oversized:
            flush_buf()
            for table in block.iter("table"):
                # Only top-level tables. _table_rows flattens nested rows into
                # their enclosing table via .iter("tr"), so a nested <table>
                # is already inside the outer split — emitting it again as its
                # own part double-counts every nested row (retry-2 defect).
                # Consider only tables with no <table> ancestor inside the block.
                if next(table.iterancestors("table"), None) is not None:
                    continue
                for md in _table_markdown_parts(table, length_fn, embed_max_tokens):
                    parts.append((md, 1))
            continue
        if oversized:
            # Oversized table-free prose block: window-split to the embed ceiling
            # (no overlap — this is the truncation-avoidance degrade, not the
            # max_tokens split, which never reaches here).
            flush_buf()
            pieces = _atomic_pieces(b_text, length_fn, embed_max_tokens, _SEPARATORS)
            for window in _merge_windows(pieces, length_fn, embed_max_tokens, 0):
                window = window.strip()
                if window:
                    parts.append((window, 0))
            continue
        if buf and length_fn(_normalize("\n\n".join([*buf, b_text]))) > embed_max_tokens:
            flush_buf()
        buf.append(b_text)
        buf_tables += b_tables

    flush_buf()
    return parts


def _normalize(raw: str) -> str:
    """Collapse intra-line whitespace and blank-line runs; keep table newlines."""
    raw = raw.replace(" ", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return text.strip()


def _build_image_refs(text: str, image_meta: list[tuple[str, bool]], anchor: str) -> list[ImageRef]:
    """One ImageRef per placeholder in ``text`` (document order), owning ``anchor``."""
    caption_by_id: dict[str, bool] = {}
    for image_id, needs_caption in image_meta:
        caption_by_id.setdefault(image_id, needs_caption)
    refs: list[ImageRef] = []
    for match in _IMG_TOKEN_RE.finditer(text):
        image_id = match.group(1) or ""
        # Assertion 8 / Forbidden: never an ImageRef with an empty id. _render no
        # longer emits an id-less placeholder, so this only guards a hand-written
        # fixture that does.
        if not image_id:
            continue
        refs.append(
            ImageRef(
                image_id=image_id,
                anchor=anchor,
                needs_caption=caption_by_id.get(image_id, False),
            )
        )
    return refs


# ---------------------------------------------------------------------------
# Recursive token-window split (oversized, table-free section only)
# ---------------------------------------------------------------------------


def _split_keep(text: str, sep: str) -> list[str]:
    """Split ``text`` on ``sep`` but keep ``sep`` attached to the piece it ends.

    ``"a.\n\nb"`` on ``"\n\n"`` → ``["a.\n\n", "b"]`` — so rejoining the windows
    with ``""`` (in :func:`_merge_windows`) recovers every separator exactly.
    ``str.split`` + ``" ".join`` would drop the ``"\n\n"`` and rewrite the text
    (assertion 5 / Forbidden). A run of separators yields separator-only pieces,
    dropped by the caller's ``strip`` check.
    """
    parts = text.split(sep)
    return [p + sep for p in parts[:-1]] + parts[-1:]


def _atomic_pieces(
    text: str, length_fn: Callable[[str], int], max_tokens: int, seps: tuple[str, ...]
) -> list[str]:
    """Break ``text`` into pieces each ``length_fn(piece) <= max_tokens``.

    Separators are preserved on their piece (assertion 5): the finest split still
    carries the space/newline/period that terminated it, so ``"".join`` of the
    resulting windows is a faithful slice of the input — no boundary is rewritten.
    """
    if length_fn(text) <= max_tokens:
        return [text] if text.strip() else []
    if not seps:
        return _char_pieces(text, length_fn, max_tokens)
    sep, rest = seps[0], seps[1:]
    if sep not in text:
        return _atomic_pieces(text, length_fn, max_tokens, rest)
    out: list[str] = []
    for part in _split_keep(text, sep):
        if not part.strip():
            continue
        if length_fn(part) <= max_tokens:
            out.append(part)
        else:
            out.extend(_atomic_pieces(part, length_fn, max_tokens, rest))
    return out


def _char_pieces(text: str, length_fn: Callable[[str], int], max_tokens: int) -> list[str]:
    """Last-resort split for a single token longer than the budget."""
    pieces: list[str] = []
    i, n = 0, len(text)
    while i < n:
        j = i + 1
        while j < n and length_fn(text[i : j + 1]) <= max_tokens:
            j += 1
        pieces.append(text[i:j])
        i = j
    return pieces


def _merge_windows(
    pieces: list[str],
    length_fn: Callable[[str], int],
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Pack pieces into ``<= max_tokens`` windows; carry a bounded overlap forward.

    Pieces already carry their own separators (:func:`_split_keep`), so windows are
    rejoined with ``""`` — never ``" ".join`` — and every ``.``/``\\n``/``\\n\\n``
    in the source survives into the chunk text (assertion 5). Overlap is a ceiling
    that may be undershot at a piece boundary, so this guarantees ``0 < overlap <=
    overlap_tokens`` word-wise whenever ``overlap_tokens > 0`` and a boundary is hit,
    never an exact overlap.
    """
    windows: list[str] = []
    current: list[str] = []
    for piece in pieces:
        if current and length_fn("".join([*current, piece])) > max_tokens:
            windows.append("".join(current))
            overlap: list[str] = []
            for prev in reversed(current):
                if overlap_tokens == 0:
                    break
                if length_fn("".join([prev, *overlap])) > overlap_tokens:
                    break
                overlap.insert(0, prev)
            current = [*overlap, piece]
            if length_fn("".join(current)) > max_tokens:
                current = [piece]
        else:
            current.append(piece)
    if current:
        windows.append("".join(current))
    return windows


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _text_of(el: etree._Element) -> str:
    return " ".join("".join(el.itertext()).split())


def _is_heading(el: etree._Element) -> bool:
    return isinstance(el.tag, str) and el.tag.lower() in _HEADING_TAGS


def _has_content(el: etree._Element) -> bool:
    """True if the element carries visible text or an image (dropping empties)."""
    if _text_of(el):
        return True
    return el.tag == "img" or el.find(".//img") is not None


def _leading_anchor(blocks: list[etree._Element], page_id: str) -> str:
    """The bkmrk-* id leading the chunk, else the page-* id (assertion 6)."""
    if blocks:
        bid = blocks[0].get("id") or ""
        if bid.startswith("bkmrk-"):
            return bid
    return page_id


def _leading_subsection(blocks: list[etree._Element]) -> str:
    for block in blocks:
        if _is_heading(block):
            return _text_of(block)
    return ""


def _section_path(page: dict, subsection: str) -> str:
    parts: list[str] = []
    if page["chapter_title"]:
        parts.append(page["chapter_title"])
    parts.append(page["page_title"])
    if subsection and subsection != page["page_title"]:
        parts.append(subsection)
    return " > ".join(parts)


def _is_generalized(text: str) -> bool:
    return any(pattern.search(text) for pattern in _GENERALIZATION_RES)


def _generalization_sentences(text: str) -> list[str]:
    """The sentences of `text` that contain a generalization phrase.

    Splitting is parenthesis-aware. The module list Stage C needs sits *inside*
    the sentence that generalizes ("Pour les autres modules (Commandes,
    Réceptions, Factures etc..), la même démarche s'applique") and that list
    legitimately holds sentence-final punctuation. Splitting on "." blindly
    would cut the list away from the phrase that gives it meaning.
    """
    sentences: list[str] = []
    depth = 0
    start = 0
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "\n":
            sentences.append(text[start:i])
            start = i + 1
        elif depth == 0 and char in _SENTENCE_END:
            end = i + 1
            if end >= n or text[end].isspace():
                sentences.append(text[start:end])
                start = end
                i = end
                continue
        i += 1
    sentences.append(text[start:])
    return [s for s in sentences if any(p.search(s) for p in _GENERALIZATION_RES)]


def _clean_module_name(item: str) -> str:
    """One list item to a full module name, or "" if it is not one.

    Every word is kept: "Sorties de stock" must not degrade to "Sorties" — the
    dropped words are unrecoverable downstream, since nothing later in the
    pipeline can know they existed.
    """
    name = " ".join(_ETC_RE.sub("", item).split())
    name = name.strip(" .,;:!?()[]\"'“”")
    if not name or not name[:1].isupper():
        return ""
    words = name.split()
    if len(words) > MAX_MODULE_WORDS:
        return ""
    if not all(_MODULE_WORD_RE.match(word) for word in words):
        return ""
    return name


def _module_candidates(text: str) -> list[str]:
    """Raw module names from parenthesized lists in a generalization sentence.

    Scope is the generalization sentence, not the page: a parenthesized group
    anywhere else belongs to a different statement, and attaching it here hands
    Stage C names it cannot tell apart from real ones.
    """
    mods: list[str] = []
    for sentence in _generalization_sentences(text):
        for group in _PAREN_RE.findall(sentence):
            for item in re.split(r"[,/]", group):
                name = _clean_module_name(item)
                if name and name not in mods:
                    mods.append(name)
    return mods


def _dedup(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
