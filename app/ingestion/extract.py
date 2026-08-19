"""Ingestion Stage A — BookStack base64 image extractor.

`askgo-621x-fr.html` is 146 MB, ~98% of which is base64 image data inlined into
`src` attributes; the markup itself is ~2.3 MB. This stage strips every inlined
image out, stores each one content-addressed through `BaseStorage.save_derived`,
and returns markup in which every `<img>` is a URL reference — the ~2.3 MB
artifact Stage B parses and D10 tier 2 serves. No LLM, no network, deterministic.

Why a streaming text pass and not a DOM transform (contract "Why this is a
streaming text pass"): building a DOM over 146 MB materialises the whole document
as a tree, holding the 143 MB of base64 attribute values simultaneously with the
source string. Instead one linear scan over `raw_html` matches only
`src="data:{mime};base64,{payload}"` — a single unambiguously delimited token
(the payload alphabet cannot contain a quote, `<`, or `>`), decodes/hashes/stores
each match, and lets the payload become garbage before the next match is read.
Every judgement that depends on document *structure* (page/chapter/bkmrk markers,
tables) is left to Stage B, which parses the small cleaned output with lxml.

Measured on the real export: 145.9 MB in, 2.60 MB cleaned out, 2199 distinct images
from 2304 occurrences, 105.5 MB stored, 4 payloads deliberately left in place, 0.8 s.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass

from app.core.logging import get_logger
from app.core.services.storage import BaseStorage

logger = get_logger(__name__)


# MIME type -> stored file suffix. A MIME absent from this map is an image this
# stage refuses to guess a suffix for: it is left untouched and counted as
# skipped (assertion 7). Every value here is in storage.DERIVED_EXTENSIONS.
MIME_SUFFIXES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


# One double-quoted `src` holding a data: URI — the exact form BookStack emits and
# the form the contract's rationale pins. The payload is captured as "anything up
# to the closing quote" (`[^"<>]*`) rather than the strict base64 alphabet, because
# a payload that is *not* valid base64 must still be matched so it can be counted as
# skipped and left untouched (assertion 7 — you cannot fail to decode a payload you
# never captured). The contract's own safety property is exactly this negation: the
# token "cannot contain a quote, a `<`, or a `>`", so the closing quote
# unambiguously terminates it and there is no structure to parse. No re flags:
# a negated class already spans newlines, and IGNORECASE here would be wrong — see
# the MIME normalisation below, which is deliberately narrower than a blanket flag.
_DATA_URI_RE = re.compile(r'src="data:(?P<mime>[^;"<>]+);base64,(?P<payload>[^"<>]*)"')


# Every *other* inlined base64 payload — the ones not in an `src` attribute. The MVP
# corpus holds exactly 4: BookStack's own callout icons, emitted as
# `background-image:url("data:image/svg+xml;base64,…")` inside its `<style>` block,
# 1452 bytes in total.
#
# They are deliberately NOT extracted. Rewriting them means matching and rewriting a
# second, CSS-shaped syntax for 1.4 KB of decorative icon, and reaching into a
# stylesheet is exactly the "interpret document structure" this stage is forbidden to
# do. They are *counted*, and that is the whole point: assertion 1 is
# `cleaned_html.count("base64,") == skipped`, a strict equality, so an uncounted
# survivor breaks it by exactly its own count. The alternative was relaxing that
# equality to `>=`, which would have retired the only check that notices a payload
# category this stage cannot handle — the failure mode being that a future export
# inlines 500 CSS images and nothing anywhere says so.
#
# The negative lookbehind is fixed-width (`src="` is 5 characters, which Python's re
# requires) and excludes everything _DATA_URI_RE already owns — extracted or skipped —
# so no payload is counted twice.
_NON_SRC_DATA_URI_RE = re.compile(r'(?<!src=")data:[^;"<>]+;base64,')


@dataclass(frozen=True)
class ExtractedImage:
    """One distinct image pulled out of the export.

    Carries the id, path, MIME and size — never the decoded bytes and never the
    base64 text (contract Forbidden): holding bytes on a record for 240 pages of
    screenshots reintroduces the memory problem this stage exists to avoid.
    """

    image_id: str  # SHA256 hex of the *decoded* bytes — the id Stage B carries
    source_path: str  # path returned by BaseStorage.save_derived
    mime_type: str  # normalised (lower-cased) — `image/PNG` is recorded `image/png`
    byte_size: int  # decoded size
    needs_caption: bool  # recorded now, acted on only post-MVP (Stage D)
    occurrences: int  # how many <img> tags in the document resolved to it


@dataclass(frozen=True)
class ExtractResult:
    cleaned_html: str  # what Stage B parses
    cleaned_path: str  # stored artifact, what D10 tier 2 serves
    images: list[ExtractedImage]
    # Every inlined base64 payload left in place, from any cause: unknown MIME,
    # undecodable payload (assertion 7), or a data: URI outside an `src` attribute
    # (CSS `url(...)`). One counter for all three because assertion 1 balances it
    # against `cleaned_html.count("base64,")`, which cannot tell them apart either;
    # the structlog warnings carry the cause.
    skipped: int


async def extract_bookstack_html(
    raw_html: str,
    *,
    document_id: str,
    storage: BaseStorage,
    decorative_max_bytes: int = 3072,
) -> ExtractResult:
    """Strip inlined base64 images, store each content-addressed, return URL markup.

    Args:
        raw_html: The decoded upload. Empty string returns an empty-but-valid
            result with a stored (empty) artifact; never raises.
        document_id: The Document.id UUID, used verbatim to build
            `src="/documents/{document_id}/images/{image_id}"`. Not validated —
            M7 supplies it from the row it created.
        storage: Injected BaseStorage. Images and the cleaned artifact go through
            `save_derived`, never `save`.
        decorative_max_bytes: Size below which an image is decorative
            (`needs_caption=False`). `0` disables the exemption. Negative → ValueError.

    Returns:
        ExtractResult, always.
    """
    if decorative_max_bytes < 0:
        raise ValueError(f"decorative_max_bytes must be >= 0, got {decorative_max_bytes}")

    # Memoise on the decoded content hash — never on the base64 text. The corpus
    # repeats the same screenshot across pages (one page carries 127 images), so a
    # per-occurrence write would be thousands of redundant decode-hash-write cycles;
    # and a payload-keyed cache would hold the 143 MB this stage exists to release.
    images: dict[str, ExtractedImage] = {}
    skipped = 0

    # Stitch the output as we scan, rather than a DOM or a full-materialised
    # re.sub result: each match is decoded, stored, and its payload dropped before
    # the next is read. `out` holds only cleaned (small) text plus the untouched
    # spans between matches.
    out: list[str] = []
    cursor = 0

    for match in _DATA_URI_RE.finditer(raw_html):
        # Everything between the previous match and this one is copied verbatim.
        out.append(raw_html[cursor : match.start()])
        cursor = match.end()

        # MIME type and subtype are case-insensitive per RFC 2045 §5.1, so the
        # lookup key is folded rather than compared verbatim. This is not a
        # hypothetical: the MVP corpus declares `data:image/PNG` on 215 of its 2304
        # inlined images (9.3%). A verbatim lookup sends every one of them down
        # assertion 7's skip path — which is *defensible-looking* behaviour, because
        # skipped increments and assertion 1 still balances, so nothing fails. The
        # only visible symptom is a cleaned artifact carrying 215 base64 blobs and
        # 215 screenshots that no citation can ever display.
        #
        # Folding here rather than with re.IGNORECASE on the pattern: the flag would
        # also fold the literal `base64,` (harmless) and is a blunter instrument than
        # the one field that actually needs it.
        mime = match.group("mime").strip().lower()
        payload = match.group("payload")

        suffix = MIME_SUFFIXES.get(mime)
        if suffix is None:
            # Unknown MIME: leave the tag byte-for-byte untouched, count it, warn.
            # Never guess a suffix (assertion 7). The warning carries the *declared*
            # spelling, not the folded one — an operator triaging this needs a string
            # they can grep the source export for.
            out.append(match.group(0))
            skipped += 1
            logger.warning(
                "extract_skipped_unknown_mime",
                mime_type=match.group("mime"),
                document_id=document_id,
            )
            continue

        try:
            # validate=True so a payload that is not real base64 raises here rather
            # than silently decoding garbage. Whitespace in the alphabet is stripped
            # first because validate=True rejects it.
            decoded = base64.b64decode("".join(payload.split()), validate=True)
        except (binascii.Error, ValueError):
            out.append(match.group(0))
            skipped += 1
            logger.warning("extract_skipped_undecodable", mime_type=mime, document_id=document_id)
            continue

        image_id = hashlib.sha256(decoded).hexdigest()

        record = images.get(image_id)
        if record is None:
            # First time we've seen these bytes: one write, one record.
            source_path = await storage.save_derived(suffix, decoded)
            record = ExtractedImage(
                image_id=image_id,
                source_path=source_path,
                mime_type=mime,
                byte_size=len(decoded),
                needs_caption=len(decoded) >= decorative_max_bytes,
                occurrences=1,
            )
            images[image_id] = record
        else:
            # Seen before: bump occurrences, reuse the record, no second write.
            images[image_id] = _bump(record)

        # decoded is not referenced past this point — released before the next match.
        out.append(
            f'src="/documents/{document_id}/images/{image_id}"'
            f' data-image-id="{image_id}"'
            f' data-needs-caption="{"true" if images[image_id].needs_caption else "false"}"'
        )

    # Trailing span after the last match (or the whole document if there were none).
    out.append(raw_html[cursor:])
    cleaned_html = "".join(out)

    # Count the payloads no `src` sweep could have reached. Measured on cleaned_html,
    # not raw_html, because that is the string assertion 1 balances against — and it
    # keeps the counter honest if a future change starts rewriting some of them.
    non_src = len(_NON_SRC_DATA_URI_RE.findall(cleaned_html))
    if non_src:
        skipped += non_src
        logger.warning(
            "extract_skipped_non_src_data_uri",
            count=non_src,
            document_id=document_id,
        )

    # Store the cleaned artifact even for empty/image-free input: D10 tier 2 has
    # nothing to serve otherwise. A save_derived failure here propagates unchanged
    # (assertion 10) — it is deliberately not wrapped.
    cleaned_path = await storage.save_derived(".html", cleaned_html.encode())

    result = ExtractResult(
        cleaned_html=cleaned_html,
        cleaned_path=cleaned_path,
        images=list(images.values()),
        skipped=skipped,
    )
    logger.info(
        "extract_complete",
        document_id=document_id,
        distinct_images=len(result.images),
        skipped=skipped,
    )
    return result


def _bump(record: ExtractedImage) -> ExtractedImage:
    """Return a copy of `record` with occurrences incremented (frozen dataclass)."""
    return ExtractedImage(
        image_id=record.image_id,
        source_path=record.source_path,
        mime_type=record.mime_type,
        byte_size=record.byte_size,
        needs_caption=record.needs_caption,
        occurrences=record.occurrences + 1,
    )
